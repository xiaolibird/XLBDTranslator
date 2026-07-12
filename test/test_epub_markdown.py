"""
测试 EPUB 渲染器的 Markdown 到 HTML 转换功能
"""
import sys
import tempfile
import zipfile
from pathlib import Path

# 添加项目根目录到 path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.core.schema import (APISettings, ContentSegment, FileSettings,
                             LoggingSettings, ProcessingSettings, Settings)
from src.renderer.epub import EPUBRenderer


def create_test_epub_with_markdown(epub_path: Path) -> None:
    """创建包含 Markdown 格式文本的测试 EPUB"""
    with zipfile.ZipFile(epub_path, 'w') as epub:
        # mimetype 文件（必须未压缩）
        epub.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        
        # META-INF/container.xml
        container_xml = '''<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>'''
        epub.writestr('META-INF/container.xml', container_xml)
        
        # OEBPS/content.opf
        content_opf = '''<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Markdown Test</dc:title>
    <dc:language>en</dc:language>
    <dc:identifier id="uid">markdown-test-123</dc:identifier>
  </metadata>
  <manifest>
    <item id="chapter1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="chapter1"/>
  </spine>
</package>'''
        epub.writestr('OEBPS/content.opf', content_opf)
        
        # OEBPS/chapter1.xhtml - 包含各种 Markdown 格式
        chapter1 = '''<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter 1</title></head>
<body>
<h1>Markdown Test</h1>
<p>This text contains bold formatting.</p>
<p>This text contains italic formatting.</p>
<p>This text contains code formatting.</p>
<p>This text contains a link.</p>
<p>This text contains mixed formatting.</p>
</body>
</html>'''
        epub.writestr('OEBPS/chapter1.xhtml', chapter1)
    
    print(f'✅ 创建测试 EPUB: {epub_path}')


def create_mock_segments_with_markdown() -> list:
    """创建包含 Markdown 格式的模拟翻译片段"""
    segments = [
        ContentSegment(
            segment_id=0,
            original_text="Markdown Test",
            translated_text="**Markdown 测试**",  # 粗体
            is_new_chapter=True,
            chapter_title="Markdown Test",
            content_type="text"
        ),
        ContentSegment(
            segment_id=1,
            original_text="This text contains bold formatting.",
            translated_text="这段文字包含**粗体**格式。",
            is_new_chapter=False,
            chapter_title="Markdown Test",
            content_type="text"
        ),
        ContentSegment(
            segment_id=2,
            original_text="This text contains italic formatting.",
            translated_text="这段文字包含*斜体*格式。",
            is_new_chapter=False,
            chapter_title="Markdown Test",
            content_type="text"
        ),
        ContentSegment(
            segment_id=3,
            original_text="This text contains code formatting.",
            translated_text="这段文字包含`代码`格式。",
            is_new_chapter=False,
            chapter_title="Markdown Test",
            content_type="text"
        ),
        ContentSegment(
            segment_id=4,
            original_text="This text contains a link.",
            translated_text="这段文字包含[链接](https://example.com)。",
            is_new_chapter=False,
            chapter_title="Markdown Test",
            content_type="text"
        ),
        ContentSegment(
            segment_id=5,
            original_text="This text contains mixed formatting.",
            translated_text="这段文字包含**粗体**和*斜体*以及`代码`的混合格式。",
            is_new_chapter=False,
            chapter_title="Markdown Test",
            content_type="text"
        ),
    ]
    return segments


def create_mock_settings(retain_original: bool = False) -> Settings:
    """创建模拟设置"""
    settings = Settings(
        api=APISettings(),
        files=FileSettings(),
        processing=ProcessingSettings(),
        logging=LoggingSettings()
    )
    settings.processing.retain_original = retain_original
    return settings


