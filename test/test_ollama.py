"""
测试 OllamaTranslator 的简单脚本
"""
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

import pytest

pytest.skip(
    "OllamaTranslator 已被 OpenAICompatibleTranslator 取代（Ollama 走 OpenAI 兼容端点，"
    "见 test_openai_compatible.py）",
    allow_module_level=True,
)

from src.core.schema import ContentSegment, Settings
from src.translator import OllamaTranslator
from src.utils.logger import get_logger

logger = get_logger(__name__)


def test_ollama_connection():
    """测试 Ollama 连接"""
    print("=" * 60)
    print("测试 1: Ollama 服务连接")
    print("=" * 60)
    
    try:
        # 创建最小化配置
        settings = Settings(
            api={'gemini_api_key': 'dummy'},  # 仍需提供但不使用
            files={'document_path': 'dummy.pdf'},
            processing={'translation_mode': 1}
        )
        
        # 初始化翻译器（会自动检查连接）
        translator = OllamaTranslator(settings)
        print("✅ Ollama 连接成功！")
        print(f"   - 服务地址: {translator.base_url}")
        print(f"   - 模型: {translator.model}")
        return True
    except Exception as e:
        print(f"❌ Ollama 连接失败: {e}")
        return False


def test_simple_translation():
    """测试简单翻译"""
    print("\n" + "=" * 60)
    print("测试 2: 简单文本翻译")
    print("=" * 60)
    
    try:
        settings = Settings(
            api={'gemini_api_key': 'dummy'},
            files={'document_path': 'dummy.pdf'},
            processing={'translation_mode': 1}
        )
        
        translator = OllamaTranslator(settings)
        
        # 创建测试段落
        segments = [
            ContentSegment(
                segment_id=1,
                original_text="Hello, world! This is a test.",
                content_type="text"
            ),
            ContentSegment(
                segment_id=2,
                original_text="Artificial Intelligence is transforming our world.",
                content_type="text"
            )
        ]
        
        print("\n原文:")
        for seg in segments:
            print(f"  [{seg.segment_id}] {seg.original_text}")
        
        print("\n正在翻译...")
        results = translator.translate_batch(segments)
        
        print("\n译文:")
        for i, result in enumerate(results):
            print(f"  [{segments[i].segment_id}] {result}")
        
        print("\n✅ 翻译测试成功！")
        return True
    except Exception as e:
        print(f"❌ 翻译测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_title_translation():
    """测试标题翻译"""
    print("\n" + "=" * 60)
    print("测试 3: 标题翻译")
    print("=" * 60)
    
    try:
        settings = Settings(
            api={'gemini_api_key': 'dummy'},
            files={'document_path': 'dummy.pdf'},
            processing={'translation_mode': 1}
        )
        
        translator = OllamaTranslator(settings)
        
        titles = [
            "Introduction",
            "Chapter 1: Getting Started",
            "Conclusion"
        ]
        
        print("\n原标题:")
        for title in titles:
            print(f"  - {title}")
        
        print("\n正在翻译...")
        result_map = translator.translate_titles(titles)
        
        print("\n译文:")
        for original, translated in result_map.items():
            print(f"  - {original} -> {translated}")
        
        print("\n✅ 标题翻译测试成功！")
        return True
    except Exception as e:
        print(f"❌ 标题翻译测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """运行所有测试"""
    print("\n")
    print("🚀 Ollama Translator 测试套件")
    print("=" * 60)
    print()
    
    # 测试 1: 连接测试
    if not test_ollama_connection():
        print("\n⚠️  无法连接到 Ollama 服务，请确保：")
        print("   1. Ollama 已安装并运行 (ollama serve)")
        print("   2. 已安装所需模型 (ollama pull qwen2.5:14b)")
        print("   3. 服务地址正确 (默认: http://localhost:11434)")
        return
    
    # 测试 2: 简单翻译
    test_simple_translation()
    
    # 测试 3: 标题翻译
    test_title_translation()
    
    print("\n" + "=" * 60)
    print("✅ 所有测试完成！")
    print("=" * 60)


if __name__ == "__main__":
    main()
