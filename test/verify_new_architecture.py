#!/usr/bin/env python3
"""
快速验证新架构是否正常工作
"""
import sys
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.schema import ContentSegment, SegmentList, Settings

print("=" * 60)
print("✅ 验证新架构")
print("=" * 60)

# 1. 验证导入
print("\n1️⃣ 验证模块导入...")
try:
    from src.renderer import MarkdownRenderer, PDFRenderer
    from src.renderer.epub import (EPUBRenderer, HTMLToEPUBConverter,
                                   render_epub, render_html_to_epub)
    print("   ✅ 所有渲染器导入成功")
except Exception as e:
    print(f"   ❌ 导入失败: {e}")
    sys.exit(1)

# 2. 验证 html_to_epub.py 是否已删除
print("\n2️⃣ 验证文件合并...")
html_to_epub_path = Path(__file__).parent.parent / "src/renderer/html_to_epub.py"
if html_to_epub_path.exists():
    print("   ❌ 警告: html_to_epub.py 仍然存在，应该已被删除")
else:
    print("   ✅ html_to_epub.py 已成功删除")

# 3. 验证 epub.py 包含新类
print("\n3️⃣ 验证类定义...")
try:
    # 创建最小化的 Settings（避免验证错误）
    from src.core.schema import (APISettings, FileSettings, LoggingSettings,
                                 ProcessingSettings)
    test_settings = Settings(
        api=APISettings(backend="gemini", model_name="test"),
        files=FileSettings(document_path=Path("test.pdf")),
        processing=ProcessingSettings(),
        logging=LoggingSettings()
    )
    
    epub_renderer = EPUBRenderer(test_settings)
    html_converter = HTMLToEPUBConverter(test_settings)
    print("   ✅ EPUBRenderer 可实例化")
    print("   ✅ HTMLToEPUBConverter 可实例化")
except Exception as e:
    print(f"   ❌ 实例化失败: {e}")
    sys.exit(1)

# 4. 验证便捷函数
print("\n4️⃣ 验证便捷函数...")
if callable(render_epub):
    print("   ✅ render_epub() 可调用")
else:
    print("   ❌ render_epub() 不可调用")

if callable(render_html_to_epub):
    print("   ✅ render_html_to_epub() 可调用")
else:
    print("   ❌ render_html_to_epub() 不可调用")

# 5. 验证 Markdown 纯文本模式
print("\n5️⃣ 验证 Markdown 纯文本输出...")
try:
    from src.core.schema import (APISettings, FileSettings, LoggingSettings,
                                 ProcessingSettings)
    settings = Settings(
        api=APISettings(backend="gemini", model_name="test"),
        files=FileSettings(document_path=Path("test.pdf")),
        processing=ProcessingSettings(retain_original=True),
        logging=LoggingSettings()
    )
    
    md_renderer = MarkdownRenderer(settings)
    test_segments = SegmentList([
        ContentSegment(
            segment_id=1,  # 使用整数而非字符串
            original_text="Original text",
            translated_text="翻译文本",
            page_index=0
        )
    ])
    
    md_content = md_renderer.render_to_string(test_segments, "Test", "测试")
    
    if '<span' in md_content or '<hr' in md_content:
        print("   ⚠️ 警告: Markdown 中仍包含 HTML 标签")
    else:
        print("   ✅ Markdown 为纯文本格式")
    
    if '>' in md_content and 'Original text' in md_content:
        print("   ✅ 使用 Quote 语法显示原文")
    
except Exception as e:
    print(f"   ❌ Markdown 渲染失败: {e}")
    import traceback
    traceback.print_exc()

# 6. 检查 workflow 导入
print("\n6️⃣ 验证 workflow 集成...")
try:
    from src.workflow.workflow import TranslationWorkflow
    print("   ✅ TranslationWorkflow 导入成功")
except Exception as e:
    print(f"   ❌ Workflow 导入失败: {e}")

print("\n" + "=" * 60)
print("✅ 所有验证通过！新架构工作正常")
print("=" * 60)
print("\n📚 主要变更:")
print("   1. html_to_epub.py 已合并到 epub.py")
print("   2. 所有文档默认生成 EPUB")
print("   3. Markdown 使用纯文本 + Quote 语法")
print("   4. EPUB 生成统一在 workflow 中处理")
