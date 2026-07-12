"""
测试渲染器重构后的功能
验证：
1. 标题翻译格式：{翻译后的书名} - {原名}
2. 层级 emoji：📚, 📖, 📄, 📝, 📌
3. PAGE_INFO 注释生成
4. 面包屑/全页码模式检测
"""
import sys
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.schema import ContentSegment, SegmentList, Settings


def create_test_segments():
    """创建测试用的 SegmentList"""
    segments = SegmentList()
    
    # Segment 1: 一级章节标题
    segments.append(ContentSegment(
        segment_id=1,
        original_text="Chapter 1: Introduction",
        translated_text="第一章：简介",
        is_new_chapter=True,
        chapter_title="Chapter 1: Introduction",
        toc_level=1,
        page_index=0,
        content_type="text"
    ))
    
    # Segment 2: 普通文本
    segments.append(ContentSegment(
        segment_id=2,
        original_text="This is the first paragraph.",
        translated_text="这是第一段。",
        is_new_chapter=False,
        page_index=0,
        content_type="text"
    ))
    
    # Segment 3: 二级章节标题
    segments.append(ContentSegment(
        segment_id=3,
        original_text="1.1 Background",
        translated_text="1.1 背景",
        is_new_chapter=True,
        chapter_title="1.1 Background",
        toc_level=2,
        page_index=1,
        content_type="text"
    ))
    
    # Segment 4: 普通文本（新页）
    segments.append(ContentSegment(
        segment_id=4,
        original_text="This is background information.",
        translated_text="这是背景信息。",
        is_new_chapter=False,
        page_index=2,
        content_type="text"
    ))
    
    # Segment 5: 三级章节标题
    segments.append(ContentSegment(
        segment_id=5,
        original_text="1.1.1 Historical Context",
        translated_text="1.1.1 历史背景",
        is_new_chapter=True,
        chapter_title="1.1.1 Historical Context",
        toc_level=3,
        page_index=3,
        content_type="text"
    ))
    
    return segments


def create_breadcrumb_segments():
    """创建面包屑模式的测试 SegmentList"""
    segments = SegmentList()
    
    # 面包屑标题
    segments.append(ContentSegment(
        segment_id=1,
        original_text="Book > Part 1 > Chapter 1",
        translated_text="书籍 > 第一部分 > 第一章",
        is_new_chapter=True,
        chapter_title="Book > Part 1 > Chapter 1",
        toc_level=1,
        page_index=0,
        content_type="text"
    ))
    
    segments.append(ContentSegment(
        segment_id=2,
        original_text="Content here.",
        translated_text="这里是内容。",
        is_new_chapter=False,
        page_index=1,
        content_type="text"
    ))
    
    return segments


def create_page_only_segments():
    """创建全页码模式的测试 SegmentList（新契约：无任何章节信息，仅页码）"""
    segments = SegmentList()

    segments.append(ContentSegment(
        segment_id=1,
        original_text="Content on page 1.",
        translated_text="第1页的内容。",
        is_new_chapter=False,
        page_index=0,
        content_type="text"
    ))

    segments.append(ContentSegment(
        segment_id=2,
        original_text="Content on page 2.",
        translated_text="第2页的内容。",
        is_new_chapter=False,
        page_index=1,
        content_type="text"
    ))

    return segments


