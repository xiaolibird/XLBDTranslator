"""
翻译工作流模块
"""
import asyncio
import json
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
        self.translator: Optional[GeminiTranslator] = None
        self.cache_manager = None
        self.checkpoint: Optional[CheckpointManager] = None
        self.glossary: Optional[Dict[str, str]] = None

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

            # 2. 初始化翻译器和缓存
            self._initialize_translator()

            # 3. 预翻译标题
            self._pre_translate_titles()

            # 4. 生成术语表
            self._generate_glossary()
            glossary_size = len(self.glossary) if self.glossary else 0

            # 5. 初始化断点续传
            self._initialize_checkpoint()

            # 6. 执行翻译循环
            self._run_translation_loop()

            # 7. 清理资源
            self._cleanup_resources()

            # 8. 渲染最终文档
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
    
    def _pre_translate_titles(self) -> None:
        """预翻译章节标题"""
        logger.info("📝 开始预翻译标题...")
        
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
        
        # 构建 translation_mode_config（优先使用已设置的实体）
        translation_mode_config = None
        mode_entity = getattr(self.settings.processing, 'translation_mode_entity', None)
        if mode_entity:
            translation_mode_config = {
                'name': getattr(mode_entity, 'name', 'Auto'),
                'style': getattr(mode_entity, 'style', 'Fluent and precise'),
                'role_desc': getattr(mode_entity, 'role_desc', 'Expert translator')
            }
        else:
            translation_mode_config = {
                'name': str(getattr(self.settings.processing, 'translation_mode', 'Default')),
                'style': 'Fluent and precise',
                'role_desc': 'Expert translator'
            }

        # 批量翻译
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
        logger.info("✅ 标题预翻译完成")
    
    def _generate_glossary(self) -> None:
        """生成或加载术语表"""
        # 准备术语表文件路径
        glossary_merged_path = self.project_dir / "glossary_merged.json"
        glossary_path = self.project_dir / "glossary.json"
        
        # 优先使用合并的术语表
        if glossary_merged_path.exists():
            glossary_path = glossary_merged_path
        
        # 尝试加载已有术语表
        if glossary_path.exists():
            try:
                with open(glossary_path, 'r', encoding='utf-8') as gf:
                    self.glossary = json.load(gf)
                logger.info(f"📚 从缓存加载已有术语表 ({len(self.glossary)} 条) -> {glossary_path}")
                return
            except Exception as e:
                logger.warning(f"⚠️ 加载已保存的术语表失败，将重新生成: {e}")
        
        # 生成新的术语表（通过预翻译部分文档）
        try:
            ratio = getattr(self.settings.processing, 'glossary_preamble_ratio', 0.1)
            pre_count = max(1, int(len(self.all_segments) * float(ratio)))
        except Exception:
            pre_count = max(1, int(len(self.all_segments) * 0.1))
        
        if pre_count > 0:
            pre_segments = self.all_segments[:pre_count]
            pending_pre = [seg for seg in pre_segments if not seg.is_translated]
            
            if pending_pre:
                logger.info(f"🧪 预翻译前 {pre_count} 个片段以构建术语表...")
                translation_mode_config = self._build_translation_mode_config()
                # 为预翻译片段提供上下文（虽然对术语表构建影响较小，但保持一致性）
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
                self._save_structure_map(self.all_segments)
            
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
        # 获取待翻译片段
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
        """异步翻译模式（使用上下文管理器自动资源清理）"""
        logger.info("⚡ 使用异步模式翻译（提升速度）")
        
        # 检查translator是否支持异步
        if not hasattr(self.translator, 'async_translator') or self.translator.async_translator is None:
            logger.warning("⚠️ 当前translator不支持异步模式，降级到同步模式")
            self._run_sync_translation(pending_segments)
            return
        
        try:
            batch_size = self.settings.processing.batch_size
            batches = [
                pending_segments[i:i+batch_size] 
                for i in range(0, len(pending_segments), batch_size)
            ]
            total_batches = len(batches)
            
            logger.info(f"🚀 开始异步翻译 {len(pending_segments)} 个片段（{total_batches} 批次，批大小 {batch_size}）...")
            
            async def translate_all_batches_with_progress():
                """所有批次并发执行，带进度显示和上下文管理器自动资源清理"""
                # 使用上下文管理器确保资源自动清理
                async with self.translator.async_translator as async_t:
                    # 创建任务和索引映射
                    task_to_index = {}
                    tasks = []
                    
                    for i, batch in enumerate(batches):
                        # 为当前batch的第一个segment获取上下文
                        context = ""
                        if batch:
                            context = self._get_context_from_memory(
                                batch[0],
                                self.settings.processing.max_context_length
                            )
                        coro = async_t.translate_text_batch_async(
                            batch, context, self.glossary
                        )
                        task = asyncio.create_task(coro)
                        tasks.append(task)
                        task_to_index[task] = i
                    
                    # 使用Rich进度条
                    if RICH_AVAILABLE:
                        console = Console()
                        with Progress(
                            SpinnerColumn(),
                            TextColumn("[progress.description]{task.description}"),
                            BarColumn(),
                            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                            TextColumn("•"),
                            TextColumn("{task.completed}/{task.total} 批次"),
                            TimeElapsedColumn(),
                            console=console
                        ) as progress:
                            task_id = progress.add_task("[cyan]异步翻译进度", total=total_batches)
                            
                            # 收集结果，保持顺序
                            results = [None] * total_batches
                            completed_count = 0
                            
                            # 使用 wait 来逐步获取结果并更新进度
                            pending = set(tasks)
                            
                            while pending:
                                # 等待至少一个任务完成
                                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

                                # 处理已完成的任务（捕获单个任务异常，避免整个并发流程中断）
                                for completed_task in done:
                                    task_index = task_to_index.get(completed_task, None)
                                    try:
                                        result = completed_task.result()
                                    except Exception as task_exc:
                                        logger.error(f"❌ 异步批次任务失败: {task_exc}")
                                        # 标记该批次为失败（用与同步路径兼容的占位符）
                                        result = [f"[Failed: {str(task_exc)}]"] * (len(batches[task_index]) if task_index is not None else 1)

                                    # 通过任务对象找到对应的索引并存储结果
                                    if task_index is not None:
                                        results[task_index] = result
                                    completed_count += 1
                                    # 逐步更新进度条
                                    progress.update(task_id, completed=completed_count)
                            
                            return results
                    else:
                        # 无进度条模式：使用gather保持顺序（return_exceptions=True避免单任务异常中断整体）
                        raw_results = await asyncio.gather(*tasks, return_exceptions=True)
                        # 统一处理异常，转换为失败占位符
                        results = []
                        for i, res in enumerate(raw_results):
                            if isinstance(res, Exception):
                                logger.error(f"❌ 异步批次 {i} 任务失败: {res}")
                                results.append([f"[Failed: {str(res)}]"] * len(batches[i]))
                            else:
                                results.append(res)
                        return results
            
            # 执行并发翻译（检查事件循环兼容性）
            try:
                # 检查是否已在事件循环中运行
                loop = asyncio.get_running_loop()
                # 如果已有运行中的循环，不能用asyncio.run，需要直接await（但这里是同步函数，记录警告）
                logger.warning("⚠️ 检测到已运行的事件循环，异步翻译可能受限。建议在独立环境运行。")
                # 降级到同步模式
                raise RuntimeError("已存在运行中的事件循环，无法使用asyncio.run")
            except RuntimeError:
                # 没有运行中的循环（这是正常情况），可以安全使用asyncio.run
                all_results = asyncio.run(translate_all_batches_with_progress())

            # 处理翻译结果并实时保存checkpoint
            success_count = 0
            batch_idx = 0
            for batch, batch_results in zip(batches, all_results):
                # batch_results 有可能是异常占位，确保可迭代
                if not isinstance(batch_results, (list, tuple)):
                    batch_results = [batch_results] * len(batch)

                for seg, trans in zip(batch, batch_results):
                    try:
                        if trans and not (isinstance(trans, str) and (trans.startswith("[Failed") or trans.endswith("Failed]"))):
                            seg.translated_text = trans
                            self.checkpoint.mark_segment_completed(seg.segment_id)
                            success_count += 1
                        else:
                            seg.translated_text = trans if trans else "[Failed: Empty response]"
                            self.checkpoint.mark_segment_failed(seg.segment_id, trans or "Empty response")
                    except Exception as proc_exc:
                        logger.error(f"❌ 处理异步翻译结果时出错: {proc_exc}")
                        seg.translated_text = f"[Failed: {proc_exc}]"
                        self.checkpoint.mark_segment_failed(seg.segment_id, str(proc_exc))

                # 每个batch完成后立即保存checkpoint（防止数据丢失）
                batch_idx += 1
                try:
                    self._save_structure_map(self.all_segments)
                    self.checkpoint.save_checkpoint()
                    logger.debug(f"💾 已保存批次 {batch_idx}/{total_batches} 的checkpoint")
                except Exception as save_exc:
                    logger.error(f"❌ 保存批次 {batch_idx} checkpoint失败: {save_exc}")
                    # 不中断翻译流程，继续处理后续批次

            logger.info(f"✅ 异步翻译完成: {success_count}/{len(pending_segments)} 成功")
            
        except Exception as e:
            logger.error(f"❌ 异步翻译失败，降级到同步模式: {e}")
            # 降级前先保存当前进度
            try:
                self._save_structure_map(self.all_segments)
                self.checkpoint.save_checkpoint()
                logger.info("💾 已保存异步翻译中断前的进度")
            except Exception as save_exc:
                logger.error(f"❌ 保存中断进度失败: {save_exc}")
            # 降级到同步模式
            self._run_sync_translation(pending_segments)
        finally:
            # 最终保证：确保在任何情况下都尝试保存当前结构与检查点
            try:
                self._save_structure_map(self.all_segments)
                if self.checkpoint:
                    self.checkpoint.save_checkpoint()
                logger.debug("✅ 异步翻译finally块：已保存最终checkpoint")
            except Exception as final_exc:
                logger.warning(f"⚠️ 最终保存检查点失败: {final_exc}")
    
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
            
            with open(self.structure_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            logger.debug(f"💾 结构状态已保存: {len(segments)} 个片段")
        except Exception as e:
            logger.error(f"❌ 保存结构状态失败: {e}")
            raise
    
    def _get_context_from_memory(self, current_segment: ContentSegment, max_length: int) -> str:
        """
        从内存中获取翻译上下文
        从上一个segment的原文中选取25%左右的MAX_CHUNK_SIZE作为上下文
        """
        # 找到当前片段的位置
        current_idx = next((i for i, seg in enumerate(self.all_segments) if seg.segment_id == current_segment.segment_id), -1)
        if current_idx == -1:
            return ""

        # 获取上一个片段的原文作为上下文
        if current_idx > 0:
            prev_seg = self.all_segments[current_idx - 1]
            if prev_seg.original_text and prev_seg.original_text.strip():
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

        return ""
