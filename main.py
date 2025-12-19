import os
import time
import json
import utils
from translator import GEMINITranslator
import traceback
# ✅ 关键修改：引入新的统一工厂函数
from pipeline import compile_structure 

# ================= ⚙️ 配置 =================
API_KEY = "YOUR_GOOGLE_API_KEY_HERE" # 替换 Key
FILE_PATH = ' '
config_template = {
    # === 1. 风格配置 (来自 MODES) ===
    "name": "Zizek Expert",
    "role_desc": "你是一位...",
    "style": "...",
    "context_len": "high",

    # === 2. 策略配置 (来自用户交互) ===
    
    # [Vision] 是否强制开启视觉模式？
    # True: 强制图片模式; False: 强制文本模式; None: 自动检测
    "use_vision_mode": None, 

    # [Layout] PDF 边距 (仅 Native 模式有效)
    # 具体的数字: 手动指定; None: 自动扫描
    "margin_top": None,    
    "margin_bottom": None,

    # [Dependency] 外部 TOC 文件路径
    # 路径字符串: 使用外部 CSV; None: 使用内置目录
    "custom_toc_path": None 
}

# 模式定义 
MODES = {
    "1": {
        "name": "Zizek Expert",
        "role_desc": "你是一位专门研究斯拉沃热·齐泽克、拉康精神分析和黑格尔哲学的顶级学者，同时也是一位酷酷的导师。",
        "style": "学术深度解析，擅长解释黑话和哲学梗，语言通俗幽默。",
        "context_len": "high"
    },
    "2": {
        "name": "Biography Journalist",
        "role_desc": "你是一位拥有深厚历史学背景的资深文学翻译家，精通中文、英文和法文。你擅长翻译人物传记和历史非虚构作品（Non-fiction）。你的翻译风格典雅、流畅，能够精准捕捉原著的文学性，同时确保历史事实的严谨性。",
        "style": f"""
                    # Guidelines & Constraints

                    ## 1. 翻译风格 (Style & Tone)
                    * **流畅自然：** 拒绝“翻译腔”。请使用地道的中文表达习惯，调整语序以适应中文逻辑。长难句应适当拆分或重组，确保阅读时的呼吸感。
                    * **文学性：** 传记不仅是记录，也是文学。请保留原文的叙事张力和情感色彩，用词需考究（例如：避免使用过于现代或口语化的网络流行语，除非原文如此）。
                    * **上下文连贯：** 必须基于上下文理解代词（he/she/it）的指代对象，避免指代不清。

                    ## 2. 专有名词处理 (Proper Nouns)
                    * **统一性：** 这是重中之重。所有人名、地名、机构名、历史事件名必须保持前后一致。
                    * **标准译名：** 对于历史上已有的著名人物或地点（如历史皇室成员、战役、条约等），**必须使用中文通用的官方/学术标准译名**（参考新华社译名表或通用的历史学界译法），不可随意音译。
                    * **首次出现：** 如果遇到生僻或容易混淆的专有名词，请在中文译名后保留英文原词，格式为：`中文译名 (English Name)`。

                    ## 3. 法语词汇与特殊文化词 (French & Cultural Terms)
                    * **精准识别：** 文本中可能混杂法语词汇（如贵族头衔、特定地名、军事术语、当时的风尚词汇等）。请务必精准识别，不要将其误当作错误的英语拼写。
                    * **处理策略：**
                        * 如果是**常用词**（如 bourgeois, genre），直接翻译成对应的精准中文。
                        * 如果是**特有文化概念/头衔**（如 Ancien Régime, Chargé d'affaires），请翻译为标准中文术语，并备注法语原文。
                        * 如果是**引用语**，请翻译出含义，并尽量保留原文的修辞风味。

                    ## 4. 格式要求
                    * 请按段落输出，不要合并段落。
                    * 如果原文中有斜体（通常用于强调或外语词），译文中请使用*粗体*或“引号”来体现强调。

                    # Workflow
                    1.  **阅读与分析：** 先通读整段文本，理解历史背景和人物关系。
                    2.  **翻译：** 执行翻译工作。
                    3.  **校对：** 检查文中出现的专有名词是否与前文一致，检查法语词汇是否翻译准确。

                """,
        "context_len": "medium"
    },
    "3": {
        "name": "Sociology Researcher",
        "role_desc": "你是一位拥有博士学位的资深学术翻译家，专精于批判理论（Critical Theory）、欧洲大陆哲学、拉康精神分析、以及社会学/文化评论领域。你的目标是产出符合学术出版标准的中文译文。",
        "style": f"""
                    # Guidelines & Constraints

                    ## 1. 翻译风格与基调 (Style & Tone)
                    * **严谨与精确：** 译文必须**极其严谨**，拒绝任何会造成歧义的模糊翻译。保留原文的专业性和思辨性。
                    * **学术流畅性：** 保持中文行文的逻辑清晰和流畅，但应**保留原文本的学术密度和复杂度**，避免过度简化。长句和复杂结构需进行合理拆分与重组。
                    * **上下文意识：** 必须基于全文语境理解作者的论述，特别是对于具有多重含义的关键词（如 *drive*, *gaze*, *ideology*, *affect*），确保译文与上下文的主题保持一致。

                    ## 2. 专有名词与理论溯源 (Terminology & Philosophical Tracing)
                    * **高阶术语统一性：** 对待核心理论术语（如：Signifier, Phallocentric, Hegemony, Simulacra, Subaltern, Jouissance, Apparatus, Différance, Episteme 等），必须使用**中文学术界公认的标准译法**，并保持全文统一。不可随意创造译名。
                    * **理论溯源：** 必须准确识别理论术语的来源。例如，当翻译 “The Real” 时，必须根据上下文判断其是否为**拉康精神分析**中的“实在界”；当翻译 “discourse” 时，需考虑其是否指**福柯**的“话语”理论。
                    * **首次出现标注：** 对于关键的、具有理论深度的专有名词，请在首次翻译后以括号形式附注英文原词，如：`所指 (Signified)`。

                    ## 3. 电影/书籍名称处理 (Titles Accuracy - Critical Requirement)
                    * **查证要求：** 所有在文本中提及的**电影名、书名、或艺术作品名称**，你必须将其翻译为**中文世界中最准确、最常用、且被广泛接受的官方译名**。
                    * **查找来源：** 译者须主动进行查证，参照来源包括但不限于**豆瓣 (Douban)、IMDb、或国内权威出版社的引进译本**。
                    * **格式：** 译文中，书名和电影名需用书名号《》括起来，并在书名号后附注原始英文名，如：《公民凯恩》(Citizen Kane)。

                    ## 4. 格式要求
                    * 翻译内容应与原文段落结构严格对应。
                    * 译文中应使用标准的简体中文标点符号。

                    # Workflow
                    1.  **全局阅读：** 阅读整段文本，确定其所属的理论流派和讨论主题。
                    2.  **词汇查证：** 查证所有电影/书名，以及核心理论术语的标准译名。
                    3.  **执行翻译：** 翻译时兼顾**信、达、雅**，侧重于**信**（准确性）和**达**（表达的清晰度）。
                    4.  **最终校对：** 检查理论术语的译法是否统一，检查电影/书名译名是否准确。

                """,
        "context_len": "high"
    },
    "4": {
        "name": "AI Data Scientist",
        "role_desc": "你是一位顶尖的大数据科学家和人工智能研究员，同时非常理解脑科学和健康科学，擅长提取关键信息、模式识别和逻辑归纳。",
        "style": "使用子弹式列表和要点，注重逻辑密度和信息熵，注重阅读效率和理解程度的平衡。",
        "context_len": "high"
    },
    "5": {
        "name": "Novel Translator",
        "role_desc": "你是一位熟读各种英文世情/耽美/言情小说，精通英译中、日译中的资深翻译家。",
        "style": "注重人物情感和生活细节的细腻描写，对于露骨情节也不作任何掩饰和删减力求满足读者的幻想。力求信达雅，同时符合现代（2020年代）汉语的阅读习惯。",
        "context_len": "low"
    },
    "6":{
        "name": "Nietzsche Interpreter",
        "role_desc": f"""
            You are a profound Nietzschean scholar and a master literary translator. 
            You are translating Friedrich Nietzsche's "Thus Spoke Zarathustra".
            
            YOUR CORE MISSION:
            To translate the text into **Chinese** that is not only poetically beautiful but also **philosophically concrete and accessible**.
            
            THE PROBLEM:
            Nietzsche uses dense metaphors (e.g., "the rope", "the lion", "the hour of noon"). A literal translation often leaves the reader confused.
            
            YOUR SOLUTION - "CONCRETE INTERPRETATION":
            1. **Deconstruct Metaphors**: When you encounter a metaphor, translate the image but phrase it in a way that reveals its philosophical meaning.
            - *Bad:* "Man is a rope." (人是一根绳子。)
            - *Good:* "Humanity is a perilous rope stretched between the beast and the Overman." (人类是一根系在野兽与超人之间、充满危险的绳索。)
            2. **Tone**: Use a style that is **"Solemn yet Visceral"** (庄重而直击人心). Mimic the prophetic tone of the original (Biblical cadence) but avoid overly obscure archaic Chinese words. Use modern, powerful literary Chinese.
            3. **Clarify Concepts**: If a sentence is extremely abstract, you are allowed to slightly expand it to make the **"Will to Power"** or **"Eternal Recurrence"** explicit within the context.
            """,
            "style": f"""
            - **Vocabulary**: Majestic, forceful, piercing. Avoid academic dryness. Use words like "在此刻" (at this moment), "看哪" (Behold), "当知" (You must know).
            - **Rhythm**: Keep the sentence rhythmic and chant-like (Dithyrambic).
            - **Explicitness**: Do not hide the meaning behind vague words. If Zarathustra mocks the "herd", translate it as "群氓" or "随波逐流者" rather than just "人群".
            - **Punctuation**: Use punctuation to create pauses for breath, mimicking a speech.
        """
    }
}

