"""
多供应商回退翻译器

按 provider 链（如 deepseek → gemini）包装底层翻译器：
- 懒加载：只有轮到某个 provider 时才实例化（构建失败如缺 API key 自动跳过）
- 致命错误自动切换：欠费(402)/认证失败(401/403) 一次即切；配额/限流(429) 连续多次才切，
  避免瞬时限流误杀整个 provider
- 非致命错误按原样抛出，交给上层既有的重试与失败标记逻辑
"""
import asyncio
import re
import threading
from typing import Dict, List, Optional, Any, Tuple

from ..core.schema import Settings, SegmentList, TranslationMap
from ..core.exceptions import APIError, APIAuthenticationError, TranslationError
from .base import BaseTranslator, BaseAsyncTranslator
from ..utils.logger import get_logger

logger = get_logger(__name__)

# 硬性致命：认证/余额/权限问题，重试无意义，一次即切换
_HARD_PATTERNS = (
    'insufficient balance', 'payment required',
    'unauthorized', 'authentication', 'invalid api key',
    'incorrect api key', 'api key not valid', 'api_key_invalid',
    'permission denied', 'permission_denied', 'billing',
    # 模型下线/不存在：对该 provider 的所有后续请求都会同样失败
    # （engine 抛 "HTTPError: 404 Not Found"，DeepSeek body 为 "Model Not Exist"）
    'no longer available', 'model_not_found', 'not_found',
    'model not exist', 'model not found', 'model does not exist',
    'is not found',  # Gemini: "models/xxx is not found for API version ..."
)
# 软性致命：配额/限流，可能是瞬时的，连续达到阈值才切换
_SOFT_PATTERNS = (
    'resource_exhausted', 'resource exhausted', 'quota',
    'rate limit', 'rate_limit', 'too many requests', 'usage limit',
)
# 数字状态码必须整词匹配：避免命中 request id / 错误正文里恰好出现的数字串
_HARD_CODE_RE = re.compile(r'\b(401|402|403|404)\b')
_SOFT_CODE_RE = re.compile(r'\b429\b')
SOFT_SWITCH_THRESHOLD = 3

_AGENT_PROVIDERS = {'claude-agent', 'claude_agent', 'agent', 'claude'}

SUPPORTED_PROVIDERS = {
    'gemini',
    'deepseek', 'openai', 'openai-compatible', 'openai_compatible', 'ollama',
} | _AGENT_PROVIDERS


def classify_fatal(exc: Exception) -> Optional[str]:
    """判断异常是否为「该 provider 已不可用」级别的错误。

    Returns:
        'hard' / 'soft' / None（非致命）
    """
    if isinstance(exc, APIAuthenticationError):
        return 'hard'
    # google.genai / google.api_core 的错误对象带数字 code
    code = getattr(exc, 'code', None)
    if isinstance(code, int):
        if code in (401, 402, 403, 404):
            return 'hard'
        if code == 429:
            return 'soft'
    # 只对 API 层错误做文本分类：JSONParseError 等是模型输出问题，
    # 不代表 provider 不可用
    if not isinstance(exc, APIError):
        return None
    # 用 .message 而非 str(exc)：__str__ 会拼接 context（含 repr(response)
    # 等模型输出回显），书籍正文里出现 'billing'/'401' 等词会误杀健康 provider
    text = (getattr(exc, 'message', None) or str(exc)).lower()
    if _HARD_CODE_RE.search(text) or any(p in text for p in _HARD_PATTERNS):
        return 'hard'
    if _SOFT_CODE_RE.search(text) or any(p in text for p in _SOFT_PATTERNS):
        return 'soft'
    return None


def canonical_provider(provider: str) -> str:
    """把 provider 名归一化到实际后端：别名与同配置的同类 provider 视为同一个。

    deepseek/openai/ollama 等都由 OpenAICompatibleTranslator + 同一组 openai_*
    配置驱动，同链中出现两个只会得到完全相同的实例（假回退）。
    """
    p = (provider or '').strip().lower()
    if p in _AGENT_PROVIDERS:
        return 'claude-agent'
    if p == 'gemini':
        return 'gemini'
    if p in SUPPORTED_PROVIDERS:
        return 'openai-compatible'
    return p


