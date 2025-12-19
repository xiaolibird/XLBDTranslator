import os, re, datetime, random
from collections import Counter
import numpy as np

import fitz  # PyMuPDF
from bs4 import BeautifulSoup


def clean_filename(filename):
    """清理文件名，去除特殊字符"""
    return re.sub(r'[\\/*?:"<>|]', "", filename).replace(" ", "_")

def get_mode_selection(modes):
    """交互式选择模式"""
    print("\n🎭 请选择翻译模式 (Personas):")

    for key, val in modes.items():
        print(f"  [{key}] {val['name']}")
    
    choice = input("\n请输入数字 (默认 1): ").strip()
    if choice not in modes:
        choice = "1"
    
    print(f"✅ 已选择: {modes[choice]['name']}\n")
    return modes[choice]

def create_output_directory(input_file_path, mode_name):
    """创建工程文件夹"""
    date_str = datetime.datetime.now().strftime("%Y%m%d")
    base_name = os.path.splitext(os.path.basename(input_file_path))[0]
    safe_name = clean_filename(base_name)
    safe_mode = clean_filename(mode_name)
    
    folder_name = f"{date_str}_{safe_name}_{safe_mode}"
    project_path = os.path.join(os.getcwd(), folder_name)
    
    if not os.path.exists(project_path):
        os.makedirs(project_path)
        print(f"📂 创建工程文件夹: {folder_name}")
    else:
        print(f"📂 使用已有文件夹: {folder_name}")
        
    return project_path

def get_last_checkpoint_id(md_path):
    """
    读取 Markdown 文件，找到最后一个已完成的 Segment ID。
    支持新旧两种格式的兼容。
    """
    if not os.path.exists(md_path):
        return -1
        
    try:
        with open(md_path, 'r', encoding='utf-8') as f:
            content = f.read()
            
        # 1. 尝试匹配新格式: > 🔖 **Segment 101**
        ids = re.findall(r'> 🔖 \*\*Segment (\d+)\*\*', content)
        
        # 2. 如果没找到，尝试匹配旧格式 (兼容旧文件): ### Segment 101
        if not ids:
            ids = re.findall(r'### Segment (\d+)', content)
        
        if ids:
            return int(ids[-1]) # 返回最后一个找到的 ID
        return -1
        
    except Exception as e:
        print(f"⚠️ 读取进度文件失败: {e}")
        return -1

def recover_context_from_file(md_path):
    """从文件恢复上下文"""
    if not os.path.exists(md_path): return ""
    try:
        with open(md_path, 'r', encoding='utf-8') as f:
            f.seek(0, 2)
            file_size = f.tell()
            read_size = min(1000, file_size)
            if read_size == 0: return ""
            f.seek(file_size - read_size)
            return f.read()
    except: return ""

def extract_text_from_epub_item(item):
    """从 EPUB Item 提取文本"""
    try:
        soup = BeautifulSoup(item.get_content(), 'html.parser')
        for script in soup(["script", "style"]):
            script.extract()
        text = soup.get_text()
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        return '\n'.join(chunk for chunk in chunks if chunk)
    except Exception as e:
        print(f"   ⚠️ Error extracting text from EPUB item: {e}")
        return ""

