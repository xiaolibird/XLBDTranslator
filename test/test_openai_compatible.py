"""
集成测试：验证 Ollama + gemma3 实战翻译性能
硬件目标：M2 Pro 16GB | macOS 26.1
"""
import asyncio
import json
import sys
import time
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.core.schema import ContentSegment, Settings
from src.translator.engine import OpenAICompatibleTranslator
from src.translator.support import PromptManager
from src.utils.logger import get_logger
from src.utils.ui import load_modes_config
from src.workflow.builder import SettingsBuilder

# --- 硬件与模型硬编码参数（对齐 config.env）---
OLLAMA_BASE_URL = "http://127.0.0.1:11434"  # 使用 127.0.0.1 而非 localhost
TARGET_MODEL = "gemma3"  # 对齐 config.env 中的 API__OPENAI_MODEL
MAX_CONTEXT = 512       # 极度降低：从 1024 到 512（适应最小上下文窗口）
TIMEOUT = 60            # 对齐 config.env 的 PROCESSING__REQUEST_TIMEOUT（本地会自动用120s）
BATCH_SIZE = 1          # 保持：单批处理（减少并发压力）
TRANSLATION_MODE = "1"  # 对齐 config.env 的 PROCESSING__TRANSLATION_MODE

def setup_test_settings():
    """初始化符合 M2 Pro 限制的设置（参数对齐 config.env）"""
    # 使用测试 EPUB 文件
    test_file = project_root / "test" / "test.epub"
    if not test_file.exists():
        raise FileNotFoundError(f"请先运行: python test/create_test_epub.py 创建测试文件")
    
    # 加载翻译模式配置
    modes_config_path = project_root / "config" / "modes.json"
    modes = load_modes_config(modes_config_path)
    
    # 获取指定的翻译模式
    if TRANSLATION_MODE not in modes:
        raise ValueError(f"翻译模式 '{TRANSLATION_MODE}' 不存在于 modes.json 中")
    
    selected_mode = modes[TRANSLATION_MODE]
    
    # 直接创建 Settings 对象
    settings = Settings(
        api={
            'TRANSLATOR_PROVIDER': 'openai-compatible',
            'OPENAI_API_KEY': 'dummy_key_for_ollama',
            'OPENAI_BASE_URL': OLLAMA_BASE_URL,
            'OPENAI_MODEL': TARGET_MODEL,
            'GEMINI_API_KEY': 'DUMMY_KEY'
        },
        files={
            'DOCUMENT_PATH': str(test_file),
            'OUTPUT_DIR': './output/',
            'LOG_FILE': 'logs/test.log',
            'MODES_CONFIG_PATH': str(modes_config_path)
        },
        processing={
            'TRANSLATION_MODE': TRANSLATION_MODE,
            'BATCH_SIZE': BATCH_SIZE,
            'MAX_CONTEXT_LENGTH': MAX_CONTEXT,
            'MAX_RETRIES': 3,
            'REQUEST_TIMEOUT': TIMEOUT,
            'RATE_LIMIT_DELAY': 1.0,
            'VISION_RATE_LIMIT_DELAY': 2.0,
            'MIN_CHUNK_SIZE': 200,
            'MAX_CHUNK_SIZE': 2000,
            'enable_async': False,
            'async_threshold': 999,
            'async_max_workers': 5,
            'json_repair_retries': 2,
            'enable_checkpoint': True,
            'checkpoint_interval': 1,
            'enable_gemini_caching': False,
            'cache_ttl_hours': 1,
            'use_rich_progress': True,
            'translation_mode_entity': selected_mode
        },
        logging={
            'LOG_LEVEL': 'INFO'
        }
    )
    
    return settings

def create_mock_segments(count=5):
    """从真实文档中读取5个段落进行测试"""
    segments = []
    
    # 从 structure_map.json 中读取真实段落
    import json
    structure_file = project_root / "output" / "476ddb5f13c2cd5cebb2d1eec362c387" / "structure_map.json"
    
    if structure_file.exists():
        with open(structure_file, 'r', encoding='utf-8') as f:
            structure_data = json.load(f)
        
        # 选择5个有内容的段落进行测试
        test_segments = [7, 8, 9, 10, 11]  # segment_id 7-11
        
        for seg_id in test_segments[:count]:
            for item in structure_data:
                if item['segment_id'] == seg_id and item['original_text'].strip():
                    segments.append(ContentSegment(
                        segment_id=item['segment_id'],
                        content_type="text",
                        original_text=item['original_text'][:600],  # 限制长度避免过长
                        page_index=item.get('page_index', 0)
                    ))
                    break
    
    # 如果没有找到真实段落，使用模拟数据
    if not segments:
        sample_texts = [
            "The Unified Memory Architecture (UMA) in M2 Pro chips represents a significant advancement in computer hardware design.",
            "Generative AI models require significant computational resources for inference.",
            "Local deployment of Large Language Models ensures data privacy.",
            "Biomedical Engineering combines engineering with biological sciences.",
            "Hardware optimization becomes crucial for resource-constrained environments."
        ]
        for i in range(count):
            segments.append(ContentSegment(
                segment_id=i + 1,
                content_type="text",
                original_text=sample_texts[i % len(sample_texts)],
                page_index=0
            ))
    
    return segments

