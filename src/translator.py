import time
import re
import os
import json
import logging
from typing import List, Dict, Any, Optional
from pathlib import Path

import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
from google.api_core.exceptions import GoogleAPICallError, ResourceExhausted, ServiceUnavailable, ClientError, DeadlineExceeded
from .pipeline import ContentSegment
from PIL import Image

# 导入自定义的错误类型和配置
from src.errors import (
    APIError,
    APIRateLimitError,
    APITimeoutError,
    JSONParseError,
    TranslationError,
    FileSystemError,
    APIAuthenticationError
)
from .config import Settings

# 获取一个专用于此模块的 logger
logger = logging.getLogger(__name__)

def load_prompt_template(template_name: str) -> str:
    """从文件加载 Prompt 模板"""
    path = Path(__file__).parent / "prompts" / template_name
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        logger.error(f"Prompt template not found: {path}")
        # 返回一个基本模板以防文件丢失
        return "Translate the following text: {input_json}"

class GEMINITranslator:
    def __init__(self, settings: Any):
        """
        初始化 GEMINITranslator。

        Args:
            settings: 包含所有配置的 Settings 对象 (需包含 gemini_api_key, gemini_model, max_retries 等)。
        """
        self.settings = settings
        logger.info(f"Initializing GEMINITranslator with model: {self.settings.gemini_model}")
        self.model_name = self.settings.gemini_model

        # 1. 加载 Prompt 模板
        self.system_instruction = load_prompt_template("system_instruction.md")
        self.text_prompt_template = load_prompt_template("text_translation_prompt.md")
        self.vision_prompt_template = load_prompt_template("vision_translation_prompt.md")

        # 2. 配置 API Key
        try:
            genai.configure(api_key=self.settings.gemini_api_key)
        except Exception as e:
            raise APIAuthenticationError(
                f"Failed to configure Gemini API. Check your API key.",
                context={"error": str(e)}
            )

        # 3. 安全设置 (放宽限制以避免翻译中断)
        self.safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }
        
        # 4. 生成配置
        self.generation_config = {
            "temperature": 0.2, # 低温度保证翻译准确性
            "top_p": 0.95,
            "response_mime_type": "application/json", # 强制 JSON 输出
            "max_output_tokens": 8192,
        }
        
        # 5. 初始化模型
        try:
            self.model = genai.GenerativeModel(
                model_name=self.settings.gemini_model,
                system_instruction=self.system_instruction,
                safety_settings=self.safety_settings,
                generation_config=self.generation_config,
            )
        except Exception as e:
            raise APIError(f"Failed to initialize Gemini Model: {e}")

    def translate_batch(
        self,
        batch_segments: List[ContentSegment],
        project_config: Dict[str, Any],
        context: str = ""
    ) -> List[str]:
        """
        [入口方法] 核心翻译逻辑。
        根据 ContentSegment.content_type 自动分流到 文本模式(批量) 或 视觉模式(串行)。
        """
        if not batch_segments:
            logger.warning("translate_batch received an empty list.")
            return []

        # 检查批次中是否包含图片 (使用对象属性判断)
        has_image = any(seg.content_type == "image" for seg in batch_segments)

        if has_image:
            logger.info(f"Batch contains images. Switching to Serial Vision Mode. (IDs: {[s.segment_id for s in batch_segments]})")
            return self._translate_vision_serial(batch_segments, project_config, context)
        else:
            # logger.debug("Processing batch in Bulk Text Mode.")
            return self._translate_text_bulk(batch_segments, project_config, context)

    def translate_plain_text_list(self, text_list: List[str], project_config: Dict[str, Any]) -> Dict[str, str]:
        """
        [辅助方法] 翻译纯文本列表 (用于目录/标题翻译)。
        增强功能：支持多种 JSON 结构返回，增加正则兜底。
        返回: { "Origin Text": "Translated Text" }
        """
        if not text_list:
            return {}
            
        logger.info(f"Translating {len(text_list)} titles/headers...")
        
        # 构造 JSON 请求
        input_json = json.dumps(text_list, ensure_ascii=False)
        prompt = (
            f"You are a professional translator. Translate the following list of document headers/titles into Chinese.\n"
            f"\n\nYour style: \n\n{project_config.get('style','Fluent and precise')}\n\n"
            f"Input JSON: {input_json}\n"
            f"**You MUST OBEY THE FOLLOWING RULE!!!!!!**\n"
            f"Output JSON format: A flat JSON Dictionary where keys are the source text and values are the translation. \n"
            f"Example: {{ \"Chapter 1\": \"第一章\", \"Index\": \"索引\" }}\n"
            f"Return ONLY the JSON object."
        )
        
        try:
            response = self.model.generate_content(prompt)
            raw_text = response.text.strip()
            
            # =========================================================
            # 1. 尝试标准 JSON 解析与清洗
            # =========================================================
            try:
                # 使用你现有的清洗函数
                parsed_data = self._repair_json_content(raw_text)
                
                final_map = {}
                
                # Case A: 直接返回了字典 { "Title": "标题", "Chapter 1": "第一章" }
                if isinstance(parsed_data, dict):
                    # 检查是否包含嵌套列表 (例如 {"items": [...]})
                    has_nested_list = False
                    for val in parsed_data.values():
                        if isinstance(val, list):
                            parsed_data = val # 降级为列表处理
                            has_nested_list = True
                            break
                    
                    if not has_nested_list:
                        # 假设是直接映射，过滤掉非字符串的值
                        return {str(k): str(v) for k, v in parsed_data.items() if isinstance(v, str)}

                # Case B: 返回了列表 [{"original": "...", "translation": "..."}]
                if isinstance(parsed_data, list):
                    for item in parsed_data:
                        if isinstance(item, dict):
                            # 模糊匹配 key，增强鲁棒性
                            k = None
                            v = None
                            
                            # 找 Key
                            for key_candidate in ["original_text", "original", "source", "text", "en"]:
                                if key_candidate in item:
                                    k = item[key_candidate]
                                    break
                            # 如果没找到常见key，取字典第一个键
                            if not k and item:
                                k = list(item.keys())[0]

                            # 找 Value
                            for val_candidate in ["translated_text", "translation", "target", "zh", "cn"]:
                                if val_candidate in item:
                                    v = item[val_candidate]
                                    break
                            # 如果没找到常见key，取字典第一个值
                            if not v and item:
                                v = list(item.values())[0]
                                
                            if k and v:
                                final_map[str(k)] = str(v)
                    
                    return final_map

            except (JSONParseError, Exception):
                # JSON 解析失败，进入兜底流程
                pass

            # =========================================================
            # 2. 正则兜底 (Regex Fallback)
            # =========================================================
            # 如果 JSON 彻底挂了，尝试匹配 "原文": "译文" 的模式
            # 这里的正则比 _regex_fallback 更通用，不依赖特定字段名
            logger.warning("JSON parse failed for titles, attempting regex fallback...")
            
            fallback_map = {}
            
            # 匹配模式： "任意内容" : "任意内容"
            # 排除转义引号的影响
            pattern = r'"(.*?)(?<!\\)"\s*:\s*"(.*?)(?<!\\)"'
            matches = re.findall(pattern, raw_text)
            
            for k, v in matches:
                # 过滤掉显然不是翻译对的内容 (比如 key 和 value 一样)
                if k != v: 
                    fallback_map[k] = v
            
            if fallback_map:
                logger.info(f"Regex fallback recovered {len(fallback_map)} titles.")
                return fallback_map

            return {}

        except Exception as e:
            logger.error(f"Title translation failed: {e}")
            return {}

    def _translate_vision_serial(
        self,
        batch_segments: List[ContentSegment],
        project_config: Dict[str, Any],
        context: str
    ) -> List[str]:
        """
        [视觉模式] 强制串行处理。
        支持图片处理，如果遇到混入的文本片段，会降级调用文本接口。
        """
        results: List[str] = []
        # 使用滑动窗口上下文
        current_context = context[-self.settings.max_context_length:] if context else ""
        
        for seg in batch_segments:
            try:
                translation = ""
                
                # 情况 A: 图片片段
                if seg.content_type == "image":
                    if seg.image_path and os.path.exists(seg.image_path):
                        translation = self._call_vision_api(seg.image_path, project_config, current_context)
                        time.sleep(self.settings.rate_limit_delay or 2.0)
                    else:
                        logger.error(f"Segment {seg.segment_id} missing image path: {seg.image_path}")
                        translation = "[Error: Image Not Found]"

                # 情况 B: 混合的文本片段 (降级处理)
                else:
                    logger.info(f"Mixed Text Segment {seg.segment_id} in vision batch. Using text fallback.")
                    # 复用批量文本接口，只传一个元素的列表，取第一个结果
                    fallback_res = self._translate_text_bulk([seg], project_config, current_context)
                    translation = fallback_res[0] if fallback_res else "[Error: Fallback Failed]"

                # 结果处理
                results.append(translation)

                # 实时更新上下文 (简单的字符串拼接)
                current_context += "\n" + translation
                if len(current_context) > self.settings.max_context_length:
                    current_context = current_context[-self.settings.max_context_length:]

            except Exception as e:
                logger.error(f"Vision translation failed for segment {seg.segment_id}: {e}")
                results.append(f"[Translation Failed: {str(e)}]")
        
        return results

    def _translate_text_bulk(
        self,
        batch_segments: List[ContentSegment],
        project_config: Dict[str, Any],
        context: str
    ) -> List[str]:
        """
        [文本模式] 批量 JSON 处理 + 自动重试 + 错误恢复。
        """
        # 1. 构建 JSON Payload (从对象属性提取)
        input_data = [
            {"id": seg.segment_id, "text": seg.original_text} 
            for seg in batch_segments
        ]
        input_json = json.dumps(input_data, ensure_ascii=False)
        safe_context = context[-self.settings.max_context_length:] if context else "No Context"

        # 2. 填充 Prompt
        prompt = self.text_prompt_template.format(
            role=project_config.get("role", "Expert translator"),
            style=project_config.get("style", "Neutral"), 
            role_desc=project_config.get("role_desc", "Accurate and fluent"),
            context=safe_context,
            input_json=input_json
        )
        
        last_error: Optional[Exception] = None
        last_raw_response: str = "<No Response Received>"
        # 3. 重试循环
        for attempt in range(self.settings.max_retries):
            try:
                # API 调用
                response = self.model.generate_content(prompt)
                raw_text = response.text.strip()
                last_raw_response = raw_text
                
                # JSON 解析与修复
                
                output_list: List[Dict[str, Any]] = []
                is_json_valid = False

                # --- 1. 尝试标准 JSON 解析 ---
                try:
                    result = self._repair_json_content(raw_text)
                    
                    # 归一化：不管返回的是 list 还是 dict，统一转成 list
                    temp_list = []
                    if isinstance(result, list):
                        temp_list = result
                    elif isinstance(result, dict) and 'translations' in result and isinstance(result['translations'], list):
                        temp_list = result['translations']
                    
                    # --- 2. 严格校验 (Strict Validation) ---
                    # 检查点：列表不为空 + 元素也是字典 + 包含 'id' 和 'translation'
                    valid_items = []
                    for item in temp_list:
                        if isinstance(item, dict) and 'id' in item and 'translation' in item:
                            valid_items.append(item)
                    
                    # 关键判定：如果我们拿到了所有数据的 JSON，那就完美
                    if len(valid_items) == len(batch_segments):
                        output_list = valid_items
                        is_json_valid = True
                    else:
                        # JSON 虽解析成功，但数量对不上 (比如 input 5 个，json 只回了 3 个)
                        # 这时候标记为 False，让下面的正则去尝试“打捞”更多数据
                        logger.warning(f"JSON parsed but length mismatch. Input: {len(batch_segments)}, Output: {len(valid_items)}. Attempting regex salvage...")
                        # 暂时先存下 JSON 的结果，万一正则更烂，至少还有这些
                        output_list = valid_items 
                        
                except (json.JSONDecodeError, JSONParseError):
                    # JSON 彻底挂了，准备正则兜底
                    logger.warning("JSON parse failed completely. Fallback to Regex.")
                    is_json_valid = False

                # --- 3. 正则兜底 (Regex Fallback/Salvage) ---
                # 如果 JSON 无效，或者 JSON 数量不够，尝试正则
                if not is_json_valid:
                    regex_list = self._regex_fallback(raw_text)
                    
                    # 决策：谁提取的数据多，就用谁
                    # (通常正则能救回那些因为少了一个逗号而导致整个 JSON 崩溃的数据)
                    if len(regex_list) > len(output_list):
                        logger.info(f"Regex salvage successful. Recovered {len(regex_list)} items (JSON had {len(output_list)}).")
                        output_list = regex_list
                    else:
                        logger.info("Regex didn't find more items than JSON. Keeping JSON result.")

                # =========================================================
                # 后续映射逻辑 (保持不变)
                # =========================================================
                
                # 结果映射 (Map Output ID -> Input ID)
                input_ids = [s.segment_id for s in batch_segments]
                output_map = {
                    int(item['id']): str(item.get('translation', '')) 
                    for item in output_list 
                    # 再次确保 ID 是数字且存在
                    if 'id' in item and str(item['id']).isdigit()
                }
                
                final_results = []
                missing_ids = []
                
                for uid in input_ids:
                    if uid in output_map:
                        final_results.append(output_map[uid])
                    else:
                        missing_ids.append(uid)
                        final_results.append("[Missing Translation]")
                
                if missing_ids:
                    # 如果这已经是最后一次尝试，且还有缺失，记录警告
                    if attempt == self.settings.max_retries - 1:
                        logger.error(f"Batch partial failure after retries. Missing IDs: {missing_ids}")
                    else:
                        # 如果不是最后一次，且缺了很多，抛出异常触发 Retry 可能会更好？
                        # 这里是个策略选择。如果缺了一半，建议 throw error 让整个 batch 重试
                        if len(missing_ids) > len(batch_segments) / 2:
                            raise ValueError(f"Too many missing translations ({len(missing_ids)}/{len(batch_segments)})")
                        
                        logger.warning(f"Partial success. Missing IDs: {missing_ids}")
                
                return final_results

            except (ValueError, json.JSONDecodeError) as e:
                # 捕获上面的 ValueError (缺失太多触发重试)
                last_error = e
                logger.warning(f"Validation Error (Attempt {attempt + 1}): {e}")

            except (json.JSONDecodeError, JSONParseError) as e:
                snippet = last_raw_response[:200].replace('\n', ' ') + "..."
                logger.warning(f"JSON Error (Attempt {attempt + 1}): {e} | Snippet: {snippet}")
                last_error = e
            except GoogleAPICallError as e:
                # 细分 API 错误
                if isinstance(e, ResourceExhausted):
                    logger.warning("Rate Limit hit. Cooling down...")
                elif isinstance(e, ServiceUnavailable):
                    logger.warning("Service Unavailable.")
                else:
                    logger.error(f"API Call Error: {e}")
                last_error = e
            except Exception as e:
                logger.error(f"Unexpected Error (Attempt {attempt + 1}): {e}")
                last_error = e

            # 指数退避策略
            delay = self.settings.rate_limit_delay * (2 ** attempt)
            time.sleep(delay)
        
        # =========================================================
        # 最终兜底与“验尸报告” (Post-mortem)
        # =========================================================
        logger.critical(f"❌ All retries failed for batch IDs: {[s.segment_id for s in batch_segments]}")
        logger.critical(f"   Last Exception: {last_error}")
        
        # 将造成崩溃的原始文本打印出来
        logger.critical("   👇 CRASH DUMP (Last Raw Response) 👇")
        logger.critical("-" * 30)
        logger.critical(last_raw_response) 
        logger.critical("-" * 30)
        
        return [f"[Failed: {last_error}]"] * len(batch_segments)

    def _call_vision_api(self, img_path: str, project_config: Dict, context: str) -> str:
        """
        [底层] 调用 Vision API 处理单张图片。
        利用 _repair_json_content 复用清洗逻辑。
        """
        try:
            with Image.open(img_path) as img:
                # 1. 构建 Prompt
                prompt = self.vision_prompt_template.format(
                    role=project_config.get("name", "Expert"),
                    style=project_config.get("style", "Fluent and understandable"),
                    role_desc=project_config.get("role_desc", "Expert translator"),
                    context=context
                )

                # 2. 配置调整 (移除强制 JSON MIME type)
                vision_config = self.generation_config.copy()
                if "response_mime_type" in vision_config:
                    del vision_config["response_mime_type"]

                # 3. 调用模型
                response = self.model.generate_content(
                    [prompt, img],
                    generation_config=vision_config
                )
                
                raw_text = response.text.strip()

                # =========================================================
                # ♻️ 复用清洗与错误处理逻辑
                # =========================================================
                try:
                    # 1. 尝试使用 _repair_json_content (它会自动处理 Markdown 代码块和简单的括号缺失)
                    parsed_data = self._repair_json_content(raw_text)
                    
                    # 2. 提取有效内容
                    # Case A: 解析出字典 {"translation": "..."}
                    if isinstance(parsed_data, dict):
                        for key in ["translation", "content", "translated_text", "text"]:
                            if key in parsed_data:
                                return str(parsed_data[key]).strip()
                        # 没找到常见key，返回第一个 value
                        if parsed_data:
                            return str(list(parsed_data.values())[0]).strip()
                            
                    # Case B: 解析出列表 [{"translation": "..."}] (虽然Vision很少见)
                    elif isinstance(parsed_data, list) and parsed_data:
                        first_item = parsed_data[0]
                        if isinstance(first_item, dict):
                            # 递归逻辑太繁琐，直接取值
                            return str(list(first_item.values())[0]).strip()
                        return str(first_item).strip()
                    
                    # Case C: 解析出来就是个字符串 (有些奇怪的JSON结构)
                    return str(parsed_data).strip()

                except (JSONParseError, Exception):
                    # =====================================================
                    # 🛑 兜底逻辑 (Fallback)
                    # =====================================================
                    # 如果 _repair_json_content 抛出异常，说明这根本不是 JSON，
                    # 或者坏得无法修复。
                    # 对于 Vision 任务，这通常意味着模型直接返回了纯文本翻译，
                    # 或者是包含了 {translation: ...} 但格式错误的文本。
                    
                    # 简单的字符串清洗，处理类似 {translation: "..."} 但没引号的情况
                    if "translation" in raw_text and (raw_text.startswith("{") or raw_text.endswith("}")):
                        # 尝试一种非常暴力的去除两端花括号和键名的做法
                        clean = raw_text.replace('{"translation":', '').replace("{'translation':", "")
                        clean = clean.replace('translation:', '')
                        clean = clean.strip().lstrip('{"').rstrip('}"').strip()
                        return clean

                    # 默认认为就是纯文本
                    return raw_text

        except Exception as e:
            # 这里的 Exception 捕获 API 调用本身的错误 (如网络问题)
            raise TranslationError(f"Vision API call failed for {img_path}: {e}")

        except Exception as e:
            # 这里的 raise TranslationError 需要确保你导入了这个异常类
            # 如果没有，可以直接 log error 然后 return 空字符串
            # logger.error(f"Vision error: {e}")
            # return "" 
            raise TranslationError(f"Vision API call failed for {img_path}: {e}")

    def _repair_json_content(self, text: str) -> Any:
        """
        尝试修复不标准的 JSON 字符串 (如去除 Markdown 代码块)。
        """
        # 去除 ```json ... ```
        pattern = r'^```(?:json)?\s*(.*)\s*```$'
        match = re.search(pattern, text, re.DOTALL)
        if match:
            text = match.group(1)
        
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 尝试简单修复：有时 LLM 会遗漏闭合括号
            try:
                if text.strip().startswith("[") and not text.strip().endswith("]"):
                    return json.loads(text + "]")
            except:
                pass
            raise JSONParseError("Failed to parse JSON")

    def _regex_fallback(self, text: str) -> List[Dict[str, Any]]:
        """
        当 JSON 解析完全失败时，尝试用正则提取 ID 和 翻译内容。
        """
        # 匹配 "id": 123, "translation": "..."
        # pattern = r'"id":\s*(\d+),\s*"translation":\s*"(.*?)"(?=\s*\}|\s*,)'
        pattern = r'"id":\s*(\d+),\s*"translation":\s*"(.*?)(?<!\\)"(?=\s*\}|\s*,)'
        matches = re.findall(pattern, text, re.DOTALL)
        
        if not matches:
            # 尝试匹配单引号
            pattern_sq = r"'id':\s*(\d+),\s*'translation':\s*'(.*?)'(?=\s*\}|\s*,)"
            matches = re.findall(pattern_sq, text, re.DOTALL)
            
        if not matches:
            raise JSONParseError("Regex fallback also failed.")
            
        return [{"id": int(mid), "translation": mtext} for mid, mtext in matches]