def inspect_pdf_structure(pdf_path):
    """
    🔍 PDF 结构诊断器 (The PDF X-Ray)
    打印 PDF 的元数据和章节目录树，用于验证是否存在层级信息。
    """
    print("=" * 60)
    print(f"🕵️‍♂️ Inspecting: {pdf_path}")
    print("=" * 60)

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        print(f"❌ 无法打开文件: {e}")
        return

    # --- 1. 检查元数据 (Metadata) ---
    # 这里通常只有书名、作者，没有目录
    print("\n[1] 📄 Metadata (元数据):")
    meta = doc.metadata
    if meta:
        for key, value in meta.items():
            if value:
                print(f"    - {key:<15}: {value}")
    else:
        print("    (Empty Metadata)")

    # --- 2. 检查目录/书签 (TOC / Outlines) ---
    # PyMuPDF 返回格式: [[lvl, title, page, dest_dict], ...]
    # lvl: 层级 (1, 2, 3...)
    # title: 标题
    # page: 页码 (1-based)
    print("\n[2] 🌳 Table of Contents (章节目录):")
    toc = doc.get_toc(simple=False) # simple=False 获取更多详情
    
    if not toc:
        print("    ⚠️  CRITICAL: This PDF has NO Structure (No Outlines found).")
        print("        (它是扁平的。我们只能按页切分，无法提取章节名。)")
    else:
        print(f"    ✅ Found {len(toc)} entries. Structure visualization:")
        print("-" * 60)
        
        # 打印前 50 条，防止刷屏
        display_limit = 50
        
        for i, item in enumerate(toc):
            if i >= display_limit:
                print(f"\n    ... (Remaining {len(toc) - display_limit} entries hidden) ...")
                break
                
            lvl, title, page_num = item[0], item[1], item[2]
            
            # 视觉化缩进：每一级缩进 4 个空格
            indent = "    " * (lvl - 1)
            
            # 图标区分层级
            icon = "📂" if lvl == 1 else "  └─📄" if lvl == 2 else "    └─📍"
            
            # 清洗标题 (去除换行)
            clean_title = title.replace('\n', ' ').strip()
            
            print(f"{indent}{icon} [{page_num:>3}页] {clean_title}")

    print("-" * 60)
    doc.close()

def calculate_robust_margin(values, margin_type="top"):
    """
    工业级统计清洗函数：
    1. 剔除 0 值（认为是无页眉/页脚的页面，不参与计算）
    2. 使用 IQR (四分位距) 剔除离群值
    3. 返回中位数作为最可能的整数值
    """
    # 1. 剔除 0 值和极小值 (噪音)
    clean_values = [v for v in values if v > 5.0]
    
    if not clean_values:
        print(f"   ⚠️ 数据不足，未检测到有效的 {margin_type} margin，建议设为 0")
        return 0

    # 如果样本太少，直接取最大值（宁可多切不可少切）
    if len(clean_values) < 5:
        return int(max(clean_values))

    # 2. 统计学去噪 (IQR Method)
    q75, q25 = np.percentile(clean_values, [75 ,25])
    iqr = q75 - q25
    
    # 定义“正常范围”：放宽一点，1.5倍 IQR
    lower_bound = q25 - 1.5 * iqr
    upper_bound = q75 + 1.5 * iqr
    
    final_values = [x for x in clean_values if lower_bound <= x <= upper_bound]
    
    if not final_values:
        final_values = clean_values # 如果过滤完了，就回退到原始数据

    # 3. 计算统计量
    mean_val = np.mean(final_values)
    median_val = np.median(final_values)
    std_val = np.std(final_values)
    
    # 95% 置信区间 (虽然对于离散的排版数据，中位数更有意义)
    ci_lower = mean_val - 1.96 * (std_val / np.sqrt(len(final_values)))
    ci_upper = mean_val + 1.96 * (std_val / np.sqrt(len(final_values)))
    
    print(f"   📊 [{margin_type.upper()}] 样本数: {len(values)} -> 有效: {len(clean_values)} -> 去噪后: {len(final_values)}")
    print(f"      统计特征: Median={median_val:.1f}, Mean={mean_val:.1f}, Std={std_val:.2f}")
    print(f"      95% CI: [{ci_lower:.1f}, {ci_upper:.1f}]")

    # 决策：返回最接近的中位数整数
    return int(round(median_val))