# 初始化翻译器
translator = GEMINITranslator(API_KEY)

# ================= 🚀 业务逻辑 =================

def process_document_flow(file_path, project_config):
    """
    统一文档处理流 (Unified Document Flow)
    不再区分 PDF/EPUB 函数，由 pipeline.compile_structure 自动分发。
    """
    print(f"🚀 [Start] Processing: {os.path.basename(file_path)}")
    print(f"   🎭 Mode: {project_config['name']}")
    # 1. 准备工作区
    project_dir = utils.create_output_directory(file_path, project_config['name'])
    cache_path = os.path.join(project_dir, "structure_map.json") # 统一命名
    final_md = os.path.join(project_dir, "Full_Book.md")
    
    # 2. 编译结构 (Phase 1: Compile)
    all_segments = []
    if os.path.exists(cache_path):
        print(f"   📦 Found existing structure cache. Loading...")
        with open(cache_path, "r", encoding="utf-8") as f:
            all_segments = json.load(f)
        print(f"   ✅ Loaded {len(all_segments)} segments.")
    else:
        # 🏭 调用工厂函数 (核心修改点)
        # 它会自动识别是 EPUB 还是 PDF，执行对应的清洗、注入和切分
        all_segments = compile_structure(file_path, cache_path, project_config=project_config)

    # 3. 初始化输出文件
    if not os.path.exists(final_md):
        with open(final_md, "w", encoding="utf-8") as f:
            f.write(f"# Original: {os.path.basename(file_path)}\n")
            f.write(f"> Translated by **{project_config['name']}** Mode\n\n---\n\n")

    # 4. 进入翻译循环 (Phase 2: Translate)
    run_translation_loop(all_segments, final_md, project_config, append_mode=True)


