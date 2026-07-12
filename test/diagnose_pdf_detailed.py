#!/usr/bin/env python3
"""
诊断 PDF 渲染问题的详细工具
比较生成的 HTML 和最终 PDF，找出渲染差异
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))


def analyze_debug_html():
    """分析调试 HTML 文件"""
    print("=" * 60)
    print("分析调试 HTML 文件")
    print("=" * 60)
    
    output_dir = project_root / "output"
    
    debug_htmls = list(output_dir.glob("*/*_debug.html"))
    
    if not debug_htmls:
        print("❌ 未找到调试 HTML 文件")
        print("💡 提示: 先运行翻译生成 PDF，会自动生成 *_debug.html 文件")
        return
    
    for html_path in debug_htmls:
        print(f"\n📄 分析文件: {html_path.name}")
        
        try:
            content = html_path.read_text(encoding='utf-8')
            
            # 统计信息
            stats = {
                'total_chars': len(content),
                'blockquotes': content.count('<blockquote'),
                'h2_headers': content.count('<h2'),
                'h3_headers': content.count('<h3'),
                'h4_headers': content.count('<h4'),
                'h5_headers': content.count('<h5'),
                'h6_headers': content.count('<h6'),
                'page_markers': content.count('data-source-page'),
            }
            
            print(f"   总字符数: {stats['total_chars']:,}")
            print(f"   引用块: {stats['blockquotes']}")
            print(f"   标题数: h2={stats['h2_headers']}, h3={stats['h3_headers']}, "
                  f"h4={stats['h4_headers']}, h5={stats['h5_headers']}, h6={stats['h6_headers']}")
            print(f"   页码标记: {stats['page_markers']}")
            
            # 检查可疑内容
            issues = []
            
            # 检查是否有超长行
            lines = content.split('\n')
            long_lines = [i for i, line in enumerate(lines) if len(line) > 10000]
            if long_lines:
                issues.append(f"超长行 ({len(long_lines)} 行): 可能导致渲染问题")
            
            # 检查是否有未闭合的标签
            open_tags = content.count('<blockquote')
            close_tags = content.count('</blockquote>')
            if open_tags != close_tags:
                issues.append(f"blockquote 标签不匹配: {open_tags} 个开始, {close_tags} 个结束")
            
            # 检查是否有空的 blockquote
            import re
            empty_bq = re.findall(r'<blockquote[^>]*>\s*</blockquote>', content)
            if empty_bq:
                issues.append(f"空引用块: {len(empty_bq)} 个")
            
            # 检查字符编码问题
            try:
                content.encode('utf-8')
            except UnicodeEncodeError as e:
                issues.append(f"字符编码错误: {e}")
            
            if issues:
                print("\n   ⚠️  发现潜在问题:")
                for issue in issues:
                    print(f"      - {issue}")
            else:
                print("\n   ✅ HTML 结构正常")
            
            # 提取一些样本内容
            print("\n   📝 内容样本:")
            blockquote_match = re.search(r'<blockquote[^>]*>(.*?)</blockquote>', content, re.DOTALL)
            if blockquote_match:
                sample = blockquote_match.group(1)[:200]
                print(f"      引用块示例: {sample}...")
            
        except Exception as e:
            print(f"   ❌ 分析失败: {e}")


def compare_with_pdf():
    """比较 HTML 和 PDF"""
    print("\n" + "=" * 60)
    print("比较 HTML 和 PDF 渲染结果")
    print("=" * 60)
    
    output_dir = project_root / "output"
    
    for project_dir in output_dir.iterdir():
        if not project_dir.is_dir():
            continue
        
        # 查找 HTML 和 PDF 文件
        pdf_files = list(project_dir.glob("*.pdf"))
        html_files = list(project_dir.glob("*_debug.html"))
        
        if not pdf_files or not html_files:
            continue
        
        pdf_path = pdf_files[0]
        html_path = html_files[0]
        
        print(f"\n📂 项目: {project_dir.name}")
        print(f"   HTML: {html_path.name} ({html_path.stat().st_size:,} bytes)")
        print(f"   PDF:  {pdf_path.name} ({pdf_path.stat().st_size:,} bytes)")
        
        # 检查 PDF 文件大小是否异常小
        pdf_size = pdf_path.stat().st_size
        if pdf_size < 10000:  # 小于 10KB 可能有问题
            print(f"   ⚠️  PDF 文件过小 ({pdf_size} bytes)，可能渲染失败")
        
        # 尝试用 WeasyPrint 重新渲染
        try:
            from weasyprint import HTML
            from weasyprint.text.fonts import FontConfiguration
            
            html_content = html_path.read_text(encoding='utf-8')
            font_config = FontConfiguration()
            
            # 重新渲染
            test_pdf = HTML(string=html_content).write_pdf(font_config=font_config)
            print(f"   🔄 重新渲染: {len(test_pdf):,} bytes")
            
            # 比较大小差异
            size_diff = abs(len(test_pdf) - pdf_size)
            if size_diff > 1000:
                print(f"   ⚠️  大小差异: {size_diff:,} bytes (可能配置不同)")
            
        except Exception as e:
            print(f"   ❌ 重新渲染失败: {e}")


def suggest_fixes():
    """提供修复建议"""
    print("\n" + "=" * 60)
    print("常见问题和修复建议")
    print("=" * 60)
    
    suggestions = [
        ("字体显示不全", [
            "1. 确认 CSS 中的字体名称正确（区分大小写）",
            "2. 使用系统自带字体（如 STHeiti）而不是第三方字体",
            "3. 在 font-family 中添加多个备选字体",
        ]),
        ("内容被裁剪或隐藏", [
            "1. 检查 CSS 的 overflow 属性",
            "2. 检查页面边距设置（@page margin）",
            "3. 检查 blockquote 的 padding 和 margin",
            "4. 某些 CSS 属性在 PDF 中不支持（如 position: fixed）",
        ]),
        ("文字重叠或错位", [
            "1. 检查 line-height 设置",
            "2. 检查字体大小和容器宽度",
            "3. WeasyPrint 不支持某些高级 CSS（如 flexbox 的某些属性）",
        ]),
        ("需要拖拽才显示", [
            "1. 可能是 PDF 查看器的渲染缓存问题",
            "2. 尝试使用不同的 PDF 阅读器",
            "3. 文本可能被背景色覆盖（检查 z-index）",
            "4. 字体嵌入不完整（检查 font-config）",
        ]),
    ]
    
    for problem, fixes in suggestions:
        print(f"\n🔧 {problem}:")
        for fix in fixes:
            print(f"   {fix}")


if __name__ == "__main__":
    print("🔍 PDF 渲染问题详细诊断\n")
    
    analyze_debug_html()
    compare_with_pdf()
    suggest_fixes()
    
    print("\n" + "=" * 60)
    print("💡 下一步:")
    print("=" * 60)
    print("1. 检查上面列出的具体问题")
    print("2. 打开 *_debug.html 文件在浏览器中查看")
    print("3. 比较浏览器显示和 PDF 显示的差异")
    print("4. 根据差异调整 CSS 样式")
