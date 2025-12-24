import fitz  # PyMuPDF
import numpy as np
import random

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

def detect_page_numbers(blocks, page_height):
    """
    检测页码位置，返回页码区域的边界框列表
    """
    import re
    
    page_number_patterns = [
        r'^\d+$',                    # 纯数字: 123
        r'^-\s*\d+\s*-$',           # 带横线的页码: - 123 -
        r'^\d+\s*/\s*\d+$',         # 分页格式: 123/456
        r'^Page\s+\d+$',            # Page 123
        r'^\d+\s*页$',              # 中文页码: 123页
        r'^第\s*\d+\s*页$',         # 第123页
    ]
    
    page_number_zones = []
    
    for block in blocks:
        if not isinstance(block, dict) or 'bbox' not in block:
            continue
            
        bbox = block['bbox']  # [x0, y0, x1, y1]
        text_height = bbox[3] - bbox[1]
        
        # 只考虑很小的文本块（可能是页码）
        # 页码通常小于页面高度的5%
        if text_height > page_height * 0.05:
            continue
            
        # 提取文本内容
        block_text = ""
        if 'lines' in block:
            for line in block['lines']:
                if 'spans' in line:
                    for span in line['spans']:
                        block_text += span.get('text', '')
        
        block_text = block_text.strip()
        
        # 检查是否匹配页码模式
        for pattern in page_number_patterns:
            if re.match(pattern, block_text, re.IGNORECASE):
                page_number_zones.append(bbox)
                print(f"      📄 检测到页码: '{block_text}' at y={bbox[1]:.1f}-{bbox[3]:.1f}")
                break
    
    return page_number_zones

def is_bbox_overlap(bbox1, bbox2, tolerance=5):
    """
    检查两个边界框是否重叠（带容差）
    """
    x0_1, y0_1, x1_1, y1_1 = bbox1
    x0_2, y0_2, x1_2, y1_2 = bbox2
    
    # 添加容差
    x0_1 -= tolerance
    y0_1 -= tolerance
    x1_1 += tolerance
    y1_1 += tolerance
    
    # 检查重叠
    return not (x1_1 < x0_2 or x0_1 > x1_2 or y1_1 < y0_2 or y0_1 > y1_2)

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
            
            # 🎯 新增：检测页码位置
            page_numbers = detect_page_numbers(blocks, h)
            
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
                
                # 🎯 新增：跳过页码区域
                if any(is_bbox_overlap(b, pn_bbox) for pn_bbox in page_numbers):
                    continue

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
                
                # 🎯 新增：跳过页码区域
                if any(is_bbox_overlap(b, pn_bbox) for pn_bbox in page_numbers):
                    continue

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
    sample_h = calculate_robust_margin(raw_page_height, "height")
    
    # ✅ 为页码留出安全区域
    # 如果检测到页码，给边距增加10%的安全缓冲
    page_number_detected = any(raw_top_margins) or any(raw_bottom_margins)
    if page_number_detected:
        suggested_top_pts = int(suggested_top_pts * 1.1)  # 多裁10%作为缓冲
        print(f"   📄 检测到页码，增加安全缓冲: Top +10% -> {suggested_top_pts}")

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
    import fitz
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