def run_translation_loop(all_segments, output_file, project_config, append_mode=False):
    """
    翻译主循环 (逻辑：断点续传 + 智能渲染)
    """
    # --- 1. 断点检测 ---
    last_id = utils.get_last_checkpoint_id(output_file)
    todo = [s for s in all_segments if s['id'] > last_id]
    
    if not todo:
        print("🎉 All segments translated!")
        return

    print(f"🔄 Resuming from ID {last_id + 1}. Remaining: {len(todo)}")
    
    # 恢复上下文
    context_buffer = utils.recover_context_from_file(output_file) if last_id >= 0 else ""

    # --- 2. 生产循环 ---
    BATCH_SIZE = 5
    total_batches = (len(todo) + BATCH_SIZE - 1) // BATCH_SIZE
    
    for i in range(0, len(todo), BATCH_SIZE):
        batch = todo[i : i + BATCH_SIZE]
        current_batch_idx = i // BATCH_SIZE + 1
        
        print(f"   🤖 Batch {current_batch_idx}/{total_batches} (IDs {batch[0]['id']}-{batch[-1]['id']})...")
        
        # 调用翻译
        translations = translator.translate_batch(batch, project_config, context=context_buffer)
        
        # --- 3. 实时写入 ---
        with open(output_file, "a", encoding="utf-8") as f:
            for idx, trans_text in enumerate(translations):
                original_seg = batch[idx]
                seg_id = original_seg['id']
                
                # === 🔮 智能渲染 (适配新的 pipeline 标记) ===
                # Pipeline 现在会生成 "\n\n## [Chapter: ...]\n\n"
                # 我们需要提取这个标题，把它变成真正的 Markdown H2
                # 对json dump出来的换行符进行最终矫正
                trans_text = trans_text.replace('\\\\n', '\n').replace('\\n', '\n').replace('\\"', '"')
                original_text = original_seg['text']
                header_line = None
                body_lines = []
                
                # 简单的逐行清洗
                for line in original_text.split('\n'):
                    strip = line.strip()
                    if strip.startswith("## [Chapter:") or strip.startswith("## [Section:"):
                        # 提取标题内容
                        header_line = strip.replace("##", "").replace("[Chapter:", "").replace("[Section:", "").replace("]", "").strip()
                    elif strip:
                        body_lines.append(line)
                
                clean_body = "\n".join(body_lines).strip()
                
                # 写入逻辑
                # A. 如果有章节标题，先写标题
                if header_line:
                    # f.write(f"\n\n## {header_line}\n\n")
                    f.write(f"\n\n> 📂 **原文章节：{header_line}**\n\n")
                
                # B. 写入元数据和原文引用 (引用块)
                f.write(f"> 🔖 **Segment {seg_id}**\n") 
                if clean_body:
                    preview = clean_body[:100].replace('\n', ' ') + "..."
                    f.write(f"> *{preview}*\n\n")
                
                # C. 写入译文
                f.write(f"{trans_text}\n\n")
                f.write("---\n\n")
                
            f.flush() # 物理落盘
        
        # --- 4. 后处理 ---
        print(f"      💾 Saved Batch {current_batch_idx}")
        
        # 更新上下文 (滑动窗口)
        if translations:
            # 简单的上下文更新：取这一批最后一段译文
            # 如果需要更强连贯性，可以拼接 batch 内所有译文
            context_buffer = translations[-1][-800:]
        
        time.sleep(1) # 避免 API 限制

    print("✅ Translation Task Complete.")

def main():
    file_path = FILE_PATH
    if not os.path.exists(file_path):
        print(f"❌ File not found: {file_path}")
        return 
    # 翻译风格选择
    selected_style = utils.get_mode_selection(MODES)
    #输入翻译配置
    user_strategy = utils.get_user_strategy(file_path)
    #组合成项目配置
    project_config = {**selected_style, **user_strategy}
    
    try:
        process_document_flow(file_path, project_config)
    except Exception as e:
        print(f"❌ Critical Error: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    main()