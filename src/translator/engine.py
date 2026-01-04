"""
Gemini 翻译客户端
使用 tenacity 进行重试管理
"""
import asyncio
import base64
import json
import time
import re
import mimetypes
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor
from urllib import request, error

from google import genai
from google.genai import types
from google.genai import errors as genai_errors
from google.api_core.exceptions import GoogleAPICallError, ResourceExhausted, ServiceUnavailable, ClientError, DeadlineExceeded
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from ..core.schema import Settings, ContentSegment, TranslationMap, SegmentList
from ..core.exceptions import (
    APIError, APIRateLimitError, APITimeoutError, APIAuthenticationError,
    JSONParseError, TranslationError
)
from .base import BaseTranslator, BaseAsyncTranslator
from .support import CachePersistenceManager, PromptManager
from ..utils.logger import get_logger

logger = get_logger(__name__)


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
        
        # 创建System Instruction缓存（委托给缓存管理器）
        if settings.processing.enable_gemini_caching and self.cache_persistence:
            # 使用translation_mode_entity的name作为display_name（可选）
            mode_name = getattr(settings.processing.translation_mode_entity, 'name', None)
            cache_name = self.cache_persistence.get_or_create_system_cache(
                system_instruction=self.prompt_manager.get_system_instruction(use_vision=settings.processing.use_vision_mode),
                model_name=settings.api.gemini_model,
                display_name=f"sys_{mode_name}" if mode_name else None
            )
            if cache_name:
                self.cache_refs['system'] = cache_name
                logger.info(f"✅ System Instruction 缓存已就绪: {cache_name[:50]}...")
    
    @property
    def async_translator(self):
        """懒加载异步翻译器"""
        if self._async_translator is None:
            self._async_translator = AsyncGeminiTranslator(self)
        return self._async_translator
    

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

        self.generation_config = {
            "temperature": 0.2,  # 降低温度以获得更稳定的输出
            "top_p": 0.95,
            "response_mime_type": "application/json",
            "max_output_tokens": 8192,
        }

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
            return response
        except Exception as e:
            # 缓存失败时降级
            if cache_name:
                logger.warning(f"⚠️  {purpose} 缓存使用失败，降级为普通调用: {e}")
                config_no_cache = config.model_copy(update={
                    "cached_content": None,
                    "system_instruction": self._base_generation_config.system_instruction,
                })
                return self._client.models.generate_content(
                    model=self.settings.api.gemini_model,
                    contents=contents,
                    config=config_no_cache,
                )
            raise
    


    def _attempt_json_self_correction(
        self,
        original_prompt: str,
        failed_json: str,
        error_message: str,
        retries_left: int,
        is_vision: bool = False,
        image_part: Optional[Any] = None
    ) -> Optional[Any]:
        """
        尝试引导模型自我修正 JSON 输出。
        """
        if retries_left <= 0:
            logger.warning("❌ JSON 自我修正重试次数已用尽。")
            return None

        logger.info(f"🔄 尝试 JSON 自我修正 (剩余 {retries_left} 次)...")
        repair_prompt_content = self.prompt_manager.format_json_repair_prompt(
            original_prompt=original_prompt,
            broken_json=failed_json,
            error_details=error_message
        )

        try:
            # 使用与原始请求相同的配置
            gen_cfg = {
                "temperature": self.generation_config["temperature"],
                "top_p": self.generation_config["top_p"],
                "max_output_tokens": self.generation_config["max_output_tokens"],
                "response_mime_type": "application/json",
            }

            content_parts = [repair_prompt_content]
            if is_vision and image_part is not None:
                content_parts.append(image_part)

            response = self.model.generate_content(
                content_parts,
                generation_config=gen_cfg
            )
            # raw_text = response.text.strip()
            raw_text = response.candidates[0].content.parts[0].text
            # 尝试解析修正后的 JSON
            parsed_data = json.loads(raw_text)
            logger.info("✅ JSON 自我修正成功。")
            return parsed_data
        except (json.JSONDecodeError, GoogleAPICallError, genai_errors.APIError, Exception) as e:
            logger.warning(f"⚠️ JSON 自我修正失败 (第 {self.settings.processing.json_repair_retries - retries_left + 1} 次): {e}")
            # 递归重试
            return self._attempt_json_self_correction(
                original_prompt=original_prompt,
                failed_json=raw_text if 'raw_text' in locals() else failed_json, # 传入最新的失败JSON
                error_message=str(e),
                retries_left=retries_left - 1,
                is_vision=is_vision,
                image_part=image_part
            )

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

    def translate_titles(self, titles: List[str]) -> TranslationMap:
        """翻译标题列表"""
        if not titles:
            return {}

        input_json_str = json.dumps(titles, ensure_ascii=False)
        original_prompt = self.prompt_manager.format_title_prompt(input_json_str)

        try:
            response = self._generate_content(
                contents=original_prompt,
                generation_config=self.generation_config,
                use_cache=True,
                purpose="Title Translation"
            )
            raw_text = response.candidates[0].content.parts[0].text
            # 解析响应，并处理自我修正
            parsed_data = self._handle_json_response_with_correction(
                raw_text,
                original_prompt,
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

        except Exception as e:
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

        content_sample = "\n".join(text_to_analyze)
        
        # 构建 Prompt
        original_prompt = f"""
        You are an expert linguist and terminologist.
        Analyze the following pairs of original and translated text. Identify all key, recurring, or specialized terms (like names, places, philosophical concepts, technical jargon) and create a definitive glossary.

        RULES:
        1. The output MUST be a flat JSON object.
        2. Keys are the original English terms.
        3. Values are their corresponding Chinese translations found in the text.
        4. Focus on nouns and proper nouns.
        5. Be precise. The goal is to enforce consistency.

        Example Output Format:
        {{
            "Slavoj Žižek": "斯拉沃热·齐泽克",
            "the Real": "实在界",
            "Objet petit a": "客体小 a"
        }}

        Text to Analyze:
        <text>
        {content_sample[:8000]}
        </text>

        Return ONLY the JSON object.
        """

        try:
            if self._client is None:
                raise APIAuthenticationError("Gemini client is not configured")

            extraction_config = types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.2,
                max_output_tokens=8192,
            )

            response = self._client.models.generate_content(
                model=self.settings.api.gemini_model,
                contents=original_prompt,
                config=extraction_config,
            )
            # raw_text = response.text.strip()
            raw_text = response.candidates[0].content.parts[0].text
            # 处理自我修正
            parsed_glossary = self._handle_json_response_with_correction(
                raw_text, 
                original_prompt, 
                is_glossary_extraction=True
            )
            
            # 归一化不同可能的模型输出格式为平坦的 {str: str} 形式
            final_glossary: Dict[str, str] = {}

            if isinstance(parsed_glossary, dict):
                for k, v in parsed_glossary.items():
                    try:
                        if k and v:
                            final_glossary[str(k).strip()] = str(v).strip()
                    except TypeError:
                        # 跳过不可哈希或非标量的键
                        continue

            elif isinstance(parsed_glossary, list):
                for item in parsed_glossary:
                    if isinstance(item, dict):
                        for k, v in item.items():
                            if k and v:
                                final_glossary[str(k).strip()] = str(v).strip()
                    elif isinstance(item, (list, tuple)) and len(item) == 2:
                        k, v = item
                        if k and v:
                            final_glossary[str(k).strip()] = str(v).strip()

            # 日志和返回
            if final_glossary:
                logger.info(f"   - ✅ 成功提取 {len(final_glossary)} 个术语。")
                for k, v in list(final_glossary.items())[:5]:
                    logger.info(f"     - '{k}' -> '{v}'")
                if len(final_glossary) > 5:
                    logger.info("     - ... (更多术语)")
                return final_glossary

            logger.warning(f"   - ⚠️ 术语提取未能产生有效字典。原始响应类型: {type(parsed_glossary)}. 原始响应片段: {raw_text[:200]}")
            return {}
        except Exception as e:
            logger.error(f"   - ❌ 提取术语表时发生错误: {e}")
            return {}

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        retry=retry_if_exception_type((APIError, GoogleAPICallError)),
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
        safe_context = context[-self.settings.processing.max_context_length:] if context else "No Context"

        # 格式化术语表
        if glossary:
            glossary_text = "\n".join([f"- **{k}**: Must be translated as **{v}**" for k, v in glossary.items()])
        else:
            glossary_text = "N/A"

        # 格式化提示（固定部分已在system instruction，仅填充动态变量）
        original_prompt = self.prompt_manager.format_text_prompt(
            context=safe_context,
            input_json=input_json,
            glossary=glossary_text
        )

        response = self._generate_content(
            contents=original_prompt,
            generation_config=self.generation_config,
            use_cache=True,
            purpose="Text Translation"
        )
        
        raw_text = response.candidates[0].content.parts[0].text
        # 解析响应，并处理自我修正
        output_list = self._handle_json_response_with_correction(
            raw_text, 
            original_prompt, 
            is_text_translation=True
        )

        # 映射结果
        input_ids = [s.segment_id for s in segments]
        output_map = {
            int(item['id']): str(item.get('translation', ''))
            for item in output_list
            if 'id' in item and str(item['id']).isdigit()
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
                        current_context
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
                logger.error(f"❌ Vision翻译失败 (segment {seg.segment_id}): {e}")
                results.append(f"[Failed: {str(e)}]")
                continue

        return results

    def _call_vision_api(self, img_path: str, context: str) -> str:
        """调用视觉 API（支持 Gemini Caching）"""
        try:
            # 使用 prompt_manager 格式化提示
            original_prompt = self.prompt_manager.format_vision_prompt(context)

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

            # 解析 JSON 并提取 "translation" 字段，处理自我修正
            parsed_json = self._handle_json_response_with_correction(
                raw_text,
                original_prompt,
                is_vision_translation=True,
                image_part=image_part,
            )

            if isinstance(parsed_json, dict) and "translation" in parsed_json:
                return parsed_json["translation"]

            logger.error(
                "❌ Vision API did not return valid JSON with a 'translation' key even after correction. "
                f"Got: {raw_text[:200]}"
            )
            return "[Failed: Invalid JSON Response]"

        except Exception as e:
            logger.error(f"❌ Vision API调用失败 for {img_path}: {e}")
            return f"[Failed: {str(e)}]"

    def _handle_json_response_with_correction(
        self,
        raw_text: str,
        original_prompt: str,
        is_title_translation: bool = False,
        is_glossary_extraction: bool = False,
        is_text_translation: bool = False,
        is_vision_translation: bool = False,
        image_part: Optional[Any] = None
    ) -> Any:
        """
        处理 JSON 响应，优化解析顺序：
        1. 先尝试标准JSON解析
        2. 尝试去除代码块后解析
        3. 尝试正则表达式兜底解析
        4. 只有以上全部失败后，才请求模型自我修正
        """
        final_parsed_data = None
        current_raw_text = raw_text

        for attempt in range(self.settings.processing.json_repair_retries + 1):
            # ========== 阶段1：标准JSON解析 ==========
            try:
                final_parsed_data = self._repair_json_content(current_raw_text)
                logger.debug("✅ 标准JSON解析成功")
                return final_parsed_data
            except JSONParseError as e:
                logger.debug(f"⚠️ 标准JSON解析失败: {e}")
            
            # ========== 阶段2：正则表达式兜底解析 ==========
            try:
                if is_text_translation:
                    fallback_result = self._regex_fallback(current_raw_text)
                    if fallback_result and len(fallback_result) > 0:
                        # 检查是否是真正的翻译失败标签
                        if fallback_result[0].get("translation") != "[Translation Failed - JSON Parse Error]":
                            logger.info(f"✅ 正则表达式解析成功，提取 {len(fallback_result)} 条翻译")
                            return fallback_result
                elif is_title_translation or is_glossary_extraction:
                    fallback_result = self._regex_fallback_for_dict_like(current_raw_text)
                    if fallback_result:
                        logger.info(f"✅ 正则表达式解析成功（字典格式）")
                        return fallback_result
            except Exception as e:
                logger.debug(f"⚠️ 正则表达式解析失败: {e}")
            
            # ========== 阶段3：模型自我修正（仅当前两阶段都失败时） ==========
            if attempt < self.settings.processing.json_repair_retries:
                logger.warning(f"⚠️ JSON 解析失败 (尝试 {attempt+1}/{self.settings.processing.json_repair_retries + 1}): [MEDIUM] 标准解析和正则解析均失败")
                logger.info(f"💡 建议: 尝试请求模型自我修正 (剩余 {self.settings.processing.json_repair_retries - attempt} 次)")
                logger.info("🔄 尝试请求模型自我修正...")
                
                corrected_data = self._attempt_json_self_correction(
                    original_prompt=original_prompt,
                    failed_json=current_raw_text,
                    error_message="JSON解析和正则解析均失败",
                    retries_left=self.settings.processing.json_repair_retries - attempt,
                    is_vision=is_vision_translation,
                    image_part=image_part
                )
                
                if corrected_data:
                    logger.info("✅ 模型自我修正成功")
                    final_parsed_data = corrected_data
                    return final_parsed_data
                else:
                    logger.warning("❌ 模型自我修正失败")
                    # 重置为原始文本，继续下一轮尝试
                    current_raw_text = raw_text
            else:
                logger.warning("❌ 达到最大重试次数，所有解析方法均失败。")
                break

        # ========== 最终兜底：返回错误标记 ==========
        logger.error(f"❌ 所有解析方法失败（标准JSON + 正则 + 模型修正）")
        
        if is_text_translation:
            logger.warning("⚠️ 返回翻译失败标签以保证输出文件完整性")
            return [{"id": 1, "translation": "[Translation Failed - All Parsing Methods Exhausted]"}]
        elif is_title_translation or is_glossary_extraction:
            return {}
        elif is_vision_translation:
            return {}
        
        return final_parsed_data

    def _parse_json_response(self, text: str) -> List[Dict[str, Any]]:
        """解析文本翻译的 JSON 响应，支持多种格式"""
        try:
            # 调用新的处理函数，并明确这是文本翻译场景
            # 注意: 这里 original_prompt 传递空字符串，因为 _parse_json_response 之前没有直接的 prompt 信息。
            # 只有当原始 API 调用失败时，_handle_json_response_with_correction 才会使用到 original_prompt 进行自我修正。
            # 如果是 _parse_json_response 独立调用（例如从缓存读取），那么 original_prompt 确实是未知的。
            result = self._handle_json_response_with_correction(text, "", is_text_translation=True)
            if isinstance(result, list):
                return result
            elif isinstance(result, dict) and 'translations' in result:
                return result['translations']
            else:
                return []
        except Exception as e:
            logger.error(f"❌ 最终JSON解析失败，包括修正和正则回退: {e}")
            return []

    def _repair_json_content(self, text: str) -> Any:
        """修复 JSON 字符串 (只进行代码块去除，不进行高级字符串修复)"""
        # 去除 Markdown 代码块
        pattern = r'^```(?:json)?\s*(.*)\s*```$'
        match = re.search(pattern, text, re.DOTALL)
        if match:
            text = match.group(1)

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            # 不再进行内部高级修复，直接抛出，由上层处理自我修正
            raise JSONParseError(f"Initial JSON parse failed: {e}")

    def _regex_fallback(self, text: str) -> List[Dict[str, Any]]:
        """正则表达式兜底解析"""
        logger.info("🔄 Using regex fallback for JSON parsing...")

        # 策略1: 标准JSON格式
        pattern = r'"id":\s*(\d+),\s*"translation":\s*"(.*?)(?<!\\)"(?=\s*\}|\s*,)'
        matches = re.findall(pattern, text, re.DOTALL)

        if not matches:
            # 策略2: 单引号格式
            pattern_sq = r"'id':\s*(\d+),\s*'translation':\s*'(.*?)'(?=\s*\}|\s*,)"
            matches = re.findall(pattern_sq, text, re.DOTALL)

        if not matches:
            # 策略3: 更宽松的匹配（处理不完整的JSON）
            pattern_loose = r'"id":\s*(\d+).*?"translation":\s*"(.*?)"'
            matches = re.findall(pattern_loose, text, re.DOTALL)

        if not matches:
            # 策略4: 极度宽松的匹配
            pattern_ultra = r'id["\s:]+(\d+).*?translation["\s:]+["\']([^"\']*?)["\']'
            matches = re.findall(pattern_ultra, text, re.DOTALL | re.IGNORECASE)

        if not matches:
            logger.error(f"❌ Regex fallback failed completely. Original text: {repr(text[:500])}")
            # 返回翻译失败的标签，确保至少能生成完整的输出文件
            logger.warning("⚠️ Returning translation failure tag to ensure output file integrity.")
            return [{"id": 1, "translation": "[Translation Failed - JSON Parse Error]"}]

        logger.debug(f"✅ Regex found {len(matches)} matches.")
        return [{"id": int(mid), "translation": mtext.replace('\\"', '"').replace("\\'", "'")} for mid, mtext in matches]


# ========================================================================
# 异步翻译客户端
# ========================================================================

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
        异步批量翻译文本segment
        
        将segments分成更小的chunk并发翻译，大幅提升速度
        特性：超时控制、自动重试、异常隔离
        """
        if not segments:
            return []
        
        logger.info(f"🚀 使用异步模式翻译 {len(segments)} 个文本段（超时: {self.async_timeout}s）...")
        
        # 将 segments 分成小批次 (避免单个请求过大)
        chunk_size = min(5, self.settings.processing.batch_size)
        chunks = [segments[i:i + chunk_size] for i in range(0, len(segments), chunk_size)]
        
        # 并发翻译所有 chunk，带超时控制
        tasks = [
            self._translate_single_chunk_async(chunk, context, glossary, retry_count=2)
            for chunk in chunks
        ]
        
        try:
            # 添加总体超时控制
            chunk_results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=self.async_timeout
            )
        except asyncio.TimeoutError:
            logger.error(f"❌ 异步翻译超时（{self.async_timeout}s），取消所有任务")
            # 超时时返回失败标记
            return [f"[Failed: Timeout after {self.async_timeout}s]"] * len(segments)
        
        # 合并结果
        results = []
        for chunk, chunk_result in zip(chunks, chunk_results):
            if isinstance(chunk_result, Exception):
                logger.error(f"❌ Chunk 翻译失败: {chunk_result}")
                # 为失败的chunk填充失败标记，使用实际的chunk大小
                actual_chunk_size = len(chunk)
                results.extend([f"[Failed: {str(chunk_result)}]"] * actual_chunk_size)
            else:
                results.extend(chunk_result)
        
        success_count = len([r for r in results if not r.startswith('[Failed')])
        logger.info(f"✅ 异步翻译完成，成功 {success_count} / {len(segments)}")
        return results[:len(segments)]  # 确保返回正确数量的结果

    async def _translate_single_chunk_async(
        self,
        segments: SegmentList,
        context: str,
        glossary: Optional[Dict[str, str]] = None,
        retry_count: int = 0
    ) -> List[str]:
        """异步翻译单个 chunk，支持重试"""
        
        # 准备输入数据
        input_data = [
            {
                "id": seg.segment_id,
                "original": seg.original_text,
                "context": (context or "No Context")[-self.settings.processing.max_context_length:]
            }
            for seg in segments
        ]
        input_json = json.dumps(input_data, ensure_ascii=False)
        
        # 格式化术语表
        glossary_text = "N/A"
        if glossary:
            glossary_text = "\n".join([f"- **{k}**: Must be translated as **{v}**" for k, v in glossary.items()])
        
        # 截取上下文
        safe_context = context[-self.settings.processing.max_context_length:] if context else "No Context"
        
        # 格式化 Prompt - 使用 prompt_manager
        original_prompt = self.prompt_manager.format_text_prompt(
            context=safe_context,
            input_json=input_json,
            glossary=glossary_text
        )
        
        # 获取当前事件循环（安全方式）
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 如果没有运行中的事件循环，回退到 get_event_loop
            loop = asyncio.get_event_loop()
        
        def _call_with_cache():
            return self.base._generate_content(
                contents=original_prompt,
                generation_config=self.generation_config,
                use_cache=True,
                purpose="Async Text Translation"
            )
        
        # 重试逻辑
        last_error = None
        for attempt in range(retry_count + 1):
            try:
                # 在线程池中执行同步的 API 调用（使用缓存如果可用）
                response = await loop.run_in_executor(self.executor, _call_with_cache)
                
                # raw_text = response.text.strip()
                raw_text = response.candidates[0].content.parts[0].text
                # 解析响应（复用同步方法）
                output_list = self.base._handle_json_response_with_correction(
                    raw_text,
                    original_prompt,
                    is_text_translation=True
                )
                
                # 映射结果
                input_ids = [s.segment_id for s in segments]
                output_map = {
                    int(item['id']): str(item.get('translation', ''))
                    for item in output_list
                    if 'id' in item and str(item['id']).isdigit()
                }
                
                # 生成最终结果
                results = [output_map.get(uid, "[Translation Failed]") for uid in input_ids]
                
                if attempt > 0:
                    logger.info(f"✅ Chunk 翻译重试成功（第 {attempt + 1} 次尝试）")
                
                return results
            
            except Exception as e:
                last_error = e
                if attempt < retry_count:
                    wait_time = 2 ** attempt  # 指数退避
                    logger.warning(f"⚠️ Chunk 翻译失败（尝试 {attempt + 1}/{retry_count + 1}），{wait_time}s 后重试: {e}")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"❌ Chunk 翻译失败，已用尽所有重试: {e}")
        
        # 所有重试都失败，返回失败标记
        input_ids = [s.segment_id for s in segments]
        return [f"[Failed: {str(last_error)}]"] * len(input_ids)

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
                    semaphore
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
        retry_count: int = 2
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
                return self.base._call_vision_api(img_path, context)
            
            # 重试逻辑
            last_error = None
            for attempt in range(retry_count + 1):
                try:
                    result = await loop.run_in_executor(self.executor, _process_vision)
                    
                    # 添加延迟避免速率限制
                    await asyncio.sleep(self.settings.processing.vision_rate_limit_delay)
                    
                    if attempt > 0:
                        logger.info(f"✅ 视觉 API 重试成功（第 {attempt + 1} 次尝试）: {img_path}")
                    
                    return result
                
                except Exception as e:
                    last_error = e
                    if attempt < retry_count:
                        wait_time = 2 ** attempt
                        logger.warning(f"⚠️ 视觉 API 失败（尝试 {attempt + 1}/{retry_count + 1}），{wait_time}s 后重试: {img_path}")
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(f"❌ 视觉 API 失败，已用尽所有重试: {img_path}")
            
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
        self.executor.shutdown(wait=True)
        logger.info("🧹 异步翻译器已清理资源")


# ========================================================================
# OpenAI-compatible (DeepSeek) translator
# ========================================================================


class OpenAICompatibleTranslator(BaseTranslator):
    """OpenAI-compatible 翻译客户端（DeepSeek API 兼容 OpenAI 格式）。

    DeepSeek docs:
      - base_url: https://api.deepseek.com (or https://api.deepseek.com/v1)
      - endpoint: POST /chat/completions

    Env vars are provided via Settings.api:
      - API_OPENAI_API_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY
      - OPENAI_BASE_URL / DEEPSEEK_BASE_URL
      - OPENAI_MODEL / DEEPSEEK_MODEL
    """

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.prompt_manager = PromptManager(settings)
        self._async_translator: Optional[AsyncOpenAICompatibleTranslator] = None

        self.api_key: Optional[str] = settings.api.openai_api_key
        self.base_url: str = settings.api.openai_base_url
        self.model: str = settings.api.openai_model
        
        # 验证和修复 base_url 配置
        self.base_url = self._validate_and_fix_base_url(self.base_url)
        
        # 自动检测是否为本地服务（Ollama）或 DeepSeek API
        # 适配 M2 Pro 16GB 硬件环境，本地模式需要特殊处理
        self.is_local: bool = self._detect_local_service(self.base_url)
        self.is_deepseek: bool = self._detect_deepseek_api(self.base_url)
        
        # DeepSeek 长文本模式：自动切换为单 message 模式（system + instruction + mode + context + prompt）
        # 原因：DeepSeek 对长上下文支持更好，且 system message 可能影响性能
        self.use_long_text_mode: bool = self.is_deepseek
        
        if self.is_local:
            logger.info("🏠 检测到本地模式（Ollama）")
            logger.info(f"   - 服务地址: {self.base_url}")
            logger.info(f"   - 模型: {self.model}")
            logger.info("   - 已启用 M2 Pro 优化：num_ctx=8192, num_thread=10")
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
                "OpenAI-compatible API key is missing. Set API_OPENAI_API_KEY (or OPENAI_API_KEY/DEEPSEEK_API_KEY).",
                context={"setting": "API_OPENAI_API_KEY"},
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

    def translate_batch(
        self,
        segments: SegmentList,
        context: str = "",
        glossary: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        if not segments:
            return []

        has_image = any(seg.content_type == "image" for seg in segments)
        if has_image:
            return self._translate_vision_batch(segments, context)
        return self._translate_text_batch(segments, context, glossary)

    def translate_titles(self, titles: List[str]) -> TranslationMap:
        if not titles:
            return {}

        input_json_str = json.dumps(titles, ensure_ascii=False)
        original_prompt = self.prompt_manager.format_title_prompt(input_json_str)

        raw_text = self._chat_completions(
            system_instruction=self.prompt_manager.get_system_instruction(use_vision=False),
            user_content=original_prompt,
        )

        parsed_data = self._handle_json_response_with_repair(
            raw_text=raw_text,
            original_prompt=original_prompt,
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

        content_sample = "\n".join(text_to_analyze)
        original_prompt = f"""
You are an expert linguist and terminologist.
Analyze the following pairs of original and translated text. Identify all key, recurring, or specialized terms (like names, places, philosophical concepts, technical jargon) and create a definitive glossary.

RULES:
1. The output MUST be a flat JSON object.
2. Keys are the original English terms.
3. Values are their corresponding Chinese translations found in the text.
4. Focus on nouns and proper nouns.
5. Be precise. The goal is to enforce consistency.

Text to Analyze:
<text>
{content_sample[:8000]}
</text>

Return ONLY the JSON object.
""".strip()

        raw_text = self._chat_completions(
            system_instruction="You output JSON only.",
            user_content=original_prompt,
        )

        parsed = self._handle_json_response_with_repair(
            raw_text=raw_text,
            original_prompt=original_prompt,
            is_dict_like=True,
        )

        final_glossary: Dict[str, str] = {}
        if isinstance(parsed, dict):
            for k, v in parsed.items():
                if k and v:
                    final_glossary[str(k).strip()] = str(v).strip()
        return final_glossary

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        retry=retry_if_exception_type((APIError,)),
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

        safe_context = context[-self.settings.processing.max_context_length:] if context else "No Context"

        glossary_text = "N/A"
        if glossary:
            glossary_text = "\n".join(
                [f"- **{k}**: Must be translated as **{v}**" for k, v in glossary.items()]
            )

        # DeepSeek 长文本模式：将 system instruction 嵌入到 user content 中
        if self.use_long_text_mode:
            system_instruction = self.prompt_manager.get_system_instruction(use_vision=False)
            user_prompt = self.prompt_manager.format_text_prompt(
                context=safe_context,
                input_json=input_json,
                glossary=glossary_text,
            )
            # 将 system instruction 和 user prompt 合并为完整的长文本 prompt
            combined_prompt = f"{system_instruction}\n\n{'='*80}\n\n{user_prompt}"
            
            raw_text = self._chat_completions(
                system_instruction="",  # 长文本模式下 system_instruction 为空
                user_content=combined_prompt,
            )
        else:
            # 标准模式：system 和 user 分离
            original_prompt = self.prompt_manager.format_text_prompt(
                context=safe_context,
                input_json=input_json,
                glossary=glossary_text,
            )

            raw_text = self._chat_completions(
                system_instruction=self.prompt_manager.get_system_instruction(use_vision=False),
                user_content=original_prompt,
            )

        output_list = self._handle_json_response_with_repair(
            raw_text=raw_text,
            original_prompt=combined_prompt if self.use_long_text_mode else original_prompt,
            is_text_translation=True,
        )

        input_ids = [s.segment_id for s in segments]
        output_map = {
            int(item['id']): str(item.get('translation', ''))
            for item in output_list
            if isinstance(item, dict) and 'id' in item and str(item['id']).isdigit()
        }
        return [output_map.get(uid, "[Failed: Missing translation]") for uid in input_ids]

    def _translate_vision_batch(self, segments: SegmentList, context: str) -> List[str]:
        results: List[str] = []
        current_context = context[-self.settings.processing.max_context_length:] if context else ""

        for seg in segments:
            if seg.content_type == "image" and seg.image_path:
                results.append(self._call_vision_api(seg.image_path, current_context))
            else:
                fallback = self._translate_text_batch([seg], current_context, glossary=None)
                results.append(fallback[0] if fallback else "[Fallback Failed]")

            current_context += f"\n{results[-1]}"
            if len(current_context) > self.settings.processing.max_context_length:
                current_context = current_context[-self.settings.processing.max_context_length:]

        return results

    def _call_vision_api(self, img_path: str, context: str) -> str:
        original_prompt = self.prompt_manager.format_vision_prompt(context)

        try:
            with open(img_path, 'rb') as f:
                b64 = base64.b64encode(f.read()).decode('ascii')
            image_url = f"data:image/png;base64,{b64}"

            raw_text = self._chat_completions(
                system_instruction=self.prompt_manager.get_system_instruction(use_vision=True),
                user_content=[
                    {"type": "text", "text": original_prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            )

            parsed = self._handle_json_response_with_repair(
                raw_text=raw_text,
                original_prompt=original_prompt,
                is_dict_like=True,
            )
            if isinstance(parsed, dict) and 'translation' in parsed:
                return str(parsed['translation'])
            return "[Failed: Invalid JSON Response]"
        except Exception as e:
            logger.error(f"❌ OpenAI-compatible Vision API 调用失败 for {img_path}: {e}")
            return f"[Failed: {str(e)}]"

    def _build_chat_completions_url(self) -> str:
        """构建 Chat Completions API URL
        
        针对本地 Ollama 服务的路径修复逻辑：
        - 本地模式：强制使用 http://127.0.0.1:11434/v1/chat/completions
        - 云端模式（DeepSeek）：保持原逻辑
        """
        base = (self.base_url or '').rstrip('/')
        
        # 本地模式：强制使用 127.0.0.1:11434/v1/chat/completions（Ollama 标准接口）
        if self.is_local:
            # 统一使用 127.0.0.1 而非 localhost，避免 DNS 解析问题
            return 'http://127.0.0.1:11434/v1/chat/completions'
        
        # 云端模式：确保 base_url 是完整的 URL
        if not base.startswith(('http://', 'https://')):
            # 如果不是完整的 URL，尝试修复常见的错误配置
            if 'deepseek' in base.lower():
                # DeepSeek 常见错误配置修复
                logger.warning(f"⚠️ 检测到不完整的 DeepSeek URL 配置: '{base}'，自动修复为标准 URL")
                return 'https://api.deepseek.com/v1/chat/completions'
            else:
                # 其他服务，尝试添加 https:// 前缀
                logger.warning(f"⚠️ 检测到不完整的 URL 配置: '{base}'，尝试添加 https:// 前缀")
                base = f'https://{base}'
        
        # 云端模式：DeepSeek 支持两种格式
        # https://api.deepseek.com/chat/completions 或
        # https://api.deepseek.com/v1/chat/completions
        if base.endswith('/v1'):
            return base + '/chat/completions'
        return base + '/chat/completions'

    def _chat_completions(self, system_instruction: str, user_content: Any) -> str:
        """调用 Chat Completions API
        
        本地模式优化（M2 Pro 16GB）：
        - 强制 120s 超时（本地推理较慢）
        - 注入 options 字段：num_ctx=8192（长文本记忆），num_thread=10（适配 M2 Pro 核心数）
        
        DeepSeek 长文本模式：
        - 所有内容已预先合并到 user_content 中，system_instruction 为空
        - 格式：完整的长文本 prompt 包含 system instruction + mode + context + input
        - 原因：DeepSeek 对长上下文支持更好，且避免 system message 限制
        """
        # DeepSeek 长文本模式：所有内容已预先合并，无需额外处理
        if self.use_long_text_mode:
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "user", "content": user_content},
                ],
                "temperature": 0.2,
                "stream": False,
            }
            logger.debug("📝 使用长文本模式（所有内容预先合并）")
        else:
            # 标准模式：system + user 分离
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": user_content},
                ],
                "temperature": 0.2,
                "stream": False,
            }
        
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
            logger.debug("   📊 长文本模式: System Instruction + 分隔符 + Mode + Glossary + Context + Input JSON")
        else:
            logger.debug("   📊 标准模式: System Instruction + User Content")
        
        # 本地模式：注入 Ollama 专用参数，针对 M2 Pro 16GB 优化
        if self.is_local:
            payload["options"] = {
                "num_ctx": 1024,      # 进一步降低：从 2048 到 1024（适应超长上下文）
                "num_thread": 1,      # 进一步降低：从 2 到 1（单线程，极低内存压力）
            }
            logger.debug("🔧 本地模式 payload 已注入 options: num_ctx=1024, num_thread=1")

        url = self._build_chat_completions_url()
        data = json.dumps(payload).encode('utf-8')
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.api_key}',
        }
        
        # 动态超时：本地模式强制 120s，云端模式根据服务调整
        if self.is_deepseek:
            timeout = 120  # DeepSeek响应较慢，增加到120秒
            logger.debug(f"⏱️  DeepSeek模式超时设置: {timeout}s")
        elif self.is_local:
            timeout = 120 if self.is_local else self.settings.processing.request_timeout
            if self.is_local:
                logger.debug(f"⏱️  本地模式超时设置: {timeout}s")
        else:
            timeout = self.settings.processing.request_timeout

        req = request.Request(url, data=data, headers=headers, method='POST')
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                resp_text = resp.read().decode('utf-8')
        except error.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace') if hasattr(e, 'read') else ''
            raise APIError(f"OpenAI-compatible HTTPError: {e.code} {e.reason} {body[:200]}")
        except error.URLError as e:
            raise APITimeoutError(f"OpenAI-compatible request failed: {e}")
        except TimeoutError as e:
            raise APITimeoutError(f"OpenAI-compatible request timeout: {e}")

        try:
            parsed = json.loads(resp_text)
            content = parsed['choices'][0]['message']['content']
            if not isinstance(content, str):
                return json.dumps(content, ensure_ascii=False)
            return content.strip()
        except Exception as e:
            raise APIError(f"OpenAI-compatible response parse failed: {e}")

    def _strip_code_fences(self, text: str) -> str:
        pattern = r'^```(?:json)?\s*(.*)\s*```$'
        match = re.search(pattern, text, re.DOTALL)
        return match.group(1) if match else text

    def _handle_json_response_with_repair(
        self,
        raw_text: str,
        original_prompt: str,
        *,
        is_text_translation: bool = False,
        is_dict_like: bool = False,
    ) -> Any:
        current = raw_text

        for _ in range(self.settings.processing.json_repair_retries + 1):
            try:
                current = self._strip_code_fences(current)
                return json.loads(current)
            except json.JSONDecodeError as e:
                last_err = str(e)

            if is_text_translation:
                try:
                    fallback = self._regex_fallback_for_list(current)
                    if fallback:
                        return fallback
                except Exception:
                    pass

            repair_prompt = self.prompt_manager.format_json_repair_prompt(
                original_prompt=original_prompt,
                broken_json=current,
                error_details=last_err,
            )
            current = self._chat_completions(
                system_instruction=self.prompt_manager.get_system_instruction(use_vision=False),
                user_content=repair_prompt,
            )

        if is_text_translation:
            return [{"id": 1, "translation": "[Translation Failed - All Parsing Methods Exhausted]"}]
        if is_dict_like:
            return {}
        raise JSONParseError("Failed to parse JSON after repair attempts")

    def _regex_fallback_for_list(self, text: str) -> List[Dict[str, Any]]:
        pattern = r'"id":\s*(\d+),\s*"translation":\s*"(.*?)(?<!\\)"(?=\s*\}|\s*,)'
        matches = re.findall(pattern, text, re.DOTALL)
        return [
            {"id": int(mid), "translation": mtext.replace('\\"', '"').replace("\\'", "'")}
            for mid, mtext in matches
        ]


class AsyncOpenAICompatibleTranslator(BaseAsyncTranslator):
    """异步 OpenAI-compatible 翻译器（线程池包装，同步HTTP请求并发执行）。
    
    并发控制策略（M2 Pro 16GB 优化）：
    - 本地模式（Ollama）：max_workers=2，防止 16GB 统一内存溢出
    - 云端模式（DeepSeek）：max_workers=10，充分利用网络并发
    支持上下文管理器自动资源清理。
    """

    def __init__(self, base_translator: OpenAICompatibleTranslator):
        super().__init__(base_translator)
        
        # 动态并发控制：根据服务类型调整并发数
        if base_translator.is_local:
            # 本地模式：2 并发（M2 Pro 16GB 限制，避免显存溢出）
            max_workers = 2
        elif base_translator.is_deepseek:
            # DeepSeek：3 并发（避免触发速率限制，DeepSeek响应较慢）
            max_workers = 3
        else:
            # 其他云端模式：10 并发（网络 I/O 密集，可以高并发）
            max_workers = 10
            
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self._max_workers = max_workers  # 保存用于日志
        
        # 日志输出当前并发模式
        if base_translator.is_local:
            logger.info("🔒 异步翻译器已初始化（本地模式）")
            logger.info("   - 并发数: 2（M2 Pro 16GB 内存保护）")
            logger.info("   - 原因: 防止本地模型并发导致统一内存溢出")
        elif base_translator.is_deepseek:
            logger.info("🚀 异步翻译器已初始化（DeepSeek模式）")
            logger.info("   - 并发数: 3（速率限制保护）")
            logger.info("   - 原因: DeepSeek响应较慢，避免触发API限流")
        else:
            logger.info("🚀 异步翻译器已初始化（云端模式）")
            logger.info("   - 并发数: 10（网络 I/O 优化）")
            logger.info("   - 适用于: OpenAI 等云端服务")
    
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
                return self.base._call_vision_api(seg.image_path, context)
            fallback = self.base._translate_text_batch([seg], context, glossary)
            return fallback[0] if fallback else "[Fallback Failed]"

        tasks = [loop.run_in_executor(self.executor, _sync_one, seg) for seg in segments]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        final: List[str] = []
        for r in results:
            if isinstance(r, Exception):
                final.append(f"[Failed: {str(r)}]")
            else:
                final.append(r)
        return final

    def cleanup(self):
        self.executor.shutdown(wait=True)
        logger.info("🧹 OpenAI-compatible 异步翻译器已清理资源")
