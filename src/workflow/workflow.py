"""
翻译工作流模块
"""
import asyncio
import json, os, traceback, signal, threading
from pathlib import Path
from typing import Dict, Optional, List

from ..core.schema import Settings, SegmentList, ContentSegment
from ..core.exceptions import TranslationError
from ..utils.logger import logger
from ..utils.file import create_output_directory, get_file_hash
from ..translator import GeminiTranslator, OpenAICompatibleTranslator, CheckpointManager
from ..parser.loader import load_document_structure as parse_document
from ..parser.helpers import is_likely_chinese
from ..renderer.markdown import MarkdownRenderer

# 尝试导入 Rich 进度显示
try:
    from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
    from rich.console import Console
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False


# 全局引用，用于信号处理器访问当前工作流实例
_current_workflow: Optional['TranslationWorkflow'] = None


def _emergency_save_handler(signum, frame):
    """紧急保存信号处理器（捕获 SIGTERM/SIGINT）"""
    global _current_workflow
    signal_name = signal.Signals(signum).name
    logger.warning(f"⚠️ 收到 {signal_name} 信号，尝试紧急保存...")
    
    if _current_workflow is not None:
        try:
            _current_workflow._emergency_save()
            logger.info("✅ 紧急保存完成")
        except Exception as e:
            logger.error(f"❌ 紧急保存失败: {e}")
    
    # 重新抛出信号，允许进程正常终止
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


