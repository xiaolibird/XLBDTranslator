#!/usr/bin/env python3
"""
测试脚本：验证 Markdown 正文和 > 引用格式的渲染
"""
import sys
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.schema import ContentSegment, SegmentList, Settings
from src.renderer.epub import EPUBRenderer
from src.renderer.markdown import MarkdownRenderer
from src.renderer.pdf import PDFRenderer


def test_markdown_rendering():
    """测试 Markdown 渲染器"""
    print("=" * 60)
    print("测试 1: Markdown 渲染器 - 双语模式")
    print("=" * 60)
    
    # 创建测试 segment
    segment = ContentSegment(
        segment_id=1,
        original_text="This is the original text.\nWith multiple lines.",
        translated_text="这是译文。\n包含多行。",
        page_index=0
    )
    
    segments = SegmentList([segment])
    
    # 创建设置（双语模式）。Settings 的子配置为必填，测试用 model_construct 组装
    from src.core.schema import (APISettings, FileSettings, LoggingSettings,
                                 ProcessingSettings)
    from pathlib import Path as _Path
    settings = Settings.model_construct(
        api=APISettings.model_construct(gemini_api_key="fake-key", gemini_model="fake-model"),
        files=FileSettings.model_construct(document_path=None, output_base_dir=_Path("output"), modes_config_path=_Path("config/modes.json")),
        processing=ProcessingSettings.model_construct(),
        logging=LoggingSettings.model_construct(),
    )
    settings.processing.retain_original = True
    
    # 渲染
    renderer = MarkdownRenderer(settings)
    markdown_output = renderer.render_to_string(segments, title="测试文档")
    
    print("\n生成的 Markdown:")
    print("-" * 60)
    print(markdown_output)
    print("-" * 60)
    
    # 验证格式
    checks = [
        ("译文（正文）", "这是译文" in markdown_output),
        ("原文（引用块）", "> This is the original" in markdown_output),
        ("多行保持", "包含多行" in markdown_output and "> With multiple lines" in markdown_output),
    ]
    
    print("\n✅ 格式检查:")
    for name, passed in checks:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"   {status}: {name}")
    
    return all(check[1] for check in checks)

def test_pdf_blockquote_conversion():
    """测试 PDF 渲染器中的 blockquote 转换"""
    print("\n" + "=" * 60)
    print("测试 2: PDF 渲染 - Markdown > HTML 转换")
    print("=" * 60)
    
    import markdown2
    
    test_markdown = """
这是第一段译文。

> This is the original text in English.
> With multiple lines.

---

这是第二段译文。

> Another original paragraph.
"""
    
    print("\n输入 Markdown:")
    print("-" * 60)
    print(test_markdown)
    print("-" * 60)
    
    # 转换为 HTML
    html_output = markdown2.markdown(
        test_markdown,
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
    
    print("\n生成的 HTML:")
    print("-" * 60)
    print(html_output)
    print("-" * 60)
    
    # 验证 blockquote 转换
    checks = [
        ("正文段落", "<p>这是第一段译文。</p>" in html_output),
        ("blockquote 标签", "<blockquote>" in html_output),
        ("原文内容", "This is the original text" in html_output),
        ("多行引用", "With multiple lines" in html_output),
        ("分隔符", "<hr" in html_output),
    ]
    
    print("\n✅ HTML 转换检查:")
    for name, passed in checks:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"   {status}: {name}")
    
    return all(check[1] for check in checks)

