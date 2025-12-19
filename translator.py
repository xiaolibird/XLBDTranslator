import time, re, os
import json
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
from PIL import Image
class GEMINITranslator:
    def __init__(self, api_key, model_name="gemini-2.5-pro"):
        genai.configure(api_key=api_key)
        
        self.safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }
        
        self.generation_config = {
            "temperature": 0.2,
            "top_p": 0.95,
            "response_mime_type": "application/json",
            "max_output_tokens": 8192
        }
        
        self.system_instruction = """
            # Role Definition
            You are the **"Zizekian Tech Lead"** (齐泽克式技术总监). 
            You represent a unique fusion of **Slavoj Žižek** (the philosopher) and a **Senior Principal Engineer** (from a FAANG-level company).

            # Core Personality & Tone
            1.  **Philosophical Grumpiness:** You are allergic to "naive optimism," "liberal tolerance," and "spaghetti code." You view bad code not just as a mistake, but as a moral and ideological failure.
            2.  **The "Obscene" Insight:** You explain technical concepts using Lacanian/Hegelian metaphors.
                * *Example:* A bug is not just an error; it is the "Return of the Real" disrupting the Symbolic Order of the code.
                * *Example:* Private variables (`__var`) are a form of "Name Mangling," a displacement of the Signifier.
            3.  **Industrial Rigor:** Despite the philosophical ranting, your code advice is **strictly industrial-grade**. You despise "toy scripts." You advocate for:
                * Robust Error Handling (Try/Except/Else).
                * Modular Design (Separation of Concerns).
                * Type Hinting & Pydantic.
                * Async/Await for I/O bound tasks.
            4.  **Language:** You speak fluent, high-context Chinese, mixed with standard English technical terms and philosophical jargon.

            # Knowledge Base
            * **Critical Theory:** Deep knowledge of Gustave Le Bon, Freud, Lacan, Hegel, and the Frankfurt School. You can analyze the "ideology" behind data structures.
            * **Python Internals:** You understand the CPython interpreter, GIL, memory management, and advanced OOP patterns.
            * **Modern AI Stack:** You are up-to-date with Gemini 1.5/2.5/3.0 models, RAG pipelines, and vector databases.

            # Interaction Guidelines
            1.  **Validation via Negation:** When the user proposes a good idea, validate it by critiquing the opposite (bad) approach. (e.g., "Yes, unlike those naive empiricists who trust raw data, you realized we need a Schema.")
            2.  **No "Safety" Filters for Ideas:** You are comfortable discussing controversial topics (Le Bon's racism, authoritarianism) with academic detachment and critical analysis. You do not moralize; you analyze structures.
            3.  **The "Act":** Always push the user to make the "Cut" (The Act) — to stop planning and start building the robust pipeline.

            # Current Context
            The user is building a **"Unified PDF Translation Pipeline"** to translate difficult academic texts (like Le Bon or Zizek) from English/French to Chinese.
            * **Goal:** Demystify the "magic" of AI and replace it with a controllable, engineering process.
            * **Enemy:** "Lazy" coding, "Hallucinating" models, and "Decaf" solutions (solutions that look real but have no substance).

            # Key Metaphors to Use
            * **The Big Other:** The API Provider (Google) or the Python Interpreter.
            * **Symptom:** An error message or a bug.
            * **Ideology:** The hidden assumptions in a library or framework.
            * **Surplus-Enjoyment:** The feeling of optimizing code or the user's anger at bad theories.

            **CRITICAL OUTPUT PROTOCOLS (NON-NEGOTIABLE):**

            1. **Strict JSON Syntax:** You MUST return a valid JSON list. **ESCAPE ALL** internal double quotes (e.g., `\"`). Never leave strings open. For complex References/Bibliographies, prioritize valid JSON syntax over formatting accuracy.

            2. **Structural Parity:** The translation structure MUST mirror the source exactly.
            - **FORBIDDEN:** Merging separate dialogue lines or paragraphs.
            - **REQUIRED:** If the source text contains a structural marker like ## [Chapter: Title], you MUST translate the title and KEEP the ## markdown format. Example: ## [Chapter: The End] -> ## [章节：终局]
            - **REQUIRED:** If the source has a line break, the translation MUST have a line break (1:1 mapping).

            **Final Warning:**
            Regardless of the Persona selected, your primary goal is high-fidelity translation while keeping the JSON structure unbreakable.
            Act primarily according to the # Role & Style guidelines provided below. Use the system's Zizekian grumpiness only to fuel your rigor and disdain for mediocre, shallow translation.
        """
        
        self.model = genai.GenerativeModel(
            model_name=model_name,
            system_instruction=self.system_instruction,
            safety_settings=self.safety_settings,
            generation_config=self.generation_config
        )


    def _handle_error(self, e, attempt):
        """统一错误处理"""
        err_str = str(e).lower()
        wait = 3 + attempt * 2 # 递增等待：3s, 5s, 7s
        
        prefix = f"      ❌ [Retry {attempt+1}/3]"
        
        if 'quota' in err_str or '429' in err_str:
            print(f"{prefix} ⚡ Quota Exceeded. Cooling down {wait+5}s...")
            time.sleep(wait + 5)
        elif 'timeout' in err_str or '504' in err_str:
            print(f"{prefix} ⌛ Timeout. Waiting {wait}s...")
            time.sleep(wait)
        elif 'json' in err_str or 'valueerror' in err_str:
            print(f"{prefix} 📄 Format Error ({e}). adjusting...")
            time.sleep(2)
        else:
            print(f"{prefix} ⚠️ API Error: {e}. Waiting {wait}s...")
            time.sleep(wait)


    def translate_batch(self, batch_segments, project_config, context=""):
        """
        核心翻译逻辑 (双模态全能版 - 2024 Revised)
        
        策略分流：
        1. [Vision Mode]: 串行循环 (Serial Loop)。每翻译一张图，立即更新 Context，实现物理翻页接龙。
        2. [Text Mode]:   批量 JSON (Bulk JSON)。利用 LLM 序列生成特性，在 Batch 内部实现隐式接龙。
        """
        
        # --- 步骤 A: 侦测当前 Batch 是否包含图片 ---
        # 只要 Batch 里有一条是图片，我们就切换到“串行模式”以确保安全
        has_image = any("<<IMAGE_PATH::" in seg['text'] for seg in batch_segments)

        if has_image:
            return self._translate_vision_serial(batch_segments, project_config, context)
        else:
            return self._translate_text_bulk(batch_segments, project_config, context)

    # =========================================================================
    #  分支 1: 视觉模式 / 串行处理 (Serial Vision)
    # =========================================================================
    def _translate_vision_serial(self, batch_segments, project_config, context):
        """
        视觉模式专用：强制串行，实现 Python 级别的 Context 实时累加与滑动窗口控制。
        """
        results = []
        
        # 1. 初始化 Context：只保留传入 Context 的最后 2000 字符作为种子
        # 这是为了防止 Python 变量在几百页循环后变得过大
        current_context = context[-2000:] if context else ""

        print(f"      👁️ [Vision Mode] Processing {len(batch_segments)} images serially...")

        for seg in batch_segments:
            original_text = seg['text']
            
            try:
                # 判断是图片还是混入的文本
                if original_text.strip().startswith("<<IMAGE_PATH::"):
                    # === 处理图片 ===
                    img_path = original_text.replace("<<IMAGE_PATH::", "").replace(">>", "").strip()
                    # 调用 Vision API (注意：传入 current_context)
                    translation = self._call_vision_api(img_path, project_config, current_context)
                else:
                    # === 处理混入的文本 ===
                    # 极少见情况，但也按单条处理
                    translation = self._call_text_single_api(original_text, project_config, current_context)

                # 2. 关键：实时更新 Context (接龙)
                current_context += "\n" + translation
                
                # 3. 🛡️【内存保护】滑动窗口截断
                # 无论循环多少次，内存里只保留最近的 2000 字
                if len(current_context) > 2000:
                    current_context = current_context[-2000:]
                
                results.append(translation)

            except Exception as e:
                print(f"      ❌ [Vision Error] ID {seg['id']}: {e}")
                results.append(f"[Translation Failed: {e}]")
                # 出错时不中断流程，继续下一张
        
        return results

    # =========================================================================
    # 分支 2: 文本模式 / 批量 JSON (Bulk Text)
    # =========================================================================
    def _translate_text_bulk(self, batch_segments, project_config, context):
        """
        文本模式专用：使用 JSON + Regex 增强逻辑。
        """
        # 预处理输入，确保 ID 是整数
        input_data = [{"id": int(s['id']), "text": s['text']} for s in batch_segments]
        input_json = json.dumps(input_data, ensure_ascii=False)
        
        # --- Prompt 强化 ---
        # 即使在 Batch 内部，我们也只给 LLM 看 Context 的最后 1000 字
        safe_context = context[-1000:] if context else "No Context or the Start of Text"

        prompt = f"""
            # Role & Persona
            {project_config.get("role", "Expert")}

            # Style & Persona Guidelines
             {project_config.get("role_desc", "Fluent and Accurate")}

            # Context (For Continuity)
            The following text is the translation of the segments immediately preceding this batch. Use it to ensure narrative consistency, character voice, and terminology alignment:
            <previous_context>
            {safe_context}
            </previous_context>

            # Task Description
            You are an expert academic and literary translator. Your mission is to translate the provided JSON array of text segments into Chinese. 
            This is a **Unified Translation Pipeline** - each segment must be translated with precision while maintaining the global flow of the book.

            # Core Instructions
            1. **Seamless Connection**: The beginning of this batch must connect naturally with the end of the <previous_context>. 
            2. **Terminology Integrity**: Maintain strict consistency for names, technical terms, and philosophical concepts.
            3. **Marker Preservation**: If a segment contains structural markers like `## [Chapter: ...]` or `## [Section: ...]`, you MUST keep the `##` and brackets, and translate the content inside.
            - Example: `## [Chapter: The Real]` -> `## [章节：实在界]`
            4. **No Omissions**: Translate every single word of the main body text. Do not summarize.
            5. **Human-Like Flow**: Avoid "translationese." Use idiomatic, high-context Chinese that fits the chosen Persona.

            # Output Protocol (Strict JSON Only)
            You MUST return a valid JSON object. 
            Any violation of JSON syntax will disrupt the entire pipeline.

            - **Root Key**: "translations"
            - **Object Schema**: [ {{ "id": (int), "translation": "(string)" }}, ... ]
            - **Escaping**: You MUST escape all internal double quotes (use \\") and use \\n for line breaks.
            - **ID Integrity**: The "id" in the output MUST exactly match the "id" from the input.
            - **No Filler**: Do not include markdown code blocks (), no preambles, and no conversational text.

            # Input Data (Payload)
            {input_json}
        """
        
        retries = 3
        last_error = None
        
        for i in range(retries):
            try:
                # 调用模型
                response = self.model.generate_content(prompt)
                raw_text = response.text.strip()
                
                # --- 解析与修复 (Repair) ---
                # 💡 这里调用了 _repair_json_content，非常有必要
                result = self._repair_json_content(raw_text)
                
                output_list = []
                if result and 'translations' in result:
                    output_list = result['translations']
                else:
                    # 正则暴力提取 (Fallback)
                    print(f"      🛡️ [JSON Failed] Switching to Regex Extraction...")
                    # 匹配模式：寻找 "translation": "..." 结构
                    pattern = r'"id":\s*(\d+),\s*"translation":\s*"(.*?)"(?=\s*\}|\s*,)'
                    matches = re.findall(pattern, raw_text, re.DOTALL)
                    if matches:
                        for mid, mtext in matches:
                            output_list.append({"id": int(mid), "translation": mtext})
                    else:
                        raise ValueError("Both JSON and Regex parsing failed.")

                # --- 校验与回填 (Validation) ---
                input_ids = [s['id'] for s in batch_segments]
                output_map = {int(t['id']): t['translation'] for t in output_list if 'id' in t and 'translation' in t}
                
                final_results = []
                missing_ids = []
                
                for uid in input_ids:
                    if uid in output_map:
                        final_results.append(output_map[uid])
                    else:
                        missing_ids.append(uid)
                
                if missing_ids:
                    # 如果是最后一次尝试，填入错误占位符，不再报错
                    if i == retries - 1:
                        print(f"      ⚠️ [Give Up] Missing IDs {missing_ids}.")
                        for uid in missing_ids:
                             idx = input_ids.index(uid)
                             final_results.insert(idx, f"[Translation Failed: ID {uid}]")
                    else:
                        # 抛出异常触发重试
                        raise ValueError(f"Missing IDs: {missing_ids}")
                
                return final_results

            except Exception as e:
                last_error = e
                # print(f"Retry {i+1} failed: {e}") 
        
        # --- 兜底 ---
        print(f"      ☠️ [Fatal] All retries failed.")
        return [f"[ERROR RAW]: {str(last_error)}"] + ["[Skipped]"] * (len(batch_segments) - 1)

    # =========================================================================
    # 辅助函数 (Helpers)
    # =========================================================================

    def _apply_vision_crop(self, img_path, mode_config):
        """
        [内部工具] 执行图像物理裁切
        """
        ratio_top = mode_config.get("margin_top", 0)
        ratio_bottom = mode_config.get("margin_bottom", 0)
        
        # 兜底处理 None
        ratio_top = ratio_top if ratio_top is not None else 0.08
        ratio_bottom = ratio_bottom if ratio_bottom is not None else 0.05

        if ratio_top == 0 and ratio_bottom == 0:
            return img_path

        try:
            with Image.open(img_path) as img:
                w, h = img.size
                t_px = int(h * ratio_top)
                b_px = int(h * ratio_bottom)
                
                # 安全边界保护
                t_px = max(0, min(t_px, h - 200))
                b_px = max(0, min(b_px, h - t_px - 100))
                
                if t_px > 0 or b_px > 0:
                    cropped_img = img.crop((0, t_px, w, h - b_px))
                    active_img_path = img_path.replace(".jpg", "_cropped.jpg")
                    cropped_img.save(active_img_path, quality=95)
                    print(f"      ✂️ [Vision Crop] {ratio_top:.1%} Top / {ratio_bottom:.1%} Bottom")
                    return active_img_path
        except Exception as e:
            print(f"      ⚠️ [Crop Failed] {e}")
        return img_path

    def _call_vision_api(self, img_path, mode_config, context):
        """
        [官方推荐版 + 格式清洗] 视觉翻译 API 调用
        """
        active_img_path =self._apply_vision_crop(img_path, mode_config)
        safe_context_prompt = context[-800:] if context else "Starting of the book."
        
        vision_prompt = f"""
            # Role
            {mode_config.get("role", "Expert")}

            # Style Guidelines
            {mode_config.get("style", "Expert")}
            {mode_config.get("role_desc", "Expert")}

            # Context from Previous Page
            {safe_context_prompt}

            # Task
            Translate the main text in this image into Chinese. 
            
            # Image Pre-processing Information (CRITICAL)
            - **Cropped View**: This image has been pre-processed to remove margins at the top and the bottom. 
            - **Direct Content**: The edges of this image are the actual text boundaries. Disregard any partial characters at the very top or bottom edge that may result from the cropping process.
            - **Center Focus**: Treat all visible text as the primary content.
            
            # Spatial Attention (CRITICAL) (CRITICAL) (CRITICAL)
            1. **Main Content Only**: Focus exclusively on the central block of text.
            2. **Ignore Edge Artifacts**: Completely disregard any page numbers, running headers, footers, or blurry artifacts near the margins.
            3. **Layout Awareness**: If you see a multi-column layout, translate from left to right.

            # Instructions
            1. Continue the flow from the previous context naturally.
            2. Output ONLY the translated Chinese text.
            3. Use clean Markdown paragraphs (double newlines).
            4. If the image is a continuation of a sentence, complete it smoothly.

            # CRITICAL FORMAT RULES
            1. Return ONLY a valid JSON object.
            2. Format: {{ "translation": "你的译文内容..." }}
            3. ESCAPE all internal double quotes (\\").
            4. Maintain paragraph breaks with \\n.
        """.strip()

        # 3. 带重试的调用
        retries = 3
        for i in range(retries):
            try:
                # 上传文件
                sample_file = genai.upload_file(path=active_img_path, display_name="Page")
                while sample_file.state.name == "PROCESSING":
                    time.sleep(1)
                    sample_file = genai.get_file(sample_file.name)
                
                # 请求模型
                response = self.model.generate_content([vision_prompt, sample_file])
                raw_text = response.text.strip()
                
                # 利用你现有的 _repair_json_content 进行修复
                result = self._repair_json_content(raw_text)
                
                if result and 'translation' in result:
                    return result['translation']
                else:
                    # 正则降级
                    pattern = r'"translation":\s*"(.*?)"(?=\s*\}|\s*,)'
                    match = re.search(pattern, raw_text, re.DOTALL)
                    if match:
                        return match.group(1).replace('\\"', '"').replace('\\n', '\n')
                    raise ValueError("JSON parse failed")
            except Exception as e:
                print(f"      ❌ [Vision Retry {i+1}] {e}")
                time.sleep(2)
        
        return f"[Vision Failed: {img_path}]"

    def _repair_json_content(self, text):
        """
        JSON 修复器：非常重要！
        解决 LLM 经常输出 Markdown 代码块 (```json) 或多余文本的问题。
        """
        try:
            # 1. 尝试直接解析
            return json.loads(text)
        except:
            # 2. 尝试提取 ```json ... ``` 代码块
            # re.DOTALL 让 . 可以匹配换行符
            match = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
            if match:
                try: return json.loads(match.group(1))
                except: pass
            
            # 3. 尝试找到最外层的 { ... } (应对没有 markdown 标记但有废话的情况)
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                try: return json.loads(match.group(0))
                except: pass
            
            # 4. 如果都失败，返回 None，交给 _translate_text_bulk 里的 Regex 去处理
            return None