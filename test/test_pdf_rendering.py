#!/usr/bin/env python3
"""
测试 PDF 渲染问题的各种可能原因
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

import pytest

import markdown2

try:
    from weasyprint import CSS, HTML
    from weasyprint.text.fonts import FontConfiguration
except (ImportError, OSError) as e:
    # weasyprint 依赖系统库（libgobject 等），本机缺失时跳过而非报收集错误
    pytest.skip(f"weasyprint 不可用: {e}", allow_module_level=True)


def test_font_embedding():
    """测试字体嵌入"""
    print("=" * 60)
    print("测试 1: 字体嵌入")
    print("=" * 60)
    
    test_texts = [
        ("基本中文", "这是测试文本"),
        ("标点符号", "！？。，、；：""''《》【】（）"),
        ("特殊符号", "——…€£¥§©®™"),
        ("繁体", "繁體中文測試"),
        ("混合", "English + 中文 + 123"),
    ]
    
    for name, text in test_texts:
        html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body><p>{text}</p></body></html>"""
        
        try:
            font_config = FontConfiguration()
            pdf_bytes = HTML(string=html).write_pdf(font_config=font_config)
            print(f"✅ {name}: {len(pdf_bytes)} bytes")
        except Exception as e:
            print(f"❌ {name}: {e}")


def test_long_text_rendering():
    """测试长文本渲染"""
    print("\n" + "=" * 60)
    print("测试 2: 长文本渲染")
    print("=" * 60)
    
    # 生成长文本
    long_text = "这是一段很长的文本。" * 100
    
    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body><blockquote><p>{long_text}</p></blockquote></body></html>"""
    
    try:
        font_config = FontConfiguration()
        pdf_bytes = HTML(string=html).write_pdf(font_config=font_config)
        print(f"✅ 长文本渲染: {len(pdf_bytes)} bytes")
    except Exception as e:
        print(f"❌ 长文本渲染失败: {e}")


def test_markdown_html_conversion():
    """测试 Markdown 到 HTML 的转换"""
    print("\n" + "=" * 60)
    print("测试 3: Markdown HTML 转换")
    print("=" * 60)
    
    markdown_text = """
## 📚 测试章节

这是正文内容，包含**粗体**和*斜体*。

> 这是引用块
> 包含多行

- 列表项 1
- 列表项 2

###### --- 原文第 1 页 ---
"""
    
    html = markdown2.markdown(
        markdown_text,
        extras=[
            "fenced-code-blocks",
            "tables",
            "header-ids",
            "code-friendly",
        ]
    )
    
    print("Markdown 转换后的 HTML:")
    print(html[:500])
    print("...")
    
    # 测试渲染
    full_html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body>{html}</body></html>"""
    
    try:
        font_config = FontConfiguration()
        pdf_bytes = HTML(string=full_html).write_pdf(font_config=font_config)
        print(f"\n✅ Markdown PDF 渲染: {len(pdf_bytes)} bytes")
    except Exception as e:
        print(f"\n❌ Markdown PDF 渲染失败: {e}")


def test_css_rendering():
    """测试带 CSS 的渲染"""
    print("\n" + "=" * 60)
    print("测试 4: CSS 样式渲染")
    print("=" * 60)
    
    css_path = project_root / "config" / "pdf_style.css"
    
    if not css_path.exists():
        print(f"❌ CSS 文件不存在: {css_path}")
        return
    
    css_content = css_path.read_text(encoding='utf-8')
    
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <style>{css_content}</style>
</head>
<body>
    <h2>📚 测试标题</h2>
    <blockquote data-source-page="1">
        <p>这是测试引用块的内容。</p>
    </blockquote>
</body>
</html>"""
    
    try:
        font_config = FontConfiguration()
        pdf_bytes = HTML(string=html).write_pdf(font_config=font_config)
        print(f"✅ CSS 样式 PDF 渲染: {len(pdf_bytes)} bytes")
        
        # 保存测试 PDF
        test_pdf_path = project_root / "test_css_rendering.pdf"
        test_pdf_path.write_bytes(pdf_bytes)
        print(f"📄 已保存测试 PDF: {test_pdf_path}")
        
    except Exception as e:
        print(f"❌ CSS 样式 PDF 渲染失败: {e}")
        import traceback
        traceback.print_exc()


def test_actual_translation_file():
    """测试实际的翻译文件"""
    print("\n" + "=" * 60)
    print("测试 5: 实际翻译文件")
    print("=" * 60)
    
    output_dir = project_root / "output"
    if not output_dir.is_dir():
        # 注意：本文件的 project_root 锚在 test/ 目录（历史遗留），所以这里查的是
        # test/output——任何机器上都不存在，本测试实际恒 skip。守卫的价值是防 CI
        # 在 weasyprint 可用后第一次激活本测试时对缺失目录裸 iterdir 抛 FileNotFoundError。
        # 不改锚点：改成仓库根会让单测突然遍历 1.5G 真实产物目录并渲染写盘，副作用更大。
        pytest.skip("test/output 不存在（本测试因锚点历史原因恒 skip，守卫防 CI 裸抛）")

    # 找到第一个有 checkpoint 的项目
    for project_dir in output_dir.iterdir():
        if not project_dir.is_dir():
            continue
        
        checkpoint_file = project_dir / "checkpoint.json"
        if not checkpoint_file.exists():
            continue
        
        print(f"📂 测试项目: {project_dir.name}")
        
        # 尝试渲染为 PDF
        try:
            import json
            with open(checkpoint_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            segments = data.get('segments', [])
            print(f"   Segments 数量: {len(segments)}")
            
            # 构造简化的 Markdown
            md_lines = []
            for i, seg in enumerate(segments[:5]):  # 只测试前 5 个
                if seg.get('translated_text'):
                    md_lines.append(seg['translated_text'])
            
            markdown_text = '\n\n'.join(md_lines)
            
            # 转换为 HTML
            html_body = markdown2.markdown(markdown_text, extras=["fenced-code-blocks"])
            
            full_html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body>{html_body}</body></html>"""
            
            # 渲染 PDF
            font_config = FontConfiguration()
            pdf_bytes = HTML(string=full_html).write_pdf(font_config=font_config)
            print(f"   ✅ 渲染成功: {len(pdf_bytes)} bytes")
            
            # 保存测试 PDF
            test_pdf_path = project_root / f"test_actual_{project_dir.name[:8]}.pdf"
            test_pdf_path.write_bytes(pdf_bytes)
            print(f"   📄 已保存: {test_pdf_path}")
            
            return  # 只测试第一个
            
        except Exception as e:
            print(f"   ❌ 渲染失败: {e}")
            import traceback
            traceback.print_exc()
            return


if __name__ == "__main__":
    print("🔍 PDF 渲染问题诊断工具\n")
    
    test_font_embedding()
    test_long_text_rendering()
    test_markdown_html_conversion()
    test_css_rendering()
    test_actual_translation_file()
    
    print("\n" + "=" * 60)
    print("✅ 所有测试完成")
    print("=" * 60)
    print("\n请检查生成的测试 PDF 文件：")
    print("- test_css_rendering.pdf")
    print("- test_actual_*.pdf")
    print("\n如果这些测试文件正常，但实际翻译 PDF 有问题，")
    print("可能是：")
    print("1. 特定 segment 的内容导致渲染异常")
    print("2. 文档结构过于复杂")
    print("3. CSS 样式与某些内容冲突")
