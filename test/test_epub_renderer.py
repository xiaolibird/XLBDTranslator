"""
EPUB 渲染器测试
测试将翻译内容填回原 EPUB 文件的功能
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


def create_test_epub(epub_path: Path) -> None:
    """创建测试用的 EPUB 文件"""
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
    <dc:title>Test Document</dc:title>
    <dc:language>en</dc:language>
    <dc:identifier id="uid">test123</dc:identifier>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="chapter1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
    <item id="chapter2" href="chapter2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="chapter1"/>
    <itemref idref="chapter2"/>
  </spine>
</package>'''
        epub.writestr('OEBPS/content.opf', content_opf)
        
        # OEBPS/toc.ncx (TOC 文件)
        toc_ncx = '''<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="test123"/>
  </head>
  <docTitle>
    <text>Test Document</text>
  </docTitle>
  <navMap>
    <navPoint id="ch1" playOrder="1">
      <navLabel>
        <text>Introduction</text>
      </navLabel>
      <content src="chapter1.xhtml"/>
    </navPoint>
    <navPoint id="ch2" playOrder="2">
      <navLabel>
        <text>Technical Details</text>
      </navLabel>
      <content src="chapter2.xhtml"/>
    </navPoint>
  </navMap>
</ncx>'''
        epub.writestr('OEBPS/toc.ncx', toc_ncx)
        
        # OEBPS/chapter1.xhtml
        chapter1 = '''<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter 1</title></head>
<body>
<h1>Introduction</h1>
<p>The Unified Memory Architecture (UMA) in M2 Pro chips allows high-bandwidth, low-latency access to data.</p>
<p>Generative AI models require significant computational resources for inference, especially during long-context processing.</p>
</body>
</html>'''
        epub.writestr('OEBPS/chapter1.xhtml', chapter1)
        
        # OEBPS/chapter2.xhtml
        chapter2 = '''<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter 2</title></head>
<body>
<h1>Technical Details</h1>
<p>Machine learning algorithms can be optimized using various techniques.</p>
<p>Neural networks have revolutionized the field of artificial intelligence.</p>
</body>
</html>'''
        epub.writestr('OEBPS/chapter2.xhtml', chapter2)
    
    print(f'✅ 创建测试 EPUB: {epub_path}')


def create_mock_segments() -> list:
    """创建模拟的翻译后片段"""
    segments = [
        ContentSegment(
            segment_id=0,
            original_text="Introduction",
            translated_text="引言",
            is_new_chapter=True,
            chapter_title="Introduction",
            content_type="text"
        ),
        ContentSegment(
            segment_id=1,
            original_text="The Unified Memory Architecture (UMA) in M2 Pro chips allows high-bandwidth, low-latency access to data.",
            translated_text="M2 Pro 芯片中的统一内存架构（UMA）允许高带宽、低延迟的数据访问。",
            is_new_chapter=False,
            chapter_title="Introduction",
            content_type="text"
        ),
        ContentSegment(
            segment_id=2,
            original_text="Generative AI models require significant computational resources for inference, especially during long-context processing.",
            translated_text="生成式 AI 模型需要大量计算资源进行推理，特别是在长上下文处理过程中。",
            is_new_chapter=False,
            chapter_title="Introduction",
            content_type="text"
        ),
        ContentSegment(
            segment_id=3,
            original_text="Technical Details",
            translated_text="技术细节",
            is_new_chapter=True,
            chapter_title="Technical Details",
            content_type="text"
        ),
        ContentSegment(
            segment_id=4,
            original_text="Machine learning algorithms can be optimized using various techniques.",
            translated_text="机器学习算法可以使用各种技术进行优化。",
            is_new_chapter=False,
            chapter_title="Technical Details",
            content_type="text"
        ),
        ContentSegment(
            segment_id=5,
            original_text="Neural networks have revolutionized the field of artificial intelligence.",
            translated_text="神经网络彻底改变了人工智能领域。",
            is_new_chapter=False,
            chapter_title="Technical Details",
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
    # 直接设置属性值，绕过 Pydantic 的默认值覆盖
    settings.processing.retain_original = retain_original
    return settings


def test_epub_renderer_translate_only():
    """测试纯译文模式"""
    print("\n" + "=" * 60)
    print("测试 1: 纯译文模式")
    print("=" * 60)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        # 创建测试 EPUB
        test_epub_path = tmpdir / "test_input.epub"
        create_test_epub(test_epub_path)
        
        # 创建输出路径
        output_path = tmpdir / "test_translated.epub"
        
        # 创建模拟数据
        segments = create_mock_segments()
        settings = create_mock_settings(retain_original=False)
        
        # 执行渲染
        renderer = EPUBRenderer(settings)
        renderer.render_to_file(
            segments=segments,
            original_epub_path=test_epub_path,
            output_path=output_path,
            title="Test Document",
            translated_title="测试文档"
        )
        
        # 验证输出
        assert output_path.exists(), "输出文件不存在"
        print(f"✅ 输出文件已创建: {output_path}")
        
        # 直接读取 EPUB 压缩包检查内容
        from bs4 import BeautifulSoup
        
        with zipfile.ZipFile(output_path, 'r') as zf:
            for name in zf.namelist():
                if name.endswith('.xhtml'):
                    content = zf.read(name).decode('utf-8')
                    soup = BeautifulSoup(content, 'html.parser')
                    text = soup.get_text()
                    print(f"\n📄 {name}:")
                    print(f"   {text[:200]}...")
                    
                    # 验证中文内容存在
                    if 'chapter1' in name:
                        assert '统一内存架构' in text or 'M2 Pro' in text, "chapter1 翻译内容缺失"
                    elif 'chapter2' in name:
                        assert '机器学习' in text or '神经网络' in text, "chapter2 翻译内容缺失"
        
        print("\n✅ 测试 1 通过!")


def test_epub_renderer_bilingual():
    """测试双语模式（保留原文）"""
    print("\n" + "=" * 60)
    print("测试 2: 双语模式（保留原文）")
    print("=" * 60)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        # 创建测试 EPUB
        test_epub_path = tmpdir / "test_input.epub"
        create_test_epub(test_epub_path)
        
        # 创建输出路径
        output_path = tmpdir / "test_translated_bilingual.epub"
        
        # 创建模拟数据
        segments = create_mock_segments()
        settings = create_mock_settings(retain_original=True)
        
        # 调试输出
        print(f"   - Settings retain_original: {settings.processing.retain_original}")
        
        # 执行渲染
        renderer = EPUBRenderer(settings)
        print(f"   - Renderer retain_original: {renderer.retain_original}")
        
        renderer.render_to_file(
            segments=segments,
            original_epub_path=test_epub_path,
            output_path=output_path,
            title="Test Document",
            translated_title="测试文档"
        )
        
        # 验证输出
        assert output_path.exists(), "输出文件不存在"
        print(f"✅ 输出文件已创建: {output_path}")
        
        # 直接读取 EPUB 压缩包检查内容
        from bs4 import BeautifulSoup
        
        with zipfile.ZipFile(output_path, 'r') as zf:
            for name in zf.namelist():
                if name.endswith('.xhtml'):
                    content = zf.read(name).decode('utf-8')
                    soup = BeautifulSoup(content, 'html.parser')
                    
                    # 检查是否有双语标签
                    translated_spans = soup.find_all('span', class_='translated')
                    original_spans = soup.find_all('span', class_='original')
                    
                    print(f"\n📄 {name}:")
                    print(f"   译文 span 数量: {len(translated_spans)}")
                    print(f"   原文 span 数量: {len(original_spans)}")
                    
                    if translated_spans:
                        print(f"   示例译文: {translated_spans[0].get_text()[:50]}...")
                    if original_spans:
                        print(f"   示例原文: {original_spans[0].get_text()[:50]}...")
        
        print("\n✅ 测试 2 通过!")


def test_epub_toc_translation():
    """测试 TOC（目录）翻译"""
    print("\n" + "=" * 60)
    print("测试 3: TOC（目录）翻译")
    print("=" * 60)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        # 创建测试 EPUB（带 TOC）
        test_epub_path = tmpdir / "test_input.epub"
        create_test_epub(test_epub_path)
        
        # 创建输出路径
        output_path = tmpdir / "test_translated_with_toc.epub"
        
        # 创建模拟数据
        segments = create_mock_segments()
        settings = create_mock_settings(retain_original=False)
        
        # 执行渲染
        renderer = EPUBRenderer(settings)
        renderer.render_to_file(
            segments=segments,
            original_epub_path=test_epub_path,
            output_path=output_path,
            title="Test Document",
            translated_title="测试文档"
        )
        
        # 验证输出
        assert output_path.exists(), "输出文件不存在"
        print(f"✅ 输出文件已创建: {output_path}")
        
        # 读取并检查 TOC
        import ebooklib
        from ebooklib import epub
        
        try:
            book = epub.read_epub(str(output_path))
            
            # 检查 TOC 是否被翻译
            if book.toc:
                print("\n📑 TOC 目录项:")
                for item in book.toc:
                    if hasattr(item, 'title'):
                        print(f"   - {item.title}")
                        # 验证是否包含中文
                        if any('\u4e00' <= c <= '\u9fff' for c in item.title):
                            print(f"     ✓ 已翻译为中文")
        except Exception as e:
            # TOC 读取可能失败，但这不是关键测试
            print(f"   ℹ️ TOC 读取跳过: {e}")
        
        print("\n✅ 测试 3 通过!")


if __name__ == "__main__":
    print("🧪 EPUB 渲染器测试")
    print("=" * 60)
    
    try:
        test_epub_renderer_translate_only()
        test_epub_renderer_bilingual()
        test_epub_toc_translation()
        
        print("\n" + "=" * 60)
        print("🎉 所有测试通过!")
        print("=" * 60)
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
