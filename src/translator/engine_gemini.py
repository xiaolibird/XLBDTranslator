# -*- coding: utf-8 -*-
"""Gemini 后端（自 engine.py 拆出，阶段 4a：两后端在原文件里零符号交叉，
唯一连接点是 engine_common 的共享 helper）。
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

from google import genai
from google.genai import types
from google.genai import errors as genai_errors
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



def _is_retryable_gemini_error(e: BaseException) -> bool:
    """Gemini 重试谓词：新版 google-genai SDK 抛的是 genai.errors.APIError 体系，
    而非旧 google.api_core.exceptions——按旧类型匹配会让真实 429/5xx 完全得不到重试。
    规则：
    - 项目内 core.APIError（空响应/安全拦截等包装）→ 重试，但排除 hard：
      APIAuthenticationError 等子类被 classify_fatal 判为 hard，重试只会空转；
    - genai ServerError（5xx，服务端瞬态故障）→ 重试；
    - genai ClientError 仅 429（限流是瞬态的）→ 重试；
    - 其余 ClientError（401/402/403/404 等致命 4xx）与任意其他异常 → 不重试，
      立刻上抛让回退链/致命短路机制接手，避免对欠费/认证错误空转 3 轮。"""
    if isinstance(e, APIError):
        from .fallback import classify_fatal
        return classify_fatal(e) != 'hard'
    if isinstance(e, genai_errors.ServerError):
        return True
    if isinstance(e, genai_errors.ClientError):
        return getattr(e, "code", None) == 429
    return False



# 只有错误文本明确指向 context cache 本身失效（过期/被删/找不到）才值得清空
# cache_refs + 磁盘元数据并降级无缓存重发；429/401/402/404 等 provider 级故障、
# 网络超时等瞬态问题与缓存健康与否无关，不应被牵连着一起清缓存。
_CACHE_INVALID_PATTERNS = ('cached content', 'cachedcontent', 'cache_not_found', 'cache not found')


def _looks_like_cache_invalidity(exc: Exception) -> bool:
    """判断异常是否明确指示 Gemini context cache 本身失效。

    见 _generate_content 的缓存降级分支：此前 `except Exception` 不加区分地
    把 429/401/402/404/超时等一切错误都当成「缓存坏了」处理——一次瞬态限流
    就会把健康的 cache 引用清空、磁盘元数据删掉，并立刻无缓存重发同一批（对
    429 等于双倍烧配额），此后全书剩余批次全部改为无缓存全价发送。这里改为
    先看错误文本是否明确提到 cache/cached content，不是的话原样上抛，交给
    @retry（_is_retryable_gemini_error）或回退链按各自既有语义处理，缓存
    保持不变，下次重试仍可复用。"""
    text = str(exc).lower()
    return any(p in text for p in _CACHE_INVALID_PATTERNS)




# 文本翻译的结构化输出 schema：强制模型返回
# {"translations": [{"id": int, "translation": str}]} 顶层对象。
# 顶层用对象而非数组：与 DeepSeek response_format=json_object 的「顶层必须为
# 对象」语义、以及全部 prompt 的输出契约统一（_normalize_translation_list 兼容
# 解包旧数组格式）。仅用于文本批量翻译；术语表/标题翻译是动态键字典，无法用
# 固定 schema 约束，故不设。
_TRANSLATION_RESPONSE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "translations": types.Schema(
            type=types.Type.ARRAY,
            items=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "id": types.Schema(type=types.Type.INTEGER),
                    "translation": types.Schema(type=types.Type.STRING),
                },
                required=["id", "translation"],
            ),
        ),
    },
    required=["translations"],
)

# 术语抽取的分窗大小（字符）。此前对全文只取前 8000 字符导致大文档后半段术语系统性缺失；


class _ResponseValidationError(APIError):
    """API 请求成功但响应内容不可用（安全过滤 block、空响应、无 candidates）。

    与缓存/网络故障区分开：这类错误不应触发 _generate_content 的缓存失效
    降级重发（内容级错误重发同样会失败，且会误删健康的 context cache）。
    """



class GeminiTranslator(BaseTranslator):
    """Gemini 翻译客户端。
    
    继承自 BaseTranslator，实现 Google Gemini 的具体翻译逻辑。
    """

    def __init__(
        self, 
        settings: Settings, 
        cache_manager: Optional[CachePersistenceManager] = None
    ):
        """
        Args:
            settings: 全局设置对象（包含document_path用于计算hash）
            cache_manager: 缓存持久化管理器（可选），如果提供则使用，否则自动创建
        """
        # 调用父类构造函数
        super().__init__(settings)
        
        self.generation_config = {}
        self.cache_refs: Dict[str, str] = {}
        self._async_translator = None  # 懒加载异步翻译器
        self._client: Optional[genai.Client] = None
        self._base_generation_config: Optional[types.GenerateContentConfig] = None
        
        # 初始化 Prompt 管理器
        self.prompt_manager = PromptManager(settings)
        
        # 初始化缓存持久化管理器（优先使用传入的，否则根据doc_hash创建）
        self.cache_persistence = cache_manager
        if self.cache_persistence is None and settings.processing.enable_gemini_caching and self.doc_hash:
            self.cache_persistence = CachePersistenceManager(settings)

        # 配置 API
        self._configure_api()

        # 初始化模型（新 SDK：Client + GenerateContentConfig；通过适配器保留旧调用形态）
        self.model = self._create_model()
        
        # 注意：初始化时不创建缓存
        # 缓存创建分两阶段：
        # 1. 预翻译阶段：调用 create_base_cache() 创建基础缓存（无 glossary、无 mode）
        # 2. 正式翻译阶段：调用 create_full_cache() 创建完整缓存（含 glossary 和 mode）
        logger.info("🔧 GeminiTranslator 初始化完成（延迟缓存创建）")
    
    def create_base_cache(self) -> Optional[str]:
        """
        创建基础缓存（用于预翻译阶段）
        
        只包含 system_instruction + text_translation_prompt
        不包含 glossary 和 mode
        
        Returns:
            缓存名称，如果失败返回 None
        """
        if not self.settings.processing.enable_gemini_caching or not self.cache_persistence:
            logger.info("ℹ️ Gemini 缓存未启用，跳过基础缓存创建")
            return None
        
        # 生成基础 system instruction（无 mode、无 glossary）
        system_instruction = self.prompt_manager.get_system_instruction(
            use_vision=self.settings.processing.use_vision_mode,
            include_mode=False,
            include_glossary=False
        )
        
        cache_name = self.cache_persistence.get_or_create_system_cache(
            system_instruction=system_instruction,
            model_name=self.settings.api.gemini_model,
            display_name="base_pretranslate"
        )
        
        if cache_name:
            self.cache_refs['base'] = cache_name
            logger.info(f"✅ 基础缓存已就绪（预翻译用）: {cache_name[:50]}...")
        
        return cache_name
    
    def create_full_cache(self, glossary: Optional[Dict[str, str]] = None) -> Optional[str]:
        """
        创建完整缓存（用于正式翻译阶段）
        
        包含 system_instruction + text_translation_prompt + mode + glossary
        
        Args:
            glossary: 术语表字典
            
        Returns:
            缓存名称，如果失败返回 None
        """
        if not self.settings.processing.enable_gemini_caching or not self.cache_persistence:
            logger.info("ℹ️ Gemini 缓存未启用，跳过完整缓存创建")
            return None
        
        # 生成完整 system instruction（含 mode 和 glossary）
        system_instruction = self.prompt_manager.get_system_instruction(
            use_vision=self.settings.processing.use_vision_mode,
            include_mode=True,
            include_glossary=bool(glossary),
            glossary_text=self.prompt_manager.format_glossary(glossary)
        )
        
        mode_name = getattr(self.settings.processing.translation_mode_entity, 'name', 'Default')
        glossary_count = len(glossary) if glossary else 0
        
        cache_name = self.cache_persistence.get_or_create_system_cache(
            system_instruction=system_instruction,
            model_name=self.settings.api.gemini_model,
            display_name=f"full_{mode_name}_g{glossary_count}"
        )
        
        if cache_name:
            self.cache_refs['system'] = cache_name  # 正式翻译使用 'system' key
            logger.info(f"✅ 完整缓存已就绪（正式翻译用）: {cache_name[:50]}...")
            logger.info(f"   - 翻译模式: {mode_name}")
            logger.info(f"   - 术语表: {glossary_count} 条")
        
        return cache_name
    
    def use_base_cache(self) -> bool:
        """切换到使用基础缓存（预翻译阶段）"""
        if 'base' in self.cache_refs:
            self.cache_refs['system'] = self.cache_refs['base']
            return True
        return False

    def begin_formal_translation(self, glossary: Optional[Dict[str, str]] = None) -> None:
        """进入正式翻译阶段：建完整缓存 + 重建 base config。

        重建 _base_generation_config 让非缓存路径与缓存失效降级路径
        （_generate_content 回填 system_instruction 处）同样带上
        mode + glossary——此前这两条路径会静默丢失翻译模式。
        """
        super().begin_formal_translation(glossary)
        self.create_full_cache(glossary=glossary)  # 内部有 caching 开关守卫

        full_si = self.prompt_manager.get_system_instruction(
            use_vision=self.settings.processing.use_vision_mode,
            include_mode=True,
            include_glossary=bool(glossary),
            glossary_text=self.prompt_manager.format_glossary(glossary),
        )
        self._base_generation_config = self._base_generation_config.model_copy(
            update={"system_instruction": full_si}
        )
        logger.info(
            "🧭 正式翻译阶段：base config 已重建（mode + glossary 进入 system instruction，"
            f"术语 {len(self._formal_glossary)} 条）"
        )
    
    @property
    def async_translator(self):
        """懒加载异步翻译器"""
        if self._async_translator is None:
            # 函数内延迟 import：async 模块顶层 import 本模块（拿同步实现与 schema），
            # 顶层反向 import 会成环——同 fallback↔engine 既有的破环手法
            from .engine_gemini_async import AsyncGeminiTranslator
            self._async_translator = AsyncGeminiTranslator(self)
        return self._async_translator

    def cleanup(self):
        """清理异步线程池（直连模式下由 workflow._cleanup_resources 调用）。"""
        if self._async_translator is not None:
            try:
                self._async_translator.cleanup()
            except Exception:
                pass
    

    def _configure_api(self):
        """配置 Gemini API"""
        try:
            # Gemini Developer API
            self._client = genai.Client(api_key=self.settings.api.gemini_api_key)
        except Exception as e:
            raise APIAuthenticationError(
                "Failed to configure Gemini API. Check your API key.",
                context={"error": str(e)}
            )

    def _create_model(self):
        """创建 Gemini 模型实例（新 SDK：仅准备 base config，并返回适配器）"""

        if self._client is None:
            raise APIAuthenticationError("Gemini client is not configured")

        safety_settings = [
            types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
        ]

        # 从 settings 读取生成参数（而不是硬编码）
        self.generation_config = {
            "temperature": self.settings.processing.temperature,
            "top_p": self.settings.processing.top_p,
            "response_mime_type": "application/json",
            "max_output_tokens": self.settings.processing.max_output_tokens,
        }
        
        # 可选参数：top_k（如果设置了才添加）
        if self.settings.processing.top_k is not None:
            self.generation_config["top_k"] = self.settings.processing.top_k
        
        logger.debug(f"🔧 API 生成参数: temperature={self.generation_config['temperature']}, "
                    f"top_p={self.generation_config['top_p']}, "
                    f"max_output_tokens={self.generation_config['max_output_tokens']}")

        # 根据processing模式选择对应的system instruction（包含prompt固定部分）
        use_vision = self.settings.processing.use_vision_mode
        system_instruction = self.prompt_manager.get_system_instruction(use_vision=use_vision)

        self._base_generation_config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            safety_settings=safety_settings,
            **self.generation_config,
        )

    def _generate_content(self, contents: Any, generation_config: Optional[Dict[str, Any]] = None, use_cache: bool = True, purpose: str = "API Call") -> Any:
        """统一的内容生成方法，处理缓存逻辑
        
        Args:
            contents: 要发送的内容
            generation_config: 生成配置覆盖（字典格式）
            use_cache: 是否尝试使用系统缓存
            purpose: 调用目的（用于日志）
            
        Returns:
            API响应对象
        """
        # 构建配置
        config_update = generation_config or {}
        config = self._base_generation_config.model_copy(update=config_update)
        
        # 处理缓存
        cache_name = self.cache_refs.get("system") if use_cache else None
        if cache_name:
            config = config.model_copy(update={
                "cached_content": cache_name,
                "system_instruction": None,
                "tools": None,
                "tool_config": None,
            })
        
        try:
            response = self._client.models.generate_content(
                model=self.settings.api.gemini_model,
                contents=contents,
                config=config,
            )
            if cache_name:
                logger.debug(f"🔄 {purpose} 使用 Gemini Cache: {cache_name[:30]}...")
            # Validate response structure to avoid downstream NoneType subscripts
            # 注意：以下三类是「请求成功但响应内容不可用」（内容级错误），用
            # _ResponseValidationError 标记——它们与缓存无关，绝不能触发下面的
            # 缓存失效降级（否则一次安全过滤 block 会误删整本书的 context cache
            # 并再全价重发一次注定同样被拦的请求）
            if not response:
                logger.error(f"❌ {purpose} returned empty response object")
                raise _ResponseValidationError(f"Empty response from model for {purpose}", context={"response": repr(response)})

            candidates = getattr(response, 'candidates', None)
            # Check for prompt_feedback block reasons (e.g., prohibited content)
            prompt_fb = getattr(response, 'prompt_feedback', None)
            if prompt_fb is not None and getattr(prompt_fb, 'block_reason', None):
                block_reason = getattr(prompt_fb, 'block_reason')
                logger.error(f"❌ {purpose} blocked by model: {block_reason}")
                raise _ResponseValidationError(f"Model blocked content for {purpose}", context={"block_reason": str(block_reason), "response": repr(response)})

            if not candidates or candidates[0] is None:
                logger.error(f"❌ {purpose} response has no candidates: {repr(response)}")
                raise _ResponseValidationError(f"Model response missing candidates for {purpose}", context={"response": repr(response)})

            return response
        except _ResponseValidationError:
            # 内容级错误：缓存是好的，直接上抛给调用方按内容失败处理
            raise
        except Exception as e:
            # 缓存失败时降级——但仅当错误明确指示 cache 本身失效时才清缓存+
            # 无缓存重发；429/401/402/404/超时等与缓存健康无关的错误原样上抛，
            # 交给 @retry / 回退链按既有语义处理，避免误杀健康的 context cache
            # （见 _looks_like_cache_invalidity 的说明）
            if cache_name and _looks_like_cache_invalidity(e):
                logger.warning(f"⚠️  {purpose} 缓存使用失败，降级为普通调用: {e}")

                # P1修复：清理失效的缓存引用。
                # 用原子 pop 而非 check-then-del：异步线程池共享同一 cache_refs，
                # TTL 到期瞬间多个在飞请求同时进此分支，check-then-del 会二次
                # KeyError 掩盖原始错误
                if self.cache_refs.get('system') == cache_name:
                    self.cache_refs.pop('system', None)
                    logger.info("🗑️  已清理内存中的失效缓存引用")
                
                # P1修复：从元数据中删除失效缓存
                if self.cache_persistence:
                    self.cache_persistence.remove_invalid_cache(cache_name)
                
                config_no_cache = config.model_copy(update={
                    "cached_content": None,
                    "system_instruction": self._base_generation_config.system_instruction,
                })
                response2 = self._client.models.generate_content(
                    model=self.settings.api.gemini_model,
                    contents=contents,
                    config=config_no_cache,
                )

                # 同样验证备用响应
                if not response2:
                    logger.error(f"❌ {purpose} fallback returned empty response object")
                    raise APIError(f"Empty fallback response from model for {purpose}", context={"response": repr(response2)})

                # Check fallback prompt feedback as well
                prompt_fb2 = getattr(response2, 'prompt_feedback', None)
                if prompt_fb2 is not None and getattr(prompt_fb2, 'block_reason', None):
                    block_reason2 = getattr(prompt_fb2, 'block_reason')
                    logger.error(f"❌ {purpose} fallback blocked by model: {block_reason2}")
                    raise APIError(f"Fallback model blocked content for {purpose}", context={"block_reason": str(block_reason2), "response": repr(response2)})

                candidates2 = getattr(response2, 'candidates', None)
                if not candidates2 or candidates2[0] is None:
                    logger.error(f"❌ {purpose} fallback response has no candidates: {repr(response2)}")
                    raise APIError(f"Fallback model response missing candidates for {purpose}", context={"response": repr(response2)})

                return response2
            raise
    


    def translate_batch(
        self,
        segments: SegmentList,
        context: str = "",
        glossary: Optional[Dict[str, str]] = None
    ) -> List[str]:
        """
        核心翻译方法
        根据内容类型自动分流到对应处理逻辑
        """
        if not segments:
            return []

        # 检查是否包含图片
        has_image = any(seg.content_type == "image" for seg in segments)

        if has_image:
            return self._translate_vision_batch(segments, context, glossary)
        else:
            return self._translate_text_batch(segments, context, glossary)

    @retry(
        stop=_stop_by_settings,
        wait=wait_exponential(multiplier=1, min=1, max=30),
        # 与 _translate_text_batch 同款谓词：致命错误（401/402/404 等）不重试，
        # 立即上抛给外层 except 做 _reraise_if_provider_fatal 判定。
        retry=retry_if_exception(_is_retryable_gemini_error),
        reraise=True
    )
    def _translate_titles_once(self, titles: List[str]) -> TranslationMap:
        """标题翻译单次尝试，供 translate_titles 的 @retry 包装调用。

        此前无重试：一次瞬态错误（429/超时）就被外层 except 静默吞成 {}，
        整批标题留原文且回退链感知不到失败。现让异常在此原样上抛，
        由装饰器负责瞬态重试，外层 except 只处理重试耗尽后的最终结果。
        """
        input_json_str = json.dumps(titles, ensure_ascii=False)
        original_prompt = self.prompt_manager.format_title_prompt(input_json_str)

        response = self._generate_content(
            contents=original_prompt,
            generation_config=self.generation_config,
            use_cache=True,
            purpose="Title Translation"
        )
        raw_text = response.candidates[0].content.parts[0].text
        # 解析响应（含正则兜底）
        parsed_data = self._parse_json_response(
            raw_text,
            is_title_translation=True
        )

        # 归一化处理
        if isinstance(parsed_data, dict):
            return {str(k): str(v) for k, v in parsed_data.items() if isinstance(v, str)}
        elif isinstance(parsed_data, list) and parsed_data:
            result = {}
            for item in parsed_data:
                if isinstance(item, dict):
                    for k, v in item.items():
                        if k != 'id':  # 跳过 id 字段
                            result[str(k)] = str(v)
            return result

        return {}

    def translate_titles(self, titles: List[str]) -> TranslationMap:
        """翻译标题列表"""
        if not titles:
            return {}

        try:
            return self._translate_titles_once(titles)
        except Exception as e:
            # provider 级错误（hard：欠费/认证/模型下线；soft：配额限流）必须上抛
            # 而非降级为空 {}：标题翻译若静默返回空表，回退链无从触发/计数，
            # 标题会整批留原文。瞬态错误经 _translate_titles_once 的 @retry
            # 重试耗尽后仍失败——维持原语义，降级返回 {} 由调用方兜底。
            _reraise_if_provider_fatal(e)
            logger.error(f"Title translation failed even after correction attempts: {e}")
            return {}

    def extract_glossary(self, segments: SegmentList) -> Dict[str, str]:
        """从已翻译的片段中自动提取术语表"""
        logger.info("🧠 正在提取术语表以增强后续翻译...")
        if not segments:
            logger.warning("   - 无内容可供提取术语表。")
            return {}

        # 准备用于分析的文本
        text_to_analyze = []
        for seg in segments:
            if seg.is_translated:
                text_to_analyze.append(f"Original: {seg.original_text}\nTranslated: {seg.translated_text}\n---")
        
        if not text_to_analyze:
            logger.warning("   - 提供的片段均未翻译，无法提取术语。")
            return {}

        # 按窗口覆盖全文（不再只取前 8000 字符），逐窗抽取后合并
        final_glossary: Dict[str, str] = {}
        windows = list(_iter_glossary_windows(text_to_analyze))
        for idx, content_sample in enumerate(windows, 1):
            if len(windows) > 1:
                logger.info(f"   - 术语抽取窗口 {idx}/{len(windows)}")
            try:
                final_glossary.update(self._extract_glossary_window(content_sample))
            except Exception as e:
                logger.error(f"   - ❌ 术语抽取窗口 {idx} 失败（跳过）: {e}")

        if final_glossary:
            logger.info(f"   - ✅ 成功提取 {len(final_glossary)} 个术语。")
            for k, v in list(final_glossary.items())[:5]:
                logger.info(f"     - '{k}' -> '{v}'")
            if len(final_glossary) > 5:
                logger.info("     - ... (更多术语)")
        else:
            logger.warning("   - ⚠️ 术语提取未能产生有效字典。")
        return final_glossary

    def _extract_glossary_window(self, content_sample: str) -> Dict[str, str]:
        """对单个内容窗口做一次术语抽取，返回归一化后的 {原文: 译文} 字典。"""
        original_prompt = _glossary_extraction_prompt(content_sample)

        if self._client is None:
            raise APIAuthenticationError("Gemini client is not configured")

        # Use centralized _generate_content to benefit from response validation and cache fallback
        try:
            response = self._generate_content(
                contents=original_prompt,
                generation_config={
                    "response_mime_type": "application/json",
                    "temperature": self.settings.processing.temperature,
                    "max_output_tokens": self.settings.processing.max_output_tokens,
                },
                use_cache=True,
                purpose="Glossary Extraction"
            )
        except APIError as e:
            # 被安全过滤器拦截时跳过该窗口，允许翻译继续。
            # 用 .message 而非 str(e)：__str__ 拼接 context（含 repr(response)
            # 回显），书籍正文里出现 'blocked' 一词会误判（与 fallback.classify_fatal 同理）
            _msg = (getattr(e, 'message', None) or str(e))
            if "blocked" in _msg.lower() or "PROHIBITED_CONTENT" in _msg:
                logger.warning(f"⚠️  Glossary Extraction was blocked by safety filters. Skipping window. Error: {e}")
                return {}
            raise

        raw_text = response.candidates[0].content.parts[0].text
        parsed_glossary = self._parse_json_response(
            raw_text,
            is_glossary_extraction=True
        )

        # 归一化不同可能的模型输出格式为平坦的 {str: str} 形式
        window_glossary: Dict[str, str] = {}
        if isinstance(parsed_glossary, dict):
            for k, v in parsed_glossary.items():
                try:
                    if k and v:
                        window_glossary[str(k).strip()] = str(v).strip()
                except TypeError:
                    continue
        elif isinstance(parsed_glossary, list):
            for item in parsed_glossary:
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
        wait=wait_exponential(multiplier=1, min=1, max=30),
        # 谓词式重试而非类型匹配：genai ClientError 里混着可重试的 429 和致命的 401/402/404，
        # 只有按 code 区分才能既覆盖真实限流又不对欠费空转。
        retry=retry_if_exception(_is_retryable_gemini_error),
        reraise=True
    )
    def _translate_text_batch(
        self,
        segments: SegmentList,
        context: str,
        glossary: Optional[Dict[str, str]] = None
    ) -> List[str]:
        """文本批量翻译（带重试）"""
        # 构建输入数据
        input_data = [
            {"id": seg.segment_id, "text": seg.original_text}
            for seg in segments
        ]
        input_json = json.dumps(input_data, ensure_ascii=False)

        # 截取上下文
        safe_context = context[-self.settings.processing.max_context_length:] if context else ""

        # 正式阶段 glossary 已在 system instruction（缓存与非缓存路径均由
        # begin_formal_translation 保证）；仅旁路调用才走 user message 兜底
        glossary_text = ""
        if glossary and not self._formal_phase:
            glossary_text = self.prompt_manager.format_glossary(glossary)

        # 格式化提示（固定部分已在system instruction，仅填充动态变量）
        original_prompt = self.prompt_manager.format_text_prompt(
            context=safe_context,
            input_json=input_json,
            glossary=glossary_text
        )

        # 结构化输出：强制模型返回符合 schema 的 JSON 数组，几乎消除畸形输出。
        text_generation_config = {**self.generation_config, "response_schema": _TRANSLATION_RESPONSE_SCHEMA}

        response = self._generate_content(
            contents=original_prompt,
            generation_config=text_generation_config,
            use_cache=True,
            purpose="Text Translation"
        )
        try:
            raw_text = response.candidates[0].content.parts[0].text
        except Exception:
            # If the response was blocked/malformed, return failed markers for this batch
            logger.error("❌ Text Translation response invalid or blocked; marking batch as failed")
            return ["[Failed: Blocked or invalid response]" for _ in segments]
        
        # 解析响应，传递期望的 ID 列表以便检测缺失的翻译
        input_ids = [s.segment_id for s in segments]
        output_list = self._parse_json_response(
            raw_text,
            is_text_translation=True,
            expected_ids=input_ids
        )
        output_list = _normalize_translation_list(output_list)

        # 映射结果
        output_map = {
            int(item['id']): str(item.get('translation', ''))
            for item in output_list
            if isinstance(item, dict) and 'id' in item and str(item['id']).isdigit()
        }

        # 生成最终结果
        results = []
        for uid in input_ids:
            results.append(output_map.get(uid, "[Failed: Missing translation]"))

        return results

    def _translate_vision_batch(
        self,
        segments: SegmentList,
        context: str,
        glossary: Optional[Dict[str, str]] = None
    ) -> List[str]:
        """视觉批量翻译（串行处理）"""
        results = []
        current_context = context[-self.settings.processing.max_context_length:] if context else ""

        for seg in segments:
            try:
                if seg.content_type == "image" and seg.image_path:
                    translation = self._call_vision_api(
                        seg.image_path,
                        current_context,
                        glossary
                    )
                    time.sleep(self.settings.processing.vision_rate_limit_delay)
                else:
                    # 降级处理文本
                    fallback_result = self._translate_text_batch([seg], current_context, glossary)
                    translation = fallback_result[0] if fallback_result else "[Fallback Failed]"

                results.append(translation)

                # 更新上下文
                current_context += f"\n{translation}"
                if len(current_context) > self.settings.processing.max_context_length:
                    current_context = current_context[-self.settings.processing.max_context_length:]

            except Exception as e:
                _reraise_if_provider_fatal(e)
                logger.error(f"❌ Vision翻译失败 (segment {seg.segment_id}): {e}")
                results.append(f"[Failed: {str(e)}]")
                continue

        return results

    def _call_vision_api(self, img_path: str, context: str, glossary: Optional[Dict[str, str]] = None) -> str:
        """调用视觉 API（支持 Gemini Caching；瞬态异常有限内层重试）"""
        # 文本批有 @retry 装饰而视觉此前零重试：一次瞬态抖动即让该页
        # 本轮定格失败，这里补上与文本路径对等的有限重试。
        last_error: Optional[Exception] = None
        for attempt in range(_VISION_TRANSIENT_RETRIES + 1):
            try:
                # 正式阶段 glossary 已在 system instruction；旁路调用才走兜底
                glossary_text = ""
                if glossary and not self._formal_phase:
                    glossary_text = self.prompt_manager.format_glossary(glossary)

                # 使用 prompt_manager 格式化提示
                original_prompt = self.prompt_manager.format_vision_prompt(context, glossary=glossary_text)

                mime_type, _ = mimetypes.guess_type(img_path)
                mime_type = mime_type or "image/png"
                with open(img_path, "rb") as f:
                    image_bytes = f.read()

                image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)

                vision_config = {
                    "temperature": self.generation_config["temperature"],
                    "top_p": self.generation_config["top_p"],
                    "max_output_tokens": self.generation_config["max_output_tokens"],
                    "response_mime_type": "application/json",
                }

                response = self._generate_content(
                    contents=[original_prompt, image_part],
                    generation_config=vision_config,
                    use_cache=True,
                    purpose="Vision Translation"
                )

                raw_text = (response.text or "").strip()

                # 解析 JSON 并提取 "translation" 字段（含正则兜底）
                parsed_json = self._parse_json_response(
                    raw_text,
                    is_vision_translation=True,
                )

                if isinstance(parsed_json, dict) and "translation" in parsed_json:
                    return parsed_json["translation"]

                logger.error(
                    "❌ Vision API did not return valid JSON with a 'translation' key even after correction. "
                    f"Got: {raw_text[:200]}"
                )
                return "[Failed: Invalid JSON Response]"

            except Exception as e:
                # hard/soft 的 provider 级错误立即上抛给回退链（hard 必然复现、
                # soft 需要回退链计数切换），只有瞬态异常才值得原地重试
                _reraise_if_provider_fatal(e)
                last_error = e
                if attempt < _VISION_TRANSIENT_RETRIES:
                    wait_time = min(8, 2 ** (attempt + 1))  # 2s → 4s，指数等待
                    logger.warning(
                        f"⚠️ Vision API 瞬态失败（尝试 {attempt + 1}/{_VISION_TRANSIENT_RETRIES + 1}），"
                        f"{wait_time}s 后重试 {img_path}: {e}"
                    )
                    time.sleep(wait_time)

        logger.error(f"❌ Vision API调用失败 for {img_path}: {last_error}")
        return f"[Failed: {str(last_error)}]"

    def _parse_json_response(
        self,
        raw_text: str,
        is_title_translation: bool = False,
        is_glossary_extraction: bool = False,
        is_text_translation: bool = False,
        is_vision_translation: bool = False,
        expected_ids: Optional[List[int]] = None
    ) -> Any:
        """
        处理 JSON 响应（简化版，废除 LLM 自我修正，优先保存成功部分）

        注：original_prompt/image_part 参数随 LLM 自我修正机制一并废除
        （函数体从未引用，仅让读者误以为会拿原 prompt 重问模型）。
        
        纠错流程：
        1. 标准 JSON 解析
        2. 正则表达式兜底解析（尽可能提取成功的翻译）
        3. 对于缺失的 segment，标记为失败（不再调用 LLM 修正）
        
        Args:
            expected_ids: 期望的 segment ID 列表（用于检测缺失的翻译）
        """
        # ========== 阶段1：标准JSON解析 ==========
        try:
            parsed_data = self._repair_json_content(raw_text)
            logger.debug("✅ 标准JSON解析成功")
            return parsed_data
        except JSONParseError as e:
            logger.debug(f"⚠️ 标准JSON解析失败: {e}")
        
        # ========== 阶段2：正则表达式兜底解析 ==========
        try:
            if is_text_translation:
                fallback_result = self._regex_fallback(raw_text)
                if fallback_result and len(fallback_result) > 0:
                    # 检查是否是真正的翻译失败标签
                    first_trans = fallback_result[0].get("translation", "")
                    if not first_trans.startswith("[Failed") and not first_trans.startswith("[Translation Failed"):
                        extracted_count = len(fallback_result)
                        logger.info(f"✅ 正则表达式解析成功，提取 {extracted_count} 条翻译")
                        
                        # 如果提供了期望的 ID 列表，检查缺失的翻译
                        if expected_ids:
                            extracted_ids = {item.get("id") for item in fallback_result}
                            missing_ids = [eid for eid in expected_ids if eid not in extracted_ids]
                            
                            if missing_ids:
                                logger.warning(f"⚠️ {len(missing_ids)} 个 segment 翻译缺失: {missing_ids[:5]}{'...' if len(missing_ids) > 5 else ''}")
                                # 为缺失的 ID 添加失败标记
                                for mid in missing_ids:
                                    fallback_result.append({
                                        "id": mid,
                                        "translation": "[Failed: Missing in response]"
                                    })
                        
                        return fallback_result
                        
            elif is_title_translation or is_glossary_extraction:
                fallback_result = self._regex_fallback_for_dict_like(raw_text)
                if fallback_result:
                    logger.info(f"✅ 正则表达式解析成功（字典格式），提取 {len(fallback_result)} 项")
                    return fallback_result
                    
        except Exception as e:
            logger.debug(f"⚠️ 正则表达式解析失败: {e}")
        
        # ========== 最终兜底：返回错误标记 ==========
        logger.error(f"❌ JSON 解析失败（标准JSON + 正则均失败），原始响应长度: {len(raw_text)}")
        logger.debug(f"   原始响应末尾: {raw_text[-200:] if len(raw_text) > 200 else raw_text}")
        
        if is_text_translation:
            # 如果提供了期望的 ID 列表，为所有 ID 返回失败标记
            if expected_ids:
                return [{"id": eid, "translation": "[Failed: JSON Parse Error]"} for eid in expected_ids]
            return [{"id": 1, "translation": "[Failed: JSON Parse Error]"}]
        elif is_title_translation or is_glossary_extraction:
            return {}
        elif is_vision_translation:
            return {}
        
        return None

    def _repair_json_content(self, text: str) -> Any:
        """修复 JSON 字符串 (只进行代码块去除，不进行高级字符串修复)"""
        # 去除 Markdown 代码块：先整串包裹匹配，不中再宽松搜索首个代码块
        # （与 OpenAI 版 _strip_code_fences 同款修复）
        match = re.search(r'^```(?:json)?\s*(.*)\s*```$', text.strip(), re.DOTALL) \
            or re.search(r'```(?:json)?\s*(.*?)\s*```', text, re.DOTALL)
        if match:
            text = match.group(1)

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            # 不再进行内部高级修复，直接抛出，由上层处理自我修正
            raise JSONParseError(f"Initial JSON parse failed: {e}")

    def _regex_fallback(self, text: str) -> List[Dict[str, Any]]:
        """正则表达式兜底解析（支持截断恢复）"""
        logger.info("🔄 Using regex fallback for JSON parsing...")

        # 检测是否被截断（末尾没有 ] 或最后一个对象不完整）
        is_truncated = not text.rstrip().endswith((']', '}'))
        if is_truncated:
            logger.warning("⚠️ Detected incomplete JSON (missing closing bracket or truncated content)")

        # 单档宽松匹配：结构化输出已从根上保证合法 JSON，这里只作最小安全网
        # （主要覆盖不支持 response_schema 的本地 Ollama 及截断场景）。
        pattern = r'"id"\s*:\s*(\d+)[^}]*?"translation"\s*:\s*"((?:[^"\\]|\\.)*?)(?:"|$)'
        matches = re.findall(pattern, text, re.DOTALL)

        if not matches:
            logger.error(f"❌ Regex fallback failed completely. Text length: {len(text)}, Last 200 chars: {repr(text[-200:])}")
            logger.warning("⚠️ Returning translation failure tag to ensure output file integrity.")
            return [{"id": 1, "translation": "[Translation Failed - JSON Parse Error]"}]

        logger.info(f"✅ Regex extracted {len(matches)} segments" + (" (from truncated JSON)" if is_truncated else ""))
        
        result = []
        for mid, mtext in matches:
            # 清理转义字符
            cleaned_text = mtext.replace('\\"', '"').replace("\\'", "'").replace('\\n', '\n')
            # 检测最后一个对象是否被截断
            if is_truncated and (mid, mtext) == matches[-1]:
                # 检查是否在句子中间截断（没有标点符号结尾）
                if cleaned_text and not cleaned_text.rstrip().endswith(('。', '！', '？', '.', '!', '?', '」', '"', ')', '）')):
                    logger.warning(f"⚠️ Segment {mid} appears truncated (no sentence-ending punctuation), marking as incomplete")
                    cleaned_text += "[...翻译被截断]"
            result.append({"id": int(mid), "translation": cleaned_text})
        
        return result

    def _regex_fallback_for_dict_like(self, text: str) -> Optional[Dict[str, str]]:
        """正则表达式兜底解析（字典格式，用于 title translation 和 glossary extraction）
        
        目标格式示例：
        {"Chapter 1": "第一章", "Introduction": "简介"}
        或
        {"术语A": "翻译A", "术语B": "翻译B"}
        """
        logger.info("🔄 Using regex fallback for dict-like JSON parsing...")

        result = {}

        # 单档键值对提取（结构化输出已保证合法 JSON，此处仅为最小安全网）
        pattern = r'"([^"]+)"\s*:\s*"([^"]*)"'
        matches = re.findall(pattern, text, re.DOTALL)

        for key, value in matches:
            # 跳过可能的元数据字段
            if key.lower() in ('id', 'type', 'status', 'error'):
                continue
            # 清理转义字符
            cleaned_key = key.replace('\\"', '"').replace("\\'", "'").replace('\\n', '\n')
            cleaned_value = value.replace('\\"', '"').replace("\\'", "'").replace('\\n', '\n')
            result[cleaned_key] = cleaned_value

        if result:
            logger.info(f"✅ Regex extracted {len(result)} key-value pairs (dict format)")
            return result
        else:
            logger.error(f"❌ Regex fallback for dict-like failed. Text length: {len(text)}")
            return None


# ========================================================================
# Gemini 异步翻译客户端
# ========================================================================
