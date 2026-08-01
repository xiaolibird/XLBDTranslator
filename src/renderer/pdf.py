"""
PDF 渲染器
负责将 ContentSegment 列表渲染为 PDF 文件
直接从 SegmentList 获取信息，利用 toc_level 控制层级间距
"""
import re
from pathlib import Path
from typing import List, Optional, Dict, Tuple

from ..core.schema import ContentSegment, Settings, SegmentList
from ..utils.logger import get_logger


class PDFRenderer:
    """
    PDF 渲染器

    职责：将 ContentSegment 列表转换为 PDF 文件
    - 直接从 SegmentList 读取 page_index, toc_level 等信息
    - 根据 toc_level 计算层级间距（h5 最近，h2 最远）
    - 页码标记显示在 blockquote 右上方外侧
    - 页码对于 markdown 是 h6，对 PDF 只渲染不进入章节信息
    """

    # 层级间距配置（单位：em）
    TOC_LEVEL_SPACING = {
        2: 0.20,  # h2 最远
        3: 0.15,
        4: 0.10,
        5: 0.05,  # h5 最近
    }

    def __init__(self, settings: Settings):
        self.settings = settings

        self.logger = get_logger(__name__)

        # CSS 文件路径（动态定位）
        self.css_path = self._locate_css_file()

    def _locate_css_file(self, version: str = "desktop") -> Optional[Path]:
        """定位 CSS 文件
        
        Args:
            version: 'desktop' 或 'mobile'，指定要使用的CSS版本
        """
        # 根据版本选择 CSS 文件名
        css_filename = "pdf_style.css" if version == "desktop" else "pdf_style_mobile.css"
        
        # 优先级：config/ -> assets/ -> 项目根目录
        candidates = [
            Path(__file__).parent.parent.parent / "config" / css_filename,  # 配置目录（推荐）
            Path(__file__).parent.parent.parent / "assets" / css_filename,
            Path(__file__).parent.parent.parent / css_filename,  # 项目根目录（向后兼容）
        ]

        for css_path in candidates:
            if css_path.exists():
                return css_path

        return None

    def render_to_file(self, segments: SegmentList, output_path: Path,
                       title: str = "Document", translated_title: str = "",
                       version: str = "desktop", generate_both: bool = False) -> List[Path]:
        """
        将片段列表渲染到 PDF 文件 (优化版，支持高阶 CSS 渲染)

        直接从 SegmentList 获取 page_index, toc_level 等信息，
        不再完全依赖 markdown 生成的内容

        Args:
            segments: 片段列表
            output_path: 输出路径（桌面版）
            title: 文档标题
            translated_title: 翻译后的标题
            version: 'desktop' 或 'mobile'
            generate_both: 是否同时生成桌面版和移动版

        Returns:
            实际成功生成的 PDF 路径列表（渲染失败的版本不在其中——调用方
            必须按此判断成败，内部异常已降级为日志不再上抛）
        """
        produced: List[Path] = []
        if generate_both:
            # 生成桌面版
            if self._render_single_version(segments, output_path, title, translated_title, "desktop"):
                produced.append(output_path)
            # 生成移动版
            mobile_path = output_path.parent / f"{output_path.stem}_mobile{output_path.suffix}"
            if self._render_single_version(segments, mobile_path, title, translated_title, "mobile"):
                produced.append(mobile_path)
        else:
            # 只生成指定版本
            if self._render_single_version(segments, output_path, title, translated_title, version):
                produced.append(output_path)
        return produced

    def _render_single_version(self, segments: SegmentList, output_path: Path,
                              title: str, translated_title: str, version: str) -> bool:
        """
        渲染单个版本的 PDF

        Returns:
            是否成功生成（失败已内部记录日志并降级，不上抛）
        """
        # 动态加载指定版本的 CSS
        css_path = self._locate_css_file(version)
        version_label = "桌面版" if version == "desktop" else "移动版(iPhone)"
        
        try:
            # 1. 延迟导入依赖，确保环境缺失时不会直接崩溃
            import markdown2
            from weasyprint import HTML, CSS
            from weasyprint.text.fonts import FontConfiguration

            # 2. 从 SegmentList 构建增强的元数据映射
            segment_metadata = self._build_segment_metadata(segments)
            
            # 3. 生成 Markdown 内容
            from .markdown import MarkdownRenderer
            md_renderer = MarkdownRenderer(self.settings)
            markdown_content = md_renderer.render_to_string(segments, title, translated_title)

            # 4. 提取页码信息并清理 Segment 标记
            clean_markdown, page_map = self._extract_page_numbers_and_clean(markdown_content)

            # 5. 转换为 HTML (增强扩展支持)
            # code-friendly 防止下划线误伤样式，header-ids 支持 string-set 抓取标题
            html_body = markdown2.markdown(
                clean_markdown,
                extras=[
                    "fenced-code-blocks", 
                    "tables", 
                    "footnotes", 
                    "break-on-newline", 
                    "header-ids",
                    "code-friendly",
                    "cuddled-lists"
                ]
            )

            # 5.4. 转换 blockquote 为 .original 样式
            html_body = self._convert_blockquote_to_original(html_body)

            # 5.5. 后处理：为 blockquote 添加页码属性和层级间距
            html_body = self._enhance_blockquotes_with_metadata(html_body, segment_metadata)
            
            # 5.6. 处理层级标题间距（基于 toc_level）
            html_body = self._add_heading_spacing(html_body, segment_metadata)

            # 6. 生成 HTML 模板（使用动态CSS路径）
            display_title = translated_title if translated_title else title
            html_content = self._create_html_template(html_body, display_title, title, css_path)

            # 调试：保存 HTML 到临时文件
            # debug_html_path = output_path.parent / f"{output_path.stem}_debug.html"
            # debug_html_path.write_text(html_content, encoding='utf-8')
            # self.logger.info(f"🔍 调试 HTML 已保存: {debug_html_path}")

            # 7. 准备 PDF 渲染环境
            output_path.parent.mkdir(parents=True, exist_ok=True)
            
            # 初始化字体配置
            font_config = FontConfiguration()
            
            # 注意：CSS 已经内嵌到 HTML 模板的 <style> 标签中
            # 不再通过 stylesheets 参数重复传递，避免双重应用导致渲染冲突
            # 这是导致"拖动后才显示文字"问题的可能原因之一
            stylesheets = []

            if css_path and css_path.exists():
                self.logger.info(f"🎨 [{version_label}] CSS 样式已内嵌到 HTML: {css_path.name}")
            else:
                self.logger.warning(f"⚠️ [{version_label}] 未找到 CSS 样式表，PDF 将使用默认样式")

            # 8. 渲染 PDF
            # base_url 设为输出目录或项目根目录，确保图片相对路径解析正确
            # presentational_hints=False 避免 HTML 属性与 CSS 冲突
            # 重要：禁用 optimize_size 的 'fonts' 选项，避免中文字体子集化导致乱码
            HTML(string=html_content, base_url=str(output_path.parent)).write_pdf(
                output_path,
                stylesheets=stylesheets,
                font_config=font_config,
                presentational_hints=False,  # 减少样式冲突
                optimize_size=('images',)  # 仅优化图片，保留完整字体
            )

            self.logger.info(f"✅ [{version_label}] PDF 已成功生成: {output_path}")
            return True

        except ImportError as e:
            lib_name = str(e).split("'")[-2] if "'" in str(e) else "weasyprint/markdown2"
            self.logger.error(f"⚠️ PDF 导出跳过: 缺少 Python 依赖库 - {lib_name}")
            self.logger.error("💡 请运行: pip install weasyprint markdown2")
            self.logger.error("📄 降级处理: 仅生成 Markdown 文件")
            return False

        except Exception as e:
            error_msg = str(e)
            # 针对 WeasyPrint 常见的系统底层库缺失报错进行诊断
            if any(lib in error_msg for lib in ["libgobject", "cairo", "pango", "gdk-pixbuf"]):
                self.logger.error("⚠️ PDF 导出跳过: 缺少必要的系统底层库 (Pango/Cairo)")
                self.logger.error("💡 macOS 请运行: brew install cairo pango gdk-pixbuf libffi")
                self.logger.error("💡 提示: conda 环境需带 DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib 运行")
                self.logger.error("💡 Ubuntu 请运行: apt-get install libpango1.0-dev libcairo2-dev")
            else:
                self.logger.error(f"⚠️ PDF 导出失败: {error_msg}")
            self.logger.error("📄 降级处理: 仅生成 Markdown 文件")
            return False

    def _build_segment_metadata(self, segments: SegmentList) -> Dict[int, Dict]:
        """
        从 SegmentList 构建元数据映射
        
        Returns:
            {segment_index: {
                'page_index': int,
                'toc_level': int,
                'is_new_chapter': bool,
                'chapter_title': str
            }}
        """
        metadata = {}
        for i, segment in enumerate(segments):
            metadata[i] = {
                'page_index': segment.page_index,
                'toc_level': segment.toc_level or 0,
                'is_new_chapter': segment.is_new_chapter,
                'chapter_title': segment.chapter_title or '',
                'segment_id': segment.segment_id
            }
        return metadata

    def _extract_page_numbers_and_clean(self, markdown_content: str):
        """
        从 markdown 内容中提取页码信息并清理标记
        
        Returns:
            (clean_markdown, page_map): 清理后的 markdown 和页码映射
            page_map: {marker_index: page_number}
        """
        import re
        
        page_map = {}
        marker_index = 0
        
        # 查找所有页码标记
        page_pattern = r'\n*#{6}\s*---\s*原文第\s*(\d+)\s*页\s*---\s*\n*'
        
        def replace_with_marker(match):
            nonlocal marker_index
            page_num = match.group(1)
            page_map[marker_index] = page_num
            # self.logger.info(f"📍 找到页码标记: 第 {page_num} 页 (索引 {marker_index})")
            marker_index += 1
            return f'\n\n<!-- PAGE_MARKER_{marker_index - 1} -->\n\n'
        
        # 替换页码标记为注释标记
        clean_markdown = re.sub(page_pattern, replace_with_marker, markdown_content)
        
        self.logger.info(f"📊 总共提取了 {len(page_map)} 个页码标记")
        
        # 清理 Segment 标记
        segment_pattern = r"🔖\s*\*\*Segment\s+\d+\*\*(?: \(Image\))?.*"
        clean_markdown = re.sub(segment_pattern, "", clean_markdown)
        
        # 清理多余的连续空行
        clean_markdown = re.sub(r'\n{3,}', '\n\n', clean_markdown)
        
        return clean_markdown, page_map

    def _convert_blockquote_to_original(self, html: str) -> str:
        """将 markdown2 生成的 <blockquote> 转换为带分隔符的双语结构
        
        转换规则：
        - 译文 (普通 <p>)
        - <hr class="bilingual-separator"> (分隔符)
        - <span class="original">原文</span> (原文)
        
        示例：
        <p>译文</p>
        <blockquote><p>原文</p></blockquote>
        ↓
        <p>译文</p>
        <hr class="bilingual-separator"/>
        <span class="original">原文</span>
        """
        def replace_blockquote(match):
            blockquote_content = match.group(1)
            # 提取所有 <p> 标签的内容
            paragraphs = re.findall(r'<p>(.*?)</p>', blockquote_content, re.DOTALL)
            if paragraphs:
                # 用 <br/> 连接多个段落
                original_text = '<br/>'.join(p.strip() for p in paragraphs if p.strip())
            else:
                # 如果没有 <p> 标签，直接使用内容
                original_text = blockquote_content.strip()
            
            # 返回：分隔符 + 原文
            return f'<hr class="bilingual-separator"/><span class="original">{original_text}</span>'
        
        # 匹配 <blockquote>...</blockquote>
        html = re.sub(r'<blockquote>(.*?)</blockquote>', replace_blockquote, html, flags=re.DOTALL)
        return html

    def _enhance_blockquotes_with_metadata(self, html_body: str, segment_metadata: Dict[int, Dict]) -> str:
        """
        将普通段落包装成 content-block div，并添加 data-source-page 属性
        
        新策略（避免 blockquote 乱码）：
        1. 查找 --- 分隔符之间的内容段落
        2. 将每个内容段落包装成 <div class="content-block">
        3. 从 segment_metadata 按顺序添加页码属性
        """
        # 提取所有 segment 的页码和章节标题（按 segment_id 排序）
        page_numbers = []
        chapter_titles = []
        for seg_idx in sorted(segment_metadata.keys()):
            meta = segment_metadata[seg_idx]
            page_idx = meta.get('page_index')
            if page_idx is not None:
                # page_index 是 0-based，显示时 +1
                page_numbers.append(page_idx + 1)
            chapter_titles.append(meta.get('chapter_title', '') or '')
        
        # 使用 <hr> 作为分隔符来识别内容块
        # markdown2 会将 --- 转换为 <hr />
        parts = re.split(r'(<hr\s*/?>)', html_body)
        
        result_parts = []
        content_block_count = 0
        
        for i, part in enumerate(parts):
            # 如果是 <hr> 标签，直接保留
            if re.match(r'<hr\s*/?>', part):
                result_parts.append(part)
                continue
            
            # 检查这部分是否包含实际内容（段落）
            # 跳过标题和空内容
            has_content = bool(re.search(r'<p[^>]*>.*?</p>', part, re.DOTALL))
            
            if has_content and part.strip():
                # 获取当前块对应的页码（使用 HTML 元素而非 CSS 伪元素）
                page_marker_html = ""
                # 仅当 settings 中启用页码标记时才生成页码元素
                if self.settings.processing.render_page_markers and content_block_count < len(page_numbers):
                    page_num = page_numbers[content_block_count]
                    page_marker_html = f'<span class="page-marker">P{page_num}</span>'

                # 获取对应的章节标题并注入为隐藏元素用于 running header
                chapter_title_html = ''
                if content_block_count < len(chapter_titles):
                    from html import escape
                    ch_title = chapter_titles[content_block_count] or ''
                    # Hidden element that sets the running string for headers
                    chapter_title_html = f'<div class="chapter-title" style="string-set: chapter content(); display:none;">{escape(ch_title)}</div>'
                
                # 包装成 content-block，注入用于外侧装饰的元素，页码标记放在内容开头
                # 注入 <span class="decor"> 以便通过 CSS 绝对定位放置在左侧外边距区域
                result_parts.append(
                    f'<div class="content-block">{chapter_title_html}<span class="decor" aria-hidden="true"></span>{page_marker_html}{part}</div>'
                )
                content_block_count += 1
            else:
                result_parts.append(part)
        
        self.logger.info(f"✅ 共创建 {content_block_count} 个 content-block（使用 HTML 页码标记）")
        
        return ''.join(result_parts)
    
    def _add_heading_spacing(self, html_body: str, segment_metadata: Dict[int, Dict]) -> str:
        """
        根据 toc_level 为标题元素添加间距样式
        h5 最近 (0.05em), h4 (0.10em), h3 (0.15em), h2 最远 (0.20em)
        """
        # 为 h2-h5 标题添加 data-toc-level 属性和对应的间距
        for level in range(2, 6):
            spacing = self.TOC_LEVEL_SPACING.get(level, 0.10)
            # 匹配 <h2>, <h3>, <h4>, <h5> 标签
            pattern = rf'<h{level}(\s[^>]*)?>|<h{level}>'
            
            def add_spacing_attr(match):
                tag = match.group(0)
                if 'data-toc-spacing' in tag:
                    return tag
                if tag == f'<h{level}>':
                    return f'<h{level} data-toc-level="{level}" style="margin-top: {spacing}em;">'
                else:
                    # 已有属性的情况
                    return tag.replace(f'<h{level}', f'<h{level} data-toc-level="{level}" style="margin-top: {spacing}em;"')
            
            html_body = re.sub(pattern, add_spacing_attr, html_body)
        
        return html_body

    def _create_html_template(self, html_body: str, translated_title: str, original_title: str, 
                             css_path: Optional[Path] = None) -> str:
        """强化版模板：嵌入完整CSS样式，确保与test_final.html一致"""
        # 读取CSS文件内容
        css_content = ""
        css_file = css_path if css_path else self.css_path
        if css_file and css_file.exists():
            css_content = css_file.read_text(encoding='utf-8')
        
        display_title = f"{translated_title} - {original_title}" if translated_title != original_title else translated_title
        
        # Add fallback CSS for running headers if not present in css_content
        running_header_css = """
        /* Running header: use the last .chapter-title element's string for header */
        .chapter-title { string-set: chapter content(); display: none; }
        @page {
            @top-center {
                content: string(chapter);
                font-size: 12px;
                color: #333333;
            }
        }
        """

        # Avoid duplicating rules if css already contains 'string-set' or '@top-center'
        if 'string-set' not in css_content and '@top-center' not in css_content:
            css_content = css_content + '\n' + running_header_css

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>{display_title}</title>
    <style>
        {css_content}
    </style>
</head>
<body>
    <div class="main-content">
        {html_body}
    </div>
</body>
</html>"""

    def render_to_string(self, segments: SegmentList, title: str = "Document", 
                         translated_title: str = "") -> str:
        """
        生成清理后的 Markdown 字符串（用于调试）

        Args:
            segments: 要渲染的片段列表
            title: 原始文档标题
            translated_title: 翻译后的文档标题

        Returns:
            清理后的 Markdown 字符串
        """
        from .markdown import MarkdownRenderer
        md_renderer = MarkdownRenderer(self.settings)
        markdown_content = md_renderer.render_to_string(segments, title, translated_title)
        clean_markdown, _ = self._extract_page_numbers_and_clean(markdown_content)
        return clean_markdown