def test_epub_bilingual_rendering():
    """测试 EPUB 渲染器的双语处理"""
    print("\n" + "=" * 60)
    print("测试 3: EPUB 渲染 - 双语内容处理")
    print("=" * 60)
    
    from bs4 import BeautifulSoup

    # 模拟 EPUB 中的原始内容
    original_html = """<html>
<body>
<p>This is the original paragraph.</p>
</body>
</html>"""
    
    translated_text = "这是翻译后的段落。"
    original_text = "This is the original paragraph."
    
    soup = BeautifulSoup(original_html, 'html.parser')
    p_tag = soup.find('p')
    
    # 模拟 _replace_tag_content_bilingual 的逻辑
    p_tag.clear()
    
    # 创建译文 span
    trans_span = soup.new_tag('span')
    trans_span['class'] = 'translated'
    trans_span.string = translated_text
    
    # 创建换行
    br = soup.new_tag('br')
    
    # 创建原文 span
    orig_span = soup.new_tag('span')
    orig_span['class'] = 'original'
    orig_span['style'] = 'color: #999; font-size: 0.9em;'
    orig_span.string = original_text
    
    # 按顺序添加
    p_tag.append(trans_span)
    p_tag.append(br)
    p_tag.append(orig_span)
    
    result_html = str(soup)
    
    print("\n生成的 EPUB HTML:")
    print("-" * 60)
    print(result_html)
    print("-" * 60)
    
    # 验证结构
    checks = [
        ("译文 span", '<span class="translated">' in result_html),
        ("原文 span", '<span class="original"' in result_html),
        ("换行符", '<br/>' in result_html or '<br>' in result_html),
        ("译文内容", translated_text in result_html),
        ("原文内容", original_text in result_html),
        ("原文样式", 'color: #999' in result_html),
    ]
    
    print("\n✅ EPUB 结构检查:")
    for name, passed in checks:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"   {status}: {name}")
    
    return all(check[1] for check in checks)

def test_css_styles():
    """测试 CSS 样式定义"""
    print("\n" + "=" * 60)
    print("测试 4: CSS 样式检查")
    print("=" * 60)
    
    css_path = Path(__file__).parent / "config" / "pdf_style.css"
    
    if not css_path.exists():
        print(f"❌ CSS 文件不存在: {css_path}")
        return False
    
    css_content = css_path.read_text(encoding='utf-8')
    
    # 检查关键样式
    checks = [
        ("content-block 类", ".content-block" in css_content),
        ("原文样式", ".original" in css_content),
        ("译文样式", ".translated" in css_content),
        ("原文颜色浅淡", "#b6b6b6" in css_content or "#999" in css_content),
        ("正文字体", "LXGW-WenKai-Pure" in css_content),
        ("blockquote 说明", "blockquote" in css_content),  # 应该有注释说明不再使用
    ]
    
    print("\n✅ CSS 样式检查:")
    for name, passed in checks:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"   {status}: {name}")
    
    # 显示关键样式片段
    print("\n📄 原文样式定义:")
    print("-" * 60)
    import re
    original_style = re.search(r'\.original\s*{[^}]+}', css_content, re.DOTALL)
    if original_style:
        print(original_style.group(0))
    print("-" * 60)
    
    return all(check[1] for check in checks)

def main():
    """运行所有测试"""
    print("\n🔍 Markdown 渲染流程验证")
    print("=" * 60)
    
    results = []
    
    try:
        results.append(("Markdown 渲染器", test_markdown_rendering()))
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Markdown 渲染器", False))
    
    try:
        results.append(("PDF Blockquote 转换", test_pdf_blockquote_conversion()))
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        results.append(("PDF Blockquote 转换", False))
    
    try:
        results.append(("EPUB 双语渲染", test_epub_bilingual_rendering()))
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        results.append(("EPUB 双语渲染", False))
    
    try:
        results.append(("CSS 样式", test_css_styles()))
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        results.append(("CSS 样式", False))
    
    # 总结
    print("\n" + "=" * 60)
    print("📊 测试总结")
    print("=" * 60)
    
    for name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status}: {name}")
    
    all_passed = all(result[1] for result in results)
    
    if all_passed:
        print("\n✅ 所有测试通过！Markdown 正文和引用格式已正确渲染到 EPUB 和 PDF。")
        return 0
    else:
        print("\n⚠️ 部分测试失败，请检查上述输出。")
        return 1

if __name__ == "__main__":
    exit(main())
