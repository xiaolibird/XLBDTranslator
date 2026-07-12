"""
测试渲染器新功能：
1. Markdown 纯文本输出（无 HTML 标签）
2. EPUB 生成功能（支持 EPUB 源回填和其他源转换）
"""
from pathlib import Path

from src.core.schema import (APISettings, ContentSegment, FileSettings,
                             LoggingSettings, ProcessingSettings, SegmentList,
                             Settings)
from src.renderer import MarkdownRenderer
from src.renderer.epub import EPUBRenderer, render_html_to_epub

# 创建测试设置（Settings 的子配置为必填，用 model_construct 组装测试用实例）
settings = Settings.model_construct(
    api=APISettings.model_construct(gemini_api_key="fake-key", gemini_model="fake-model"),
    files=FileSettings.model_construct(document_path=None, output_base_dir=Path("output"), modes_config_path=Path("config/modes.json")),
    processing=ProcessingSettings.model_construct(),
    logging=LoggingSettings.model_construct(),
)
settings.processing.retain_original = True  # 测试双语模式

# 创建测试片段
segments = SegmentList([
    ContentSegment(
        segment_id=1,
        original_text="This is a test paragraph.",
        translated_text="这是一个测试段落。",
        page_index=0,
        is_new_chapter=True,
        chapter_title="Chapter 1: Introduction",
        toc_level=2
    ),
    ContentSegment(
        segment_id=2,
        original_text="Another paragraph with more content.\nIt has multiple lines.",
        translated_text="另一个包含更多内容的段落。\n它有多行。",
        page_index=0,
        is_new_chapter=False
    ),
    ContentSegment(
        segment_id=3,
        original_text="Final paragraph on a new page.",
        translated_text="新页面上的最后一段。",
        page_index=1,
        is_new_chapter=False
    )
])

# 测试 1: Markdown 纯文本输出
print("=" * 60)
print("测试 1: Markdown 纯文本输出（无 HTML）")
print("=" * 60)

md_renderer = MarkdownRenderer(settings)
output_dir = Path("./test_output")
output_dir.mkdir(exist_ok=True)

md_output = output_dir / "test_pure_markdown.md"
md_renderer.render_to_file(
    segments=segments,
    output_path=md_output,
    title="Test Document",
    translated_title="测试文档"
)

print(f"✅ Markdown 已生成: {md_output}")
print(f"📄 预览前 500 字符：")
print("-" * 60)
content = md_output.read_text(encoding='utf-8')
print(content[:500])
print("-" * 60)
print()

# 验证是否包含 HTML 标签
if '<span' in content or '<hr' in content:
    print("❌ 警告: Markdown 中仍包含 HTML 标签！")
else:
    print("✅ 验证通过: Markdown 为纯文本格式")
print()

# 测试 2: HTML → EPUB 转换（适用于 PDF/TXT 等源文件）
print("=" * 60)
print("测试 2: HTML → EPUB 转换功能")
print("=" * 60)

try:
    import markdown2

    # 生成 Markdown 内容
    md_renderer = MarkdownRenderer(settings)
    markdown_content = md_renderer.render_to_string(
        segments=segments,
        title="Test Document",
        translated_title="测试文档"
    )
    
    # Markdown → HTML
    html_body = markdown2.markdown(
        markdown_content,
        extras=["fenced-code-blocks", "tables", "header-ids"]
    )
    
    # 构建完整 HTML
    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>测试文档</title>
</head>
<body>
    {html_body}
</body>
</html>"""
    
    # HTML → EPUB
    epub_output = output_dir / "test_document.epub"
    render_html_to_epub(
        html_content=html_content,
        output_path=epub_output,
        settings=settings,
        title="Test Document",
        translated_title="测试文档"
    )
    
    if epub_output.exists():
        print(f"✅ EPUB 已生成: {epub_output}")
        print(f"   大小: {epub_output.stat().st_size / 1024:.2f} KB")
    
except Exception as e:
    print(f"⚠️ EPUB 生成失败: {e}")
    print("💡 提示: 请确保已安装 ebooklib, markdown2")

print()
print("=" * 60)
print("测试完成！")
print("=" * 60)