def _create_translator(provider: str, settings: Settings) -> BaseTranslator:
    """按 provider 名创建底层翻译器（与 workflow._initialize_translator 的映射一致）"""
    from .engine import GeminiTranslator, OpenAICompatibleTranslator

    p = provider.lower()
    if p == 'gemini':
        return GeminiTranslator(settings)
    if p in _AGENT_PROVIDERS:
        from .agent import ClaudeAgentTranslator
        return ClaudeAgentTranslator(settings)
    if p in SUPPORTED_PROVIDERS:
        if p == 'deepseek':
            base_url = getattr(settings.api, 'openai_base_url', '') or ''
            if 'deepseek' not in base_url.lower():
                logger.warning(
                    f"⚠️ provider 'deepseek' 的 OPENAI_BASE_URL 并非 DeepSeek 地址 ({base_url})，"
                    "请检查 API__OPENAI_BASE_URL / API__OPENAI_API_KEY 配置"
                )
        return OpenAICompatibleTranslator(settings)
    raise TranslationError(f"未知 translator provider: {provider}")


class FallbackTranslator(BaseTranslator):
    """按 provider 链回退的翻译器包装。

    对上层暴露与单一翻译器完全一致的接口；内部维护当前 provider 指针，
    遇到致命错误时永久前进到下一个 provider 并重放当前调用。
    """

    def __init__(self, settings: Settings, providers: List[str]):
        super().__init__(settings)

        chain = []
        seen_backends = set()
        for p in providers:
            p = (p or '').strip().lower()
            if not p:
                continue
            if p not in SUPPORTED_PROVIDERS:
                logger.warning(f"⚠️ 回退链中未知 provider '{p}'，已忽略")
                continue
            backend = canonical_provider(p)
            if backend in seen_backends:
                logger.warning(f"⚠️ 回退链中 '{p}' 与已有条目是同一后端 ({backend})，已去重")
                continue
            seen_backends.add(backend)
            chain.append(p)
        if not chain:
            raise TranslationError("FallbackTranslator: 回退链为空或全部无效")

        self.providers = chain
        self._instances: Dict[int, BaseTranslator] = {}
        self._dead: set = set()
        self._current_idx = 0
        self._soft_failures: Dict[int, int] = {}
        self._lock = threading.RLock()
        self._async_translator: Optional['AsyncFallbackTranslator'] = None

        logger.info(f"🛟 回退翻译器已启用: {' → '.join(self.providers)}")
        # 立即解析首个可用 provider（缺 key 等构建失败会自动跳过）
        self._get_current()

    # ------------------------------------------------------------------
    # provider 生命周期管理
    # ------------------------------------------------------------------

    def _get_current(self) -> Tuple[int, BaseTranslator]:
        """返回当前可用的 (index, translator)，必要时懒加载；全部失效则抛错。"""
        with self._lock:
            while self._current_idx < len(self.providers):
                idx = self._current_idx
                if idx in self._dead:
                    self._current_idx += 1
                    continue
                if idx in self._instances:
                    return idx, self._instances[idx]
                provider = self.providers[idx]
                try:
                    instance = _create_translator(provider, self.settings)
                    self._instances[idx] = instance
                    logger.info(f"🛟 当前翻译 provider: {provider} ({idx + 1}/{len(self.providers)})")
                    return idx, instance
                except Exception as e:
                    logger.warning(f"⚠️ provider '{provider}' 初始化失败，跳过: {e}")
                    self._dead.add(idx)
                    self._current_idx += 1
            raise APIError(
                "所有翻译 provider 均不可用（回退链已耗尽）",
                context={"providers": self.providers},
            )

    def _mark_failed(self, idx: int, kind: str, exc: Exception) -> bool:
        """记录 provider 致命错误。返回是否已切换到下一个 provider。"""
        with self._lock:
            if idx != self._current_idx or idx in self._dead:
                # 其他并发调用已经处理过切换
                return True
            provider = self.providers[idx]
            if kind == 'soft':
                count = self._soft_failures.get(idx, 0) + 1
                self._soft_failures[idx] = count
                if count < SOFT_SWITCH_THRESHOLD:
                    logger.warning(
                        f"⚠️ provider '{provider}' 配额/限流错误 "
                        f"({count}/{SOFT_SWITCH_THRESHOLD} 次): {exc}"
                    )
                    return False
            self._dead.add(idx)
            self._current_idx = idx + 1
            logger.warning(
                f"🛟 provider '{provider}' 判定不可用（{kind} 错误: {str(exc)[:200]}），切换到下一个 provider"
            )
            return True

    def _call_with_fallback(self, method_name: str, *args, **kwargs) -> Any:
        """在当前 provider 上执行调用；致命错误时切换 provider 并重放。"""
        while True:
            idx, translator = self._get_current()
            try:
                result = getattr(translator, method_name)(*args, **kwargs)
                with self._lock:
                    self._soft_failures[idx] = 0
                return result
            except Exception as e:
                kind = classify_fatal(e)
                if kind is None:
                    raise
                switched = self._mark_failed(idx, kind, e)
                if not switched:
                    # 软性错误未达阈值：按原样抛出，让上层的重试逻辑处理
                    raise
                # 已切换：循环重放当前调用（_get_current 耗尽时会抛错）

    # ------------------------------------------------------------------
    # BaseTranslator 接口
    # ------------------------------------------------------------------

    def translate_batch(
        self,
        segments: SegmentList,
        context: str = "",
        glossary: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        return self._call_with_fallback('translate_batch', segments, context=context, glossary=glossary)

    def translate_titles(self, titles: List[str]) -> TranslationMap:
        return self._call_with_fallback('translate_titles', titles)

    def extract_glossary(self, segments: SegmentList) -> Dict[str, str]:
        return self._call_with_fallback('extract_glossary', segments)

    @property
    def async_translator(self) -> Optional['AsyncFallbackTranslator']:
        # 尊重本地模型的强制同步保护（engine.is_local → async_translator=None）：
        # 链中含指向 localhost 的 openai-compatible provider 时，线程池并发
        # 会压垮本地 Ollama（16GB 内存），返回 None 让 workflow 走同步路径
        if any(canonical_provider(p) == 'openai-compatible' for p in self.providers):
            base_url = (getattr(self.settings.api, 'openai_base_url', '') or '').lower()
            if 'localhost' in base_url or '127.0.0.1' in base_url:
                return None
        if self._async_translator is None:
            self._async_translator = AsyncFallbackTranslator(self)
        return self._async_translator

    # ------------------------------------------------------------------
    # Gemini 缓存接口透传（provider 不支持时静默降级）
    # ------------------------------------------------------------------

    def create_base_cache(self) -> Optional[str]:
        _, t = self._get_current()
        if hasattr(t, 'create_base_cache'):
            return t.create_base_cache()
        return None

    def create_full_cache(self, glossary: Optional[Dict[str, str]] = None) -> Optional[str]:
        _, t = self._get_current()
        if hasattr(t, 'create_full_cache'):
            return t.create_full_cache(glossary=glossary)
        return None

    def use_base_cache(self) -> bool:
        _, t = self._get_current()
        if hasattr(t, 'use_base_cache'):
            return t.use_base_cache()
        return False

    def cleanup(self):
        if self._async_translator is not None:
            try:
                self._async_translator.cleanup()
            except Exception:
                pass
        for instance in self._instances.values():
            try:
                instance.cleanup()
            except Exception:
                pass
            inner_async = getattr(instance, '_async_translator', None)
            if inner_async is not None:
                try:
                    inner_async.cleanup()
                except Exception:
                    pass


class AsyncFallbackTranslator(BaseAsyncTranslator):
    """异步包装：把回退感知的同步调用放进线程池执行。

    并发度由 workflow 层的 semaphore（async_max_workers）控制，
    这里的线程池只负责承载阻塞调用。
    """

    def __init__(self, base_translator: FallbackTranslator):
        super().__init__(base_translator)
        from concurrent.futures import ThreadPoolExecutor

        max_workers = getattr(base_translator.settings.processing, 'async_max_workers', 10)
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        logger.info(f"🛟 回退翻译器异步模式已初始化（workers={max_workers}）")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()
        return False

    async def translate_text_batch_async(
        self,
        segments: SegmentList,
        context: str,
        glossary: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self.executor,
            lambda: self.base.translate_batch(segments, context=context, glossary=glossary),
        )

    async def translate_vision_batch_async(
        self,
        segments: SegmentList,
        context: str,
        glossary: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        # 文本/视觉都由 base.translate_batch 按 content_type 分流，走同一条路
        return await self.translate_text_batch_async(segments, context, glossary=glossary)

    def cleanup(self):
        if getattr(self, 'executor', None) is not None:
            self.executor.shutdown(wait=True)
            self.executor = None
            logger.info("🧹 回退翻译器异步资源已清理")
