# -*- coding: utf-8 -*-
"""OpenAI 兼容后端（DeepSeek/Ollama/…；自 engine.py 拆出，阶段 4a）。
不 import google.genai——本侧在原文件里就与 genai 零依赖（审计三轮核实）。
外部一律经 `src.translator.engine` 门面 import，不直连本模块。
"""
import asyncio
import base64
import http.client
import json
import socket
import threading
import time
import re
import mimetypes
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor
from urllib import request, error
from urllib.parse import urlparse

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, retry_if_exception

from ..core.schema import Settings, ContentSegment, TranslationMap, SegmentList, contains_failed_marker
from ..core.exceptions import (
    APIError, APIRateLimitError, APITimeoutError, APIAuthenticationError,
    JSONParseError, TranslationError
)
from .base import BaseTranslator, BaseAsyncTranslator
from .support import CachePersistenceManager, PromptManager
from ..utils.logger import get_logger
from .engine_common import (
    GLOSSARY_WINDOW_SIZE, _glossary_extraction_prompt, _iter_glossary_windows,
    _normalize_translation_list, _reraise_if_provider_fatal, _stop_by_settings,
    _VISION_TRANSIENT_RETRIES,
)

logger = get_logger(__name__)


def _is_retryable_openai_error(e: BaseException) -> bool:
    """OpenAI 兼容路径重试谓词：_chat_completions 把 402/401/404 HTTPError 一律
    包装成 APIError，按类型匹配（retry_if_exception_type）会让欠费/认证失败/模型下线
    也空转 3 轮（每轮指数等待）才交给回退链；只有 classify_fatal != 'hard' 的
    APIError（超时/5xx/429/解析失败等包装）才值得重试。"""
    from .fallback import classify_fatal
    return isinstance(e, APIError) and classify_fatal(e) != 'hard'


# 视觉调用的瞬态异常内层重试次数：质检回路对 image 段"只报告不重译"，
# 这里的有限重试是该页图片在本轮运行中唯一的自愈机会——一次网络抖动