def test_markdown_renderer():
    """测试 Markdown 渲染器"""
    from src.renderer.markdown import MarkdownRenderer
    
    print("=" * 60)
    print("测试 Markdown 渲染器")
    print("=" * 60)
    
    # 创建 mock settings
    class MockDocument:
        retain_original = True
    
    class MockProcessing:
        render_page_markers = True
    
    class MockSettings:
        document = MockDocument()
        processing = MockProcessing()
    
    settings = MockSettings()
    renderer = MarkdownRenderer(settings)
    
    # 测试 1: 普通层级模式
    print("\n--- 测试 1: 普通层级模式 ---")
    segments = create_test_segments()
    result = renderer.render_to_string(
        segments, 
        title="Test Book",
        translated_title="测试书籍"
    )
    print(result[:1500] + "..." if len(result) > 1500 else result)
    
    # 验证标题格式
    assert "# 测试书籍 - Test Book" in result, "标题格式不正确"
    print("✅ 标题格式正确: # 测试书籍 - Test Book")
    
    # 验证层级 emoji
    assert "📚" in result, "缺少一级标题 emoji 📚"
    assert "📖" in result, "缺少二级标题 emoji 📖"
    assert "📄" in result, "缺少三级标题 emoji 📄"
    print("✅ 层级 emoji 正确: 📚, 📖, 📄")
    
    # 验证页码标记（新版渲染器用可见标题行取代了 PAGE_INFO 注释）
    assert "--- 原文第 1 页 ---" in result, "缺少页码标记"
    print("✅ 页码标记已生成")
    
    # 测试 2: 面包屑模式（新契约：由 settings.processing.use_breadcrumb 配置驱动，而非嗅探文本）
    print("\n--- 测试 2: 面包屑模式 ---")
    breadcrumb_segments = create_breadcrumb_segments()
    settings.processing.use_breadcrumb = True
    try:
        title_mode = renderer._detect_title_mode(breadcrumb_segments)
        print(f"检测到的模式: {title_mode}")
        assert title_mode == 'breadcrumb', f"期望 breadcrumb，得到 {title_mode}"
        print("✅ 面包屑模式检测正确")

        result2 = renderer.render_to_string(breadcrumb_segments, "Breadcrumb Book", "面包屑书籍")
        # 面包屑模式使用 🧭
        assert "🧭" in result2, "面包屑模式应使用 🧭"
        print("✅ 面包屑模式使用 🧭")
    finally:
        settings.processing.use_breadcrumb = False
    
    # 测试 3: 全页码模式
    print("\n--- 测试 3: 全页码模式 ---")
    page_only_segments = create_page_only_segments()
    title_mode = renderer._detect_title_mode(page_only_segments)
    print(f"检测到的模式: {title_mode}")
    assert title_mode == 'page_only', f"期望 page_only，得到 {title_mode}"
    print("✅ 全页码模式检测正确")
    
    result3 = renderer.render_to_string(page_only_segments, "Page Only Book", "全页码书籍")
    # 全页码模式下无章节标题，结构信息以页码标记呈现
    assert "--- 原文第 1 页 ---" in result3, "全页码模式应渲染页码标记"
    print("✅ 全页码模式渲染页码标记")
    
    print("\n" + "=" * 60)
    print("✅ Markdown 渲染器测试全部通过!")
    print("=" * 60)


def test_pdf_renderer():
    """测试 PDF 渲染器（仅测试 HTML 生成，不实际生成 PDF）"""
    print("\n" + "=" * 60)
    print("测试 PDF 渲染器 (HTML 生成)")
    print("=" * 60)
    
    # 创建 mock settings
    class MockDocument:
        retain_original = False
    
    class MockProcessing:
        render_page_markers = True
    
    class MockFiles:
        pass
    
    class MockSettings:
        document = MockDocument()
        processing = MockProcessing()
        files = MockFiles()
    
    settings = MockSettings()
    
    from src.renderer.pdf import PDFRenderer
    pdf_renderer = PDFRenderer(settings)
    
    # 测试元数据构建
    segments = create_test_segments()
    metadata = pdf_renderer._build_segment_metadata(segments)
    
    print("\n--- 测试元数据构建 ---")
    for idx, meta in metadata.items():
        print(f"  Segment {idx}: page={meta['page_index']}, level={meta['toc_level']}, is_chapter={meta['is_new_chapter']}")
    
    assert len(metadata) == 5, f"期望 5 个 segment，得到 {len(metadata)}"
    assert metadata[0]['toc_level'] == 1, "第一个 segment 应该是 level 1"
    assert metadata[2]['toc_level'] == 2, "第三个 segment 应该是 level 2"
    print("✅ 元数据构建正确")
    
    # 测试层级间距配置
    print("\n--- 测试层级间距配置 ---")
    for level, spacing in pdf_renderer.TOC_LEVEL_SPACING.items():
        print(f"  h{level}: {spacing}em")
    
    assert pdf_renderer.TOC_LEVEL_SPACING[2] == 0.20, "h2 间距应为 0.20em"
    assert pdf_renderer.TOC_LEVEL_SPACING[5] == 0.05, "h5 间距应为 0.05em"
    print("✅ 层级间距配置正确")
    
    # 测试 render_to_string
    print("\n--- 测试 Markdown 输出 ---")
    result = pdf_renderer.render_to_string(segments, "Test", "测试")
    print(result[:800] + "..." if len(result) > 800 else result)
    
    print("\n" + "=" * 60)
    print("✅ PDF 渲染器测试全部通过!")
    print("=" * 60)


if __name__ == "__main__":
    test_markdown_renderer()
    test_pdf_renderer()
    
    print("\n" + "=" * 60)
    print("🎉 所有测试通过！")
    print("=" * 60)
