# -*- coding: utf-8 -*-
"""
独立的多提供商 LLM 客户端（从 ScholarWorkflow._create_llm_client/_call_llm 抽出）。

供 workflow 与全文精读（closereading）共用：给一个 prompt 字符串，返回文本；
可选结构化 JSON 输出、覆盖 model / max_tokens / temperature（精读读全文需要更大 max_tokens）。
提供商：gemini（google-genai）与 openai-compatible（deepseek 等）。凭据缺失时回退复用
config/config.env 的 OpenAI 兼容凭据（与原 workflow 行为一致）。
"""
import json
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import httpx

from ..utils.logger import get_logger

logger = get_logger(__name__)


class _RetryableHTTP(Exception):
    """内部：可重试的 HTTP 状态（429/5xx）。"""


# claude CLI headless（走 Claude 订阅，不需要 API key）。DeepSeek 欠费时的主力路径。
_AGENT_PROVIDERS = {'claude-agent', 'claude_agent', 'claude', 'agent'}
_AGENT_TIMEOUT = 600
# 每个 claude -p 是独立 node 实例，内存开销大；全局限并发（对齐 translator/agent.py 的做法）
_AGENT_SEMAPHORE = threading.Semaphore(4)
# claude 的会话历史按「工作目录」分桶。若在仓库目录下调用，流水线每跑一块就往用户
# 本项目的 resume 列表里塞一条（一篇论文 7+ 条），把真人会话淹掉。故固定在专用目录下
# 起子进程，历史落到独立桶里。注意：不能改用 CLAUDE_CONFIG_DIR 隔离——那会连登录态
# 一起隔离，headless 直接报 "Not logged in"。
_AGENT_CWD = Path.home() / '.claude-pipeline-cwd'

# 数字状态码须整词匹配：避免命中 request id（如 req-a402fb99）或计数
# （如 "4021 tokens"）里恰好出现的数字子串。语义对齐 translator/fallback.py
# 的 classify_fatal 里的 _HARD_CODE_RE（该文件独立实现，禁止跨模块 import——
# 两边的异常类型体系不同，fallback.py 走 APIError.message，这里是裸 str(e)）。
_FATAL_CODE_RE = re.compile(r'\b(401|402|403)\b')
_FATAL_PHRASES = ('quota', 'resource_exhausted', 'resource exhausted', 'api key',
                   'permission denied', 'payment required', 'insufficient balance')

# 内容层拒答：**这一篇**被过滤器挡了，不代表 provider 坏了。仍然换下家接手
# （尊重裁决而非绕过），但只对本次调用生效，调用结束后回到原来的链位。
# 不这么做的后果实测过：221 篇精读回填里，一篇讲医疗 AI 安全的论文（正文含越狱
# 样例）触发 AUP 拒答，整个 client 被永久推到本地 ollama，后续 28 篇全部陪葬。
_CONTENT_REFUSAL_PHRASES = (
    "can't help with this", 'cannot help with this', 'legal/aup',
    'output blocked', 'content policy', 'safety filter', 'blocked_by_safety',
)