class OpenAICompatibleTranslator(BaseTranslator):
    """OpenAI-compatible 翻译客户端（DeepSeek API 兼容 OpenAI 格式）。

    DeepSeek docs:
      - base_url: https://api.deepseek.com (or https://api.deepseek.com/v1)
      - endpoint: POST /chat/completions

    Env vars are provided via Settings.api（SECTION__KEY 双下划线格式）:
      - API__OPENAI_API_KEY
      - API__OPENAI_BASE_URL
      - API__OPENAI_MODEL
    """

    def __init__(self, settings: Settings, provider: str = 'openai-compatible'):
        super().__init__(settings)
        self._async_translator: Optional[AsyncOpenAICompatibleTranslator] = None

        # provider='ollama' 读独立的 ollama_* 配置组：deepseek 与本地 ollama
        # 因此可以同链共存（此前共享 openai_* 一组配置，同链只会得到两个
        # 指向同一后端的实例）
        if provider == 'ollama':
            self.api_key = 'ollama'  # 本地服务无鉴权，占位即可
            self.base_url = settings.api.ollama_base_url
            self.model = settings.api.ollama_model
        else:
            self.api_key: Optional[str] = settings.api.openai_api_key
            self.base_url: str = settings.api.openai_base_url
            self.model: str = settings.api.openai_model

        # 验证和修复 base_url 配置
        self.base_url = self._validate_and_fix_base_url(self.base_url)

        # 自动检测是否为本地服务（Ollama）或 DeepSeek API
        # 适配 M2 Pro 16GB 硬件环境，本地模式需要特殊处理
        self.is_local: bool = self._detect_local_service(self.base_url)
        self.is_deepseek: bool = self._detect_deepseek_api(self.base_url)

        # PromptManager 的简化版/完整版选择必须由**本实例**是否本地决定，
        # 不能只看主 provider（settings.api.translator_provider）——回退链里
        # 主 provider=ollama 时，本实例若是 deepseek/openai-compatible（云端
        # 回退），self.is_local 为 False，仍然拿完整版；反之主 provider 是
        # 云端而本实例是 ollama 时，self.is_local 为 True，强制降级到简化版
        self.prompt_manager = PromptManager(settings, force_simple=self.is_local)
        
        # DeepSeek 长文本模式：_chat_completions 会把 system instruction（正式
        # 阶段含 mode+glossary）并入单条 user message
        # 原因：DeepSeek 对长上下文支持更好，且 system message 可能影响性能
        self.use_long_text_mode: bool = self.is_deepseek
        
        if self.is_local:
            logger.info("🏠 检测到本地模式（Ollama）")
            logger.info(f"   - 服务地址: {self.base_url}")
            logger.info(f"   - 模型: {self.model}")
            logger.info("   - 推理参数交由 Ollama/模型默认值（不注入 num_ctx/num_thread）")
        elif self.is_deepseek:
            logger.info("🚀 检测到 DeepSeek API")
            logger.info(f"   - API 地址: {self.base_url}")
            logger.info(f"   - 模型: {self.model}")
            logger.info("   - 已启用长文本模式（所有内容合并为单个 user message）")
        else:
            logger.info("☁️  检测到云端模式（OpenAI）")
            logger.info(f"   - API 地址: {self.base_url}")
            logger.info(f"   - 模型: {self.model}")

        if not self.api_key:
            raise APIAuthenticationError(
                "OpenAI-compatible API key is missing. 在 config/config.env 中设置 API__OPENAI_API_KEY（双下划线）。",
                context={"setting": "API__OPENAI_API_KEY"},
            )
    
    def _validate_and_fix_base_url(self, base_url: str) -> str:
        """验证并修复 base_url 配置
        
        Args:
            base_url: 原始的 base_url 配置
            
        Returns:
            修复后的 base_url
        """
        if not base_url:
            raise ValueError("OPENAI_BASE_URL 不能为空")
            
        base = base_url.strip()
        
        # 如果已经是完整的 URL，直接返回
        if base.startswith(('http://', 'https://')):
            return base
            
        # 处理常见的错误配置
        if 'deepseek' in base.lower():
            # DeepSeek 常见错误配置
            logger.warning(f"⚠️ 检测到不完整的 DeepSeek URL 配置: '{base}'")
            logger.warning("   自动修复为: https://api.deepseek.com")
            return 'https://api.deepseek.com'
        elif 'localhost' in base or '127.0.0.1' in base:
            # 本地服务
            if not base.startswith('http://'):
                fixed_url = f'http://{base}'
                logger.warning(f"⚠️ 本地服务 URL 缺少协议: '{base}' -> '{fixed_url}'")
                return fixed_url
            return base
        else:
            # 其他云端服务，假设是域名，添加 https://
            fixed_url = f'https://{base}'
            logger.warning(f"⚠️ 检测到不完整的 URL 配置: '{base}' -> '{fixed_url}'")
            logger.warning("   如果这是错误的，请在配置中提供完整的 URL（包含 http:// 或 https://）")
            return fixed_url

    def _detect_local_service(self, base_url: str) -> bool:
        """检测是否为本地服务（Ollama）
        
        Args:
            base_url: API 基础 URL
            
        Returns:
            True 如果是本地服务（包含 localhost 或 127.0.0.1）
        """
        if not base_url:
            return False
        url_lower = base_url.lower()
        return 'localhost' in url_lower or '127.0.0.1' in url_lower
    
    def _detect_deepseek_api(self, base_url: str) -> bool:
        """检测是否为 DeepSeek API
        
        Args:
            base_url: API 基础 URL
            
        Returns:
            True 如果是 DeepSeek API（包含 api.deepseek.com）
        """
        if not base_url:
            return False
        url_lower = base_url.lower()
        return 'deepseek.com' in url_lower or 'deepseek' in url_lower

    @property
    def async_translator(self) -> Optional['AsyncOpenAICompatibleTranslator']:
        # 本地模式强制同步翻译，降低功耗和内存压力
        if self.is_local:
            logger.debug("🔒 本地模式禁用异步翻译（降低功耗）")
            return None
        
        if self._async_translator is None:
            self._async_translator = AsyncOpenAICompatibleTranslator(self)
        return self._async_translator

    def cleanup(self):
        """清理异步线程池与持久连接（直连模式下由 workflow._cleanup_resources 调用；
        fallback 模式下由 FallbackTranslator.cleanup 逐实例调用）。"""
        if self._async_translator is not None:
            try:
                self._async_translator.cleanup()
            except Exception:
                pass
        local = self.__dict__.get('_conn_local')
        conn = getattr(local, 'conn', None) if local is not None else None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _build_system_instruction(self, use_vision: bool = False) -> str:
        """按当前翻译阶段构建 system instruction。

        正式阶段（begin_formal_translation 之后）包含 mode 与 glossary；
        结果按 use_vision 预算缓存——阶段内输入不变，逐请求重拼 ~7KB 大
        字符串（含全表 glossary join）是纯浪费，且缓存保证字节级恒定，
        DeepSeek 前缀缓存按稳定前缀命中。
        """
        # agent.py 跳过本类 __init__，缓存字典懒初始化
        cache: Dict[bool, str] = self.__dict__.setdefault('_si_cache', {})
        key = bool(use_vision) if self._formal_phase else None  # 预翻译阶段不缓存（量小且 mode 可能后置变化）
        if key is not None and key in cache:
            return cache[key]
        if self._formal_phase:
            si = self.prompt_manager.get_system_instruction(
                use_vision=use_vision,
                include_mode=True,
                include_glossary=bool(self._formal_glossary),
                glossary_text=self.prompt_manager.format_glossary(self._formal_glossary),
            )
            cache[key] = si
            return si
        return self.prompt_manager.get_system_instruction(use_vision=use_vision)

    def begin_formal_translation(self, glossary: Optional[Dict[str, str]] = None) -> None:
        super().begin_formal_translation(glossary)
        self.__dict__['_si_cache'] = {}  # 阶段切换时使旧预算失效
        logger.info(
            "🧭 正式翻译阶段：mode + glossary 已并入 system instruction "
            f"(术语 {len(self._formal_glossary)} 条)"
        )

    def translate_batch(
        self,
        segments: SegmentList,
        context: str = "",
        glossary: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        # 本方法（主翻译路径的唯一入口）无需自带 @retry：429/超时/连接抖动的
        # tenacity 覆盖已在被调用的 _translate_text_batch 上（本文件
        # _translate_text_batch 定义处的 @retry(stop=_stop_by_settings,
        # retry=retry_if_exception(_is_retryable_openai_error), reraise=True)）。
        # 调用路径：同步走 workflow._run_sync_batches 直接调本方法；异步走
        # workflow._process_single_batch → AsyncOpenAICompatibleTranslator.
        # translate_text_batch_async → self.base._translate_text_batch（见本
        # 文件 AsyncOpenAICompatibleTranslator.translate_text_batch_async），
        # 两条路径最终都落到同一个被 @retry 包装的 _translate_text_batch，
        # 而不是本方法 translate_batch 自身——异步路径并不经过本方法。
        # _http_post_json 把超时/连接抖动/HTTP 4xx&5xx 一律包装成
        # APIError/APITimeoutError（core/exceptions.py），
        # _is_retryable_openai_error 按 classify_fatal!='hard' 放行重试，
        # 402/401/404 等致命错误立即 reraise 不空转——与 Gemini 侧同款语义，
        # 核实为已覆盖，此处不再补一份重复的 @retry。
        if not segments:
            return []

        has_image = any(seg.content_type == "image" for seg in segments)
        if has_image:
            return self._translate_vision_batch(segments, context, glossary)
        return self._translate_text_batch(segments, context, glossary)

    @retry(
        stop=_stop_by_settings,
        wait=wait_exponential(multiplier=1, min=1, max=20),
        # 与 _translate_text_batch 同款谓词：致命错误（401/402/404 等）不重试，
        # 立即上抛给外层 except 做 _reraise_if_provider_fatal 判定。
        retry=retry_if_exception(_is_retryable_openai_error),
        reraise=True,
    )
    def _translate_titles_once(self, titles: List[str]) -> TranslationMap:
        """标题翻译单次尝试，供 translate_titles 的 @retry 包装调用。

        此前无重试：一次瞬态错误（429/超时/连接抖动）就被外层 except 静默吞成
        {}，整批标题留原文——而 DeepSeek/Ollama 是本项目的主力 provider，
        标题翻译这条路径反而比 Gemini 更缺重试保护。与 Gemini 版
        _translate_titles_once 同款模式：异常在此原样上抛，由装饰器负责瞬态
        重试，外层 except 只处理重试耗尽后的最终结果。
        """
        input_json_str = json.dumps(titles, ensure_ascii=False)
        original_prompt = self.prompt_manager.format_title_prompt(input_json_str)

        raw_text = self._chat_completions(
            system_instruction=self._build_system_instruction(use_vision=False),
            user_content=original_prompt,
        )

        parsed_data = self._parse_json_response(
            raw_text=raw_text,
            is_dict_like=True,
        )

        if isinstance(parsed_data, dict):
            return {str(k): str(v) for k, v in parsed_data.items() if isinstance(v, str)}

        if isinstance(parsed_data, list) and parsed_data:
            result: Dict[str, str] = {}
            for item in parsed_data:
                if isinstance(item, dict):
                    for k, v in item.items():
                        if k != 'id':
                            result[str(k)] = str(v)
            return result

        return {}

    def translate_titles(self, titles: List[str]) -> TranslationMap:
        if not titles:
            return {}

        try:
            return self._translate_titles_once(titles)
        except Exception as e:
            # 对齐 Gemini 版降级语义：标题阶段位于全部译文落盘之后、渲染之前，
            # 一次瞬态错误若上抛会让 100% 完成的翻译产不出任何成品。
            # provider 级致命错误仍上抛给回退链；瞬态错误经 @retry 耗尽后
            # 仍失败——维持原语义，降级返回 {}，标题保用原文，渲染继续。
            _reraise_if_provider_fatal(e)
            logger.warning(f"Title translation failed; keeping original titles: {e}")
            return {}

    def extract_glossary(self, segments: SegmentList) -> Dict[str, str]:
        if not segments:
            return {}

        text_to_analyze: List[str] = []
        for seg in segments:
            if seg.is_translated:
                text_to_analyze.append(
                    f"Original: {seg.original_text}\nTranslated: {seg.translated_text}\n---"
                )

        if not text_to_analyze:
            return {}

        # 按窗口覆盖全文（不再只取前 8000 字符），逐窗抽取后合并
        final_glossary: Dict[str, str] = {}
        for content_sample in _iter_glossary_windows(text_to_analyze):
            try:
                final_glossary.update(self._extract_glossary_window(content_sample))
            except Exception as e:
                logger.error(f"术语抽取窗口失败（跳过）: {e}")
        return final_glossary

    def _extract_glossary_window(self, content_sample: str) -> Dict[str, str]:
        """对单个内容窗口做一次术语抽取，返回归一化后的 {原文: 译文} 字典。"""
        original_prompt = _glossary_extraction_prompt(content_sample)

        raw_text = self._chat_completions(
            system_instruction="You output JSON only.",
            user_content=original_prompt,
        )

        parsed = self._parse_json_response(
            raw_text=raw_text,
            is_dict_like=True,
        )

        window_glossary: Dict[str, str] = {}
        if isinstance(parsed, dict):
            for k, v in parsed.items():
                if k and v:
                    window_glossary[str(k).strip()] = str(v).strip()
        elif isinstance(parsed, list):
            # 与 Gemini 版对齐：模型偶尔返回 [{en: zh}, ...] 或 [[en, zh], ...]
            # 数组，此前 OpenAI 版静默返回 0 条术语
            for item in parsed:
                if isinstance(item, dict):
                    for k, v in item.items():
                        if k and v:
                            window_glossary[str(k).strip()] = str(v).strip()
                elif isinstance(item, (list, tuple)) and len(item) == 2:
                    k, v = item
                    if k and v:
                        window_glossary[str(k).strip()] = str(v).strip()
        return window_glossary

    @retry(
        stop=_stop_by_settings,
        wait=wait_exponential(multiplier=1, min=1, max=20),
        # 谓词式重试而非类型匹配：APIError 里混着 402/401/404 的 hard 包装，
        # 对欠费/模型下线重试只会空转 3 轮才交给回退链
        retry=retry_if_exception(_is_retryable_openai_error),
        reraise=True,
    )
    def _translate_text_batch(
        self,
        segments: SegmentList,
        context: str,
        glossary: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        input_data = [{"id": seg.segment_id, "text": seg.original_text} for seg in segments]
        input_json = json.dumps(input_data, ensure_ascii=False)

        safe_context = context[-self.settings.processing.max_context_length:] if context else ""

        # 正式阶段 glossary 已在 system instruction（稳定前缀，DeepSeek 可命中
        # 前缀缓存）；仅当旁路调用方未走 begin_formal_translation 却直接传了
        # 术语表时，才退回 user message 注入
        user_glossary = ""
        if glossary and not self._formal_phase:
            user_glossary = self.prompt_manager.format_glossary(glossary)

        original_prompt = self.prompt_manager.format_text_prompt(
            context=safe_context,
            input_json=input_json,
            glossary=user_glossary,
        )
        # 长文本模式（DeepSeek）的 system 并入单条 user message 的合并职责
        # 统一在 _chat_completions，这里不再预拼接
        raw_text = self._chat_completions(
            system_instruction=self._build_system_instruction(use_vision=False),
            user_content=original_prompt,
        )

        # 解析响应，传递期望的 ID 列表以便检测缺失的翻译
        input_ids = [s.segment_id for s in segments]
        output_list = self._parse_json_response(
            raw_text=raw_text,
            is_text_translation=True,
            expected_ids=input_ids,
        )
        output_list = _normalize_translation_list(output_list)

        output_map = {
            int(item['id']): str(item.get('translation', ''))
            for item in output_list
            if isinstance(item, dict) and 'id' in item and str(item['id']).isdigit()
        }
        return [output_map.get(uid, "[Failed: Missing translation]") for uid in input_ids]

    def _translate_vision_batch(self, segments: SegmentList, context: str,
                                glossary: Optional[Dict[str, str]] = None) -> List[str]:
        results: List[str] = []
        current_context = context[-self.settings.processing.max_context_length:] if context else ""

        for seg in segments:
            try:
                if seg.content_type == "image" and seg.image_path:
                    translation = self._call_vision_api(seg.image_path, current_context, glossary)
                    time.sleep(self.settings.processing.vision_rate_limit_delay)
                else:
                    fallback = self._translate_text_batch([seg], current_context, glossary=glossary)
                    translation = fallback[0] if fallback else "[Fallback Failed]"

                results.append(translation)

                current_context += f"\n{translation}"
                if len(current_context) > self.settings.processing.max_context_length:
                    current_context = current_context[-self.settings.processing.max_context_length:]
            except Exception as e:
                # 与 Gemini 版对齐的逐段隔离：文本段降级调用带 @retry(reraise=True)，
                # 耗尽后异常若不在此捕获会丢弃整批已算出的 results（含已成功的视觉段）。
                # provider 级致命错误仍上抛给回退链，其余单段打标继续。
                _reraise_if_provider_fatal(e)
                logger.error(f"❌ Vision翻译失败 (segment {seg.segment_id}): {e}")
                results.append(f"[Failed: {str(e)}]")
                continue

        return results

    def _call_vision_api(self, img_path: str, context: str,
                         glossary: Optional[Dict[str, str]] = None) -> str:
        # 正式阶段 glossary 已在 system instruction；旁路调用才走 user message 兜底
        user_glossary = ""
        if glossary and not self._formal_phase:
            user_glossary = self.prompt_manager.format_glossary(glossary)
        original_prompt = self.prompt_manager.format_vision_prompt(context, glossary=user_glossary)

        # 文本批有 @retry 装饰而视觉此前零重试（与 Gemini 版同样的缺口）：
        # 瞬态异常有限重试，耗尽后才落 [Failed:]。
        last_error: Optional[Exception] = None
        for attempt in range(_VISION_TRANSIENT_RETRIES + 1):
            try:
                with open(img_path, 'rb') as f:
                    b64 = base64.b64encode(f.read()).decode('ascii')
                image_url = f"data:image/png;base64,{b64}"

                raw_text = self._chat_completions(
                    system_instruction=self._build_system_instruction(use_vision=True),
                    user_content=[
                        {"type": "text", "text": original_prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                )

                parsed = self._parse_json_response(
                    raw_text=raw_text,
                    is_dict_like=True,
                )
                if isinstance(parsed, dict) and 'translation' in parsed:
                    return str(parsed['translation'])
                return "[Failed: Invalid JSON Response]"
            except Exception as e:
                # hard/soft 的 provider 级错误立即上抛给回退链，瞬态异常才原地重试
                _reraise_if_provider_fatal(e)
                last_error = e
                if attempt < _VISION_TRANSIENT_RETRIES:
                    wait_time = min(8, 2 ** (attempt + 1))  # 2s → 4s，指数等待
                    logger.warning(
                        f"⚠️ OpenAI-compatible Vision API 瞬态失败"
                        f"（尝试 {attempt + 1}/{_VISION_TRANSIENT_RETRIES + 1}），"
                        f"{wait_time}s 后重试 {img_path}: {e}"
                    )
                    time.sleep(wait_time)

        logger.error(f"❌ OpenAI-compatible Vision API 调用失败 for {img_path}: {last_error}")
        return f"[Failed: {str(last_error)}]"

    def _http_post_json(self, url: str, data: bytes, headers: Dict[str, str], timeout: int) -> str:
        """POST 并返回响应体，按线程复用持久连接（keep-alive）。

        此前每次请求用 urllib.request.urlopen 新建 TCP+TLS 连接——一本书
        150-200 次 TLS 握手、跨境 RTT 下累计 25-60s 纯握手开销。改为
        http.client 线程本地长连接（异步线程池下每线程各持一条），陈旧
        连接（服务端已断开）自动重建一次。
        """
        parsed = urlparse(url)
        key = (parsed.scheme, parsed.netloc)
        # agent.py 跳过本类 __init__，线程本地存储懒初始化
        local = self.__dict__.setdefault('_conn_local', threading.local())

        last_exc: Optional[Exception] = None
        for attempt in range(2):
            conn = getattr(local, 'conn', None)
            if conn is None or getattr(local, 'conn_key', None) != key:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                conn_cls = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
                conn = conn_cls(parsed.netloc, timeout=timeout)
                local.conn, local.conn_key = conn, key
            elif conn.sock is not None:
                # 复用连接时按本次请求更新超时
                conn.sock.settimeout(timeout)

            try:
                path = parsed.path or '/'
                conn.request('POST', path, body=data, headers=headers)
                resp = conn.getresponse()
                body = resp.read().decode('utf-8', errors='replace')
                if resp.status >= 400:
                    raise APIError(f"OpenAI-compatible HTTPError: {resp.status} {resp.reason} {body[:200]}")
                return body
            except APIError:
                raise  # 应用层错误：响应已读完，连接可继续复用
            except socket.timeout as e:
                # 超时不重试（语义与旧 urlopen 一致，重试交给上层 tenacity）
                local.conn = None
                try:
                    conn.close()
                except Exception:
                    pass
                raise APITimeoutError(f"OpenAI-compatible request timeout: {e}")
            except (http.client.HTTPException, ConnectionError, BrokenPipeError, OSError) as e:
                # 陈旧连接（keep-alive 被服务端关闭）最常见：重建一次再试
                local.conn = None
                try:
                    conn.close()
                except Exception:
                    pass
                last_exc = e
                if attempt == 0:
                    continue
        raise APITimeoutError(f"OpenAI-compatible request failed: {last_exc}")

    def _build_chat_completions_url(self) -> str:
        """构建 Chat Completions API URL

        - 本地模式：强制使用 http://127.0.0.1:11434/v1/chat/completions
        - 云端模式：base_url 的 http(s):// 前缀已由 __init__ 的
          _validate_and_fix_base_url 唯一入口保证（否则抛 ValueError），
          这里只负责拼 /chat/completions——DeepSeek 对带/不带 /v1 的
          base 两种拼法都支持，无需归一化
        """
        # 本地模式：强制使用 127.0.0.1:11434/v1/chat/completions（Ollama 标准接口）
        if self.is_local:
            # 统一使用 127.0.0.1 而非 localhost，避免 DNS 解析问题
            return 'http://127.0.0.1:11434/v1/chat/completions'

        base = (self.base_url or '').rstrip('/')
        return base + '/chat/completions'

    def _chat_completions(self, system_instruction: str, user_content: Any) -> str:
        """调用 Chat Completions API

        本地模式（Ollama）：
        - 超时取 max(120, request_timeout)——本地大模型一批可达十几分钟
        - 不注入 num_ctx/num_thread，推理参数交由 Ollama/模型默认值

        DeepSeek 长文本模式：
        - system_instruction 在此处（唯一位置）并入单条 user message，
          调用方一律正常传 system_instruction，不做预拼接
        - 原因：DeepSeek 对长上下文支持更好，且避免 system message 限制
        """
        # DeepSeek 长文本模式：把 system instruction 并入唯一的 user message
        if self.use_long_text_mode:
            if system_instruction:
                if isinstance(user_content, str):
                    user_content = f"{system_instruction}\n\n{'=' * 80}\n\n{user_content}"
                elif isinstance(user_content, list):
                    # 多模态内容：system 作为首个 text part 注入
                    user_content = [
                        {"type": "text", "text": f"{system_instruction}\n\n{'=' * 80}"}
                    ] + list(user_content)
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "user", "content": user_content},
                ],
                "temperature": self.settings.processing.temperature,
                "stream": False,
            }
            logger.debug("📝 使用长文本模式（system instruction 已并入单条 user message）")
        else:
            # 标准模式：system + user 分离
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": user_content},
                ],
                "temperature": self.settings.processing.temperature,
                "stream": False,
            }

        # 结构化输出：云端（DeepSeek/OpenAI 兼容）原生支持 JSON mode，从根上保证返回是合法 JSON；
        # 本地 Ollama 不保证，跳过以免报错（由正则安全网兜底）。
        # 不设 max_tokens：不同模型输出上限不一（deepseek-chat 仅 8192），显式设过高会 400；
        # 交给服务端默认，避免为一个非必要参数引入整批失败风险。
        if not self.is_local:
            payload["response_format"] = {"type": "json_object"}
        
        # 记录发送给API的文本长度
        total_text_length = 0
        for message in payload["messages"]:
            content = message["content"]
            if isinstance(content, str):
                total_text_length += len(content)
            elif isinstance(content, list):
                # 处理多模态内容
                for item in content:
                    if isinstance(item, dict) and "text" in item:
                        total_text_length += len(item["text"])
        
        logger.info(f"📤 发送API请求 - 文本总长度: {total_text_length} 字符")
        if self.use_long_text_mode:
            logger.debug("   📊 长文本模式: [System Instruction(正式阶段含 Mode+Glossary)] + 分隔符 + Context + Input JSON（单条 user message）")
        else:
            logger.debug("   📊 标准模式: System Instruction + User Content")
        
        # 本地模式不再注入 num_ctx/num_thread：旧值（1024/1，为 16GB 内存设）
        # 一旦被 Ollama 严格执行会静默截断输入毁掉译文；上下文交给模型默认值。
        # 但 max_tokens 必须显式解锁：Ollama 无此字段时按默认 num_predict 截断
        # 生成（实测 qwen3.5:35b 被限 ~3.9K），思考型模型的 thinking 先烧掉大半
        # 预算、译文被拦腰截断；云端仍不设（各服务上限不一，设过高会 400）。
        # 24576 而非 16384：20K 字符的大批下 thinking 可膨胀至 1 万+ token，
        # 16384 会被整个烧完导致 content 为空（实测连续空响应）
        if self.is_local:
            payload["max_tokens"] = 24576

        url = self._build_chat_completions_url()
        data = json.dumps(payload).encode('utf-8')
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.api_key}',
        }
        
        # 动态超时：在用户配置之上保证下限，不得低于配置值。
        # 本地下限 1800s——大模型一批实测 2-10 分钟（qwen3.5 thinking 随输入膨胀），
        # 回退链切到 ollama 时若沿用云端的 120s 配置，客户端超时后服务端仍在算
        # 旧请求、重试排队必然雪崩；DeepSeek 下限 120s（云端响应慢的老经验值）
        timeout = self.settings.processing.request_timeout
        if self.is_local:
            timeout = max(1800, timeout)
            logger.debug(f"⏱️  本地模式超时设置: {timeout}s")
        elif self.is_deepseek:
            timeout = max(120, timeout)
            logger.debug(f"⏱️  DeepSeek模式超时设置: {timeout}s")

        resp_text = self._http_post_json(url, data, headers, timeout)

        try:
            parsed = json.loads(resp_text)
            # DeepSeek 前缀缓存命中监测：prompt 拼装顺序（system+glossary 稳定
            # 前缀在先）是否真的命中缓存，唯一的量化证据就是这两个字段
            usage = parsed.get('usage') or {}
            hit = usage.get('prompt_cache_hit_tokens')
            miss = usage.get('prompt_cache_miss_tokens')
            if hit is not None or miss is not None:
                logger.debug(f"💾 DeepSeek 前缀缓存: hit={hit} miss={miss} tokens")
            content = parsed['choices'][0]['message']['content']
            if not isinstance(content, str):
                return json.dumps(content, ensure_ascii=False)
            return content.strip()
        except Exception as e:
            raise APIError(f"OpenAI-compatible response parse failed: {e}")

    def _strip_code_fences(self, text: str) -> str:
        # 先按整串包裹匹配（贪婪，内容含内嵌 ``` 也安全）；不中再宽松搜索首个
        # 代码块——模型在代码块外带说明文字时（claude-agent 无 JSON mode 最常见）
        # 此前直接掉进正则兜底
        strict = re.search(r'^```(?:json)?\s*(.*)\s*```$', text.strip(), re.DOTALL)
        if strict:
            return strict.group(1)
        loose = re.search(r'```(?:json)?\s*(.*?)\s*```', text, re.DOTALL)
        if loose:
            return loose.group(1)
        return text

    def _parse_json_response(
        self,
        raw_text: str,
        *,
        is_text_translation: bool = False,
        is_dict_like: bool = False,
        expected_ids: Optional[List[int]] = None,
    ) -> Any:
        """
        处理 JSON 响应（简化版，与 Gemini translator 逻辑一致）
        
        纠错流程：
        1. 标准 JSON 解析
        2. 正则表达式兜底解析（尽可能提取成功的翻译）
        3. 对于缺失的 segment，标记为失败（不再调用 LLM 修正）
        
        Args:
            expected_ids: 期望的 segment ID 列表（用于检测缺失的翻译）
        """
        # ========== 阶段1：标准JSON解析 ==========
        try:
            cleaned = self._strip_code_fences(raw_text)
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.debug(f"⚠️ 标准JSON解析失败: {e}")
        
        # ========== 阶段2：正则表达式兜底解析 ==========
        if is_text_translation:
            try:
                fallback = self._regex_fallback_for_list(raw_text)
                if fallback and len(fallback) > 0:
                    extracted_count = len(fallback)
                    logger.info(f"✅ 正则表达式解析成功，提取 {extracted_count} 条翻译")
                    
                    # 如果提供了期望的 ID 列表，检查缺失的翻译
                    if expected_ids:
                        extracted_ids = {item.get("id") for item in fallback}
                        missing_ids = [eid for eid in expected_ids if eid not in extracted_ids]
                        
                        if missing_ids:
                            logger.warning(f"⚠️ {len(missing_ids)} 个 segment 翻译缺失: {missing_ids[:5]}{'...' if len(missing_ids) > 5 else ''}")
                            # 为缺失的 ID 添加失败标记
                            for mid in missing_ids:
                                fallback.append({
                                    "id": mid,
                                    "translation": "[Failed: Missing in response]"
                                })
                    
                    return fallback
            except Exception as e:
                logger.debug(f"⚠️ 正则表达式解析失败: {e}")
        
        # ========== 最终兜底：返回错误标记 ==========
        logger.error(f"❌ JSON 解析失败（标准JSON + 正则均失败），原始响应长度: {len(raw_text)}")
        
        if is_text_translation:
            if expected_ids:
                return [{"id": eid, "translation": "[Failed: JSON Parse Error]"} for eid in expected_ids]
            return [{"id": 1, "translation": "[Failed: JSON Parse Error]"}]
        if is_dict_like:
            return {}
        raise JSONParseError("Failed to parse JSON")

    def _regex_fallback_for_list(self, text: str) -> List[Dict[str, Any]]:
        """正则表达式兜底解析（与 Gemini translator 的 _regex_fallback 逻辑一致）"""
        logger.info("🔄 Using regex fallback for JSON parsing...")
        
        # 检测是否被截断
        is_truncated = not text.rstrip().endswith((']', '}'))
        if is_truncated:
            logger.warning("⚠️ Detected incomplete JSON (missing closing bracket)")
        
        # 单档宽松匹配（response_format=json_object 已保证云端返回合法 JSON，此处仅安全网）
        pattern = r'"id"\s*:\s*(\d+)[^}]*?"translation"\s*:\s*"((?:[^"\\]|\\.)*?)(?:"|$)'
        matches = re.findall(pattern, text, re.DOTALL)

        if not matches:
            return []
        
        logger.info(f"✅ Regex extracted {len(matches)} segments" + (" (from truncated JSON)" if is_truncated else ""))
        
        result = []
        for mid, mtext in matches:
            cleaned_text = mtext.replace('\\"', '"').replace("\\'", "'").replace('\\n', '\n')
            # 检测最后一个对象是否被截断
            if is_truncated and (mid, mtext) == matches[-1]:
                if cleaned_text and not cleaned_text.rstrip().endswith(('。', '！', '？', '.', '!', '?', '」', '"', ')', '）')):
                    cleaned_text += "[...翻译被截断]"
            result.append({"id": int(mid), "translation": cleaned_text})
        
        return result

