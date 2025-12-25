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
        将片段列表渲染到 PDF 文件

        Args:
            segments: 要渲染的片段列表
            output_path: 输出文件路径
            title: 文档标题
        """
        try:
            # 导入依赖
            import markdown2
            from weasyprint import HTML, CSS

            # 1. 生成 Markdown 内容
            from .markdown import MarkdownRenderer
            md_renderer = MarkdownRenderer(self.settings)
            markdown_content = md_renderer.render_to_string(segments, title)

            # 2. 清理 Segment 标记
            clean_markdown = self._clean_segment_markers(markdown_content)

            # 3. 转换为 HTML
            html_body = markdown2.markdown(
                clean_markdown,
                extras=["fenced-code-blocks", "tables", "footnotes", "break-on-newline", "header-ids"]
            )

            # 4. HTML 模板
            html_content = self._create_html_template(html_body, title)

            # 5. 渲染 PDF
            output_path.parent.mkdir(parents=True, exist_ok=True)

            stylesheets = []
            if self.css_path and self.css_path.exists():
                stylesheets.append(CSS(filename=str(self.css_path)))
                print(f"🎨 使用样式表: {self.css_path.name}")
            else:
                print("⚠️ 未找到 CSS 样式表，PDF 将使用默认样式")

            # 设置 base_url 为输出目录，确保相对路径图片能正确加载
            HTML(string=html_content, base_url=str(output_path.parent)).write_pdf(
                output_path,
                stylesheets=stylesheets
            )

            print(f"✅ PDF 已生成: {output_path}")

        except ImportError as e:
            print(f"⚠️ PDF 导出跳过: 缺少依赖库 - {e}")
            print("💡 建议安装: pip install weasyprint markdown2")
        except ImportError as e:
            if "weasyprint" in str(e).lower():
                print("⚠️ PDF 导出跳过: 未安装 weasyprint")
                print("💡 安装命令: pip install weasyprint")
            else:
                print(f"⚠️ PDF 导出跳过: 缺少依赖 - {e}")
            print("📄 Markdown 文件已生成，PDF 导出被跳过")

        except Exception as e:
            error_msg = str(e)
            if "libgobject" in error_msg or "cairo" in error_msg or "pango" in error_msg:
                print("⚠️ PDF 导出跳过: 缺少系统依赖库")
                print("💡 macOS 安装: brew install cairo pango gdk-pixbuf")
                print("💡 Ubuntu: apt-get install libpango1.0-dev libcairo2-dev")
                print("💡 详情: https://doc.courtbouillon.org/weasyprint/stable/first_steps.html")
            else:
                print(f"⚠️ PDF 导出失败: {e}")
            print("📄 Markdown 文件已生成，PDF 导出被跳过")

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
        """创建 HTML 模板"""
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>{title}</title>
</head>
<body>
    {html_body}
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
