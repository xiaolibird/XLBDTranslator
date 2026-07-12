"""
测试双版本PDF输出功能
验证桌面版和移动版PDF能够正确生成
"""
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.core.schema import ContentSegment, SegmentList, Settings
from src.renderer.pdf import PDFRenderer


def create_test_segments() -> SegmentList:
    """创建测试用的片段列表"""
    segments = SegmentList()
    
    # 添加标题
    segments.append(ContentSegment(
        segment_id=1,
        original_text="Test Document",
        translated_text="测试文档",
        segment_type="heading",
        toc_level=2,
        is_new_chapter=True,
        chapter_title="测试文档",
        page_index=0
    ))
    
    # 添加内容段落
    for i in range(5):
        segments.append(ContentSegment(
            segment_id=i + 2,
            original_text=f"This is paragraph {i+1}. It contains some sample text for testing the PDF rendering with different CSS styles.",
            translated_text=f"这是第{i+1}段。它包含一些示例文本，用于测试使用不同CSS样式的PDF渲染功能。",
            segment_type="paragraph",
            toc_level=None,
            is_new_chapter=False,
            chapter_title="测试文档",
            page_index=i // 2  # 每两段换一页
        ))
    
    # 添加子标题
    segments.append(ContentSegment(
        segment_id=7,
        original_text="Subsection",
        translated_text="小节",
        segment_type="heading",
        toc_level=3,
        is_new_chapter=False,
        chapter_title="小节",
        page_index=3
    ))
    
    # 添加更多段落
    for i in range(3):
        segments.append(ContentSegment(
            segment_id=i + 8,
            original_text=f"Another paragraph {i+1}. Testing mobile version readability on iPhone 13 mini.",
            translated_text=f"另一段落{i+1}。测试在iPhone 13 mini上的移动版本可读性。",
            segment_type="paragraph",
            toc_level=None,
            is_new_chapter=False,
            chapter_title="小节",
            page_index=3 + i
        ))
    
    return segments

def main():
    """主测试函数"""
    print("🧪 开始测试双版本PDF输出功能...")
    
    # 创建临时设置
    settings = Settings()
    settings.processing.render_page_markers = True
    
    # 创建测试片段
    segments = create_test_segments()
    print(f"✅ 创建了 {len(segments)} 个测试片段")
    
    # 创建PDF渲染器
    pdf_renderer = PDFRenderer(settings)
    
    # 输出路径
    output_dir = Path(__file__).parent / "output"
    output_dir.mkdir(exist_ok=True)
    
    desktop_path = output_dir / "test_dual_desktop.pdf"
    
    print("\n📄 测试1: 仅生成桌面版...")
    try:
        pdf_renderer.render_to_file(
            segments,
            desktop_path,
            title="Test Document",
            translated_title="测试文档",
            version="desktop",
            generate_both=False
        )
        print(f"✅ 桌面版生成成功: {desktop_path}")
    except Exception as e:
        print(f"❌ 桌面版生成失败: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n📱 测试2: 仅生成移动版...")
    mobile_only_path = output_dir / "test_dual_mobile_only.pdf"
    try:
        pdf_renderer.render_to_file(
            segments,
            mobile_only_path,
            title="Test Document",
            translated_title="测试文档",
            version="mobile",
            generate_both=False
        )
        print(f"✅ 移动版生成成功: {mobile_only_path}")
    except Exception as e:
        print(f"❌ 移动版生成失败: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n📄📱 测试3: 同时生成两个版本...")
    both_path = output_dir / "test_dual_both.pdf"
    try:
        pdf_renderer.render_to_file(
            segments,
            both_path,
            title="Test Document",
            translated_title="测试文档",
            generate_both=True
        )
        print(f"✅ 桌面版生成成功: {both_path}")
        mobile_both_path = output_dir / "test_dual_both_mobile.pdf"
        print(f"✅ 移动版生成成功: {mobile_both_path}")
    except Exception as e:
        print(f"❌ 双版本生成失败: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n✅ 所有测试完成！")
    print(f"📁 输出目录: {output_dir}")

if __name__ == "__main__":
    main()
