"""
Gemini 翻译客户端
使用 tenacity 进行重试管理
"""
import json
import time
import re
from pathlib import Path
from typing import List, Dict, Any, Optional

import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
from google.api_core.exceptions import GoogleAPICallError, ResourceExhausted, ServiceUnavailable, ClientError, DeadlineExceeded
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from PIL import Image
from google.generativeai import GenerationConfig

from ..core.schema import Settings, ContentSegment, TranslationMap, SegmentList
from ..core.exceptions import (
    APIError, APIRateLimitError, APITimeoutError, APIAuthenticationError,
    JSONParseError, TranslationError
)
from .prompts import (
    SYSTEM_INSTRUCTION, format_text_prompt, format_vision_prompt, format_title_prompt
)
from ..utils.logger import get_logger

logger = get_logger(__name__)


class GeminiTranslator:
    """Gemini 翻译客户端"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.generation_config = {}

        # 配置 API
        self._configure_api()

        # 初始化模型
        self.model = self._create_model()

    def _configure_api(self):
        """配置 Gemini API"""
        try:
            genai.configure(api_key=self.settings.api.gemini_api_key)
        except Exception as e:
            raise APIAuthenticationError(
                "Failed to configure Gemini API. Check your API key.",
                context={"error": str(e)}
            )

    def _create_model(self):
        """创建 Gemini 模型实例"""
        safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }

        self.generation_config = {
            "temperature": 0.2,  # 降低温度以获得更稳定的输出
            "top_p": 0.95,
            "response_mime_type": "application/json",  
            "max_output_tokens": 8192,
        }

        try:
            return genai.GenerativeModel(
                model_name=self.settings.api.gemini_model,
                system_instruction=SYSTEM_INSTRUCTION,  # 移除硬编码的系统指令，避免与动态角色冲突
                safety_settings=safety_settings,
                generation_config=self.generation_config,
            )
        except Exception as e:
            raise APIError(f"Failed to initialize Gemini Model: {e}")

    def translate_batch(
        self,
        segments: SegmentList,
        translation_mode_config: Dict[str, Any],
        context: str = ""
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
            return self._translate_vision_batch(segments, translation_mode_config, context)
        else:
            return self._translate_text_batch(segments, translation_mode_config, context)

    def translate_titles(self, titles: List[str], translation_mode_config: Dict[str, Any]) -> TranslationMap:
        """翻译标题列表"""
        if not titles:
            return {}

        input_json = json.dumps(titles, ensure_ascii=False)
        prompt = format_title_prompt(input_json, translation_mode_config.get('style', 'Fluent and precise'))

        try:
            response = self.model.generate_content(prompt)
            raw_text = response.text.strip()

            # 解析响应
            parsed_data = self._repair_json_content(raw_text)

            # 归一化处理
            if isinstance(parsed_data, dict):
                return {str(k): str(v) for k, v in parsed_data.items() if isinstance(v, str)}
            elif isinstance(parsed_data, list) and parsed_data:
                # 如果返回列表，尝试转换为字典
                result = {}
                for item in parsed_data:
                    if isinstance(item, dict):
                        for k, v in item.items():
                            if k != 'id':  # 跳过 id 字段
                                result[str(k)] = str(v)
                return result

            return {}

        except Exception as e:
            logger.error(f"Title translation failed: {e}")
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
        translation_mode_config: Dict[str, Any],
        context: str
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

        # 格式化提示
        prompt = format_text_prompt(
            role=translation_mode_config.get("name", "Expert translator"),
            style=translation_mode_config.get("style", "Neutral"),
            role_desc=translation_mode_config.get("role_desc", "Accurate and fluent"),
            context=safe_context,
            input_json=input_json
        )
        # API 调用
        response = self.model.generate_content(prompt)
        raw_text = response.text.strip()
        
        # 解析响应
        output_list = self._parse_json_response(raw_text)

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
            results.append(output_map.get(uid, "[Translation Failed]"))

        return results

    def _translate_vision_batch(
        self,
        segments: SegmentList,
        translation_mode_config: Dict[str, Any],
        context: str
    ) -> List[str]:
        """视觉批量翻译（串行处理）"""
        results = []
        current_context = context[-self.settings.processing.max_context_length:] if context else ""

        for seg in segments:
            try:
                if seg.content_type == "image" and seg.image_path:
                    translation = self._call_vision_api(seg.image_path, translation_mode_config, current_context)
                    time.sleep(self.settings.processing.vision_rate_limit_delay)
                else:
                    # 降级处理文本
                    fallback_result = self._translate_text_batch([seg], translation_mode_config, current_context)
                    translation = fallback_result[0] if fallback_result else "[Fallback Failed]"

                results.append(translation)

                # 更新上下文
                current_context += f"\n{translation}"
                if len(current_context) > self.settings.processing.max_context_length:
                    current_context = current_context[-self.settings.processing.max_context_length:]

            except Exception as e:
                logger.error(f"Vision translation failed for segment {seg.segment_id}: {e}")
                results.append(f"[Translation Failed: {str(e)}]")

        return results

    def _call_vision_api(self, img_path: str, translation_mode_config: Dict, context: str) -> str:
        """调用视觉 API"""
        try:
            with Image.open(img_path) as img:
                # 格式化提示
                prompt = format_vision_prompt(
                    role=translation_mode_config.get("name", "Expert"),
                    style=translation_mode_config.get("style", "Fluent and understandable"),
                    role_desc=translation_mode_config.get("role_desc", "Expert translator"),
                    context=context
                )

                # Vision 专用配置, 重新强制 JSON 输出以匹配 prompt
                vision_config = GenerationConfig(
                    temperature=self.generation_config['temperature'],
                    top_p=self.generation_config['top_p'],
                    max_output_tokens=self.generation_config['max_output_tokens'],
                    response_mime_type="application/json",
                )

                # API 调用
                response = self.model.generate_content(
                    [prompt, img],
                    generation_config=vision_config
                )

                raw_text = response.text.strip()
                
                # 解析 JSON 并提取 "translation" 字段
                parsed_json = self._repair_json_content(raw_text)
                if isinstance(parsed_json, dict) and "translation" in parsed_json:
                    return parsed_json["translation"]
                
                # 如果解析失败或格式不正确，记录并返回错误
                print(f"❌ Vision API did not return valid JSON with a 'translation' key. Got: {raw_text[:200]}")
                return "[Translation Failed - Invalid JSON Response]"

        except Exception as e:
            raise TranslationError(f"Vision API call failed for {img_path}: {e}")

    def _parse_json_response(self, text: str) -> List[Dict[str, Any]]:
        """解析 JSON 响应，支持多种格式"""
        # print(f"🔍 解析响应文本: {repr(text[:200])}...")  # 调试信息

        try:
            # 1. 尝试标准 JSON 解析
            result = self._repair_json_content(text)

            # 2. 归一化处理
            if isinstance(result, list):
                return result
            elif isinstance(result, dict) and 'translations' in result:
                return result['translations']
            else:
                return []

        except (json.JSONDecodeError, JSONParseError) as e:
            logger.warning(f"⚠️ Standard JSON parsing failed: {e}")
            # 3. 正则兜底
            return self._regex_fallback(text)

    def _repair_json_content(self, text: str) -> Any:
        """修复 JSON 字符串"""
        original_text = text

        # 去除 Markdown 代码块
        pattern = r'^```(?:json)?\s*(.*)\s*```$'
        match = re.search(pattern, text, re.DOTALL)
        if match:
            text = match.group(1)

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            logger.debug(f"🔧 Attempting to repair JSON: {e}")

            # 尝试多种修复策略
            repaired_text = self._advanced_json_repair(text, str(e))
            if repaired_text != text:
                try:
                    return json.loads(repaired_text)
                except json.JSONDecodeError as e2:
                    logger.error(f"❌ JSON repair failed: {e2}")

            raise JSONParseError("Failed to parse JSON")

    def _advanced_json_repair(self, text: str, error_msg: str) -> str:
        """高级JSON修复"""
        # 策略1: 修复未结束的字符串
        if "Unterminated string" in error_msg:
            # 查找最后一个完整的 "translation": " 模式
            pattern = r'"translation":\s*"([^"]*)$'
            match = re.search(pattern, text, re.MULTILINE)
            if match:
                incomplete_string = match.group(1)
                # 如果字符串没有以引号结束，尝试添加引号和逗号
                if not text.strip().endswith('"'):
                    text = re.sub(pattern, f'"translation": "{incomplete_string}"', text, flags=re.MULTILINE)
                    # 确保对象正确结束
                    if not text.strip().endswith('}'):
                        text += '\n    }\n  ]\n}'

        # 策略2: 修复引号转义问题
        # 将中文引号转换为转义的英文引号
        text = text.replace('"', '\\"').replace('\\"', '"')  # 先转义所有引号，然后恢复JSON结构引号
        text = re.sub(r'(?<!\\)"([^"]*(?<!\\)"[^"]*)*(?<!\\)"', lambda m: m.group(0).replace('"', '\\"'), text)

        # 策略3: 确保JSON结构完整
        text = text.strip()
        if text.startswith('{') and not text.endswith('}'):
            text += '\n}'
        elif text.startswith('[') and not text.endswith(']'):
            text += '\n]'

        return text

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