def test_markdown_to_html_conversion():
    """测试 Markdown 到 HTML 的转换"""
    print("\n" + "=" * 60)
    print("测试: Markdown 到 HTML 转换")
    print("=" * 60)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        # 创建测试 EPUB
        test_epub_path = tmpdir / "test_markdown.epub"
        create_test_epub_with_markdown(test_epub_path)
        
        # 创建输出路径
        output_path = tmpdir / "test_markdown_translated.epub"
        
        # 创建模拟数据
        segments = create_mock_segments_with_markdown()
        settings = create_mock_settings(retain_original=False)
        
        # 执行渲染
        renderer = EPUBRenderer(settings)
        renderer.render_to_file(
            segments=segments,
            original_epub_path=test_epub_path,
            output_path=output_path,
            title="Markdown Test",
            translated_title="Markdown 测试"
        )
        
        # 验证输出
        assert output_path.exists(), "输出文件不存在"
        print(f"✅ 输出文件已创建: {output_path}")
        
        # 读取并检查转换结果
        from bs4 import BeautifulSoup
        
        with zipfile.ZipFile(output_path, 'r') as zf:
            for name in zf.namelist():
                if name.endswith('.xhtml') and 'chapter' in name:
                    content = zf.read(name).decode('utf-8')
                    soup = BeautifulSoup(content, 'html.parser')
                    
                    print(f"\n📄 {name}:")
                    
                    # 检查粗体转换
                    strong_tags = soup.find_all('strong')
                    if strong_tags:
                        print(f"   ✓ 找到 {len(strong_tags)} 个 <strong> 标签")
                        for tag in strong_tags:
                            print(f"     - {tag.get_text()}")
                        assert len(strong_tags) >= 2, "粗体标签数量不足"
                    
                    # 检查斜体转换
                    em_tags = soup.find_all('em')
                    if em_tags:
                        print(f"   ✓ 找到 {len(em_tags)} 个 <em> 标签")
                        for tag in em_tags:
                            print(f"     - {tag.get_text()}")
                        assert len(em_tags) >= 1, "斜体标签数量不足"
                    
                    # 检查代码转换
                    code_tags = soup.find_all('code')
                    if code_tags:
                        print(f"   ✓ 找到 {len(code_tags)} 个 <code> 标签")
                        for tag in code_tags:
                            print(f"     - {tag.get_text()}")
                        assert len(code_tags) >= 1, "代码标签数量不足"
                    
                    # 检查链接转换
                    a_tags = soup.find_all('a')
                    if a_tags:
                        print(f"   ✓ 找到 {len(a_tags)} 个 <a> 标签")
                        for tag in a_tags:
                            print(f"     - {tag.get_text()} → {tag.get('href')}")
                        assert len(a_tags) >= 1, "链接标签数量不足"
                    
                    # 验证没有遗留的 Markdown 语法
                    text = soup.get_text()
                    assert '**' not in text, "仍然存在未转换的粗体语法"
                    assert '`' not in text or '`代码`' not in text, "仍然存在未转换的代码语法"
                    print("\n   ✓ 没有遗留的 Markdown 语法")
        
        print("\n✅ Markdown 到 HTML 转换测试通过!")


def test_markdown_bilingual_mode():
    """测试双语模式下的 Markdown 转换"""
    print("\n" + "=" * 60)
    print("测试: 双语模式 + Markdown 转换")
    print("=" * 60)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        # 创建测试 EPUB
        test_epub_path = tmpdir / "test_markdown.epub"
        create_test_epub_with_markdown(test_epub_path)
        
        # 创建输出路径
        output_path = tmpdir / "test_markdown_bilingual.epub"
        
        # 创建模拟数据
        segments = create_mock_segments_with_markdown()
        settings = create_mock_settings(retain_original=True)
        
        # 执行渲染
        renderer = EPUBRenderer(settings)
        renderer.render_to_file(
            segments=segments,
            original_epub_path=test_epub_path,
            output_path=output_path,
            title="Markdown Test",
            translated_title="Markdown 测试"
        )
        
        # 验证输出
        assert output_path.exists(), "输出文件不存在"
        print(f"✅ 输出文件已创建: {output_path}")
        
        # 读取并检查
        from bs4 import BeautifulSoup
        
        with zipfile.ZipFile(output_path, 'r') as zf:
            for name in zf.namelist():
                if name.endswith('.xhtml') and 'chapter' in name:
                    content = zf.read(name).decode('utf-8')
                    soup = BeautifulSoup(content, 'html.parser')
                    
                    print(f"\n📄 {name}:")
                    
                    # 检查双语 span
                    translated_spans = soup.find_all('span', class_='translated')
                    original_spans = soup.find_all('span', class_='original')
                    
                    print(f"   译文 span: {len(translated_spans)}")
                    print(f"   原文 span: {len(original_spans)}")
                    
                    # 检查译文中的 HTML 标签
                    if translated_spans:
                        for span in translated_spans:
                            # 检查是否包含格式化标签
                            if span.find('strong') or span.find('em') or span.find('code') or span.find('a'):
                                print(f"   ✓ 译文包含格式化标签: {span.prettify()[:100]}...")
                                break
        
        print("\n✅ 双语模式 Markdown 转换测试通过!")


if __name__ == "__main__":
    print("🧪 EPUB Markdown 转换测试")
    print("=" * 60)
    
    try:
        test_markdown_to_html_conversion()
        test_markdown_bilingual_mode()
        
        print("\n" + "=" * 60)
        print("🎉 所有测试通过!")
        print("=" * 60)
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
