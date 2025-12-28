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
        
    def execute(self) -> None:
        """执行完整的翻译工作流"""
        logger.info(f"🚀 开始处理文档: {self.file_path.name}")
        logger.info(f"   - 翻译模式: {self.settings.processing.translation_mode_entity.name}")
        logger.info(f"   - 项目标识 (Hash): {self.project_name}")
        
        try:
            # 1. 加载文档结构
            self._load_document()
            
            # 2. 初始化翻译器和缓存
            self._initialize_translator()
            
            # 3. 预翻译标题
            self._pre_translate_titles()
            
            # 4. 生成术语表
            self._generate_glossary()
            
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

            self.translator = GeminiTranslator(
                self.settings,
                cache_manager=self.cache_manager
            )
            logger.info("✅ Gemini 翻译器已初始化")
            return

        if provider in {'deepseek', 'openai', 'openai-compatible', 'openai_compatible'}:
            # OpenAI-compatible provider (DeepSeek)
            self.cache_manager = None
            self.translator = OpenAICompatibleTranslator(self.settings)
            logger.info(f"✅ OpenAI-compatible 翻译器已初始化 (provider={provider})")
            return

        raise TranslationError(f"未知 translator_provider: {provider}")
    
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
                translations = self.translator.translate_batch(pending_pre, context="")
                for seg, t in zip(pending_pre, translations):
                    seg.translated_text = t
                self._save_structure_map(self.all_segments)
            
            # 提取术语表
            self.glossary = self.translator.extract_glossary(pre_segments)
            
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
        """同步翻译模式"""
        logger.info("🔄 使用同步模式翻译")
        logger.info(f"📝 开始同步翻译 {len(pending_segments)} 个片段...")
        
        try:
            results = self.translator.translate_batch(
                pending_segments,
                context="",
                glossary=self.glossary
            )
            
            # 处理翻译结果
            success_count = 0
            for seg, trans in zip(pending_segments, results):
                if trans and not trans.startswith("[Failed"):
                    seg.translated_text = trans
                    self.checkpoint.mark_segment_completed(seg.segment_id)
                    success_count += 1
                else:
                    seg.translated_text = trans if trans else "[Failed: Empty response]"
                    self.checkpoint.mark_segment_failed(seg.segment_id, trans or "Empty response")
            
            # 保存结果
            self._save_structure_map(self.all_segments)
            self.checkpoint.save_checkpoint()
            
            logger.info(f"✅ 同步翻译完成: {success_count}/{len(pending_segments)} 成功")
            
        except Exception as e:
            logger.error(f"❌ 同步翻译失败: {e}")
            for seg in pending_segments:
                seg.translated_text = f"[Failed: {str(e)}]"
                self.checkpoint.mark_segment_failed(seg.segment_id, str(e))
            self._save_structure_map(self.all_segments)
            self.checkpoint.save_checkpoint()
            raise
    
    def _run_async_translation(self, pending_segments: SegmentList) -> None:
        """异步翻译模式"""
        logger.info("⚡ 使用异步模式翻译（提升速度）")
        
        try:
            batch_size = self.settings.processing.batch_size
            batches = [
                pending_segments[i:i+batch_size] 
                for i in range(0, len(pending_segments), batch_size)
            ]
            total_batches = len(batches)
            
            logger.info(f"🚀 开始异步翻译 {len(pending_segments)} 个片段（{total_batches} 批次，批大小 {batch_size}）...")
            
            async def translate_all_batches_with_progress():
                """所有批次并发执行，带进度显示"""
                tasks = [
                    self.translator.async_translator.translate_text_batch_async(
                        batch, "", self.glossary
                    )
                    for batch in batches
                ]
                
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
                        
                        results = [None] * total_batches
                        for i, task in enumerate(asyncio.as_completed(tasks)):
                            result = await task
                            results[i] = result
                            progress.update(task_id, advance=1)
                        
                        return results
                else:
                    return await asyncio.gather(*tasks)
            
            # 执行并发翻译
            all_results = asyncio.run(translate_all_batches_with_progress())
            
            # 处理翻译结果
            success_count = 0
            batch_idx = 0
            for batch, batch_results in zip(batches, all_results):
                for seg, trans in zip(batch, batch_results):
                    if trans and not trans.startswith("[Failed"):
                        seg.translated_text = trans
                        self.checkpoint.mark_segment_completed(seg.segment_id)
                        success_count += 1
                    else:
                        seg.translated_text = trans if trans else "[Failed: Empty response]"
                        self.checkpoint.mark_segment_failed(seg.segment_id, trans or "Empty response")
                
                # 每5个batch保存一次
                batch_idx += 1
                if batch_idx % 5 == 0:
                    self._save_structure_map(self.all_segments)
                    self.checkpoint.save_checkpoint()
            
            # 最终保存
            self._save_structure_map(self.all_segments)
            self.checkpoint.save_checkpoint()
            
            logger.info(f"✅ 异步翻译完成: {success_count}/{len(pending_segments)} 成功")
            
        except Exception as e:
            logger.error(f"❌ 异步翻译失败，降级到同步模式: {e}")
            self._run_sync_translation(pending_segments)
    
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
        通过 segment_id 在 all_segments 中查找前文已翻译片段
        """
        # 找到当前片段的位置
        current_idx = next((i for i, seg in enumerate(self.all_segments) if seg.segment_id == current_segment.segment_id), -1)
        if current_idx == -1:
            return ""
        
        # 获取前几个已翻译的片段内容
        context_parts = []
        context_length = 0
        
        # 向前查找已翻译的片段
        for i in range(current_idx - 1, -1, -1):
            seg = self.all_segments[i]
            if seg.is_translated and seg.translated_text:
                # 估算长度（中文字符按2字节算）
                text_length = len(seg.translated_text.encode('utf-8'))
                if context_length + text_length > max_length:
                    break
                
                context_parts.insert(0, seg.translated_text)  # 保持顺序
                context_length += text_length
        
        return " ".join(context_parts).strip()
