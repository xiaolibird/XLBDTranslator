# -*- coding: utf-8 -*-
"""Gemini 异步包装（自 engine_gemini.py 再拆：线程池转发同步实现，依赖面只有
GeminiTranslator 公开方法 + 共享 helper + _TRANSLATION_RESPONSE_SCHEMA）。
外部一律经 `src.translator.engine` 门面 import，不直连本模块。
"""
import asyncio
import json
import time
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor

from google.genai import types

from ..core.schema import ContentSegment, SegmentList, contains_failed_marker
from ..core.exceptions import APIError
from .base import BaseAsyncTranslator
from .engine_common import _normalize_translation_list, _reraise_if_provider_fatal
from .engine_gemini import GeminiTranslator, _TRANSLATION_RESPONSE_SCHEMA
from ..utils.logger import get_logger

logger = get_logger(__name__)


class AsyncGeminiTranslator(BaseAsyncTranslator):
    """异步 Gemini 翻译客户端，支持并发批量翻译。
    
    继承自 BaseAsyncTranslator，实现 Gemini 的异步翻译逻辑。
    支持上下文管理器自动资源清理。
    """

    def __init__(self, base_translator: GeminiTranslator):
        """
        Args:
            base_translator: 基础的 GeminiTranslator 实例，用于复用配置和同步方法
        """
        # 调用父类构造函数
        super().__init__(base_translator)
        
        self.generation_config = base_translator.generation_config
        self.cache_refs = base_translator.cache_refs
        self.prompt_manager = base_translator.prompt_manager  # 复用 prompt_manager
        
        # 从 settings 获取线程池大小，默认 10
        max_workers = getattr(base_translator.settings.processing, 'async_max_workers', 10)
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        
        # 从 settings 获取视觉 API 信号量，默认 3
        self.vision_semaphore_limit = getattr(base_translator.settings.processing, 'vision_max_concurrent', 3)
        
        # 从 settings 获取超时配置，默认 300 秒
        self.async_timeout = getattr(base_translator.settings.processing, 'async_batch_timeout', 300)
        
        logger.debug(f"🔧 AsyncGeminiTranslator initialized: workers={max_workers}, vision_sem={self.vision_semaphore_limit}, timeout={self.async_timeout}s")
    
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
            if hasattr(self, 'executor') and self.executor is not None:
                self.cleanup()
        except Exception:
            pass
    
    async def translate_text_batch_async(
        self,
        segments: SegmentList,
        context: str,
        glossary: Optional[Dict[str, str]] = None
    ) -> List[str]:
        """
        异步批量翻译文本segment（简化版，与同步模式逻辑完全一致）
        
        架构设计（V4：与同步模式统一，一个 batch = 一次 API 调用）：
        
        ┌─────────────────────────────────────────────────────────────────┐
        │ 同步/异步模式统一流程:                                           │
        │                                                                 │
        │ 1. 整个 batch 的 segments 打包成 JSON 数组                       │
        │    [{"id": 1, "text": "..."}, {"id": 2, "text": "..."}]         │
        │                                                                 │
        │ 2. 一次 API 调用翻译整个 batch                                   │
        │                                                                 │
        │ 3. LLM 返回对应的翻译结果数组                                    │
        │    [{"id": 1, "translation": "..."}, {"id": 2, ...}]            │
        └─────────────────────────────────────────────────────────────────┘
        
        并发控制在 workflow 层通过 Semaphore 实现，engine 层只负责单次翻译。
        
        Args:
            segments: 待翻译的 segment 列表（一个 batch）
            context: 翻译上下文（batch 之前的原文，由 workflow 层提供）
            glossary: 术语表（缓存模式下会被忽略）
        
        Returns:
            翻译结果列表
        """
        if not segments:
            return []
        
        logger.info(f"🚀 异步翻译 {len(segments)} 个文本段...")
        
        # ========== 与同步模式完全一致的数据准备 ==========
        
        # 截取上下文
        safe_context = context[-self.settings.processing.max_context_length:] if context else ""
        
        # 准备输入数据：键名 "text" 对齐同步版 _translate_text_batch 与
        # config/prompts/text_translation_prompt.md 的输入契约
        # （此前用 "original"，是复制后漂移；模型能容忍但会误导改 prompt 的人）
        input_data = [
            {"id": seg.segment_id, "text": seg.original_text}
            for seg in segments
        ]
        input_json = json.dumps(input_data, ensure_ascii=False)
        
        # 正式阶段 glossary 已在 system instruction（base config 已由
        # begin_formal_translation 重建）；旁路调用才走 user message 兜底
        glossary_text = ""
        if glossary and not self.base._formal_phase:
            glossary_text = self.base.prompt_manager.format_glossary(glossary)
        
        # 格式化 Prompt（与同步模式完全一致）
        original_prompt = self.prompt_manager.format_text_prompt(
            context=safe_context,
            input_json=input_json,
            glossary=glossary_text
        )
        
        # ========== 异步执行 API 调用 ==========
        
        # 获取当前事件循环
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        
        # 结构化输出：与同步文本翻译一致，强制返回符合 schema 的 JSON 数组
        text_generation_config = {**self.generation_config, "response_schema": _TRANSLATION_RESPONSE_SCHEMA}

        def _call_with_cache():
            return self.base._generate_content(
                contents=original_prompt,
                generation_config=text_generation_config,
                use_cache=True,
                purpose="Async Text Translation"
            )
        
        # 重试逻辑（与同步模式的 @retry 装饰器效果一致）
        retry_count = max(0, int(getattr(self.settings.processing, 'max_retries', 3)) - 1)
        last_error = None
        input_ids = [s.segment_id for s in segments]
        
        for attempt in range(retry_count + 1):
            try:
                # 在线程池中执行同步的 API 调用
                response = await asyncio.wait_for(
                    loop.run_in_executor(self.executor, _call_with_cache),
                    timeout=self.async_timeout
                )
                
                raw_text = response.candidates[0].content.parts[0].text
                
                # 解析响应（复用同步方法，传递期望的 ID 列表）
                output_list = self.base._parse_json_response(
                    raw_text,
                    is_text_translation=True,
                    expected_ids=input_ids
                )
                output_list = _normalize_translation_list(output_list)

                # 映射结果（与同步模式完全一致）
                output_map = {
                    int(item['id']): str(item.get('translation', ''))
                    for item in output_list
                    if isinstance(item, dict) and 'id' in item and str(item['id']).isdigit()
                }
                
                # 生成最终结果
                results = [output_map.get(uid, "[Failed: Missing translation]") for uid in input_ids]
                
                success_count = len([r for r in results if not r.startswith('[Failed')])
                logger.info(f"✅ 异步翻译完成，成功 {success_count}/{len(segments)}")
                
                return results
            
            except asyncio.TimeoutError:
                last_error = f"Timeout after {self.async_timeout}s"
                logger.error(f"❌ 异步翻译超时（{self.async_timeout}s）")
                break  # 超时不重试
            
            except Exception as e:
                # 与视觉路径四处调用点对齐：欠费/认证/模型下线级错误必须上抛，
                # 不能在重试循环里耗尽后吞成 [Failed:] 字符串——否则上层回退链
                # 永远感知不到 provider 已不可用，文本批会被批量写死为失败。
                _reraise_if_provider_fatal(e)
                last_error = e
                if attempt < retry_count:
                    wait_time = 2 ** attempt
                    logger.warning(f"⚠️ 翻译失败（尝试 {attempt + 1}/{retry_count + 1}），{wait_time}s 后重试: {e}")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"❌ 翻译失败，已用尽所有重试: {e}")

        # 所有重试都失败，返回失败标记
        return [f"[Failed: {str(last_error)}]"] * len(segments)

    async def translate_vision_batch_async(
        self,
        segments: SegmentList,
        context: str,
        glossary: Optional[Dict[str, str]] = None
    ) -> List[str]:
        """
        异步批量翻译包含图像的segment
        
        使用信号量限制并发数，避免触发 Gemini 速率限制
        """
        if not segments:
            return []
        
        logger.info(f"🖼️ 使用异步模式翻译 {len(segments)} 个视觉段（并发限制: {self.vision_semaphore_limit}）...")
        
        # 创建信号量，限制并发视觉 API 调用数（从配置读取）
        semaphore = asyncio.Semaphore(self.vision_semaphore_limit)
        
        # 创建翻译任务
        tasks = []
        for seg in segments:
            if seg.content_type == "image" and seg.image_path:
                task = self._call_vision_api_async(
                    seg.image_path,
                    context,
                    semaphore,
                    glossary=glossary
                )
            else:
                # 文本降级处理
                task = self._translate_text_fallback_async(seg, context, glossary)
            
            tasks.append(task)
        
        # 等待所有翻译完成
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 处理异常结果
        final_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                _reraise_if_provider_fatal(result)
                logger.error(f"❌ 视觉翻译失败 (segment {segments[i].segment_id}): {result}")
                final_results.append(f"[Failed: {str(result)}]")
            else:
                final_results.append(result)
        
        logger.info(f"✅ 异步视觉翻译完成")
        return final_results

    async def _call_vision_api_async(
        self,
        img_path: str,
        context: str,
        semaphore: asyncio.Semaphore,
        retry_count: int = 2,
        glossary: Optional[Dict[str, str]] = None
    ) -> str:
        """异步调用视觉 API，使用信号量限制并发，支持重试"""

        async with semaphore:  # 限制并发数
            # 获取当前事件循环（安全方式）
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.get_event_loop()

            # 在线程池中执行 I/O 绑定的图像处理
            def _process_vision():
                # 直接调用基础翻译器的视觉API方法
                return self.base._call_vision_api(img_path, context, glossary)
            
            # 重试逻辑
            last_error: Any = None
            for attempt in range(retry_count + 1):
                try:
                    result = await loop.run_in_executor(self.executor, _process_vision)

                    # 添加延迟避免速率限制
                    await asyncio.sleep(self.settings.processing.vision_rate_limit_delay)

                    # base._call_vision_api 把瞬态失败吞成 "[Failed: ...]" 字符串正常返回：
                    # 不检查返回值的话第一次失败就被当成功 return，except 分支的重试
                    # 永不可达（死代码）。把失败标记也算一次失败才能真正用上重试预算。
                    if contains_failed_marker(result):
                        last_error = result
                        if attempt < retry_count:
                            wait_time = 2 ** attempt
                            logger.warning(f"⚠️ 视觉 API 返回失败标记（尝试 {attempt + 1}/{retry_count + 1}），{wait_time}s 后重试: {img_path}")
                            await asyncio.sleep(wait_time)
                        else:
                            logger.error(f"❌ 视觉 API 失败，已用尽所有重试: {img_path}")
                        continue

                    if attempt > 0:
                        logger.info(f"✅ 视觉 API 重试成功（第 {attempt + 1} 次尝试）: {img_path}")

                    return result

                except Exception as e:
                    # hard/soft 的 provider 级错误已由 base 层 _reraise_if_provider_fatal
                    # 上抛至此：hard 重试必然复现，soft 必须交回退链计数切换，
                    # 都不该在这里空转重试，立即传导给上层。
                    from .fallback import classify_fatal
                    if classify_fatal(e) is not None:
                        raise
                    last_error = e
                    if attempt < retry_count:
                        wait_time = 2 ** attempt
                        logger.warning(f"⚠️ 视觉 API 失败（尝试 {attempt + 1}/{retry_count + 1}），{wait_time}s 后重试: {img_path}")
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(f"❌ 视觉 API 失败，已用尽所有重试: {img_path}")

            # 失败标记字符串直接透传（保留底层错误细节），异常则统一包一层
            if isinstance(last_error, str):
                return last_error
            return f"[Failed: {str(last_error)}]"

    async def _translate_text_fallback_async(
        self,
        segment: ContentSegment,
        context: str,
        glossary: Optional[Dict[str, str]]
    ) -> str:
        """异步文本降级处理"""
        # 获取当前事件循环（安全方式）
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        
        def _sync_fallback():
            results = self.base._translate_text_batch(
                [segment],
                context,
                glossary
            )
            return results[0] if results else "[Fallback Failed]"
        
        return await loop.run_in_executor(self.executor, _sync_fallback)

    def cleanup(self):
        """清理资源"""
        if hasattr(self, 'executor') and self.executor is not None:
            try:
                self.executor.shutdown(wait=True)
                self.executor = None  # 标记为已清理
                logger.info("🧹 异步翻译器已清理资源")
            except Exception as e:
                logger.debug(f"清理 executor 时出现警告: {e}")


# ========================================================================
# OpenAI-compatible (DeepSeek) 翻译客户端
# ========================================================================

