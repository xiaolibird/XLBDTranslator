"""
Markdown 渲染器
负责将 ContentSegment 列表渲染为最终的 Markdown 文件
专注数据读取和字符串生成，不涉及业务逻辑
"""
from pathlib import Path
from typing import List, Optional

from ..core.schema import ContentSegment, Settings, SegmentList


class MarkdownRenderer:
    """
    Markdown 渲染器

    职责：纯数据渲染
    - 读取 ContentSegment 数据
    - 根据数据生成 Markdown 字符串
    - 不涉及任何业务逻辑处理
    """

    def __init__(self, settings: Settings):
        self.settings = settings

        # 渲染配置（从 settings 读取）
        self.retain_original = self._get_retain_original_setting()
        self.render_page_markers = self._get_page_markers_setting()

        # Markdown 格式模板
        self.templates = {
            'document_title': "# {title}\n\n---\n\n",
            'chapter_header': "\n\n{hashes} 📖 {title}\n\n",
            'page_marker': "\n\n###### --- 原文第 {page} 页 --- \n\n",
            'segment_marker': "\n\n🔖 **Segment {id}**\n",
            'image_segment': "\n\n![Segment {id}]({path})",
            'image_caption': "\n> 💡 **图注/内容译文**\n> {caption}",
            'image_footer': "\n\n🔖 **Segment {id}** (Image)\n",
            'section_separator': "\n\n---",
            'original_text': "原文：{text}",
            'translated_text_first': "> 译文：{text}",
            'translated_text_continue': ">       {text}",
            'translated_only': "> {text}",
            'markdown_header': "\n{header}\n",
        }

    def _get_retain_original_setting(self) -> bool:
        """从 settings 获取是否保留原文的配置"""
        try:
            return bool(self.settings.document.retain_original)
        except AttributeError:
            return False

    def _get_page_markers_setting(self) -> bool:
        """从 settings 获取是否显示页码标记的配置"""
        try:
            return bool(self.settings.processing.render_page_markers)
        except AttributeError:
            return True

    def render_to_file(self, segments: SegmentList, output_path: Path, title: str = "Document") -> None:
        """
        将片段列表渲染到 Markdown 文件

        Args:
            segments: 要渲染的片段列表
            output_path: 输出文件路径
            title: 文档标题
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, 'w', encoding='utf-8') as f:
            # 写入文档标题
            f.write(self.templates['document_title'].format(title=title))

            # 逐个渲染片段
            for segment in segments:
                markdown_content = self.render_segment(segment)
                f.write(markdown_content)

    def render_to_string(self, segments: SegmentList, title: str = "Document") -> str:
        """
        将片段列表渲染为 Markdown 字符串

        Args:
            segments: 要渲染的片段列表
            title: 文档标题

        Returns:
            完整的 Markdown 字符串
        """
        content_parts = [self.templates['document_title'].format(title=title)]

        for segment in segments:
            content_parts.append(self.render_segment(segment))

        return "".join(content_parts)

    def render_segment(self, segment: ContentSegment) -> str:
        """
        渲染单个 ContentSegment 为 Markdown 字符串

        Args:
            segment: 要渲染的片段

        Returns:
            Markdown 格式的字符串
        """
        if not isinstance(segment, ContentSegment):
            raise ValueError(f"Expected ContentSegment, got {type(segment)}")

        # 根据内容类型分流渲染
        if segment.content_type == "image":
            return self._render_image_segment(segment)
        else:
            return self._render_text_segment(segment)

    def _render_image_segment(self, segment: ContentSegment) -> str:
        """
        渲染图片类型的片段

        Args:
            segment: 图片片段

        Returns:
            Markdown 字符串
        """
        parts = []

        # 图片显示
        if segment.image_path:
            parts.append(self.templates['image_segment'].format(
                id=segment.segment_id,
                path=segment.image_path
            ))

            # 图片译文/图注
            if segment.translated_text and segment.translated_text.strip():
                clean_caption = self._clean_text(segment.translated_text)
                parts.append(self.templates['image_caption'].format(caption=clean_caption))

        # 图片片段结尾标记
        parts.append(self.templates['image_footer'].format(id=segment.segment_id))
        parts.append(self.templates['section_separator'])

        return "".join(parts)

    def _render_text_segment(self, segment: ContentSegment) -> str:
        """
        渲染文本类型的片段

        Args:
            segment: 文本片段

        Returns:
            Markdown 字符串
        """
        parts = []

        # 结构层：章节标题或页码标记
        structure_content = self._render_structure_elements(segment)
        if structure_content:
            parts.append(structure_content)

        # 内容层：文本翻译
        content = self._render_text_content(segment)
        if content:
            parts.append(content)

        return "".join(parts)

    def _render_structure_elements(self, segment: ContentSegment) -> str:
        """
        渲染结构元素（章节标题、页码标记）

        Args:
            segment: 片段对象

        Returns:
            结构元素的 Markdown 字符串
        """
        parts = []

        # 章节标题（优先级最高）
        if segment.is_new_chapter and segment.chapter_title:
            level = max(1, min(segment.toc_level or 1, 5))  # 限制在合理范围内
            hashes = "#" * (level + 1)  # level 1 -> ##, level 2 -> ### 等
            parts.append(self.templates['chapter_header'].format(
                hashes=hashes,
                title=self._clean_text(segment.chapter_title)
            ))

        # 页码标记（仅在非章节开头且配置允许时显示）
        elif (segment.page_index is not None and
              not segment.is_new_chapter and
              self.render_page_markers):
            parts.append(self.templates['page_marker'].format(
                page=segment.page_index + 1  # 转换为 1-based 显示
            ))

        return "".join(parts)

    def _render_text_content(self, segment: ContentSegment) -> str:
        """
        渲染文本内容

        Args:
            segment: 文本片段

        Returns:
            文本内容的 Markdown 字符串
        """
        parts = []

        # 片段标记
        parts.append(self.templates['segment_marker'].format(id=segment.segment_id))

        # 根据配置选择渲染模式
        if self.retain_original:
            content = self._render_bilingual_content(segment)
        else:
            content = self._render_translation_only_content(segment)

        parts.append(content)
        parts.append(self.templates['section_separator'])

        return "".join(parts)

    def _render_bilingual_content(self, segment: ContentSegment) -> str:
        """
        渲染双语对照内容

        Args:
            segment: 片段对象

        Returns:
            双语对照的 Markdown 字符串
        """
        parts = []

        original_text = self._clean_text(segment.original_text or "")
        translated_text = self._clean_text(segment.translated_text or "")

        # 按段落分割
        orig_paras = self._split_into_paragraphs(original_text)
        trans_paras = self._split_into_paragraphs(translated_text)

        # 对齐渲染每个段落
        for i in range(max(len(orig_paras), len(trans_paras))):
            block_parts = []

            # 原文段落
            if i < len(orig_paras) and orig_paras[i].strip():
                block_parts.append(self.templates['original_text'].format(
                    text=orig_paras[i].strip()
                ))

            # 译文段落
            if i < len(trans_paras) and trans_paras[i].strip():
                trans_lines = trans_paras[i].split('\n')
                for j, line in enumerate(trans_lines):
                    if line.strip():
                        if self._is_markdown_header(line):
                            block_parts.append(self.templates['markdown_header'].format(header=line))
                        elif j == 0:
                            block_parts.append(self.templates['translated_text_first'].format(text=line))
                        else:
                            block_parts.append(self.templates['translated_text_continue'].format(text=line))

            if block_parts:
                parts.append("\n".join(block_parts) + "\n")

        return "".join(parts)

    def _render_translation_only_content(self, segment: ContentSegment) -> str:
        """
        渲染纯译文内容

        Args:
            segment: 片段对象

        Returns:
            纯译文的 Markdown 字符串
        """
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
        清理文本内容

        Args:
            text: 原始文本

        Returns:
            清理后的文本
        """
        if not text:
            return ""

        # 移除多余的换行符和回车符
        text = text.replace('\r', '')

        # 处理转义的换行符
        text = text.replace('\\n', '\n')

        return text.strip()

    def _split_into_paragraphs(self, text: str) -> List[str]:
        """
        将文本按段落分割

        Args:
            text: 要分割的文本

        Returns:
            段落列表
        """
        if not text:
            return []

        # 按双换行符分割段落
        paragraphs = text.split('\n\n')

        # 过滤掉空段落
        return [p for p in paragraphs if p.strip()]

    def _is_markdown_header(self, line: str) -> bool:
        """
        检查是否为 Markdown 标题

        Args:
            line: 要检查的行

        Returns:
            是否为标题
        """
        return line.strip().startswith('#')
