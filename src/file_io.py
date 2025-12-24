import os
import re
import datetime
from bs4 import BeautifulSoup


from pathlib import Path

def clean_filename(filename):
    """清理文件名，去除特殊字符"""
    return re.sub(r'[\\/*?:"<>|]', "", filename).replace(" ", "_")

def create_output_directory(input_file_path: str, mode_name: str, base_dir: Path) -> Path:
    """创建并返回一个基于日期、文件名和模式的项目专属输出目录。"""
    date_str = datetime.datetime.now().strftime("%Y%m%d")
    base_name = Path(input_file_path).stem
    safe_name = clean_filename(base_name)
    safe_mode = clean_filename(mode_name)
    
    folder_name = f"{date_str}_{safe_name}_{safe_mode}"
    project_path = base_dir / folder_name
    
    # exist_ok=True 确保如果目录已存在，代码不会报错
    project_path.mkdir(parents=True, exist_ok=True)
    
    print(f"📂 项目工作目录位于: {project_path}")
        
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
            
        # 1. 尝试匹配新格式: 🔖 **Segment 101**
        ids = re.findall(r'🔖 \*\*Segment (\d+)\*\*', content)
        
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

def render_segment_to_markdown(original_seg: dict, trans_text: str, retain_original: bool) -> str:
    seg_id = original_seg['id']
    original_text = (original_seg.get('text', '') or "").replace('\r', '').strip()
    
    if original_text.startswith("<<IMAGE_PATH"):
        # 提取路径：去掉前缀标记，去掉可能的后缀 '>>'
        # 假设格式为 <<IMAGE_PATH::/path/to/image.png>> 或 <<IMAGE_PATH /path...
        raw_path = original_text.replace("<<IMAGE_PATH", "").replace("::", "").replace(">>", "").strip()
        name, ext = os.path.splitext(raw_path)
        image_path = f"{name}_cropped{ext}"
        
        # 构建 Markdown 图片语法
        # 格式: ![Segment ID](文件路径)
        md_block = [f"![Image Segment {seg_id}]({image_path})"]
        
        # 如果图片有对应的翻译文本（如图注或OCR翻译），展示在图片下方
        if trans_text:
            # 清理一下翻译文本
            clean_trans = trans_text.replace('\\n', '\n').replace('\\"', '"').strip()
            md_block.append(f"\n> 💡 **图注/内容译文**：{clean_trans}")
        
        md_block.append(f"\n\n🔖 **Segment {seg_id}** (Image)\n---")
        return "\n".join(md_block)

    # 1. 基础清理：处理转义符和引号
    trans_text = trans_text.replace('\\n', '\n').replace('\\"', '"').strip()

    # 2. 标记处理逻辑 (Regex)
    # 章节：## [Chapter X] -> ## 📖 Chapter X
    def sub_chapter(m): return f"{m.group(1)}📖 {m.group(2)}"
    # 页码：###### [Page: 10] -> ###### --- 原文第 11 页 ---
    def sub_page(m): return f"\n\n###### --- 原文第 {int(m.group(1)) + 1} 页 --- \n\n"

    # 3. 处理译文 (保留并美化标记)
    trans_text = re.sub(r'(^##\s*)(\[.*?\])', sub_chapter, trans_text, flags=re.MULTILINE)
    trans_text = re.sub(r'######\s*\[Page:\s*(\d+)\]', sub_page, trans_text)

    # 4. 处理原文 (关键：彻底移除标记，防止双语对照时重复输出)
    if retain_original:
        # 在原文中，将这些结构性标记替换为空，只保留纯文本内容
        original_text = re.sub(r'(^##\s*)(\[.*?\])', '', original_text, flags=re.MULTILINE)
        original_text = re.sub(r'######\s*\[Page:\s*\d+\]', '', original_text)

    # 5. 生成预览与元数据
    # 预览去除 Markdown 符号，仅取前70字
    preview = re.sub(r'[#*-]', '', original_text).replace('\n', ' ').strip()[:70]
    header_block = f"\n\n🔖 **Segment {seg_id}**\n"
    if not retain_original:
        header_block += f'_Original: "{preview}..."_\n\n'

    # 6. 内容排版 (核心渲染)
    output_blocks = [header_block]
    
    # 辅助 lambda：清理行并去除尾部反斜杠
    clean_split = lambda t: [l.rstrip('\\').strip() for l in t.split('\n')]

    if retain_original:
        # --- 双语模式 ---
        # 按照双换行分段，对齐段落
        orig_paras = [p for p in original_text.split('\n\n') if p.strip()]
        trans_paras = [p for p in trans_text.split('\n\n') if p.strip()]
        
        for i in range(max(len(orig_paras), len(trans_paras))):
            block = []
            p_orig = clean_split(orig_paras[i]) if i < len(orig_paras) else []
            p_trans = clean_split(trans_paras[i]) if i < len(trans_paras) else []

            # 渲染原文 (已移除 Tag，全是纯文本)
            if p_orig:
                block.append(f"原文：{p_orig[0]}")
                block.extend([f"      {line}" for line in p_orig[1:]])
            
            # 渲染译文 (包含美化后的 Tag)
            if p_trans:
                for j, line in enumerate(p_trans):
                    # 如果是标题或分隔符，顶格写，保持 Markdown 格式
                    if line.startswith('#'):
                        block.append(f"\n{line}\n")
                    # 普通文本添加引用前缀
                    elif j == 0:
                        block.append(f"> 译文：{line}")
                    else:
                        block.append(f">       {line}")
            
            if block: output_blocks.append("\n".join(block))

    else:
        # --- 纯译文模式 ---
        lines = clean_split(trans_text)
        formatted = []
        for line in lines:
            # 标题/分隔符独立成行，正文放入引用块
            if line.startswith('#'):
                formatted.append(f"\n{line}\n")
            else:
                formatted.append(f"> {line}" if line else ">")
        output_blocks.append("\n".join(formatted))

    return "\n\n".join(output_blocks) + "\n\n---"

def recover_context_from_file(md_path, max_chars: int = 2000) -> str:
    """
    从文件末尾安全地恢复上下文，避免因多字节字符截断导致错误。
    它会读取比需求稍多的数据，然后按行分割，确保只返回完整的行。
    """
    if not os.path.exists(md_path):
        return ""
    
    try:
        with open(md_path, 'rb') as f: # 以二进制模式打开以精确定位
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            
            if file_size == 0:
                return ""
            
            read_size = min(file_size, max_chars + 512) # 多读一点以保证能找到换行符
            f.seek(-read_size, os.SEEK_END)
            
            # 读取二进制数据并解码
            tail_bytes = f.read(read_size)
            tail_text = tail_bytes.decode('utf-8', errors='ignore')

        # 从后向前截取所需长度的完整文本
        # 这比复杂的逐行读取更高效且同样安全
        return tail_text[-max_chars:]

    except Exception as e:
        print(f"⚠️ 恢复上下文失败: {e}")
        return ""

def is_likely_chinese(text):
    """简单检测是否包含中文字符"""
    for char in text:
        if '\u4e00' <= char <= '\u9fff':
            return True
    return False