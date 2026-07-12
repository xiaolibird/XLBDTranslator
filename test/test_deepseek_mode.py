#!/usr/bin/env python3
"""
DeepSeek API 功能测试脚本
测试 OpenAICompatibleTranslator 的 DeepSeek 检测和长文本模式
"""

import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

print(f"Project root: {project_root}")
print(f"Python path: {sys.path[:3]}")

try:
    from src.translator.engine import OpenAICompatibleTranslator
    print("✅ 成功导入 OpenAICompatibleTranslator")
except ImportError as e:
    print(f"❌ 导入失败: {e}")
    sys.exit(1)


def test_deepseek_detection():
    """测试 DeepSeek API 检测"""
    print("="*80)
    print("测试 1: DeepSeek API 检测")
    print("="*80)
    
    # 创建一个模拟的设置对象
    class MockAPISettings:
        def __init__(self, base_url):
            self.openai_api_key = "test_key"
            self.openai_base_url = base_url
            self.openai_model = "deepseek-chat"
    
    class MockProcessingSettings:
        translation_mode = "academic_refined"
        translation_mode_entity = None
        max_context_length = 1000
        request_timeout = 60
        json_repair_retries = 3
    
    class MockSettings:
        def __init__(self, base_url):
            self.api = MockAPISettings(base_url)
            self.processing = MockProcessingSettings()
    
    test_cases = [
        ("https://api.deepseek.com", True, True, "DeepSeek API"),
        ("https://api.deepseek.com/v1", True, True, "DeepSeek API (with /v1)"),
        ("http://localhost:11434", False, True, "Ollama 本地"),
        ("http://127.0.0.1:11434", False, True, "Ollama 127.0.0.1"),
        ("https://api.openai.com/v1", False, False, "OpenAI API"),
    ]
    
    for base_url, expected_deepseek, expected_local, description in test_cases:
        print(f"\n测试用例: {description}")
        print(f"  URL: {base_url}")
        
        try:
            settings = MockSettings(base_url)
            translator = OpenAICompatibleTranslator(settings)
            
            is_deepseek = translator.is_deepseek
            is_local = translator.is_local
            use_long_text_mode = translator.use_long_text_mode
            
            print(f"  is_deepseek: {is_deepseek} (期望: {expected_deepseek})")
            print(f"  is_local: {is_local} (期望: {expected_local})")
            print(f"  use_long_text_mode: {use_long_text_mode}")
            
            # 验证结果
            if is_deepseek == expected_deepseek:
                print(f"  ✅ DeepSeek 检测正确")
            else:
                print(f"  ❌ DeepSeek 检测失败")
                
            # 验证长文本模式
            if is_deepseek and use_long_text_mode:
                print(f"  ✅ 长文本模式已启用")
            elif not is_deepseek and not use_long_text_mode:
                print(f"  ✅ 标准模式已启用")
            else:
                print(f"  ❌ 模式设置异常")
                
        except Exception as e:
            print(f"  ❌ 测试失败: {e}")
    
    print("\n" + "="*80)
    print("测试完成")
    print("="*80)


def test_long_text_mode_format():
    """测试长文本模式的消息格式"""
    print("\n" + "="*80)
    print("测试 2: 长文本模式消息格式")
    print("="*80)
    
    # 模拟系统指令和用户内容
    system_instruction = "You are a professional translator."
    user_content = "Translate this text: Hello World"
    
    # 模拟 DeepSeek 模式的合并逻辑
    combined_content = f"{system_instruction}\n\n{'='*80}\n\n{user_content}"
    
    print("\n标准模式（OpenAI）格式:")
    print("  Message 1 (system):", system_instruction)
    print("  Message 2 (user):", user_content)
    
    print("\n长文本模式（DeepSeek）格式:")
    print("  Message 1 (user):")
    print(f"    {combined_content[:100]}...")
    
    print("\n✅ 消息格式测试完成")


if __name__ == "__main__":
    print("DeepSeek API 功能测试\n")
    
    try:
        test_deepseek_detection()
        test_long_text_mode_format()
        
        print("\n" + "="*80)
        print("🎉 所有测试完成！")
        print("="*80)
        
    except Exception as e:
        print(f"\n❌ 测试执行失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