def analyze_pdf_margins_by_scan(pdf_path):
    """
    智能扫描分析 PDF，采用随机分块抽样 + 鲁棒统计学估算切除值。
    """
    print("=" * 80)
    print("📐 PDF 边距探测器 (基于随机分层抽样)")
    print("=" * 80)
    
    try:
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
    except Exception as e:
        print(f"❌ 无法打开 PDF: {e}")
        return None

    # --- 1. 采样策略 (Sampling Strategy) ---
    sample_pages = set()
    
    if total_pages <= 25:
        sample_pages = set(range(total_pages))
        print(f"   🔍 文档较小 ({total_pages} 页)，进行全量扫描...")
    else:
        # 随机抽取 5 个起始点
        # 确保起始点有足够的空间放下 5 页
        max_start = total_pages - 5
        starts = []
        
        # 尝试分散采样：开头、结尾必采，中间随机
        starts.append(0) # 开头
        starts.append(max(0, max_start // 4))
        starts.append(max(0, max_start // 2))
        starts.append(max(0, max_start * 3 // 4))
        starts.append(max_start) # 结尾
        
        # 如果随机性更重要，可以用 random.sample，但固定点更稳健
        # 这里加入一点随机扰动
        starts = [min(max_start, max(0, s + random.randint(-5, 5))) for s in starts]
        
        for s in starts:
            block = range(s, s + 5)
            sample_pages.update(block)
            
        print(f"   🎲 随机抽样: 选取了 5 个区块共 {len(sample_pages)} 页进行分析...")

    sorted_pages = sorted(list(sample_pages))
    
    # 存储原始数据
    raw_top_margins = []
    raw_bottom_margins = []
    raw_page_height = []
    
    # --- 2. 执行扫描 ---
    for page_idx in sorted_pages:
        try:
            page = doc[page_idx]
            h = page.rect.height
            raw_page_height.append(h)
            
            blocks = page.get_text("blocks")
            
            # --- 寻找 Top Margin ---
            # 规则：Top 15% 区域内，最靠下的岛屿底部 + 1
            limit_top = h * 0.15
            max_y1_in_zone = 0
            found_top = False
            
            for b in blocks:
                if len(b) < 4: continue
                # b: x0, y0, x1, y1
                # 过滤掉极小的噪点 (高度<3点)
                if (b[3] - b[1]) < 3: continue 
                
                # 如果这个块完全在 limit_top 区域内
                if b[3] < limit_top:
                    if b[3] > max_y1_in_zone:
                        max_y1_in_zone = b[3]
                        found_top = True
                
                # 如果有个块跨越了 limit_top 边界 (说明是正文)，则该页可能无页眉或页眉很难分
                if b[1] < limit_top and b[3] > limit_top:
                    # 碰到正文了，停止搜索更靠下的东西
                    pass

            if found_top:
                raw_top_margins.append(max_y1_in_zone + 1)
            else:
                raw_top_margins.append(0) # 记为 0，后续统计会处理
                
            # --- 寻找 Bottom Margin ---
            # 规则：Bottom 85% 区域内，最靠上的岛屿顶部 - 1
            # 转化为：切除量 = h - (岛屿顶部 - 1)
            limit_bottom = h * 0.85
            min_y0_in_zone = h
            found_bottom = False
            
            for b in blocks:
                if len(b) < 4: continue
                if (b[3] - b[1]) < 3: continue
                
                # 如果这个块完全在 limit_bottom 区域下方
                if b[1] > limit_bottom:
                    if b[1] < min_y0_in_zone:
                        min_y0_in_zone = b[1]
                        found_bottom = True
            
            if found_bottom:
                keep_y = min_y0_in_zone - 1
                cut_amount = h - keep_y
                raw_bottom_margins.append(cut_amount)
            else:
                raw_bottom_margins.append(0)

        except Exception as e:
            continue

    doc.close()

    # --- 3. 统计分析与决策 ---
    print("-" * 50)
    suggested_top_pts = calculate_robust_margin(raw_top_margins, "top")
    print("-" * 50)
    suggested_bottom_pts = calculate_robust_margin(raw_bottom_margins, "bottom")
    print("-" * 50)
    sample_h = calculate_robust_margin(raw_page_height, "top")
    
    # ✅ 关键修改：转换为比例 (0.0 到 1.0 之间)
    # 这样无论是 72 DPI 还是 200 DPI，直接乘高度即可
    margin_top_ratio = round(suggested_top_pts / sample_h, 4)
    margin_bottom_ratio = round(suggested_bottom_pts / sample_h, 4)

    result = {
        "suggested_margin_top": margin_top_ratio,
        "suggested_margin_bottom": margin_bottom_ratio
    }
    
    print("\n" + "="*80)
    print(f"✅ 最终建议配置 (基于 {len(sorted_pages)} 页样本的统计推断):")
    print(f"   📈 转换比例完成: Top={margin_top_ratio}, Bottom={margin_bottom_ratio}")
    print("="*80 + "\n")
    
    return result

def detect_pdf_type(file_path, sample_pages=5):
    """
    返回 PDF 类型：'native', 'ocr', 'image_only'
    """
    doc = fitz.open(file_path)
    max_pages = min(len(doc), sample_pages)
    
    total_text_len = 0
    total_image_area = 0
    page_area = 0
    
    for i in range(max_pages):
        page = doc[i]
        page_area += page.rect.width * page.rect.height
        
        # 1. 检测文本量
        text = page.get_text()
        total_text_len += len(text.strip())
        
        # 2. 检测图片覆盖率
        images = page.get_images(full=True)
        # 简易估算：如果有大图覆盖，通常是扫描件
        # 这里只做简单判断：是否有图片
        if images:
            # 这是一个简化的假设，更严谨的做法是计算图片 bbox 面积
            total_image_area += page.rect.width * page.rect.height

    doc.close()

    # 判定逻辑
    avg_text_per_page = total_text_len / max_pages
    
    if avg_text_per_page < 50: # 每页不到 50 个字
        return "image_only"  # 纯图片 PDF
    
    # 如果文本很多，同时又有大图覆盖，极可能是 OCR 过的扫描件
    # (fitz 提取 OCR 文本和 Native 文本在 API 上是一样的，很难区分“透明文字”)
    # 但我们可以认为：只要能提取出字，就是 'text_available'
    # 如果用户觉得 OCR 质量烂，那是策略选择问题 (Part C)
    
    return "native_or_ocr"

def flatten_toc(toc, parent_titles=None):
    """
    递归解析 EPUB TOC (目录)，构建 {文件名: '父标题 > 子标题'} 的映射。
    实现'面包屑导航' (Breadcrumb) 效果，保留层级语义。
    """
    if parent_titles is None:
        parent_titles = []
        
    mapping = {}
    
    for item in toc:
        # 1. 提取节点与子节点
        node = None
        children = []
        
        # EbookLib 的 item 可能是 (Link, [Children]) 的元组，也可能是单独的 Link 对象
        if isinstance(item, (list, tuple)):
            node = item[0]
            children = item[1]
        elif hasattr(item, 'href'):
            node = item
            
        if not node: continue
        
        # 2. 构建面包屑标题 (Cleaning & Joining)
        # 去除标题中的换行符和多余空格
        raw_title = node.title if node.title else "Untitled"
        # clean_title = raw_title.replace('\n', ' ').strip()
        clean_title = raw_title.replace('\n', ' ').replace('\\n', ' ').strip()
        
        # 组合路径：Part 1 > Chapter 1
        full_breadcrumb = " > ".join(parent_titles + [clean_title])
        
        # 3. 记录映射 (Key = 纯文件名，不带锚点)
        # href 可能是 'chap01.xhtml#section1' -> 取 'chap01.xhtml'
        file_path = node.href.split('#')[0]
        
        # 策略：如果文件已存在（即一个文件包含多个小节），优先保留第一次出现的（通常是最高层级）
        if file_path not in mapping:
            mapping[file_path] = full_breadcrumb
            
        # 4. 递归下钻 (Drill down)
        if children:
            child_map = flatten_toc(children, parent_titles + [clean_title])
            mapping.update(child_map)
            
    return mapping

def get_user_strategy(file_path):
    """
    交互式配置向导：根据文件类型获取处理策略。
    
    Returns:
        strategy (dict): 包含以下键值:
            - use_vision_mode (bool|None): True=强制开启, False=强制关闭, None=自动
            - margin_top (float|None): 顶部裁切比例 (0.0 - 1.0)
            - margin_bottom (float|None): 底部裁切比例 (0.0 - 1.0)
            - custom_toc_path (str|None): 自定义 CSV 目录文件路径
    """
    ext = os.path.splitext(file_path)[1].lower()
    
    # 初始化默认策略
    strategy = {
        "use_vision_mode": None,   # 默认 Auto
        "margin_top": None,        # 默认 Auto (通常为 0.08)
        "margin_bottom": None,     # 默认 Auto (通常为 0.05)
        "custom_toc_path": None    # 默认无
    }
    
    print("\n" + "="*60)
    print(f"🛠️  STRATEGY SETUP (项目策略配置)")
    print(f"   Target File: {os.path.basename(file_path)}")
    print("="*60)
    
    # ==========================================
    # 1. 章节目录 (TOC) 配置
    # ==========================================
    if ext == '.pdf':
        # 仅 PDF 需要询问 CSV，因为 EPUB 自带结构
        print("\n[1/3] 📚 Table of Contents (章节目录)")
        print("      PDFs often lack a readable TOC. Do you have a CSV mapping?")
        print("      (Format: 'Page,Title,Level')")
        
        use_toc = input("      Load custom TOC CSV? (y/n) [n]: ").strip().lower()
        if use_toc == 'y':
            while True:
                path = input("      Enter CSV path: ").strip().strip("'").strip('"') # 去除误复制的引号
                if os.path.exists(path):
                    strategy["custom_toc_path"] = path
                    print(f"      ✅ Loaded: {os.path.basename(path)}")
                    break
                else:
                    print("      ❌ File not found. Please try again.")
    else:
        # EPUB 逻辑
        print(f"\n[1/3] 📚 File Structure")
        print(f"      ✅ Detected {ext.upper()} format. Using internal structure.")
        print("      (Skipping custom TOC setup)")

    # 如果不是 PDF，无需配置 Vision 和 Crop，直接返回
    if ext != '.pdf':
        print("\n✅ Setup Complete for EPUB.")
        print("="*60 + "\n")
        return strategy

    # ==========================================
    # 2. Vision 模式配置 (仅 PDF)
    # ==========================================
    print("\n[2/3] 👁️  Vision Mode (视觉/图片模式)")
    print("      Auto  = Let code detect (Recommended for most files)")
    print("      Force = Force ENABLE (Best for scans, complex layouts)")
    print("      Off   = Force DISABLE (Only use text extraction)")
    
    v_choice = input("      Selection (a/f/o) [a]: ").strip().lower()
    
    if v_choice == 'f':
        strategy["use_vision_mode"] = True
        print("      🔵 Mode: FORCED VISION (Slower but more accurate)")
    elif v_choice == 'o':
        strategy["use_vision_mode"] = False
        print("      🔵 Mode: TEXT ONLY (Fast)")
    else:
        # strategy["use_vision_mode"] stays None
        print("      🔵 Mode: AUTO DETECT")

    # ==========================================
    # 3. 裁切/边距配置 (仅 PDF)
    # ==========================================
    # 只有当 vision 模式没有被强制关闭时，裁切才最重要
    if strategy["use_vision_mode"] is not False:
        print("\n[3/3] ✂️  Image Cropping (Remove Headers/Footers)")
        print("      CRITICAL for Vision to avoid translating running titles.")
        print("      Format: 'top,bottom' ratio (0.0 to 1.0)")
        print("      Example: '0.1,0.05' (Crops top 10% and bottom 5%)")
        print("      Enter '0,0' to disable cropping.")
        print("      Press ENTER to use Defaults (Top~8%, Bottom~5%)")
        
        m_input = input("      Margins: ").strip()
        
        if "," in m_input:
            try:
                parts = m_input.split(",")
                t_val = float(parts[0].strip())
                b_val = float(parts[1].strip())
                
                # 简单的合法性检查
                if 0 <= t_val < 1.0 and 0 <= b_val < 1.0:
                    strategy["margin_top"] = t_val
                    strategy["margin_bottom"] = b_val
                    print(f"      🔵 Manual Crop: Top={t_val*100}%, Bottom={b_val*100}%")
                else:
                    print("      ⚠️ Values out of range (0-1). Using Defaults.")
            except ValueError:
                print("      ⚠️ Invalid format. Using Defaults.")
        else:
            if m_input == "0": # 用户可能只输入了一个0
                strategy["margin_top"] = 0.0
                strategy["margin_bottom"] = 0.0
                print("      🔵 Cropping: DISABLED")
            else:
                print("      🔵 Cropping: AUTO DEFAULTS")
    else:
        print("\n[3/3] ✂️  Image Cropping")
        print("      Skipped (Vision mode disabled).")

    print("="*60 + "\n")
    return strategy