"""
PDF 渲染器
负责将 ContentSegment 列表渲染为 PDF 文件
基于 MarkdownRenderer 生成内容，然后转换为 PDF
"""
import re
from pathlib import Path
from typing import List, Optional

from ..core.schema import ContentSegment, Settings, SegmentList


class PDFRenderer:
    """
    PDF 渲染器

    职责：将 ContentSegment 列表转换为 PDF 文件
    - 利用 MarkdownRenderer 生成 Markdown 内容
    - 清理 Segment 标记
    - 转换为 HTML 和 PDF
    - 支持双语对照和纯译文模式
    """

    def __init__(self, settings: Settings):
        self.settings = settings

        # CSS 文件路径（动态定位）
        self.css_path = self._locate_css_file()

    def _locate_css_file(self) -> Optional[Path]:
        """定位 CSS 文件"""
        # 优先级：config/ -> assets/ -> 项目根目录
        candidates = [
            Path(__file__).parent.parent.parent / "config" / "pdf_style.css",  # 配置目录（推荐）
            Path(__file__).parent.parent.parent / "assets" / "pdf_style.css",
            Path(__file__).parent.parent.parent / "pdf_style.css",  # 项目根目录（向后兼容）
        ]

        for css_path in candidates:
            if css_path.exists():
                return css_path

        return None

    def render_to_file(self, segments: SegmentList, output_path: Path, title: str = "Document") -> None:
        """
        将片段列表渲染到 PDF 文件 (优化版，支持高阶 CSS 渲染)
        """
        try:
            # 1. 延迟导入依赖，确保环境缺失时不会直接崩溃
            import markdown2
            from weasyprint import HTML, CSS
            from weasyprint.text.fonts import FontConfiguration

            # 2. 生成 Markdown 内容
            from .markdown import MarkdownRenderer
            md_renderer = MarkdownRenderer(self.settings)
            markdown_content = md_renderer.render_to_string(segments, title)

            # 3. 清理 Segment 标记
            clean_markdown = self._clean_segment_markers(markdown_content)

            # 4. 转换为 HTML (增强扩展支持)
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

            # 5. 生成 HTML 模板
            html_content = self._create_html_template(html_body, title)

            # 6. 准备 PDF 渲染环境
            output_path.parent.mkdir(parents=True, exist_ok=True)
            
            # 初始化字体配置
            font_config = FontConfiguration()
            stylesheets = []

            if self.css_path and self.css_path.exists():
                # 显式传递 font_config，确保 CSS 里的字体族能正确匹配系统字体
                stylesheets.append(CSS(filename=str(self.css_path), font_config=font_config))
                print(f"🎨 已加载高阶样式表: {self.css_path.name}")
            else:
                print("⚠️ 未找到 CSS 样式表，PDF 将使用默认样式")

            # 7. 渲染 PDF
            # base_url 设为输出目录或项目根目录，确保图片相对路径解析正确
            # presentational_hints 允许 HTML 属性干扰样式，配合高级 CSS 布局
            HTML(string=html_content, base_url=str(output_path.parent)).write_pdf(
                output_path,
                stylesheets=stylesheets,
                font_config=font_config,
                presentational_hints=True
            )

            print(f"✅ PDF 已成功生成: {output_path}")

        except ImportError as e:
            lib_name = str(e).split("'")[-2] if "'" in str(e) else "weasyprint/markdown2"
            print(f"⚠️ PDF 导出跳过: 缺少 Python 依赖库 - {lib_name}")
            print(f"💡 请运行: pip install weasyprint markdown2")
            print("📄 降级处理: 仅生成 Markdown 文件")

        except Exception as e:
            error_msg = str(e)
            # 针对 WeasyPrint 常见的系统底层库缺失报错进行诊断
            if any(lib in error_msg for lib in ["libgobject", "cairo", "pango", "gdk-pixbuf"]):
                print("⚠️ PDF 导出跳过: 缺少必要的系统底层库 (Pango/Cairo)")
                print("💡 macOS 请运行: brew install cairo pango gdk-pixbuf libffi")
                print("💡 Ubuntu 请运行: apt-get install libpango1.0-dev libcairo2-dev")
            else:
                print(f"⚠️ PDF 导出失败: {error_msg}")
            print("📄 降级处理: 仅生成 Markdown 文件")

    def _clean_segment_markers(self, markdown_content: str) -> str:
        """
        清理 Segment 标记，使 PDF 更纯净

        匹配模式：
        🔖 **Segment \d+** (可选: (Image))
        """
        clean_pattern = r"🔖\s*\*\*Segment\s+\d+\*\*(?: \(Image\))?.*"
        cleaned = re.sub(clean_pattern, "", markdown_content)

        # 可选：清理多余的连续空行
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)

        return cleaned

    def _create_html_template(self, html_body: str, title: str) -> str:
        """强化版模板：彻底移除默认间距干扰"""
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>{title}</title>
    <style>
        /* 强制重置，防止浏览器默认样式干扰间距识别 */
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ background-color: #fdfaf2; }}
    </style>
</head>
<body>
    <div class="main-content">
        {html_body}
    </div>
</body>
</html>"""

    def render_to_string(self, segments: SegmentList, title: str = "Document") -> str:
        """
        生成清理后的 Markdown 字符串（用于调试）

        Args:
            segments: 要渲染的片段列表
            title: 文档标题

        Returns:
            清理后的 Markdown 字符串
        """
        from .markdown import MarkdownRenderer
        md_renderer = MarkdownRenderer(self.settings)
        markdown_content = md_renderer.render_to_string(segments, title)
        return self._clean_segment_markers(markdown_content)
