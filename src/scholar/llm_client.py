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


class LLMClient:
    """封装 LLM 连接与调用。只依赖 LLMSettings，无 workflow 状态。"""

    def __init__(self, llm_settings):
        self.settings = llm_settings  # scholar.schema.LLMSettings
        self._conn = None

    @property
    def conn(self) -> dict:
        if self._conn is None:
            self._conn = self._create()
        return self._conn

    def _create(self) -> dict:
        provider = self.settings.provider.lower()

        if provider in _AGENT_PROVIDERS:
            import shutil
            cli = shutil.which('claude')
            if not cli:
                raise ValueError(
                    "claude CLI 未安装或不在 PATH，无法使用 claude-agent provider"
                    "（npm install -g @anthropic-ai/claude-code）")
            return {'cli_path': cli, 'model': self.settings.model, 'provider': 'claude-agent'}

        if provider == 'gemini':
            from google import genai
            client = genai.Client(api_key=self.settings.api_key)
            return {'client': client, 'model': self.settings.model, 'provider': 'gemini'}

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
            'model': self.settings.model,
            'provider': 'openai-compatible',
        }

    def call(self, prompt: str, model: Optional[str] = None,
             max_tokens: Optional[int] = None, temperature: Optional[float] = None,
             json_mode: bool = False, max_retries: int = 4) -> str:
        """调用 LLM，返回文本。

        model/max_tokens/temperature 缺省时用 settings；json_mode=True 请求结构化 JSON。
        对限流(429)/服务端(5xx)/超时/连接错误做指数退避重试（并发多月回填时保证稳健）。
        """
        conn = self.conn
        provider = conn.get('provider')
        temp = self.settings.temperature if temperature is None else temperature
        mtok = self.settings.max_output_tokens if max_tokens is None else max_tokens

        last_exc = None
        for attempt in range(max_retries):
            try:
                if provider == 'claude-agent':
                    return self._call_agent(prompt, model or conn['model'], json_mode)

                if provider == 'gemini':
                    from google.genai import types
                    cfg = dict(temperature=temp, max_output_tokens=mtok)
                    if json_mode:
                        cfg['response_mime_type'] = 'application/json'
                    response = conn['client'].models.generate_content(
                        model=model or conn['model'],
                        contents=prompt,
                        config=types.GenerateContentConfig(**cfg),
                    )
                    return response.text

                # OpenAI 兼容
                payload = {
                    "model": model or conn['model'],
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
                return resp.json()['choices'][0]['message']['content']
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

    def _call_agent(self, prompt: str, model: str, json_mode: bool) -> str:
        conn = self.conn
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

        text = (envelope.get('result') or '').strip()
        if json_mode and text.startswith('```'):
            text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.S).strip()
        return text
