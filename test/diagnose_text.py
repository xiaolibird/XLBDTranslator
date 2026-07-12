#!/usr/bin/env python3
"""
诊断翻译文本中的问题字符
检查 output/ 目录下的翻译文件，找出可能导致 PDF 渲染问题的字符
"""

import json
import sys
from pathlib import Path

# 添加项目路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from src.utils.text_cleaner import TextCleaner


def diagnose_translation_files():
    """诊断所有翻译输出文件"""
    
    output_dir = project_root / "output"
    if not output_dir.exists():
        print("❌ output/ 目录不存在")
        return
    
    print("🔍 扫描翻译文件...\n")
    
    total_files = 0
    total_issues = 0
    
    # 遍历所有翻译项目目录
    for project_dir in output_dir.iterdir():
        if not project_dir.is_dir():
            continue
        
        # 检查 checkpoint.json（包含翻译文本）
        checkpoint_file = project_dir / "checkpoint.json"
        if not checkpoint_file.exists():
            continue
        
        total_files += 1
        print(f"📂 检查项目: {project_dir.name}")
        
        try:
            with open(checkpoint_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            segments = data.get('segments', [])
            
            # 统计当前文件的问题
            file_zero_width = 0
            file_control = 0
            file_samples = []
            
            for i, seg in enumerate(segments):
                # 检查译文
                translated = seg.get('translated_text', '')
                if not translated:
                    continue
                
                issues = TextCleaner.diagnose_text(translated)
                
                if issues['zero_width_count'] > 0:
                    file_zero_width += issues['zero_width_count']
                    file_samples.extend(issues['sample_issues'][:2])
                
                if issues['control_char_count'] > 0:
                    file_control += issues['control_char_count']
            
            if file_zero_width > 0 or file_control > 0:
                total_issues += 1
                print(f"  ⚠️  发现问题:")
                print(f"     - 零宽字符: {file_zero_width} 个")
                print(f"     - 控制字符: {file_control} 个")
                
                if file_samples:
                    print(f"  📝 示例:")
                    for sample in file_samples[:3]:
                        print(f"     {sample}")
                print()
            else:
                print(f"  ✅ 无明显问题\n")
        
        except Exception as e:
            print(f"  ❌ 读取失败: {e}\n")
    
    # 总结
    print("=" * 60)
    print(f"📊 诊断完成:")
    print(f"   - 检查文件数: {total_files}")
    print(f"   - 有问题的文件: {total_issues}")
    
    if total_issues > 0:
        print("\n💡 建议:")
        print("   1. 重新翻译这些文件（新代码会自动清理问题字符）")
        print("   2. 或者运行清理脚本修复现有文件")


def clean_existing_files():
    """清理现有的翻译文件"""
    
    output_dir = project_root / "output"
    if not output_dir.exists():
        print("❌ output/ 目录不存在")
        return
    
    print("🧹 清理翻译文件...\n")
    
    cleaned_count = 0
    
    for project_dir in output_dir.iterdir():
        if not project_dir.is_dir():
            continue
        
        checkpoint_file = project_dir / "checkpoint.json"
        if not checkpoint_file.exists():
            continue
        
        try:
            with open(checkpoint_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            segments = data.get('segments', [])
            modified = False
            
            for seg in segments:
                # 清理译文
                if 'translated_text' in seg and seg['translated_text']:
                    original_text = seg['translated_text']
                    cleaned_text = TextCleaner.clean_text(original_text)
                    
                    if cleaned_text != original_text:
                        seg['translated_text'] = cleaned_text
                        modified = True
                
                # 清理章节标题
                if 'chapter_title' in seg and seg['chapter_title']:
                    original_title = seg['chapter_title']
                    cleaned_title = TextCleaner.clean_text(original_title)
                    
                    if cleaned_title != original_title:
                        seg['chapter_title'] = cleaned_title
                        modified = True
            
            if modified:
                # 备份原文件
                backup_file = checkpoint_file.with_suffix('.json.bak')
                checkpoint_file.rename(backup_file)
                
                # 保存清理后的文件
                with open(checkpoint_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                
                print(f"✅ 已清理: {project_dir.name}")
                print(f"   备份: {backup_file.name}")
                cleaned_count += 1
            else:
                print(f"⏭️  跳过: {project_dir.name} (无需清理)")
        
        except Exception as e:
            print(f"❌ 清理失败 {project_dir.name}: {e}")
    
    print(f"\n📊 清理完成: 共处理 {cleaned_count} 个文件")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="诊断或清理翻译文本中的问题字符")
    parser.add_argument(
        'action',
        choices=['diagnose', 'clean'],
        help='操作: diagnose=诊断问题, clean=清理文件'
    )
    
    args = parser.parse_args()
    
    if args.action == 'diagnose':
        diagnose_translation_files()
    elif args.action == 'clean':
        response = input("⚠️  即将修改翻译文件（会创建备份），确认继续？(yes/no): ")
        if response.lower() in ('yes', 'y'):
            clean_existing_files()
        else:
            print("已取消")