class LLMClient:
    """封装 LLM 连接与调用。只依赖 LLMSettings，无 workflow 状态。

    支持回退链（settings.fallback_providers，逗号分隔）：当前 provider 出现
    致命错误（凭据缺失/401/402/403/404/CLI 不可用/内容拦截/重试耗尽）时依序
    切换到下一个，链尽才抛。无 fallback 配置时行为与单 provider 完全一致。
    """

    # 非主 provider 的缺省模型：链上切换后，settings.model（主 provider 的
    # 模型名）对别家无效（如 sonnet 发给 deepseek 会 404），按此表取各家缺省
    _DEFAULT_MODELS = {
        'deepseek': 'deepseek-v4-flash',
        'claude-agent': 'sonnet',
        'gemini': 'gemini-3.5-flash',
    }

    def __init__(self, llm_settings):
        self.settings = llm_settings  # scholar.schema.LLMSettings
        self._conn = None
        # 手动精读并发化后，多线程可能同时首访 conn；无锁的懒加载会竞态双建连接
        # （openai-compatible 分支还会泄漏一个 httpx.Client）。单线程调用方零感知。
        self._conn_lock = threading.Lock()
        # provider 链：主 provider + fallback（去空白、去重复项）
        #
        # fallback 项支持 `provider:model` 写法指定该棒用哪个模型，于是**同一家换模型**
        # 也能进链——例如 `claude-agent:opus`：sonnet 被内容过滤挡了就让 opus 接手，
        # 不必绕道本地 ollama（qwen3.5 做精读类任务必然失败，纯属白烧一轮）。
        # 去重按 (provider, model) 对，否则 `claude-agent:opus` 会被主链的
        # claude-agent 判为重复而丢掉。
        primary = (llm_settings.provider or 'deepseek').strip().lower()
        fallback_raw = getattr(llm_settings, 'fallback_providers', '') or ''
        chain, chain_models, seen = [primary], [None], {(primary, None)}
        for item in fallback_raw.split(','):
            item = item.strip()
            if not item:
                continue
            name, _, mdl = item.partition(':')
            name, mdl = name.strip().lower(), mdl.strip() or None
            if not name or (name, mdl) in seen:
                continue
            seen.add((name, mdl))
            chain.append(name)
            chain_models.append(mdl)
        self._chain = chain
        self._chain_models = chain_models
        self._chain_idx = 0
        # 粘性代际：真·provider 故障（欠费/限流/CLI 缺失）推进链时 +1。内容层拒答
        # 的临时切换在收尾时靠它判断「期间有没有发生过真故障切换」——有就别把别的
        # 线程刚做出的合理推进给撤回去。
        self._sticky_gen = 0

    def close(self):
        """关闭内部 HTTP 连接池，避免 fd 泄漏。

        ingest/backfill 等长生命周期调用方在完成所有调用后须显式 close。
        """
        if self._conn is not None and isinstance(self._conn, dict):
            c = self._conn.get('client')
            if isinstance(c, httpx.Client):
                try:
                    c.close()
                except Exception:
                    pass
            # 非 httpx.Client（如 gemini genai.Client / claude-agent dict）不需要显式关闭
        self._conn = None

    @property
    def current_provider(self) -> str:
        return self._chain[self._chain_idx]

    @property
    def conn(self) -> dict:
        if self._conn is None:
            with self._conn_lock:
                if self._conn is None:
                    self._conn = self._create_from_chain()
        return self._conn

    def _create_from_chain(self) -> dict:
        """从当前链位起建连接；构造失败（缺凭据/缺 CLI）自动跳到下一个。"""
        while self._chain_idx < len(self._chain):
            name = self._chain[self._chain_idx]
            try:
                conn = self._create(name)
                conn['name'] = name
                # `provider:model` 写法指定的模型覆盖该家缺省（如 claude-agent:opus）
                mdl = self._chain_models[self._chain_idx]
                if mdl:
                    conn['model'] = mdl
                if self._chain_idx > 0:
                    logger.warning("🛟 LLM 回退链：当前 provider = {}{} ({}/{})".format(
                        name, "（{}）".format(mdl) if mdl else "",
                        self._chain_idx + 1, len(self._chain)))
                return conn
            except Exception as e:
                logger.warning("⚠️ LLM provider '{}' 初始化失败，跳过: {}".format(name, e))
                self._chain_idx += 1
        raise RuntimeError("所有 LLM provider 均不可用（回退链已耗尽: {}）".format(self._chain))

    def _advance_chain(self, failed_conn: dict, reason: str, sticky: bool = True) -> bool:
        """切到下一个 provider。返回「是否应该用新 conn 重试」（False=链已耗尽）。

        sticky=False 用于内容层拒答：换下家接手本次调用，但不算「这家坏了」，
        `call()` 收尾时会把链位拨回去（见 _restore_chain）。

        failed_conn 是调用方失败时实际使用的那份 conn（call() 里 self.conn 取到
        的那份）。多线程共享同一个 LLMClient 时，几个线程可能几乎同时对同一个
        provider 判定失败：若不核对 failed_conn 是否仍是当前 conn，会出现「线程1
        切到 idx 1，线程2/3/4 各自再把指针往前推一格」，一次失败烧穿整条回退
        链，健康的下家 provider 一次真实请求都没发过（对齐
        translator/fallback.py._mark_failed 的 idx 校验）。
        """
        with self._conn_lock:
            if self._conn is not failed_conn:
                # 其他线程已经切换过（self._conn 已被重建或清空）：直接告知调用方
                # 用新 conn 重试，不再重复推进指针。
                return True
            if self._chain_idx + 1 >= len(self._chain):
                return False
            old = self._chain[self._chain_idx]
            self._chain_idx += 1
            if sticky:
                self._sticky_gen += 1
            # 旧 conn 若持有 httpx.Client 须显式关闭，否则 fd 泄漏（Ollama 与
            # openai-compatible 路径各创建一个 Client，gemini/claude-agent 不涉及）。
            if self._conn is not None and isinstance(self._conn, dict):
                c = self._conn.get('client')
                if isinstance(c, httpx.Client):
                    try:
                        c.close()
                    except Exception:
                        pass
            self._conn = None
            logger.warning("🛟 LLM provider '{}' {}（{}），切换到 '{}'".format(
                old, "判定不可用" if sticky else "拒答本次内容（不判定为故障）",
                reason[:160], self._chain[self._chain_idx]))
            return True

    def _restore_chain(self, base_idx: int, base_gen: int) -> None:
        """内容层拒答导致的临时切换收尾：把链位拨回本次调用开始时的位置。

        只在「期间没发生过真·故障切换」时回滚（靠 _sticky_gen 判断）。否则会把
        别的线程刚做出的合理推进撤销掉——那家是真的欠费/限流，拨回去只会再挂一次。
        """
        with self._conn_lock:
            if self._sticky_gen != base_gen or self._chain_idx <= base_idx:
                return
            back_to = self._chain[base_idx]
            if self._conn is not None and isinstance(self._conn, dict):
                c = self._conn.get('client')
                if isinstance(c, httpx.Client):
                    try:
                        c.close()
                    except Exception:
                        pass
            self._conn = None
            self._chain_idx = base_idx
            logger.info("↩️ 内容拒答只影响本次调用，链位拨回 '{}'".format(back_to))

    def _model_for(self, conn: dict, override: Optional[str]) -> str:
        """解析本次调用的模型名。

        主 provider（链首）：尊重 caller 的 override（filter_model/closeread_model）。
        切换后的 provider：override 是主 provider 的模型名，对当前家无效，忽略并
        改用该 provider 的缺省模型。
        """
        name = conn.get('name') or ''
        if self._chain_idx == 0:
            return override or conn['model']
        if override and override != conn['model']:
            logger.debug("已切换 provider，忽略主链模型覆写 '{}'，使用 {}".format(override, conn['model']))
        return conn['model']

    def _base_model(self, provider: str) -> str:
        """provider 的基础模型：主 provider 用 settings.model，其余用各家缺省。"""
        if provider == self._chain[0]:
            return self.settings.model
        if provider == 'ollama':
            return self.settings.ollama_model
        return self._DEFAULT_MODELS.get(provider, self.settings.model)

    def _create(self, provider: str) -> dict:

        if provider in _AGENT_PROVIDERS:
            import shutil
            cli = shutil.which('claude')
            if not cli:
                raise ValueError(
                    "claude CLI 未安装或不在 PATH，无法使用 claude-agent provider"
                    "（npm install -g @anthropic-ai/claude-code）")
            return {'cli_path': cli, 'model': self._base_model(provider), 'provider': 'claude-agent'}

        if provider == 'gemini':
            from google import genai
            client = genai.Client(api_key=self.settings.api_key)
            return {'client': client, 'model': self._base_model(provider), 'provider': 'gemini'}

        if provider == 'ollama':
            # 本地 Ollama：独立配置组、无鉴权占位 key；本地大模型单次调用可达
            # 数分钟（thinking 型），超时须远大于云端的 120s
            import httpx as _httpx
            return {
                'client': _httpx.Client(timeout=1800.0),
                'base_url': self.settings.ollama_base_url.rstrip('/'),
                'api_key': 'ollama',
                'model': self.settings.ollama_model,
                'provider': 'openai-compatible',
            }

        # OpenAI 兼容（deepseek / openai-compatible）
        import httpx
        api_key = self.settings.openai_api_key
        base_url = self.settings.base_url
        if not api_key or not base_url:
            try:
                from ..core.schema import Settings as CoreSettings
                core = CoreSettings.from_env_file(Path('config/config.env'))
                api_key = api_key or core.api.openai_api_key
                base_url = base_url or core.api.openai_base_url
                logger.info("复用主配置 config/config.env 的 OpenAI 兼容凭据")
            except Exception as e:
                logger.debug("读取主配置失败: {}".format(e))

        if provider == 'deepseek' and not base_url:
            base_url = 'https://api.deepseek.com/v1'
        if not api_key:
            raise ValueError(
                "未找到 OpenAI 兼容 API 密钥：请在 config/scholar.env 设置 "
                "LLM__OPENAI_API_KEY，或在 config/config.env 设置 API__OPENAI_API_KEY")
        if not base_url:
            raise ValueError("未找到 API 地址：请设置 LLM__BASE_URL 或 API__OPENAI_BASE_URL")

        return {
            'client': httpx.Client(timeout=120.0),
            'base_url': base_url.rstrip('/'),
            'api_key': api_key,
            'model': self._base_model(provider),
            'provider': 'openai-compatible',
        }

    def _is_content_refusal(self, e: Exception) -> bool:
        """是否为内容层拒答（过滤器挡的是这一篇，不是 provider 坏了）。"""
        return any(k in str(e).lower() for k in _CONTENT_REFUSAL_PHRASES)

    def _is_switchable(self, e: Exception) -> bool:
        """该异常是否意味着「当前 provider 不可用，应切换」（而非单次瞬时失败）。"""
        # 重试循环耗尽后抛出的瞬时类错误：该 provider 当前不可用
        if isinstance(e, (_RetryableHTTP, httpx.TimeoutException, httpx.TransportError,
                          subprocess.TimeoutExpired)):
            return True
        if isinstance(e, httpx.HTTPStatusError):
            return e.response.status_code in (401, 402, 403, 404)
        # claude CLI 的非重试类失败（含内容拦截「Output blocked」——切下家接手，
        # 这是尊重过滤器裁决而非绕过）
        if isinstance(e, RuntimeError) and 'claude cli' in str(e).lower():
            return True
        text = str(e).lower()
        return bool(_FATAL_CODE_RE.search(text)) or any(k in text for k in _FATAL_PHRASES)

    def call(self, prompt: str, model: Optional[str] = None,
             max_tokens: Optional[int] = None, temperature: Optional[float] = None,
             json_mode: bool = False, max_retries: int = 4) -> str:
        """调用 LLM，返回文本。

        model/max_tokens/temperature 缺省时用 settings；json_mode=True 请求结构化 JSON。
        对限流(429)/服务端(5xx)/超时/连接错误做指数退避重试（并发多月回填时保证稳健）；
        致命错误（欠费/认证/CLI 缺失/重试耗尽）沿 fallback 链切换 provider，**且是粘性的**
        （那家是真的坏了，后续调用不该再撞一次）。

        **内容层拒答例外**：换下家接手本次调用，但收尾时把链位拨回来——被过滤器挡住的
        是「这一篇」而不是 provider。粘性处理的代价实测过：一篇论文触发 AUP 拒答，
        整个 client 被永久推到本地 ollama，同一批后续 28 篇全部陪葬。
        """
        with self._conn_lock:
            base_idx, base_gen = self._chain_idx, self._sticky_gen
        soft = False
        try:
            while True:
                conn = self.conn  # 懒建；构造失败会自动沿链推进
                try:
                    return self._call_once(conn, prompt, model, max_tokens, temperature,
                                           json_mode, max_retries)
                except Exception as e:
                    if not self._is_switchable(e):
                        raise
                    refusal = self._is_content_refusal(e)
                    soft = soft or refusal
                    if not self._advance_chain(conn, str(e), sticky=not refusal):
                        raise
        finally:
            if soft:
                self._restore_chain(base_idx, base_gen)

    def _call_once(self, conn: dict, prompt: str, model: Optional[str],
                   max_tokens: Optional[int], temperature: Optional[float],
                   json_mode: bool, max_retries: int) -> str:
        provider = conn.get('provider')
        model = self._model_for(conn, model)
        # ollama 的 thinking 型模型（qwen3.5）思考过程同样消耗生成预算，
        # 8K 会被 thinking 烧光导致 content 为空（实测），下限抬到 24K
        if conn.get('name') == 'ollama' and (max_tokens is None or max_tokens < 24576):
            max_tokens = 24576
        temp = self.settings.temperature if temperature is None else temperature
        mtok = self.settings.max_output_tokens if max_tokens is None else max_tokens

        last_exc = None
        # 防御 max_retries=0：range(0) 不执行会导致静默返回 None 而非抛异常
        max_retries = max(max_retries, 1)
        for attempt in range(max_retries):
            try:
                if provider == 'claude-agent':
                    return self._call_agent(conn, prompt, model, json_mode)

                if provider == 'gemini':
                    from google.genai import types
                    cfg = dict(temperature=temp, max_output_tokens=mtok)
                    if json_mode:
                        cfg['response_mime_type'] = 'application/json'
                    response = conn['client'].models.generate_content(
                        model=model,
                        contents=prompt,
                        config=types.GenerateContentConfig(**cfg),
                    )
                    if not response.text:
                        # response.text 在安全拦截 / MAX_TOKENS 截断时为 None，直接
                        # 返回会违反本方法「返回 str」的契约，下游按 str 处理拿到
                        # TypeError、错因也丢了。这里不用 switchable 的关键词（如
                        # 401/403/quota），是有意的：安全拦截是内容层裁决，换
                        # provider 不代表能绕过，不应触发 _is_switchable 切链。
                        finish_reason = None
                        try:
                            finish_reason = response.candidates[0].finish_reason
                        except Exception:
                            pass
                        raise RuntimeError(
                            "Gemini 响应内容为空（finish_reason={}），"
                            "可能被安全过滤拦截或 max_output_tokens 不足".format(finish_reason))
                    return response.text

                # OpenAI 兼容
                payload = {
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temp,
                    "max_tokens": mtok,
                }
                if json_mode:
                    payload["response_format"] = {"type": "json_object"}
                resp = conn['client'].post(
                    "{}/chat/completions".format(conn['base_url']),
                    headers={"Authorization": "Bearer {}".format(conn['api_key']),
                             "Content-Type": "application/json"},
                    json=payload,
                )
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise _RetryableHTTP("HTTP {}".format(resp.status_code))
                resp.raise_for_status()
                body = resp.json()
                content = body['choices'][0]['message']['content']
                if not content:
                    # 同 gemini 分支：content 在内容过滤 / 空补全时可为 None，
                    # 直接返回违反契约，改为抛出带明确原因的异常。
                    finish_reason = body['choices'][0].get('finish_reason')
                    raise RuntimeError(
                        "OpenAI 兼容响应内容为空（finish_reason={}），"
                        "可能被内容过滤拦截或模型未生成有效输出".format(finish_reason))
                return content
            except (_RetryableHTTP, httpx.TimeoutException, httpx.TransportError,
                    subprocess.TimeoutExpired) as e:
                last_exc = e
                if attempt < max_retries - 1:
                    time.sleep(2.0 * (2 ** attempt))  # 2s,4s,8s 退避
                    continue
                raise
        if last_exc:
            raise last_exc

    # ------------------------------------------------------------------
    # claude CLI headless：用订阅额度跑，不消耗 API key
    # ------------------------------------------------------------------

    def _call_agent(self, conn: dict, prompt: str, model: str, json_mode: bool) -> str:
        # 用调用方传入的 conn（_call_once 里已解析好的那份），不重新读 self.conn——
        # 并发场景下另一线程可能已经 _advance_chain 重建了 conn，届时 self.conn
        # 会是别家 provider 的连接（如取到 openai-compatible 的 dict，没有
        # cli_path，KeyError）。
        if json_mode:
            # claude CLI 没有原生 json_mode，靠指令约束 + 事后剥 ``` 围栏
            prompt = prompt + "\n\n【输出要求】只输出一个合法 JSON 对象，不要任何解释文字或 Markdown 代码围栏。"

        cmd = [conn['cli_path'], '-p', '--output-format', 'json', '--model', model]
        try:
            _AGENT_CWD.mkdir(parents=True, exist_ok=True)
            cwd = str(_AGENT_CWD)
        except OSError:
            cwd = None  # 建不出来就退回默认行为，不因此让调用失败
        with _AGENT_SEMAPHORE:
            proc = subprocess.run(cmd, input=prompt, capture_output=True,
                                  text=True, timeout=_AGENT_TIMEOUT, cwd=cwd)

        try:
            envelope = json.loads(proc.stdout)
        except (json.JSONDecodeError, TypeError):
            raise _RetryableHTTP(
                "claude CLI 输出非 JSON（rc={}）: {}".format(proc.returncode, (proc.stderr or proc.stdout)[:200]))

        if proc.returncode != 0 or envelope.get('is_error'):
            msg = str(envelope.get('result') or proc.stderr or '')[:300]
            # 用量上限/限流可重试；其余（如内容被拒）直接抛，避免空转
            if any(k in msg.lower() for k in ('rate limit', 'usage limit', 'overloaded', 'timeout', '529')):
                raise _RetryableHTTP("claude CLI 可重试错误: {}".format(msg))
            raise RuntimeError("claude CLI 调用失败: {}".format(msg))

        # 截断可观测性（2026-08-19 加）：08-17 那轮 digest 有 15 篇因**响应被截断**而
        # JSON 解析失败（三个独立批次各 5 篇，报错全在文末），但当时无从判断——
        # gemini 分支查 finish_reason、openai 分支查 finish_reason，**只有这条不查**，
        # 而截断信号一直就在信封里。先把它打出来（不改行为），下一轮跑完即可确认
        # 静默截断时 CLI 到底报什么 stop_reason，再决定要不要据此抛 _RetryableHTTP。
        stop = envelope.get('stop_reason')
        term = envelope.get('terminal_reason')
        out_tok = (envelope.get('usage') or {}).get('output_tokens')
        if stop not in (None, 'end_turn') or term not in (None, 'completed'):
            logger.warning(
                "  ⚠️ claude CLI 非正常结束：stop_reason={} terminal_reason={} output_tokens={}"
                "（响应可能是半截的，下游 JSON 解析若失败请先看这一行）".format(stop, term, out_tok))
        else:
            logger.debug("  claude CLI ok: stop={} term={} out_tok={}".format(stop, term, out_tok))

        text = (envelope.get('result') or '').strip()
        if json_mode and text.startswith('```'):
            text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.S).strip()
        return text