# ========================================================================
# OpenAI-compatible (DeepSeek) 异步翻译客户端
# ========================================================================
class AsyncOpenAICompatibleTranslator(BaseAsyncTranslator):
    """异步 OpenAI-compatible 翻译器（线程池包装，同步HTTP请求并发执行）。
    
    并发控制策略（M2 Pro 16GB 优化）：
    - 本地模式（Ollama）：max_workers=2，防止 16GB 统一内存溢出
    - 云端模式（DeepSeek）：max_workers=10，充分利用网络并发
    支持上下文管理器自动资源清理。
    """

    def __init__(self, base_translator: OpenAICompatibleTranslator):
        super().__init__(base_translator)
        
        # 并发控制：尊重 settings.async_max_workers（此前云端硬编码 3/10，
        # 配置被静默忽略——workflow 的 semaphore 放行 N 个批次而线程池只有 3
        # 个线程，有效并发被压到 1/5）。本地模式仍强制 2（16GB 内存保护）。
        configured = getattr(base_translator.settings.processing, 'async_max_workers', 10)
        if base_translator.is_local:
            max_workers = 2
        else:
            max_workers = max(1, int(configured))
            
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self._max_workers = max_workers  # 保存用于日志
        
        # 日志输出当前并发模式
        mode_label = "本地模式" if base_translator.is_local else (
            "DeepSeek模式" if base_translator.is_deepseek else "云端模式")
        source = "本地强制 2，内存保护" if base_translator.is_local else "来自 PROCESSING__ASYNC_MAX_WORKERS"
        logger.info(f"🚀 异步翻译器已初始化（{mode_label}）")
        logger.info(f"   - 并发数: {max_workers}（{source}）")
    
    async def __aenter__(self):
        """异步上下文管理器入口"""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器退出，自动清理资源"""
        self.cleanup()
        return False
    
    def __enter__(self):
        """同步上下文管理器入口（兼容性）"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """同步上下文管理器退出"""
        self.cleanup()
        return False
    
    def __del__(self):
        """析构函数，确保资源清理"""
        try:
            self.cleanup()
        except Exception:
            pass

    async def translate_text_batch_async(
        self,
        segments: SegmentList,
        context: str,
        glossary: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        if not segments:
            return []

        # 获取当前事件循环（安全方式）
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()

        def _sync():
            return self.base._translate_text_batch(segments, context, glossary)

        return await loop.run_in_executor(self.executor, _sync)

    async def translate_vision_batch_async(
        self,
        segments: SegmentList,
        context: str,
        glossary: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        if not segments:
            return []

        # 获取当前事件循环（安全方式）
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()

        def _sync_one(seg: ContentSegment) -> str:
            if seg.content_type == 'image' and seg.image_path:
                return self.base._call_vision_api(seg.image_path, context, glossary)
            fallback = self.base._translate_text_batch([seg], context, glossary)
            return fallback[0] if fallback else "[Fallback Failed]"

        tasks = [loop.run_in_executor(self.executor, _sync_one, seg) for seg in segments]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        final: List[str] = []
        for r in results:
            if isinstance(r, Exception):
                _reraise_if_provider_fatal(r)
                final.append(f"[Failed: {str(r)}]")
            else:
                final.append(r)
        return final

    def cleanup(self):
        self.executor.shutdown(wait=True)
        logger.info("🧹 OpenAI-compatible 异步翻译器已清理资源")
