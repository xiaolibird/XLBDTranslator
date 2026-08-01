"""
Markdown 渲染器
负责将 ContentSegment 列表渲染为最终的 Markdown 文件
专注数据读取和字符串生成，不涉及业务逻辑
"""
import re
from pathlib import Path
from typing import List, Optional, Tuple

from ..core.schema import ContentSegment, Settings, SegmentList


class MarkdownRenderer:
    """
    Markdown 渲染器

    职责：纯数据渲染
    - 读取 ContentSegment 数据
    - 根据数据生成 Markdown 字符串
    - 不涉及任何业务逻辑处理
    """

    # 层级 emoji 映射（不同层级展现不同差别）
    LEVEL_EMOJIS = {
        1: "📚",  # 一级标题 - 书籍
        2: "📖",  # 二级标题 - 打开的书
        3: "📄",  # 三级标题 - 文档
        4: "📝",  # 四级标题 - 备忘录
        5: "📌",  # 五级标题 - 图钉
    }
    
    # 面包屑/全页码模式 emoji（统一使用书签）
    BREADCRUMB_EMOJI = "🧭"
    PAGE_ONLY_EMOJI = "🔖"

    def __init__(self, settings: Settings):
        self.settings = settings

        # 渲染配置（从 settings 读取）
        self.retain_original = self._get_retain_original_setting()
        self.render_page_markers = self._get_page_markers_setting()

        # Markdown 格式模板
        # 注意：不再使用 blockquote (>) 格式，改用普通段落
        # 避免 WeasyPrint 在渲染 blockquote 时出现乱码/显示异常问题
        self.templates = {
            'document_title': "# {translated_title} - {original_title}\n\n---\n\n",
            'chapter_header': "\n\n{hashes} {emoji} {title}\n\n",
            'page_marker': "\n\n###### --- 原文第 {page} 页 --- \n\n",
            'image_segment': "\n\n![Segment {id}]({path})",
            'image_caption': "\n> 💡 **图注/内容译文**\n> {caption}",
            'image_footer': "\n",
            'section_separator': "\n\n---",
            'original_text': '> {text}',
            'translated_text': '{text}',
            'translated_only': "{text}",
            'markdown_header': "\n{header}\n",
        }

    def _get_retain_original_setting(self) -> bool:
        """从 settings 获取是否保留原文的配置"""
        try:
            return bool(self.settings.processing.retain_original)
        except AttributeError:
            return False

    def _get_page_markers_setting(self) -> bool:
        """从 settings 获取是否显示页码标记的配置"""
        try:
            enabled = bool(self.settings.processing.render_page_markers)
        except AttributeError:
            enabled = True

        # For EPUB sources, page markers (PDF page numbers) are not meaningful.
        try:
            doc_path = getattr(self.settings.files, 'document_path', None)
            if doc_path and getattr(doc_path, 'suffix', '').lower() == '.epub':
                return False
        except Exception:
            pass

        return enabled

    def _detect_title_mode(self, segments: SegmentList) -> str:
        """
        检测标题模式：面包屑、全页码、还是正常层级
        
        判断逻辑（基于 settings 配置和 TOC 结构，而非文本内容）：
        1. 如果 settings.processing.use_breadcrumb = True → 'breadcrumb'
        2. 如果没有任何 is_new_chapter=True 的 segment → 'page_only' (无 TOC 回退模式)
        3. 其他情况 → 'normal' (正常层级模式)
        
        Returns:
            'breadcrumb' | 'page_only' | 'normal'
        """
        # 1. 检查 settings 中的 breadcrumb 配置
        try:
            use_breadcrumb = self.settings.processing.use_breadcrumb
            if use_breadcrumb:
                return 'breadcrumb'
        except AttributeError:
            pass
        
        # 2. 检查 TOC 结构：是否有任何章节信息
        if not segments:
            return 'normal'
        
        has_chapter_info = any(
            seg.is_new_chapter and seg.chapter_title 
            for seg in segments
        )
        
        # 无章节信息 = 纯页码回退模式（PDF 无 TOC 的情况）
        if not has_chapter_info:
            return 'page_only'
        
        # 3. 正常层级模式
        return 'normal'

    def _get_level_emoji(self, level: int, title_mode: str) -> str:
        """
        根据层级和模式获取对应的 emoji
        """
        if title_mode == 'breadcrumb':
            return self.BREADCRUMB_EMOJI
        elif title_mode == 'page_only':
            return self.PAGE_ONLY_EMOJI
        else:
            return self.LEVEL_EMOJIS.get(level, "📄")

    def render_to_file(self, segments: SegmentList, output_path: Path, 
                       title: str = "Document", translated_title: str = "") -> None:
        """
        将片段列表渲染到 Markdown 文件

        Args:
            segments: 要渲染的片段列表
            output_path: 输出文件路径
            title: 原始文档标题
            translated_title: 翻译后的文档标题
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        content = self.render_to_string(segments, title, translated_title)
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(content)

    def render_to_string(self, segments: SegmentList, title: str = "Document", 
                         translated_title: str = "") -> str:
        """
        将片段列表渲染为 Markdown 字符串

        Args:
            segments: 要渲染的片段列表
            title: 原始文档标题
            translated_title: 翻译后的文档标题

        Returns:
            完整的 Markdown 字符串
        """
        # 检测标题模式
        title_mode = self._detect_title_mode(segments)
        
        # 使用翻译后的标题，如果没有则使用原标题
        display_translated = translated_title if translated_title else title
        
        content_parts = [self.templates['document_title'].format(
            translated_title=display_translated,
            original_title=title
        )]

        for segment in segments:
            content_parts.append(self.render_segment(segment, title_mode))

        return "".join(content_parts)

    def render_segment(self, segment: ContentSegment, title_mode: str = 'normal') -> str:
        """
        渲染单个 ContentSegment 为 Markdown 字符串

        Args:
            segment: 要渲染的片段
            title_mode: 标题模式

        Returns:
            Markdown 格式的字符串
        """
        if not isinstance(segment, ContentSegment):
            raise ValueError(f"Expected ContentSegment, got {type(segment)}")

        # 根据内容类型分流渲染
        if segment.content_type == "image":
            return self._render_image_segment(segment)
        else:
            return self._render_text_segment(segment, title_mode)

    def _render_image_segment(self, segment: ContentSegment) -> str:
        """
        渲染图片类型的片段
        """
        parts = []

        if segment.image_path:
            parts.append(self.templates['image_segment'].format(
                id=segment.segment_id,
                path=segment.image_path
            ))

            if segment.translated_text and segment.translated_text.strip():
                clean_caption = self._clean_text(segment.translated_text)
                parts.append(self.templates['image_caption'].format(caption=clean_caption))

        parts.append(self.templates['image_footer'].format(id=segment.segment_id))
        parts.append(self.templates['section_separator'])

        return "".join(parts)

    def _render_text_segment(self, segment: ContentSegment, title_mode: str = 'normal') -> str:
        """
        渲染文本类型的片段
        """
        parts = []

        # 结构层：章节标题或页码标记
        structure_content = self._render_structure_elements(segment, title_mode)
        if structure_content:
            parts.append(structure_content)

        # 内容层：文本翻译
        content = self._render_text_content(segment)
        if content:
            parts.append(content)

        return "".join(parts)

    def _render_structure_elements(self, segment: ContentSegment, title_mode: str = 'normal') -> str:
        """
        渲染结构元素（章节标题、页码标记）
        
        注意：章节标题和页码标记后都需要添加分割线，保持格式一致性
        """
        parts = []

        # 章节标题（优先级最高）
        if segment.is_new_chapter and segment.chapter_title:
            level = max(1, min(segment.toc_level or 1, 5))
            hashes = "#" * (level + 1)
            emoji = self._get_level_emoji(level, title_mode)
            parts.append(self.templates['chapter_header'].format(
                hashes=hashes,
                emoji=emoji,
                title=self._clean_text(segment.chapter_title)
            ))
            # 章节标题后添加分割线
            parts.append(self.templates['section_separator'])

        # 页码标记（仅在非章节开头且配置允许时显示，永远使用 h6）
        elif (segment.page_index is not None and
              not segment.is_new_chapter and
              self.render_page_markers):
            parts.append(self.templates['page_marker'].format(
                page=segment.page_index + 1
            ))
            # 页码标记后也添加分割线
            parts.append(self.templates['section_separator'])

        return "".join(parts)

    def _render_text_content(self, segment: ContentSegment) -> str:
        """
        渲染文本内容（不再包含 Segment 标记）
        PDF 渲染器将直接从 SegmentList 获取页码信息
        
        注意：双语模式下，分割线已经在每对译文-原文后添加，这里不再重复添加
        """
        # 检查是否有实际的文本内容
        has_content = (segment.original_text and segment.original_text.strip()) or \
                      (segment.translated_text and segment.translated_text.strip())
        
        if not has_content:
            return ""
        
        # 根据配置选择渲染模式
        if self.retain_original:
            # 双语模式：分割线已经在 _render_bilingual_content 中添加
            return self._render_bilingual_content(segment)
        else:
            # 纯译文模式：在最后添加分割线
            content = self._render_translation_only_content(segment)
            return content + self.templates['section_separator']

    def _render_bilingual_content(self, segment: ContentSegment) -> str:
        """渲染双语对照内容（纯 Markdown 语法）

        格式：段内每对为 译文正文 + 原文引用块；分割线只在**整个 segment
        末尾**加一条。此前每对都加 `---`，导致 PDF 侧按 <hr> 切出的
        content-block 数量多于 segment 数，页码/页眉从文档开头就系统性
        漂移（页码列表是按 segment 建的）。对内的视觉分隔由引用块转换出的
        bilingual-separator 承担。
        """
        parts = []

        original_text = self._clean_text(segment.original_text or "")
        translated_text = self._clean_text(segment.translated_text or "")

        orig_paras = self._split_into_paragraphs(original_text)
        trans_paras = self._split_into_paragraphs(translated_text)

        for i in range(max(len(orig_paras), len(trans_paras))):
            # 1. 先输出译文（正文）
            if i < len(trans_paras) and trans_paras[i].strip():
                trans_text = trans_paras[i].strip()
                if self._is_markdown_header(trans_text):
                    parts.append(self.templates['markdown_header'].format(header=trans_text))
                else:
                    parts.append(self.templates['translated_text'].format(text=trans_text))
                parts.append("\n")

            # 2. 再输出原文（Quote 引用块，后续会被转换为 .original 样式）
            if i < len(orig_paras) and orig_paras[i].strip():
                # 将原文的每一行都加上 > 前缀
                orig_lines = orig_paras[i].strip().split('\n')
                quoted_lines = ['> ' + line if line.strip() else '>' for line in orig_lines]
                parts.append('\n'.join(quoted_lines))
                parts.append("\n\n")

        # 分割线：每个 segment 恰好一条（与纯译文模式的语义对齐）
        parts.append(self.templates['section_separator'])
        parts.append("\n")

        return "".join(parts)

    def _render_translation_only_content(self, segment: ContentSegment) -> str:
        """渲染纯译文内容"""
        translated_text = self._clean_text(segment.translated_text or "")
        lines = translated_text.split('\n')
        formatted_lines = []

        for line in lines:
            if line.strip():
                if self._is_markdown_header(line):
                    formatted_lines.append(self.templates['markdown_header'].format(header=line))
                else:
                    formatted_lines.append(self.templates['translated_only'].format(text=line))
            else:
                formatted_lines.append(self.templates['translated_only'].format(text=""))

        return "\n".join(formatted_lines)

    def _clean_text(self, text: str) -> str:
        """
        清理文本内容 - 保留缩进版
        
        处理：
        1. 移除 \r 回车符
        2. 转义的换行符还原
        3. 将前导空格转换为安全的不间断空格 (\u00a0)
           这样保留原始缩进格式，同时避免 CSS 渲染问题
        4. 全角空格统一转换为半角
        """
        if not text:
            return ""
        
        import re
        
        # 基础清理
        text = text.replace('\r', '')
        text = text.replace('\\n', '\n')
        
        # 处理每一行
        lines = text.split('\n')
        cleaned_lines = []
        
        for line in lines:
            # 1. 提取前导空白字符的数量
            leading_spaces = len(line) - len(line.lstrip(' \t\u3000'))
            
            # 2. 将前导空白转换为不间断空格 (\u00a0)
            #    这种空格在 HTML/PDF 中不会被折叠，保留视觉缩进
            if leading_spaces > 0:
                # 每 2 个原始空格 = 1 个不间断空格（适度压缩）
                safe_indent = '\u00a0' * (leading_spaces // 2 + 1)
                cleaned_line = safe_indent + line.lstrip(' \t\u3000')
            else:
                cleaned_line = line
            
            # 3. 将行内的全角空格转为普通空格
            cleaned_line = cleaned_line.replace('\u3000', ' ')
            
            # 4. 合并行内连续多个普通空格为单个（不影响不间断空格）
            cleaned_line = re.sub(r'[ \t]{2,}', '  ', cleaned_line)
            
            cleaned_lines.append(cleaned_line)
        
        # 重新组合，保留段落换行
        text = '\n'.join(cleaned_lines)
        
        # 移除连续超过 2 个的换行
        text = re.sub(r'\n{3,}', '\n\n', text)
        
        return text.strip()

    def _split_into_paragraphs(self, text: str) -> List[str]:
        """将文本按段落分割"""
        if not text:
            return []
        paragraphs = text.split('\n\n')
        return [p for p in paragraphs if p.strip()]

    def _is_markdown_header(self, line: str) -> bool:
        """检查是否为 Markdown 标题"""
        return line.strip().startswith('#')