def main():
    settings = setup_test_settings()
    # 同步测试，不需要 asyncio
    print(f"🚀 启动 Ollama + {TARGET_MODEL} 压力测试")
    print(f"💻 硬件环境: MacBook Pro M2 Pro (16GB RAM)")
    print("-" * 70)

    translator = OpenAICompatibleTranslator(settings)
    
    # 初始化 PromptManager 并植入 system_instruction 和 modes
    prompt_manager = PromptManager(settings)
    system_instruction = prompt_manager.get_system_instruction(use_vision=False)
    
    print(f"📝 已植入系统指令: {system_instruction[:100]}...")
    print(f"🎭 翻译模式: {settings.processing.translation_mode}")
    print(f"📋 模式详情: {settings.processing.translation_mode_entity.name if settings.processing.translation_mode_entity else 'None'}")
    
    # 1. 验证初始化状态
    if not translator.is_local:
        print("❌ 错误: 翻译器未识别为本地模式，请检查 URL 配置")
        return

    print(f"✅ 翻译器状态: 本地模式检测成功")
    
    # 本地模式会强制返回 None（同步翻译）
    if translator.async_translator is None:
        print(f"✅ 翻译模式: 同步模式（本地降低功耗）")
    else:
        print(f"⚠️  异步模式意外启用")

    # 2. 模拟批量翻译测试（带上下文）
    test_segments = create_mock_segments(count=5)  # 测试 5 个段落
    
    # 创建更长的上下文（模拟真实文档的前文）
    long_context = """
This chapter explores the fundamental principles of modern computing architectures and their implications for artificial intelligence workloads. The evolution of computer hardware has been driven by the increasing demands of AI applications, from simple machine learning algorithms to complex deep learning models.

The Unified Memory Architecture represents a paradigm shift in how processors access and manage memory. Traditional systems maintained separate memory pools for CPU and GPU operations, requiring expensive data transfers. UMA eliminates this bottleneck by providing a single, coherent memory space.

Generative AI models have revolutionized natural language processing. These models can understand context, generate coherent text, and perform complex reasoning tasks. However, their computational requirements are substantial, often demanding billions of parameters.

Hardware optimization becomes crucial when deploying these models in resource-constrained environments. The M2 Pro chip represents an interesting case study in balancing performance with power efficiency.
"""
    
    print(f"\n任务: 翻译 {len(test_segments)} 个段落...")
    print(f"📄 上下文长度: {len(long_context)} 字符")
    
    # 打印完整的输入给模型的文本
    print("\n" + "="*80)
    print("🔍 完整输入给模型的文本预览:")
    print("="*80)
    
    # 模拟构建完整的输入
    prompt_manager = PromptManager(settings)
    system_instruction = prompt_manager.get_system_instruction(use_vision=False)
    
    print(f"📝 System Instruction (前200字符):\n{system_instruction[:200]}...")
    print(f"\n🎭 Mode 配置: {settings.processing.translation_mode_entity.name if settings.processing.translation_mode_entity else 'None'}")
    print(f"\n📖 Context 前文 (前300字符):\n{long_context.strip()[:300]}...")
    
    # 构建第一个段落的完整输入
    if test_segments:
        input_data = [{"id": test_segments[0].segment_id, "text": test_segments[0].original_text}]
        input_json = json.dumps(input_data, ensure_ascii=False)
        
        safe_context = long_context[-settings.processing.max_context_length:] if long_context else "No Context"
        
        original_prompt = prompt_manager.format_text_prompt(
            context=safe_context,
            input_json=input_json,
            glossary="N/A",
        )
        
        print(f"\n📄 待翻译段落:\n{test_segments[0].original_text[:200]}...")
        print(f"\n🔧 完整 User Prompt (前500字符):\n{original_prompt[:500]}...")
    
    print("\n" + "="*80)
    
    start_time = time.perf_counter()
    
    try:
        # 本地模式使用同步翻译
        print(f"⏳ 正在通过本地 GPU 进行推理 (预计耗时取决于模型加载状态)...")
        results = translator.translate_batch(
            segments=test_segments,
            context=long_context.strip()
        )
        
        end_time = time.perf_counter()
        duration = end_time - start_time

        # 3. 输出审计结果
        print("\n" + "=" * 70)
        print("📊 翻译性能报告")
        print("-" * 70)
        for i, (seg, res) in enumerate(zip(test_segments, results)):
            print(f"段落 {i+1} (ID: {seg.segment_id}):")
            print(f"  原文: {seg.original_text[:100]}...")
            print(f"  译文: {res[:100]}...")
            print()
        
        print("-" * 70)
        print(f"总计耗时: {duration:.2f} 秒")
        print(f"平均速度: {len(test_segments)/duration:.2f} 段落/秒")
        
        # 统计成功率
        successful_translations = sum(1 for res in results if not res.startswith("[Failed"))
        success_rate = successful_translations / len(results) * 100
        print(f"✅ 成功率: {successful_translations}/{len(results)} ({success_rate:.1f}%)")

    except Exception as e:
        print(f"\n❌ API 调用失败: {e}")
        print("请检查 Ollama 是否已启动模型: `ollama run gemma3` 测试手动输入是否正常。")
    
    print("\n" + "=" * 70)
    print("✅ 测试套件执行完毕")
    print("=" * 70)

if __name__ == "__main__":
    main()