class TranslationWorkflow:
    """
    翻译工作流类 - 封装完整的文档翻译业务逻辑
    
    职责：
    - 文档加载和解析
    - 标题预翻译
    - 术语表生成和管理
    - 批量翻译执行（同步/异步）
    - 进度管理和断点续传
    - 最终文档渲染
    """
    
    def __init__(self, settings: Settings):
        """
        初始化翻译工作流
        
        Args:
            settings: 全局设置对象，包含所有配置信息
            progress_callback: 进度回调函数 (stage, progress, message)
        """
        global _current_workflow
        
        self.settings = settings
        self.file_path = settings.files.document_path
        self.file_hash = get_file_hash(self.file_path)
        self.project_name = self.file_hash
        
        # 准备工作目录
        self.project_dir = create_output_directory(
            settings.files.output_base_dir,
            self.project_name
        )
        self.structure_path = self.project_dir / "structure_map.json"
        
        # 核心组件（延迟初始化）
        self.all_segments: Optional[SegmentList] = None
        self._segment_index: Dict[int, int] = {}  # segment_id -> list index 快速索引
        self.translator: Optional[GeminiTranslator] = None
        self.cache_manager = None
        self.checkpoint: Optional[CheckpointManager] = None
        self.glossary: Optional[Dict[str, str]] = None
        
        # 注册信号处理器（用于紧急保存）
        _current_workflow = self
        try:
            signal.signal(signal.SIGTERM, _emergency_save_handler)
            signal.signal(signal.SIGINT, _emergency_save_handler)
            logger.debug("✅ 已注册紧急保存信号处理器 (SIGTERM/SIGINT)")
        except Exception as e:
            logger.warning(f"⚠️ 无法注册信号处理器: {e}")
    
    def _emergency_save(self) -> None:
        """紧急保存当前状态（用于信号处理）"""
        logger.warning("🆘 执行紧急保存...")
        try:
            if self.all_segments:
                self._save_structure_map(self.all_segments)
                logger.info(f"   - structure_map.json 已保存 ({len(self.all_segments)} segments)")
            if self.checkpoint:
                self.checkpoint.save_checkpoint()
                logger.info("   - checkpoint 已保存")
        except Exception as e:
            logger.error(f"❌ 紧急保存失败: {e}")

    # Deprecated: progress reporting removed for a cleaner workflow interface.
    # Progress callbacks were removed to simplify the TranslationWorkflow class.

    def _optimize_batch_size_for_provider(self):
        """根据translator provider优化batch_size，平衡速度和context限制
        
        Returns:
            tuple: (是否优化, 原始batch_size, 优化后batch_size, 原因)
        """
        provider = getattr(self.settings.api, 'translator_provider', 'gemini').lower()
        original_batch_size = self.settings.processing.batch_size
        max_chunk_size = self.settings.processing.max_chunk_size
        
        # 估算每批次的字符量（不含prompt）
        estimated_chars_per_batch = original_batch_size * max_chunk_size
        
        # 根据provider调整batch_size
        if provider in {'deepseek', 'openai', 'openai-compatible'}:
            # 云端API使用完整版prompt，字符数较多，需要减少batch_size
            # DeepSeek/OpenAI context limit 约32K-128K，但考虑prompt开销，保守设置为60K上限
            max_safe_chars = 60000
            prompt_overhead = 2000  # 估算完整版prompt的字符数
            
            # 计算安全的batch_size
            safe_batch_size = max(1, (max_safe_chars - prompt_overhead) // max_chunk_size)
            optimized_batch_size = min(original_batch_size, safe_batch_size, 3)  # 最多3作为硬上限
            
            logger.info(f"🔧 参数优化分析 ({provider.upper()}):")
            logger.info(f"   📊 当前配置: batch_size={original_batch_size}, max_chunk_size={max_chunk_size}")
            logger.info(f"   📏 估算字符量: {estimated_chars_per_batch:,} 字符/批次 (不含prompt)")
            logger.info(f"   ⚠️  安全上限: {max_safe_chars:,} 字符/批次 (含{prompt_overhead:,}字符prompt开销)")
            logger.info(f"   🎯 优化结果: batch_size {original_batch_size} → {optimized_batch_size}")
            
            if optimized_batch_size < original_batch_size:
                new_estimated_chars = optimized_batch_size * max_chunk_size + prompt_overhead
                logger.info(f"   ✅ 新配置字符量: {new_estimated_chars:,} 字符/批次 (安全范围内)")
                logger.info(f"   💡 备选方案: 可考虑减少 max_chunk_size 至 {max_chunk_size//2} 以增加batch_size")
            else:
                logger.info(f"   ✅ 当前配置已安全: {estimated_chars_per_batch + prompt_overhead:,} 字符/批次")
            
            reason = f"云端API使用完整版prompt，减少batch_size避免超出context限制"
            
        elif provider == 'gemini':
            # Gemini使用完整版prompt，但context window较大 (1M+ tokens)
            # 保守设置上限为20万字符
            max_safe_chars = 200000
            prompt_overhead = 2000
            
            safe_batch_size = max(1, (max_safe_chars - prompt_overhead) // max_chunk_size)
            optimized_batch_size = min(original_batch_size, safe_batch_size, 4)  # 最多4
            
            logger.info(f"🔧 参数优化分析 (GEMINI):")
            logger.info(f"   📊 当前配置: batch_size={original_batch_size}, max_chunk_size={max_chunk_size}")
            logger.info(f"   📏 估算字符量: {estimated_chars_per_batch:,} 字符/批次 (不含prompt)")
            logger.info(f"   ⚠️  安全上限: {max_safe_chars:,} 字符/批次 (含{prompt_overhead:,}字符prompt开销)")
            logger.info(f"   🎯 优化结果: batch_size {original_batch_size} → {optimized_batch_size}")
            
            if optimized_batch_size < original_batch_size:
                new_estimated_chars = optimized_batch_size * max_chunk_size + prompt_overhead
                logger.info(f"   ✅ 新配置字符量: {new_estimated_chars:,} 字符/批次 (安全范围内)")
                logger.info(f"   💡 备选方案: 可考虑减少 max_chunk_size 至 {max_chunk_size//2} 以增加batch_size")
            else:
                logger.info(f"   ✅ 当前配置已安全: {estimated_chars_per_batch + prompt_overhead:,} 字符/批次")
            
            reason = f"Gemini使用完整版prompt，适度减少batch_size保证稳定性"
            
        else:
            # 本地模型使用简化版prompt，可以保持较大batch_size
            # 本地模型通常有更大的context window
            max_safe_chars = 100000  # 本地模型通常支持更大的上下文
            prompt_overhead = 500     # 简化版prompt开销较小
            
            safe_batch_size = max(1, (max_safe_chars - prompt_overhead) // max_chunk_size)
            optimized_batch_size = min(original_batch_size, safe_batch_size)
            
            logger.info(f"🔧 参数优化分析 ({provider.upper()}):")
            logger.info(f"   📊 当前配置: batch_size={original_batch_size}, max_chunk_size={max_chunk_size}")
            logger.info(f"   📏 估算字符量: {estimated_chars_per_batch:,} 字符/批次 (不含prompt)")
            logger.info(f"   ⚠️  安全上限: {max_safe_chars:,} 字符/批次 (含{prompt_overhead:,}字符prompt开销)")
            
            if optimized_batch_size < original_batch_size:
                logger.info(f"   🎯 优化结果: batch_size {original_batch_size} → {optimized_batch_size}")
                new_estimated_chars = optimized_batch_size * max_chunk_size + prompt_overhead
                logger.info(f"   ✅ 新配置字符量: {new_estimated_chars:,} 字符/批次 (安全范围内)")
                logger.info(f"   💡 备选方案: 可考虑减少 max_chunk_size 至 {max_chunk_size//2} 以增加batch_size")
            else:
                logger.info(f"   ✅ 参数保持: batch_size = {original_batch_size} (当前配置已安全)")
                logger.info(f"   📏 当前字符量: {estimated_chars_per_batch + prompt_overhead:,} 字符/批次")
            
            reason = f"本地模型使用简化版prompt，保持原有batch_size"
        
        # 应用优化后的batch_size
        optimized = optimized_batch_size != original_batch_size
        if optimized:
            self.settings.processing.batch_size = optimized_batch_size
        
        return optimized, original_batch_size, optimized_batch_size, reason

    def _build_translation_mode_config(self) -> Dict[str, str]:
        """构建用于调用翻译器的 translation_mode_config 字典"""
        mode_entity = getattr(self.settings.processing, 'translation_mode_entity', None)
        if mode_entity:
            return {
                'name': getattr(mode_entity, 'name', 'Auto'),
                'style': getattr(mode_entity, 'style', 'Fluent and precise'),
                'role_desc': getattr(mode_entity, 'role_desc', 'Expert translator')
            }
        return {
            'name': str(getattr(self.settings.processing, 'translation_mode', 'Default')),
            'style': 'Fluent and precise',
            'role_desc': 'Expert translator'
        }
        
    def execute(self) -> None:
        """执行完整的翻译工作流"""
        logger.info(f"🚀 开始处理文档: {self.file_path.name}")
        mode_name = getattr(self.settings.processing.translation_mode_entity, 'name', 'Default') if self.settings.processing.translation_mode_entity else 'Default'
        logger.info(f"   - 翻译模式: {mode_name}")
        logger.info(f"   - 项目标识 (Hash): {self.project_name}")
        
        try:
            # 0. 参数优化：根据translator provider调整batch_size
            self._optimize_batch_size_for_provider()

            # 1. 加载文档结构
            self._load_document()
            segment_count = len(self.all_segments) if self.all_segments else 0

            # 2. 初始化翻译器（此时不创建缓存，等术语表生成后再创建）
            self._initialize_translator()

            # 3. 生成术语表（通过预翻译，强制同步模式）
            self._generate_glossary()
            glossary_size = len(self.glossary) if self.glossary else 0
            
            # 4. 术语表编辑交互（如果启用）
            if self.settings.processing.enable_glossary_edit and self.glossary:
                self._prompt_glossary_edit()

            # 5. 初始化断点续传
            self._initialize_checkpoint()

            # 6. 执行翻译循环
            self._run_translation_loop()

            # 7. 翻译完成后处理标题（利用术语表保持一致性）
            self._post_translate_titles()

            # 8. 清理资源
            self._cleanup_resources()

            # 9. 渲染最终文档
            self._render_output()

        except Exception as e:
            logger.error(f"❌ 翻译工作流执行失败: {e}")
            raise
    
    def _load_document(self) -> None:
        """
        Load 阶段：加载文档结构到内存

        优先级：
        1. 从 structure_map.json 加载（包含翻译状态）
        2. 解析原始文档生成新结构
        """
        logger.info("📖 加载文档结构...")
        
        # 1. 尝试从 structure_map.json 加载
        if self.structure_path.exists() and self.settings.processing.enable_cache:
            try:
                with open(self.structure_path, 'r', encoding='utf-8') as f:
                    raw_data = json.load(f)
                    segments = [ContentSegment(**item) for item in raw_data]
                    logger.info(f"📦 从结构文件加载 {len(segments)} 个片段")
                    self.all_segments = segments
                    self._build_segment_index()  # 构建快速索引
                    logger.info(f"✅ 已加载 {len(self.all_segments)} 个内容片段")
                    return
            except Exception as e:
                logger.warning(f"⚠️ structure_map.json 损坏，将重新解析: {e}")
        
        # 2. 重新解析文档
        logger.info("⚙️ 解析文档结构...")
        segments = parse_document(self.file_path, self.structure_path, self.settings)
        
        if segments:
            logger.info(f"✅ 解析完成，生成 {len(segments)} 个片段")
            self._save_structure_map(segments)
            self.all_segments = segments
            self._build_segment_index()  # 构建快速索引
        else:
            logger.error("❌ 文档解析失败")
            raise TranslationError("文档解析失败，未生成任何内容片段")
        
        logger.info(f"✅ 已加载 {len(self.all_segments)} 个内容片段")
    
    def _initialize_translator(self) -> None:
        """初始化翻译器和缓存管理器"""
        provider = (getattr(self.settings.api, 'translator_provider', 'gemini') or 'gemini').lower()

        if provider == 'gemini':
            # 创建缓存管理器（如果启用 Gemini Context Caching）
            if self.settings.processing.enable_gemini_caching:
                from ..translator.support import CachePersistenceManager
                self.cache_manager = CachePersistenceManager(self.settings)
                logger.info("✅ Gemini 缓存管理器已初始化")

            # GeminiTranslator currently does not accept cache_manager in constructor;
            # keep cache_manager on the workflow and pass where needed inside translator.
            self.translator = GeminiTranslator(self.settings)
            logger.info("✅ Gemini 翻译器已初始化")
            return

        if provider in {'deepseek', 'openai', 'openai-compatible', 'openai_compatible'}:
            # OpenAI-compatible provider (DeepSeek)
            self.cache_manager = None
            self.translator = OpenAICompatibleTranslator(self.settings)
            logger.info(f"✅ OpenAI-compatible 翻译器已初始化 (provider={provider})")
            return
        
        # Ollama已集成到OpenAI-compatible provider中
        # 配置示例：TRANSLATOR_PROVIDER=openai-compatible, OPENAI_BASE_URL=http://localhost:11434
        
        raise TranslationError(
            f"未知 translator_provider: {provider}。"
            f"支持的provider: gemini, deepseek, openai, openai-compatible。"
            f"注意：Ollama现已集成到openai-compatible中，请使用OPENAI_BASE_URL=http://localhost:11434"
        )
    
    def _post_translate_titles(self) -> None:
        """
        翻译完成后处理章节标题
        
        放在最后执行的优势：
        1. 可以利用已生成的术语表保持一致性
        2. 不需要复杂的 mode 配置（标题翻译本身是简单任务）
        3. 不影响主翻译流程
        """
        logger.info("📝 开始翻译章节标题...")
        
        # 提取待翻译标题
        raw_titles = []
        for seg in self.all_segments:
            if (seg.is_new_chapter and seg.chapter_title and
                seg.chapter_title.strip() and not is_likely_chinese(seg.chapter_title)):
                raw_titles.append(seg.chapter_title)
        
        if not raw_titles:
            logger.info("   - 无需翻译的标题")
            return
        
        # 去重
        unique_titles = list(dict.fromkeys(raw_titles))
        logger.info(f"   - 发现 {len(unique_titles)} 个唯一标题")

        # 批量翻译（不需要 mode 配置，translate_titles 方法本身已足够简单）
        translation_map = self.translator.translate_titles(unique_titles)
        
        # 回填结果
        update_count = 0
        for seg in self.all_segments:
            if seg.is_new_chapter and seg.chapter_title in translation_map:
                translated = translation_map[seg.chapter_title]
                if translated:
                    seg.chapter_title = translated
                    update_count += 1
        
        logger.info(f"   - 更新了 {update_count} 个标题")
        
        # 保存更新后的结构
        self._save_structure_map(self.all_segments)
        logger.info("✅ 标题翻译完成")
    
    def _prompt_glossary_edit(self) -> None:
        """
        术语表编辑交互
        
        在术语表生成后暂停，允许用户查看和编辑术语表
        """
        glossary_path = self.project_dir / "glossary.json"
        
        logger.info("=" * 60)
        logger.info("📚 术语表已生成，当前包含 {} 条术语".format(len(self.glossary)))
        logger.info(f"   文件位置: {glossary_path}")
        logger.info("")
        logger.info("💡 您可以:")
        logger.info("   1. 查看并编辑上述文件中的术语翻译")
        logger.info("   2. 添加遗漏的专业术语")
        logger.info("   3. 修正不准确的翻译")
        logger.info("")
        
        # 显示部分术语预览
        preview_count = min(10, len(self.glossary))
        if preview_count > 0:
            logger.info(f"📋 术语预览 (前 {preview_count} 条):")
            for i, (term, translation) in enumerate(list(self.glossary.items())[:preview_count]):
                logger.info(f"   {i+1}. {term} → {translation}")
            if len(self.glossary) > preview_count:
                logger.info(f"   ... 还有 {len(self.glossary) - preview_count} 条")
        
        logger.info("")
        logger.info("=" * 60)
        
        try:
            user_input = input("📝 编辑完成后按 Enter 继续翻译（输入 'q' 取消）: ").strip().lower()
            
            if user_input == 'q':
                logger.info("⚠️  用户取消，退出翻译流程")
                raise KeyboardInterrupt("用户取消翻译")
            
            # 重新加载可能被编辑的术语表
            if glossary_path.exists():
                try:
                    with open(glossary_path, 'r', encoding='utf-8') as f:
                        self.glossary = json.load(f)
                    logger.info(f"✅ 已重新加载术语表 ({len(self.glossary)} 条)")
                except Exception as e:
                    logger.warning(f"⚠️  重新加载术语表失败: {e}")
        
        except EOFError:
            # 非交互模式（如在 CI/CD 中运行），直接继续
            logger.info("ℹ️  非交互模式，跳过术语表编辑")
    
    def _generate_glossary(self) -> None:
        """生成或加载术语表（支持渐进式提取）"""
        # 准备术语表文件路径
        glossary_merged_path = self.project_dir / "glossary_merged.json"
        glossary_path = self.project_dir / "glossary.json"
        
        # 优先使用合并的术语表
        if glossary_merged_path.exists():
            glossary_path = glossary_merged_path
        
        # 尝试加载已有术语表
        glossary_loaded = False
        if glossary_path.exists():
            try:
                with open(glossary_path, 'r', encoding='utf-8') as gf:
                    self.glossary = json.load(gf)
                logger.info(f"📚 从缓存加载已有术语表 ({len(self.glossary)} 条) -> {glossary_path}")
                glossary_loaded = True
                
                # 如果配置为跳过预翻译，直接返回
                if self.settings.processing.skip_pretranslate_if_glossary_exists:
                    logger.info("✅ 已有术语表，跳过预翻译阶段，直接进入正式翻译")
                    return
                else:
                    logger.warning("⚠️ 发现已有术语表，但配置为重新生成，将覆盖原有术语表")
            except Exception as e:
                logger.warning(f"⚠️ 加载已保存的术语表失败，将重新生成: {e}")
        
        # ========== 第一阶段：创建基础缓存（预翻译用） ==========
        if hasattr(self.translator, 'create_base_cache'):
            self.translator.create_base_cache()
        
        # 计算预翻译范围
        try:
            ratio = getattr(self.settings.processing, 'glossary_preamble_ratio', 0.1)
            pre_count = max(1, int(len(self.all_segments) * float(ratio)))
        except Exception:
            pre_count = max(1, int(len(self.all_segments) * 0.1))
        
        if pre_count <= 0:
            logger.info("⚪ 预翻译数量为 0，跳过术语表生成")
            self.glossary = {}
            return
        
        # 检查是否启用渐进式术语表提取
        enable_progressive = getattr(self.settings.processing, 'enable_progressive_glossary', True)
        
        if enable_progressive:
            logger.info("🔄 启用渐进式术语表提取模式")
            self._generate_glossary_progressive(pre_count, glossary_path)
        else:
            logger.info("📦 使用传统术语表提取模式（预翻译完成后一次性提取）")
            self._generate_glossary_traditional(pre_count, glossary_path)
    
    def _generate_glossary_progressive(self, pre_count: int, glossary_path) -> None:
        """渐进式术语表生成：每个 batch 翻译后立即提取并合并"""
        pre_segments = self.all_segments[:pre_count]
        pending_pre = [seg for seg in pre_segments if not seg.is_translated]
        
        if not pending_pre:
            logger.info("⚪ 所有预翻译片段已完成，跳过")
            return
        
        logger.info(f"🧪 渐进式预翻译与术语表提取")
        logger.info(f"   - 计划预翻译: {pre_count} 个片段")
        logger.info(f"   - 待处理: {len(pending_pre)} 个片段")
        logger.info(f"   - 最少术语数: {self.settings.processing.glossary_min_terms}")
        logger.info(f"   - 最大术语数: {self.settings.processing.glossary_max_terms}")
        logger.info(f"   - 饱和度阈值: {self.settings.processing.glossary_stop_threshold}")
        
        # 初始化术语表和统计
        self.glossary = {}
        batch_size = self.settings.processing.batch_size
        new_terms_history = []  # 记录每个 batch 新增的术语数量
        consecutive_low_batches = 0  # 连续低增长 batch 计数
        min_terms = self.settings.processing.glossary_min_terms
        max_terms = self.settings.processing.glossary_max_terms
        stop_threshold = self.settings.processing.glossary_stop_threshold
        
        for i in range(0, len(pending_pre), batch_size):
            batch = pending_pre[i:i+batch_size]
            batch_num = i // batch_size + 1
            total_batches = (len(pending_pre) + batch_size - 1) // batch_size
            
            logger.info(f"🔄 处理 Batch {batch_num}/{total_batches} ({len(batch)} 个片段)...")
            
            # 1. 翻译当前 batch
            context = ""
            if batch:
                context = self._get_context_from_memory(
                    batch[0],
                    self.settings.processing.max_context_length
                )
            
            batch_results = self.translator.translate_batch(batch, context=context)
            for seg, t in zip(batch, batch_results):
                seg.translated_text = t
                # 预翻译阶段也标记完成状态（如果启用了 checkpoint）
                if self.checkpoint and t and not t.startswith("[Failed") and not t.endswith("Failed]"):
                    self.checkpoint.mark_segment_completed(seg.segment_id)
            
            # 2. 从当前 batch 提取术语表
            if hasattr(self.translator, 'extract_glossary'):
                try:
                    batch_glossary = self.translator.extract_glossary(batch)
                    
                    # 合并到总术语表，统计新增数量
                    before_count = len(self.glossary)
                    self.glossary.update(batch_glossary)
                    after_count = len(self.glossary)
                    new_terms = after_count - before_count
                    new_terms_history.append(new_terms)
                    
                    logger.info(f"   ✅ Batch {batch_num} 提取术语: {len(batch_glossary)} 条，新增: {new_terms} 条，累计: {after_count} 条")
                    
                    # 检查是否达到最大术语数
                    if after_count >= max_terms:
                        logger.info(f"✅ 术语表已达到最大条目数 ({max_terms} 条)，提前结束预翻译")
                        logger.info(f"   - 已处理: {batch_num}/{total_batches} batches ({(batch_num/total_batches)*100:.1f}%)")
                        break
                    
                    # 3. 检查是否达到停止条件
                    # 条件1: 达到最少术语数
                    if after_count >= min_terms:
                        # 条件2: 术语表饱和（连续 3 个 batch 新增术语数低于阈值）
                        if len(new_terms_history) >= 3:
                            avg_new_terms = sum(new_terms_history) / len(new_terms_history)
                            recent_avg = sum(new_terms_history[-3:]) / 3
                            
                            if recent_avg < avg_new_terms * stop_threshold:
                                consecutive_low_batches += 1
                                logger.info(f"   📊 术语增长放缓: 近3批平均 {recent_avg:.1f} < 历史平均 {avg_new_terms:.1f} × {stop_threshold}")
                                
                                if consecutive_low_batches >= 2:  # 连续 2 次低增长
                                    logger.info(f"✅ 术语表已饱和（{after_count} 条），提前结束预翻译")
                                    logger.info(f"   - 已处理: {batch_num}/{total_batches} batches ({(batch_num/total_batches)*100:.1f}%)")
                                    logger.info(f"   - 节省: {total_batches - batch_num} batches")
                                    break
                            else:
                                consecutive_low_batches = 0  # 重置计数
                    
                except Exception as e:
                    logger.warning(f"⚠️ Batch {batch_num} 术语提取失败: {e}")
            
            # 4. 保存阶段性结果
            self._save_structure_map(self.all_segments)
            if self.checkpoint:
                self.checkpoint.save_checkpoint()
            if self.glossary:
                try:
                    with open(glossary_path, 'w', encoding='utf-8') as gf:
                        json.dump(self.glossary, gf, ensure_ascii=False, indent=2)
                except Exception as e:
                    logger.warning(f"⚠️ 保存阶段性术语表失败: {e}")
        
        # 最终统计
        if self.glossary:
            logger.info(f"💾 术语表已保存到: {glossary_path}")
            logger.info(f"🔥 渐进式提取完成，共 {len(self.glossary)} 条术语")
            # 显示前 5 条术语作为示例
            for idx, (k, v) in enumerate(list(self.glossary.items())[:5], 1):
                logger.info(f"   {idx}. '{k}' → '{v}'")
            if len(self.glossary) > 5:
                logger.info("   ... (更多术语)")
        else:
            logger.info("⚪ 未生成有效术语表")
    
    def _generate_glossary_traditional(self, pre_count: int, glossary_path) -> None:
        """传统术语表生成：预翻译完成后一次性提取"""
        pre_segments = self.all_segments[:pre_count]
        pending_pre = [seg for seg in pre_segments if not seg.is_translated]
        
        if pending_pre:
            logger.info(f"🧪 预翻译前 {pre_count} 个片段以构建术语表...")
            logger.info(f"   - 使用基础缓存（无 mode、无 glossary）")
            
            translation_mode_config = self._build_translation_mode_config()
            # 为预翻译片段提供上下文（同步方式，使用原文作为上下文）
            translations = []
            batch_size = self.settings.processing.batch_size
            for i in range(0, len(pending_pre), batch_size):
                batch = pending_pre[i:i+batch_size]
                context = ""
                if batch:
                    context = self._get_context_from_memory(
                        batch[0],
                        self.settings.processing.max_context_length
                    )
                batch_results = self.translator.translate_batch(batch, context=context)
                translations.extend(batch_results)
            
            for seg, t in zip(pending_pre, translations):
                seg.translated_text = t
                # 传统预翻译阶段也标记完成状态（如果启用了 checkpoint）
                if self.checkpoint and t and not t.startswith("[Failed") and not t.endswith("Failed]"):
                    self.checkpoint.mark_segment_completed(seg.segment_id)
            self._save_structure_map(self.all_segments)
            if self.checkpoint:
                self.checkpoint.save_checkpoint()
        
        # 提取术语表（若翻译器实现了该方法）
        if hasattr(self.translator, 'extract_glossary'):
            try:
                self.glossary = self.translator.extract_glossary(pre_segments)
            except Exception as e:
                logger.warning(f"⚠️ 提取术语表失败: {e}")
                self.glossary = {}
        else:
            logger.info("ℹ️ 翻译器不支持术语表提取，跳过此步骤")
            self.glossary = {}
        
        # 持久化术语表
        if self.glossary:
            try:
                with open(glossary_path, 'w', encoding='utf-8') as gf:
                    json.dump(self.glossary, gf, ensure_ascii=False, indent=2)
                logger.info(f"💾 术语表已保存到: {glossary_path}")
                logger.info(f"🔥 已生成术语表，包含 {len(self.glossary)} 条术语")
            except Exception as e:
                logger.warning(f"⚠️ 保存术语表失败: {e}")
        else:
            logger.info("⚪ 未生成有效术语表")
    
    def _initialize_checkpoint(self) -> None:
        """初始化断点续传管理器"""
        self.checkpoint = CheckpointManager(self.settings)
        self.checkpoint.update_total_segments(len(self.all_segments))
        logger.info("✅ 断点续传管理器已初始化")
        logger.info(f"   📂 检查点文件: {self.checkpoint.checkpoint_file}")
    
    def _run_translation_loop(self) -> None:
        """执行翻译循环（支持同步/异步模式）"""
        # ========== 第二阶段：创建完整缓存（正式翻译用） ==========
        # 包含 glossary 和 mode
        if hasattr(self.translator, 'create_full_cache'):
            self.translator.create_full_cache(glossary=self.glossary)
            logger.info("📦 正式翻译阶段：使用完整缓存（含 mode 和 glossary）")
        
        # 获取待翻译片段（所有片段，因为正式翻译需要重新翻译预翻译部分以保证一致性）
        # 注意：预翻译的片段也需要重新翻译，因为：
        # 1. 预翻译使用的是无 glossary 的缓存
        # 2. 正式翻译有了完整术语表，可以获得更一致的翻译质量
        
        # 判断是否需要重新翻译预翻译部分
        reprocess_pretranslated = getattr(self.settings.processing, 'reprocess_pretranslated', True)
        
        if reprocess_pretranslated:
            # 标记预翻译部分为未翻译，以便重新处理
            try:
                ratio = getattr(self.settings.processing, 'glossary_preamble_ratio', 0.1)
                pre_count = max(1, int(len(self.all_segments) * float(ratio)))
            except Exception:
                pre_count = max(1, int(len(self.all_segments) * 0.1))
            
            for seg in self.all_segments[:pre_count]:
                if seg.is_translated:
                    seg.translated_text = ""  # 清除预翻译结果
            
            logger.info(f"🔄 将重新翻译前 {pre_count} 个预翻译片段（使用完整缓存）")
        
        pending_segments = self.checkpoint.get_pending_segments(self.all_segments)
        
        if not pending_segments:
            logger.info("🎉 所有片段均已翻译完成！")
            return
        
        logger.info(f"🔄 发现 {len(pending_segments)} 个待翻译片段")
        
        # 判断是否使用异步模式
        use_async = (
            self.settings.processing.enable_async and 
            len(pending_segments) >= self.settings.processing.async_threshold and
            hasattr(self.translator, 'async_translator')
        )
        
        if use_async:
            self._run_async_translation(pending_segments)
        else:
            self._run_sync_translation(pending_segments)
        
        # 翻译循环结束后，强制保存一次
        logger.info("🔒 翻译循环完成，执行强制保存...")
        self._save_structure_map(self.all_segments)
        self.checkpoint.save_checkpoint()
        logger.info("✅ 强制保存完成")
    
    def _run_sync_translation(self, pending_segments: SegmentList) -> None:
        """同步翻译模式（带进度条）"""
        logger.info("🔄 使用同步模式翻译")
        logger.info(f"📝 开始同步翻译 {len(pending_segments)} 个片段...")
        
        try:
            # 尝试使用 rich 进度条，如果失败则回退到无进度条模式
            use_rich = self.settings.processing.use_rich_progress
            
            if use_rich:
                try:
                    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
                    
                    with Progress(
                        SpinnerColumn(),
                        TextColumn("[bold blue]{task.description}"),
                        BarColumn(),
                        TaskProgressColumn(),
                        console=None,  # 使用默认console
                    ) as progress:
                        task = progress.add_task("[cyan]同步翻译中...", total=len(pending_segments))
                        
                        success_count = 0
                        batch_size = self.settings.processing.batch_size
                        
                        for i in range(0, len(pending_segments), batch_size):
                            batch = pending_segments[i:i+batch_size]
                            # 为当前batch的第一个segment获取上下文
                            context = ""
                            if batch:
                                context = self._get_context_from_memory(
                                    batch[0],
                                    self.settings.processing.max_context_length
                                )
                            results = self.translator.translate_batch(batch, context=context)
                            
                            for seg, trans in zip(batch, results):
                                if trans and not trans.startswith("[Failed") and not trans.endswith("Failed]"):
                                    seg.translated_text = trans
                                    self.checkpoint.mark_segment_completed(seg.segment_id)
                                    success_count += 1
                                else:
                                    seg.translated_text = trans if trans else "[Failed: Empty response]"
                                    self.checkpoint.mark_segment_failed(seg.segment_id, trans or "Empty response")
                                
                                progress.update(task, advance=1)
                            
                            # 定期保存检查点
                            if (i // batch_size + 1) % self.settings.processing.checkpoint_interval == 0:
                                self._save_structure_map(self.all_segments)
                                self.checkpoint.save_checkpoint()
                        
                        logger.info(f"✅ 同步翻译完成: {success_count}/{len(pending_segments)} 成功")
                        
                except ImportError:
                    logger.warning("⚠️ Rich 库未安装，使用简单模式（无进度条）")
                    use_rich = False
            
            # 回退到简单模式（无进度条）
            if not use_rich:
                translation_mode_config = self._build_translation_mode_config()
                # 为每个batch分别处理上下文
                success_count = 0
                batch_size = self.settings.processing.batch_size

                for i in range(0, len(pending_segments), batch_size):
                    batch = pending_segments[i:i+batch_size]
                    # 为当前batch的第一个segment获取上下文
                    context = ""
                    if batch:
                        context = self._get_context_from_memory(
                            batch[0],
                            self.settings.processing.max_context_length
                        )
                    results = self.translator.translate_batch(batch, context=context)

                    for seg, trans in zip(batch, results):
                        if trans and not trans.startswith("[Failed") and not trans.endswith("Failed]"):
                            seg.translated_text = trans
                            self.checkpoint.mark_segment_completed(seg.segment_id)
                            success_count += 1
                        else:
                            seg.translated_text = trans if trans else "[Failed: Empty response]"
                            self.checkpoint.mark_segment_failed(seg.segment_id, trans or "Empty response")

                    # 定期保存检查点
                    if (i // batch_size + 1) % self.settings.processing.checkpoint_interval == 0:
                        self._save_structure_map(self.all_segments)
                        self.checkpoint.save_checkpoint()
                
                logger.info(f"✅ 同步翻译完成: {success_count}/{len(pending_segments)} 成功")
            
            # 最终保存
            self._save_structure_map(self.all_segments)
            self.checkpoint.save_checkpoint()
            
        except Exception as e:
            logger.error(f"❌ 同步翻译失败: {e}")
            for seg in pending_segments:
                seg.translated_text = f"[Failed: {str(e)}]"
                self.checkpoint.mark_segment_failed(seg.segment_id, str(e))
            self._save_structure_map(self.all_segments)
            self.checkpoint.save_checkpoint()
            raise
    
    def _run_async_translation(self, pending_segments: SegmentList) -> None:
        """异步翻译模式（多批次并发执行，真正的并行翻译）
        
        架构优化：
        - 之前：batch 串行执行，实际并发度 = 1
        - 现在：多个 batch 并发执行，实际并发度 = max_concurrent_batches
        
        调用链：
        workflow._run_async_translation (同步入口)
          └── asyncio.run(_run_concurrent_batches)  [单次调用]
                └── asyncio.gather(*batch_tasks)  [真正的并发]
                      └── _process_single_batch (每个 batch)
                            └── async_t.translate_text_batch_async
        """
        logger.info("⚡ 使用异步模式翻译（多批次并发+即时保存）")
        
        # 检查translator是否支持异步
        if not hasattr(self.translator, 'async_translator') or self.translator.async_translator is None:
            logger.warning("⚠️ 当前translator不支持异步模式，降级到同步模式")
            self._run_sync_translation(pending_segments)
            return
        
        batch_size = self.settings.processing.batch_size
        batches = [
            pending_segments[i:i+batch_size] 
            for i in range(0, len(pending_segments), batch_size)
        ]
        total_batches = len(batches)
        total_segments = len(pending_segments)
        
        # 并发度控制：同时执行多少个 batch
        max_concurrent = getattr(self.settings.processing, 'async_max_workers', 5)
        
        logger.info(f"🚀 开始并发翻译 {total_segments} 个片段")
        logger.info(f"   📊 共 {total_batches} 批次，批大小 {batch_size}，并发度 {max_concurrent}")
        
        # 用于线程安全的计数和保存
        lock = threading.Lock()
        stats = {"success": 0, "processed": 0, "completed_batches": 0}
        
        async def _process_single_batch(batch_idx: int, batch: SegmentList, semaphore: asyncio.Semaphore):
            """处理单个 batch（在 semaphore 控制下）"""
            async with semaphore:
                try:
                    # 获取上下文（读取当前已翻译的内容）
                    context = ""
                    if batch:
                        context = self._get_context_from_memory(
                            batch[0],
                            self.settings.processing.max_context_length
                        )
                    
                    # 执行翻译
                    async_t = self.translator.async_translator
                    batch_results = await async_t.translate_text_batch_async(
                        batch, context, self.glossary
                    )
                    
                    # 处理结果（线程安全）
                    batch_success = 0
                    with lock:
                        for seg, trans in zip(batch, batch_results):
                            if trans and not (isinstance(trans, str) and (trans.startswith("[Failed") or trans.endswith("Failed]"))):
                                seg.translated_text = trans
                                self.checkpoint.mark_segment_completed(seg.segment_id)
                                stats["success"] += 1
                                batch_success += 1
                            else:
                                seg.translated_text = trans if trans else "[Failed: Empty response]"
                                self.checkpoint.mark_segment_failed(seg.segment_id, trans or "Empty response")
                            stats["processed"] += 1
                        
                        stats["completed_batches"] += 1
                        
                        # 每完成一个 batch 就保存
                        self._save_structure_map(self.all_segments)
                        self.checkpoint.save_checkpoint()
                    
                    logger.info(f"✅ 批次 {batch_idx}/{total_batches} 完成 (本批成功: {batch_success}/{len(batch)}, 总进度: {stats['completed_batches']}/{total_batches})")
                    return batch_idx, True
                    
                except Exception as e:
                    logger.error(f"❌ 批次 {batch_idx} 失败: {e}")
                    
                    # 标记失败（线程安全）
                    with lock:
                        for seg in batch:
                            seg.translated_text = f"[Failed: {str(e)}]"
                            self.checkpoint.mark_segment_failed(seg.segment_id, str(e))
                            stats["processed"] += 1
                        
                        stats["completed_batches"] += 1
                        
                        # 保存失败状态
                        try:
                            self._save_structure_map(self.all_segments)
                            self.checkpoint.save_checkpoint()
                        except Exception as save_exc:
                            logger.error(f"保存失败状态时出错: {save_exc}")
                    
                    return batch_idx, False
        
        async def _run_concurrent_batches():
            """并发执行所有 batch"""
            semaphore = asyncio.Semaphore(max_concurrent)
            
            # 创建所有任务
            tasks = [
                _process_single_batch(idx, batch, semaphore)
                for idx, batch in enumerate(batches, 1)
            ]
            
            # 并发执行（使用 as_completed 获取实时进度）
            results = []
            for coro in asyncio.as_completed(tasks):
                try:
                    batch_idx, success = await coro
                    results.append((batch_idx, success))
                except Exception as e:
                    logger.error(f"批次任务异常: {e}")
            
            return results
        
        # 使用 Rich 进度条（如果可用）
        import time
        start_time = time.time()
        
        if RICH_AVAILABLE:
            console = Console()
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                TextColumn("•"),
                TextColumn("{task.completed}/{task.total}"),
                TimeElapsedColumn(),
                console=console,
                transient=False,
                refresh_per_second=2
            ) as progress:
                batch_task = progress.add_task(f"[cyan]批次进度 (并发:{max_concurrent})", total=total_batches)
                segment_task = progress.add_task("[green]片段进度", total=total_segments)
                
                # 启动后台任务更新进度条
                async def _run_with_progress():
                    semaphore = asyncio.Semaphore(max_concurrent)
                    tasks = [
                        _process_single_batch(idx, batch, semaphore)
                        for idx, batch in enumerate(batches, 1)
                    ]
                    
                    for coro in asyncio.as_completed(tasks):
                        try:
                            await coro
                            # 更新进度条
                            progress.update(batch_task, completed=stats["completed_batches"])
                            progress.update(segment_task, completed=stats["processed"])
                        except Exception as e:
                            logger.error(f"批次任务异常: {e}")
                
                # 执行并发翻译
                asyncio.run(_run_with_progress())
                
                # 最终更新
                progress.update(batch_task, completed=total_batches)
                progress.update(segment_task, completed=stats["processed"])
        else:
            # 无 Rich 的简单模式
            asyncio.run(_run_concurrent_batches())
        
        # 输出统计
        elapsed = time.time() - start_time
        logger.info(f"⏱️  翻译完成！耗时: {elapsed:.1f}s")
        logger.info(f"📊 成功: {stats['success']}/{stats['processed']} 片段")
        logger.info(f"📊 速度: {stats['processed']/elapsed:.2f} segments/s")
        logger.info(f"📊 并发效率: {max_concurrent} batches 同时运行")
    
    def _cleanup_resources(self) -> None:
        """清理资源"""
        try:
            if hasattr(self.translator, '_async_translator') and self.translator._async_translator:
                self.translator._async_translator.cleanup()
            if hasattr(self.translator, 'cache_manager') and self.translator.cache_manager:
                self.translator.cache_manager.cleanup_all_caches()
            logger.info("✅ 资源清理完成")
        except Exception as e:
            logger.debug(f"清理资源时出现警告: {e}")
    
    def _render_output(self) -> None:
        """Render: 生成最终文档（Markdown + PDF）"""
        logger.info("📄 开始渲染最终文档...")
        
        # 决定最终输出目录
        if self.settings.files.final_output_dir:
            final_dir = self.settings.files.final_output_dir
            final_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"   - 自定义输出目录: {final_dir}")
        else:
            # 默认输出到源文件所在目录
            final_dir = self.settings.files.document_path.parent
            logger.info(f"   - 输出到源文件目录: {final_dir}")
        
        # 1. 生成 Markdown
        md_renderer = MarkdownRenderer(self.settings)
        md_output_path = final_dir / f"{Path(self.file_path.name).stem}_Translated.md"
        md_renderer.render_to_file(self.all_segments, md_output_path, f"原文: {self.file_path.name}")
        logger.info(f"✅ Markdown 已保存到: {md_output_path}")
        
        # 2. 生成 PDF（可选，如果依赖可用）
        try:
            from ..renderer.pdf import PDFRenderer
            pdf_renderer = PDFRenderer(self.settings)
            
            pdf_path = final_dir / f"{Path(self.file_path.name).stem}_Translated.pdf"
            pdf_renderer.render_to_file(self.all_segments, pdf_path, f"原文: {self.file_path.name}")
            logger.info(f"✅ PDF 已保存到: {pdf_path}")
        except ImportError:
            logger.info("ℹ️  跳过 PDF 生成（未安装相关依赖）")
        except Exception as e:
            logger.warning(f"⚠️  PDF 生成失败: {e}")
            logger.info("💡 已生成 Markdown 文件，可手动转换为 PDF")
        
        logger.info("✅ 文档渲染完成")
    
    def _save_structure_map(self, segments: SegmentList) -> None:
        """
        Save: 保存完整的文档结构状态到 JSON 文件
        这是单一真理源的持久化
        """
        try:
            self.structure_path.parent.mkdir(parents=True, exist_ok=True)
            
            # 序列化为字典列表
            data = [seg.model_dump() for seg in segments]
            
            # 强制写入并刷新
            with open(self.structure_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()  # 强制刷新缓冲区
                os.fsync(f.fileno())  # 强制同步到磁盘
            
            # 统计已翻译数量（用于进度显示）
            translated_count = sum(1 for seg in segments if seg.is_translated)
            logger.info(f"💾 Structure map 已保存到: {self.structure_path}")
            logger.info(f"💾 翻译进度: {translated_count}/{len(segments)} 个片段已完成")
            
        except Exception as e:
            logger.error(f"❌ 保存结构状态失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            raise
    
    def _get_context_from_memory(self, current_segment: ContentSegment, max_length: int) -> str:
        """
        从内存中获取翻译上下文（O(1) 快速查找）
        
        设计说明：
        使用上一个 segment 的**原文**（而非译文）作为上下文
        
        这种设计的优势：
        1. 异步友好：原文在翻译前就已确定，多个 batch 可并发执行，无需等待前序翻译完成
        2. 确定性强：context 在翻译开始前就固定，便于复现和调试
        3. 预计算可行：可以在分发异步任务前预先计算所有 segment 的 context
        
        劣势：
        - 无法利用已翻译内容的术语表一致性（但通过 glossary 机制弥补）
        
        Args:
            current_segment: 当前待翻译的 segment
            max_length: 上下文最大长度限制
            
        Returns:
            前一个 segment 原文的后 25% 部分（不超过 max_length）
        """
        # 使用索引快速查找（O(1)），避免遍历
        current_idx = self._segment_index.get(current_segment.segment_id, -1)
        if current_idx <= 0:
            return ""

        # 获取上一个片段的原文作为上下文
        prev_seg = self.all_segments[current_idx - 1]
        if not prev_seg.original_text or not prev_seg.original_text.strip():
            return ""
        
        # 计算上下文长度：25% 的 MAX_CHUNK_SIZE
        context_length = int(self.settings.processing.max_chunk_size * 0.25)

        # 如果原文长度超过上下文长度限制，取后25%的内容
        original_text = prev_seg.original_text.strip()
        if len(original_text) > context_length:
            # 从原文末尾向前取指定长度
            context_text = original_text[-context_length:].strip()
        else:
            context_text = original_text

        # 确保不超过max_length参数
        if len(context_text) > max_length:
            context_text = context_text[-max_length:].strip()

        return context_text
    
    def _build_segment_index(self) -> None:
        """构建 segment_id -> index 的快速索引"""
        self._segment_index = {
            seg.segment_id: idx 
            for idx, seg in enumerate(self.all_segments)
        }
        logger.debug(f"📇 已构建 segment 索引 ({len(self._segment_index)} 条)")
