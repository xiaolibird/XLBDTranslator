"""
解析器工具函数
包含 HTML 清理、PDF 工具等通用功能
"""
import re
from pathlib import Path
from typing import Dict, Any, List, Tuple
from bs4 import BeautifulSoup
from urllib.parse import unquote

def clean_html_text(text: str) -> str:
    """清理 HTML 文本"""
    if not text or not text.strip():
        return ""

    # 移除多余的换行和空格
    text = re.sub(r'\n\s*\n', '\n\n', text.strip())

    # 移除行首行尾的空白字符，但保留段落间的换行
    lines = text.split('\n')
    cleaned_lines = []
    for line in lines:
        cleaned_line = line.strip()
        if cleaned_line:  # 只保留非空行
            cleaned_lines.append(cleaned_line)

    return '\n\n'.join(cleaned_lines)


def extract_text_from_html(html_content: str) -> str:
    """从 HTML 内容提取纯文本"""
    try:
        soup = BeautifulSoup(html_content, 'html.parser')

        # 移除脚本和样式
        for script in soup(["script", "style"]):
            script.extract()

        # 获取文本内容
        text = soup.get_text(separator='\n', strip=True)

        return clean_html_text(text)
    except Exception:
        return ""


def is_likely_chinese(text: str) -> bool:
    """简单检测是否包含中文字符"""
    for char in text:
        if '\u4e00' <= char <= '\u9fff':
            return True
    return False


def process_unified_toc(
    raw_toc_items: List[Dict[str, Any]],
    use_breadcrumb: bool = True
) -> Dict[Any, Dict[str, Any]]:
    """
    统一处理 TOC 项目
    生成章节映射表 {key: {"title": str, "level": int}}
    """
    chapter_map = {}
    title_stack = []  # 路径栈

    for item in raw_toc_items:
        level = item['level']
        raw_title = item['title'].strip()
        key = item['key']

        # 1. 维护栈：保留父级路径
        if len(title_stack) >= level:
            title_stack = title_stack[:level - 1]
        title_stack.append(raw_title)

        # 2. 策略应用
        if use_breadcrumb:
            final_title = " > ".join(title_stack)
            final_level = 1  # 面包屑强制层级为 1 (H2)
        else:
            final_title = raw_title
            final_level = level  # 保留原始语义层级

        # 3. 写入 Map (防覆盖：保留第一次出现)
        if key not in chapter_map:
            chapter_map[key] = {
                "title": final_title,
                "level": final_level
            }

    return chapter_map


def parse_epub_toc(toc, level: int = 1) -> List[Dict[str, Any]]:
    """解析 EPUB 目录结构"""
    items = []

    for node in toc:
        # 兼容 ebooklib 的两种节点格式
        entry = node[0] if isinstance(node, (list, tuple)) else node
        children = node[1] if isinstance(node, (list, tuple)) and len(node) > 1 else []

        # 检查是否有有效的 href
        if hasattr(entry, 'href') and entry.href:
            # URL-decode href so keys are comparable to spine item names
            # Also normalize path by stripping leading slashes
            href_key = unquote(entry.href.split('#')[0]).lstrip('/')
            
            items.append({
                'level': level,
                'title': entry.title or "Untitled",
                'key': href_key  # key 是文件名
            })

        if children:
            items.extend(parse_epub_toc(children, level + 1))

    return items
