"""
Claude Agent (headless CLI) 翻译客户端

通过本机 `claude -p`（Claude Code headless 模式）翻译，走 Claude 订阅而非 API key。
定位：回退链的最后兜底（API__FALLBACK_PROVIDERS=claude-agent）——API 供应商全部
欠费/失效时保证翻译不中断。token 成本高于 API 直翻，不建议做主力 provider。

实现方式：复用 OpenAICompatibleTranslator 的全部 prompt 构建与 JSON 修复逻辑，
仅把 HTTP 调用替换为 claude CLI 子进程。
"""
import json
import shutil
import subprocess
import tempfile
import threading
from typing import Any, Optional

from ..core.schema import Settings
from ..core.exceptions import APIError, APIAuthenticationError, APITimeoutError
from .engine import OpenAICompatibleTranslator, _reraise_if_provider_fatal
from .support import PromptManager
from ..utils.logger import get_logger

logger = get_logger(__name__)

# 每次 CLI 调用的超时（秒）。一个 batch 的书籍段落 + sonnet 推理，给足余量
_CLI_TIMEOUT = 600
# 同时运行的 claude 进程上限：每个进程是独立 node 实例，内存开销大（M2 Pro 16GB）
_MAX_CONCURRENT_PROCESSES = 2


class ClaudeAgentTranslator(OpenAICompatibleTranslator):
    """通过 claude CLI headless 模式翻译的客户端。

    继承 OpenAICompatibleTranslator 以复用 prompt 组装（长文本单 message 模式）、
    JSON 解析修复、批量分流等逻辑；_chat_completions 改为驱动 claude 子进程。
    """

    # 类级信号量：即使多个实例/线程并发，也全局限制 claude 进程数
    _process_semaphore = threading.Semaphore(_MAX_CONCURRENT_PROCESSES)

    def __init__(self, settings: Settings):
        # 跳过父类 __init__（其要求 api_key/base_url），直接初始化祖父类
        # 并手工设置父类方法依赖的属性
        super(OpenAICompatibleTranslator, self).__init__(settings)

        self.cli_path = shutil.which('claude')
        if not self.cli_path:
            raise APIAuthenticationError(
                "claude CLI 未安装或不在 PATH 中，无法使用 claude-agent provider",
                context={"hint": "npm install -g @anthropic-ai/claude-code"},
            )

        # 完整版 prompt：❌/✅ markdown 表格（曾触发输出过滤器误报的格式特征）
        # 已在 system_instruction.md 中改写为等信息量纯文字，claude-agent 不必再
        # 降级到丢失全部反翻译腔规则的 simple 版。若误报复现，把 force_simple
        # 改回 True 即可回退（逃生舱保留）。
        self.prompt_manager = PromptManager(settings, force_simple=False)
        self._async_translator = None
        self.model: str = getattr(settings.api, 'agent_model', 'sonnet') or 'sonnet'
        # 复用父类的长文本模式：system instruction 合并进单个 prompt
        self.use_long_text_mode = True
        self.is_local = False
        self.is_deepseek = False
        self.api_key = None
        self.base_url = ''
        # 在系统临时目录运行 CLI，避免加载任何项目的 CLAUDE.md/记忆干扰翻译
        self._cwd = tempfile.gettempdir()

        logger.info("🤖 Claude Agent 翻译器已初始化 (headless CLI)")
        logger.info(f"   - CLI: {self.cli_path}")
        logger.info(f"   - 模型: {self.model}")
        logger.info(f"   - 进程并发上限: {_MAX_CONCURRENT_PROCESSES}")

    # ------------------------------------------------------------------
    # 核心：用 claude CLI 替换 HTTP chat completions
    # ------------------------------------------------------------------

    def _run_cli(self, prompt: str, extra_args: Optional[list] = None) -> str:
        """运行一次 claude -p，返回 result 文本。"""
        cmd = [
            self.cli_path, '-p',
            '--output-format', 'json',
            '--model', self.model,
        ]
        if extra_args:
            cmd.extend(extra_args)

        logger.info(f"📤 调用 claude agent (model={self.model}, prompt {len(prompt)} 字符)")
        with self._process_semaphore:
            try:
                proc = subprocess.run(
                    cmd,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=_CLI_TIMEOUT,
                    cwd=self._cwd,
                )
            except subprocess.TimeoutExpired:
                raise APITimeoutError(f"claude CLI 超时（{_CLI_TIMEOUT}s）")
            except OSError as e:
                raise APIError(f"claude CLI 启动失败: {e}")

        envelope = None
        try:
            envelope = json.loads(proc.stdout)
        except (json.JSONDecodeError, TypeError):
            pass

        if proc.returncode != 0:
            # 退出码非零时 stdout 里通常仍是完整 envelope，result 才是真实错误
            # （如 "API Error: 400 Output blocked by content filtering policy"）
            if isinstance(envelope, dict):
                detail = f"{envelope.get('subtype')}: {str(envelope.get('result'))[:300]}"
            else:
                detail = (proc.stderr or proc.stdout or '')[-300:]
            raise APIError(f"claude CLI 退出码 {proc.returncode} ({detail})")

        if envelope is None:
            raise APIError(f"claude CLI 输出不是合法 JSON envelope; tail={(proc.stdout or '')[-200:]}")

        if envelope.get('is_error'):
            # 订阅额度用尽等错误会在 result/subtype 中体现；
            # 交给 fallback.classify_fatal 按文本分类（usage limit → soft）
            raise APIError(f"claude CLI 返回错误: {envelope.get('subtype')} {str(envelope.get('result'))[:300]}")

        result = envelope.get('result')
        if not isinstance(result, str) or not result.strip():
            raise APIError(f"claude CLI 返回空 result (subtype={envelope.get('subtype')})")
        return result.strip()

    def _chat_completions(self, system_instruction: str, user_content: Any) -> str:
        """与父类签名一致；长文本模式下 system_instruction 已合并进 user_content。"""
        if isinstance(user_content, str):
            prompt = user_content
            if system_instruction:
                prompt = f"{system_instruction}\n\n{'=' * 80}\n\n{user_content}"
            return self._run_cli(prompt)

        # 多模态列表（vision 路径）：文本部分拼接，图片走 _call_vision_api 的专用实现
        text_parts = [
            item.get('text', '') for item in user_content
            if isinstance(item, dict) and item.get('type') == 'text'
        ]
        prompt = "\n".join(p for p in text_parts if p)
        if system_instruction:
            prompt = f"{system_instruction}\n\n{'=' * 80}\n\n{prompt}"
        return self._run_cli(prompt)

    def _call_vision_api(self, img_path: str, context: str,
                         glossary: Optional[dict] = None) -> str:
        """视觉翻译：让 agent 用 Read 工具亲自读图片文件。"""
        # 正式阶段 glossary 已在 system instruction；旁路调用才走兜底
        glossary_text = ""
        if glossary and not self._formal_phase:
            glossary_text = self.prompt_manager.format_glossary(glossary)
        original_prompt = self.prompt_manager.format_vision_prompt(context, glossary=glossary_text)
        system_instruction = self._build_system_instruction(use_vision=True)
        prompt = (
            f"{system_instruction}\n\n{'=' * 80}\n\n"
            f"First, use the Read tool to view the image file at: {img_path}\n\n"
            f"{original_prompt}"
        )
        try:
            raw_text = self._run_cli(prompt, extra_args=['--allowedTools', 'Read'])
            parsed = self._handle_json_response_with_repair(
                raw_text=raw_text,
                original_prompt=prompt,
                is_dict_like=True,
            )
            if isinstance(parsed, dict) and 'translation' in parsed:
                return str(parsed['translation'])
            return "[Failed: Invalid JSON Response]"
        except Exception as e:
            _reraise_if_provider_fatal(e)
            logger.error(f"❌ Claude Agent Vision 调用失败 for {img_path}: {e}")
            return f"[Failed: {str(e)}]"
