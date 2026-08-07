"""
翻译工作流模块
"""
import asyncio
import json, os, time, traceback, signal, threading
from pathlib import Path
from typing import Callable, Dict, Optional, List

from ..core.schema import Settings, SegmentList, ContentSegment, contains_failed_marker
from ..core.exceptions import TranslationError, APIError
from ..utils.logger import logger
from datetime import datetime
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


# OpenAI 兼容 provider 的别名集合：_initialize_translator 与
# _optimize_batch_size_for_provider 共用同一常量。第一轮曾因两处集合漂移
# （漏掉 'openai_compatible' 别名）让云端 provider 落进"本地模型"分支，
# batch_size 不压且日志误报；单一常量从结构上杜绝复发。
OPENAI_COMPATIBLE_PROVIDERS = frozenset({'deepseek', 'openai', 'openai-compatible', 'openai_compatible'})

# 成功率闸门的最小样本量下限：_run_translation_loop 收尾处的整体成功率检查
# （见下方 min_ratio 判定）只在本轮实际尝试段数 >= 本常量时才生效。
# 理由：增量续跑场景下 pending 可能只有个位数/十位数（如全书 99% 已完成，
# 仅剩 5 段因某种原因失败重入队），此时 1 段偶发失败就把比率拉到 20%——
# 远低于统计意义所需的样本量，若照样 raise 会让整本书的渲染/清理全部跳过，
# 且此时 checkpoint 已经落盘过（_run_translation_loop 收尾处的强制保存发生
# 在本检查之前），下次重跑会直接命中同样的 pending 集合再次自锁。真正的
# 局部持续失败会在下一轮 digest/续跑或本函数之后的译后质检回路（_quality_
# check，定向重译硬失败段）里被兜住，小样本没有必要在这里就掐断产出。
MIN_GATE_SAMPLE = 20

# provider 参数上限查表：(max_safe_chars, prompt_overhead, batch_size 硬上限或 None)。
# 三组常量是原三分支复制代码的唯一实质差异，抽表后计算与日志各只留一份。
# 键 None 为兜底：使用简化版 prompt 的 provider（本地 Ollama、claude-agent 等）。
PROVIDER_LIMITS = {
    # 云端API context limit 约32K-128K，考虑完整版prompt开销，保守设置为60K上限
    'openai_compatible': (60000, 2000, 3),
    # Gemini context window 较大 (1M+ tokens)，保守设置上限为20万字符
    'gemini': (200000, 2000, 4),
    # 简化版prompt开销较小，通常有更大的context window，不设硬上限
    None: (100000, 500, None),
}


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
    
    if signum == signal.SIGINT:
        # Ctrl-C 走 Python 语义：抛 KeyboardInterrupt 让上层（如批量脚本
        # translate_books.py 的中断分支/汇总）有机会执行。此前 SIG_DFL+kill
        # 直接终结进程，那些分支永不可达。
        raise KeyboardInterrupt("用户中断（已紧急保存）")
    # SIGTERM：恢复默认行为并正常终止
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
        # 收尾汇总用：实际产出文件与质检残留失败数
        self.produced_outputs: List[Path] = []
        self.still_failed_count: int = 0
        self.glossary: Optional[Dict[str, str]] = None
        
        # 文档标题（原文和译文）
        self.doc_title: str = Path(self.file_path.name).stem  # 文件名（去后缀）
        self.translated_doc_title: str = ""  # 翻译后的文档标题
        
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

        # 查表取 provider 上限：集合常量与 _initialize_translator 共用（见模块头注释），
        # 三组常量以外的差异只剩 reason 文案
        if provider in OPENAI_COMPATIBLE_PROVIDERS:
            limits_key = 'openai_compatible'
            reason = "云端API使用完整版prompt，减少batch_size避免超出context限制"
        elif provider == 'gemini':
            limits_key = 'gemini'
            reason = "Gemini使用完整版prompt，适度减少batch_size保证稳定性"
        else:
            # claude-agent 等云端 provider 也会走到这里（force_simple 同样用简化版 prompt），
            # 文案不能断言"本地模型"
            limits_key = None
            reason = "该provider使用简化版prompt，保持原有batch_size"
        max_safe_chars, prompt_overhead, hard_cap = PROVIDER_LIMITS[limits_key]

        # 计算安全的batch_size（计算与日志各只有一份，替代原三分支复制）
        safe_batch_size = max(1, (max_safe_chars - prompt_overhead) // max_chunk_size)
        optimized_batch_size = min(original_batch_size, safe_batch_size)
        if hard_cap is not None:
            optimized_batch_size = min(optimized_batch_size, hard_cap)

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

        # 应用优化后的batch_size
        optimized = optimized_batch_size != original_batch_size
        if optimized:
            self.settings.processing.batch_size = optimized_batch_size
        
        return optimized, original_batch_size, optimized_batch_size, reason

    def execute(self) -> None:
        """执行完整的翻译工作流"""
        logger.info(f"🚀 开始处理文档: {self.file_path.name}")
        mode_name = getattr(self.settings.processing.translation_mode_entity, 'name', 'Default') if self.settings.processing.translation_mode_entity else 'Default'
        logger.info(f"   - 翻译模式: {mode_name}")
        logger.info(f"   - 项目标识 (Hash): {self.project_name}")
        
        try:
            # 0. 参数优化：根据translator provider调整batch_size
            self._optimize_batch_size_for_provider()

            # 1. 初始化翻译器（preflight：provider 拼错/缺 API key 在此立刻报错，
            #    不必等整本书解析完；此时不创建缓存，等术语表生成后再创建）
            self._initialize_translator()

            # 2. 加载文档结构
            self._load_document()
            segment_count = len(self.all_segments) if self.all_segments else 0

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

            # 6.5 质检回路：扫描失败/术语违例段落，定向重译（上限 1 轮），生成 quality_report.json
            self._quality_check()

            # 7. 翻译完成后处理标题（利用术语表保持一致性）
            #
            # 此步位于全部正文译文已落盘之后、渲染之前：一次标题阶段的
            # soft/瞬态错误若上抛，会让 100% 翻完的书产不出任何成品。
            # translator.translate_titles 内部的 _reraise_if_provider_fatal
            # 对 hard 与 soft 一视同仁上抛（那层的职责是让回退链感知配额/
            # 认证状态，不能改）——这里是标题阶段结束后的最后一道网：只有
            # hard 致命（402 欠费/401 认证/404 模型下线，provider 确定不可用）
            # 才继续上抛终止整轮；soft（429/quota）及其他瞬态失败降级为
            # 标题保留原文 + 记警告，继续走 cleanup/render，不重复回退链的
            # 判定逻辑，只补"标题失败不该拖垒正文成果"这一层。
            try:
                self._post_translate_titles()
            except Exception as e:
                from ..translator.fallback import classify_fatal
                if classify_fatal(e) == 'hard':
                    raise
                logger.warning(
                    f"⚠️ 标题翻译阶段失败（非致命，已降级为标题保留原文，继续渲染）: {e}"
                )

            # 8. 清理资源
            self._cleanup_resources()

            # 9. 渲染最终文档
            self._render_output()

            # 10. 收尾汇总：产物清单 + 按质检结果如实收尾（不再无条件庆祝）
            logger.info("=" * 60)
            failed_n = getattr(self, 'still_failed_count', 0)
            if failed_n:
                logger.warning(f"⚠️ 翻译完成，但有 {failed_n} 段失败（已如实标记在输出中）")
            else:
                logger.info("✅ 全部段落翻译成功")
            logger.info("📦 产物清单:")
            for out_path in self.produced_outputs:
                logger.info(f"   - {out_path}")
            if not self.produced_outputs:
                logger.warning("   - （无成品产出，请检查上方渲染日志）")
            logger.info(f"   - 质检报告: {self.project_dir / 'quality_report.json'}")

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
                # 重解析会把所有 translated_text 归空并覆盖本文件——先把坏文件
                # 改名保留现场，人工仍有机会从中找回已付费的译文。
                corrupt_path = self.structure_path.with_name(
                    f"{self.structure_path.name}.corrupt-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                )
                try:
                    os.replace(self.structure_path, corrupt_path)
                    logger.error(
                        f"❌ structure_map.json 损坏（{e}），将重新解析原文档——"
                        f"已有译文可能丢失！坏文件已保留至 {corrupt_path}，可人工找回"
                    )
                except OSError as mv_err:
                    logger.error(
                        f"❌ structure_map.json 损坏（{e}）且保留现场失败（{mv_err}），"
                        f"将重新解析原文档——已有译文可能丢失！"
                    )
        
        # 2. 重新解析文档
        logger.info("⚙️ 解析文档结构...")
        segments = parse_document(self.file_path, self.structure_path, self.settings)
        
        if segments:
            logger.info(f"✅ 解析完成，生成 {len(segments)} 个片段")
            # parse_document 内部的 pipeline 已把同一份数据写到 structure_path，
            # 此处不再第三次全量落盘
            self.all_segments = segments
            self._build_segment_index()  # 构建快速索引
        else:
            logger.error("❌ 文档解析失败")
            raise TranslationError("文档解析失败，未生成任何内容片段")
        
        logger.info(f"✅ 已加载 {len(self.all_segments)} 个内容片段")
    
    def _initialize_translator(self) -> None:
        """初始化翻译器和缓存管理器"""
        provider = (getattr(self.settings.api, 'translator_provider', 'gemini') or 'gemini').lower()

        # 配置了回退链时，用 FallbackTranslator 包装整条 provider 链：
        # 主 provider 欠费/认证失败/配额耗尽时自动切换到下一个
        fallback_raw = getattr(self.settings.api, 'fallback_providers', '') or ''
        fallback_chain = [p.strip().lower() for p in fallback_raw.split(',') if p.strip()]
        if fallback_chain:
            from ..translator.fallback import (
                FallbackTranslator, SUPPORTED_PROVIDERS, canonical_provider,
            )
            # 主 provider 拼写错误必须快速失败：否则会被链构造静默丢弃，
            # 整本书悄悄落到兜底 provider 上
            if provider not in SUPPORTED_PROVIDERS:
                raise TranslationError(f"未知 translator_provider: {provider}")
            # 按实际后端去重：'claude' 与 'claude-agent' 等别名是同一个后端
            fallback_chain = [
                p for p in fallback_chain
                if canonical_provider(p) != canonical_provider(provider)
            ]
        if fallback_chain:
            self.cache_manager = None
            self.translator = FallbackTranslator(self.settings, [provider] + fallback_chain)
            logger.info(f"✅ 回退翻译器已初始化: {provider} → {' → '.join(fallback_chain)}")
            return

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

        if provider in OPENAI_COMPATIBLE_PROVIDERS:
            # OpenAI-compatible provider (DeepSeek)
            self.cache_manager = None
            self.translator = OpenAICompatibleTranslator(self.settings)
            logger.info(f"✅ OpenAI-compatible 翻译器已初始化 (provider={provider})")
            return

        if provider in {'claude-agent', 'claude_agent', 'agent', 'claude'}:
            # Claude Agent (headless CLI)：走本机 claude 订阅，通常作为回退链兜底
            from ..translator import ClaudeAgentTranslator
            self.cache_manager = None
            self.translator = ClaudeAgentTranslator(self.settings)
            logger.info("✅ Claude Agent 翻译器已初始化 (headless CLI)")
            return

        if provider == 'ollama':
            # 本地 Ollama：独立 ollama_* 配置组（API__OLLAMA_BASE_URL/OLLAMA_MODEL），
            # 可与 deepseek 同链共存
            self.cache_manager = None
            self.translator = OpenAICompatibleTranslator(self.settings, provider='ollama')
            logger.info("✅ Ollama 翻译器已初始化 (本地)")
            return


        raise TranslationError(
            f"未知 translator_provider: {provider}。"
            f"支持的provider: gemini, deepseek, openai, openai-compatible, claude-agent。"
            f"注意：Ollama现已集成到openai-compatible中，请使用OPENAI_BASE_URL=http://localhost:11434"
        )
    
    def _post_translate_titles(self) -> None:
        """
        翻译完成后处理章节标题和文档标题
        
        优化：使用 checkpoint 缓存标题翻译结果，避免重复翻译
        
        放在最后执行的优势：
        1. 可以利用已生成的术语表保持一致性
        2. 不需要复杂的 mode 配置（标题翻译本身是简单任务）
        3. 不影响主翻译流程
        """
        logger.info("📝 开始翻译章节标题和文档标题...")
        
        # 提取待翻译标题（包括文档标题）
        raw_titles = []
        
        # 1. 首先添加文档标题（如果需要翻译）
        if self.doc_title and not is_likely_chinese(self.doc_title):
            raw_titles.append(self.doc_title)
            logger.info(f"   - 文档标题: {self.doc_title}")
        
        # 2. 添加所有章节标题
        for seg in self.all_segments:
            if (seg.is_new_chapter and seg.chapter_title and
                seg.chapter_title.strip() and not is_likely_chinese(seg.chapter_title)):
                raw_titles.append(seg.chapter_title)
        
        if not raw_titles:
            logger.info("   - 无需翻译的标题")
            return
        
        # 去重
        unique_titles = list(dict.fromkeys(raw_titles))
        logger.info(f"   - 发现 {len(unique_titles)} 个唯一标题（含文档标题）")

        # 检查缓存，分离已缓存和需要翻译的标题
        translation_map = {}
        titles_to_translate = []
        
        for title in unique_titles:
            cached = self.checkpoint.get_title_translation(title)
            if cached:
                translation_map[title] = cached
                logger.debug(f"   ✓ 使用缓存: {title} -> {cached}")
            else:
                titles_to_translate.append(title)
        
        # 批量翻译未缓存的标题
        if titles_to_translate:
            logger.info(f"   - 需要翻译 {len(titles_to_translate)} 个新标题")
            new_translations = self.translator.translate_titles(titles_to_translate)
            
            # 合并到translation_map并保存到缓存
            for original, translated in new_translations.items():
                translation_map[original] = translated
                self.checkpoint.save_title_translation(original, translated)
            
            # 保存checkpoint（包含新的标题翻译）
            self.checkpoint.save_checkpoint()
        else:
            logger.info("   - 所有标题均已缓存")
        
        # 回填结果
        update_count = 0
        
        # 1. 翻译文档标题
        if self.doc_title in translation_map:
            translated = translation_map[self.doc_title]
            if translated:
                self.translated_doc_title = translated
                logger.info(f"   - 文档标题翻译: {self.doc_title} -> {self.translated_doc_title}")
        
        # 2. 翻译章节标题
        for seg in self.all_segments:
            if seg.is_new_chapter and seg.chapter_title in translation_map:
                translated = translation_map[seg.chapter_title]
                if translated:
                    seg.chapter_title = translated
                    update_count += 1
        
        logger.info(f"   - 更新了 {update_count} 个章节标题")
        
        # 保存文件名信息到checkpoint
        original_filename = Path(self.file_path).name
        translated_filename = f"{self.translated_doc_title or self.doc_title}{Path(self.file_path).suffix}"
        self.checkpoint.save_filenames(original_filename, translated_filename)
        self.checkpoint.save_checkpoint()
        
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
                if self.checkpoint and t and not contains_failed_marker(t):
                    self.checkpoint.mark_segment_completed(seg.segment_id)
                # 记录被阻断的段落以便人工复核
                if isinstance(t, str) and t.startswith("[Failed: Blocked"):
                    try:
                        self._record_blocked_segments([seg], reason=t)
                    except Exception:
                        logger.debug("Failed to record blocked segment")
            
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

            # 4. 保存阶段性结果——按主循环同款 checkpoint_interval 取模节流：
            # _save_structure_map 是全书 JSON 序列化 + fsync，每批全量落盘只为
            # 多几段增量，大书上 60+ 次 MB 级重写纯属浪费；恢复粒度降为每
            # interval 批，与主翻译循环持平
            if batch_num % self.settings.processing.checkpoint_interval == 0:
                self._save_structure_map(self.all_segments)
                if self.checkpoint:
                    self.checkpoint.save_checkpoint()
                if self.glossary:
                    try:
                        with open(glossary_path, 'w', encoding='utf-8') as gf:
                            json.dump(self.glossary, gf, ensure_ascii=False, indent=2)
                    except Exception as e:
                        logger.warning(f"⚠️ 保存阶段性术语表失败: {e}")

        # 循环外补一次最终保存：覆盖非整 interval 的尾批与 max_terms/饱和
        # 两个 break 提前退出路径，保证断点完整
        self._save_structure_map(self.all_segments)
        if self.checkpoint:
            self.checkpoint.save_checkpoint()
        if self.glossary:
            try:
                with open(glossary_path, 'w', encoding='utf-8') as gf:
                    json.dump(self.glossary, gf, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.warning(f"⚠️ 保存最终术语表失败: {e}")

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
                if self.checkpoint and t and not contains_failed_marker(t):
                    self.checkpoint.mark_segment_completed(seg.segment_id)
                if isinstance(t, str) and t.startswith("[Failed: Blocked"):
                    try:
                        self._record_blocked_segments([seg], reason=t)
                    except Exception:
                        logger.debug("Failed to record blocked segment")
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
        # ========== 第二阶段：进入正式翻译阶段 ==========
        # 统一阶段钩子：所有 provider 从此在 system instruction 中携带
        # mode + glossary（Gemini 在钩子内自行创建完整缓存并重建 base config）
        self.translator.begin_formal_translation(self.glossary)
        logger.info("📦 正式翻译阶段：mode + glossary 已注入 system prompt")
        
        # 获取待翻译片段（所有片段，因为正式翻译需要重新翻译预翻译部分以保证一致性）
        # 注意：预翻译的片段也需要重新翻译，因为：
        # 1. 预翻译使用的是无 glossary 的缓存
        # 2. 正式翻译有了完整术语表，可以获得更一致的翻译质量
        
        # 判断是否需要重新翻译预翻译部分
        reprocess_pretranslated = getattr(self.settings.processing, 'reprocess_pretranslated', True)
        
        if reprocess_pretranslated:
            # 标记预翻译部分为未翻译，以便重新处理。
            # 关键守卫：已进 checkpoint completed 的段说明是正式阶段（带完整
            # mode+glossary）翻出来的，不再清空——此前无条件清空导致每次重跑
            # （包括上轮 100% 完成后的纯补渲染）都白烧前 ~10% 段落的 API 费用。
            # 预翻译产物恰好不在 completed（预翻译时 checkpoint 尚未初始化），
            # 因此该守卫不影响首轮"预翻译段用完整术语表重译"的原设计。
            try:
                ratio = getattr(self.settings.processing, 'glossary_preamble_ratio', 0.1)
                pre_count = max(1, int(len(self.all_segments) * float(ratio)))
            except Exception:
                pre_count = max(1, int(len(self.all_segments) * 0.1))

            cleared = 0
            for seg in self.all_segments[:pre_count]:
                if seg.is_translated and not self.checkpoint.is_segment_completed(seg.segment_id):
                    seg.translated_text = ""  # 清除预翻译结果
                    cleared += 1

            if cleared:
                logger.info(f"🔄 将重新翻译 {cleared} 个预翻译片段（使用完整术语表）")
            else:
                logger.info("✅ 预翻译片段均已由正式阶段翻译，无需重译")
        
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
            success_count = self._run_async_translation(pending_segments)
        else:
            success_count = self._run_sync_translation(pending_segments)

        # 翻译循环结束后，强制保存一次
        logger.info("🔒 翻译循环完成，执行强制保存...")
        self._save_structure_map(self.all_segments)
        self.checkpoint.save_checkpoint()
        logger.info("✅ 强制保存完成")

        # 整体成功率下限检查：只在本轮确实尝试过翻译时才检查（本函数走到这里
        # 说明 pending_segments 非空，即"本轮实际尝试了翻译"），分母用本轮
        # pending 总数而非全书段数——纯增量续跑（如全书仅剩 3 段失败 1 段）
        # 不应被全书基数误判成"大面积失败"。engine 部分失败常降级为
        # "[Failed: ...]" 字符串正常返回而不抛异常（结构化输出缺 id、视觉
        # 逐段隔离等），_run_sync_batches/_process_single_batch 各自的连续
        # 失败熔断只统计"整批 0 成功"，对"每批恰 1 段成功"这种持续性局部
        # 退化视而不见——这里补最后一道全局闸门。
        #
        # 最小样本量下限（MIN_GATE_SAMPLE）：本轮尝试段数 < 该常量时豁免，
        # 不 raise。小样本续跑（如 pending=5 仅 1 段成功）没有统计意义，
        # 强行按比率掐断会让整本 99% 完成的书颗粒无收，且此时 checkpoint
        # 已经落盘（见上方强制保存），重跑会直接自锁在同样的 pending 集合上；
        # 失败段落自有译后质检回路（_quality_check，见下方，定向重译硬失败
        # 段）与下一轮续跑兜底，无需在此处过度反应。大样本（>=20）仍按原逻辑
        # 拦截，防止真正的 provider 持续性退化被小样本豁免误伤放过。
        if success_count is not None:
            total_attempted = len(pending_segments)
            if total_attempted >= MIN_GATE_SAMPLE:
                min_ratio = float(getattr(self.settings.processing, 'min_success_ratio', 0.5))
                actual_ratio = success_count / total_attempted
                if actual_ratio < min_ratio:
                    raise APIError(
                        f"本轮翻译成功率过低：{success_count}/{total_attempted} "
                        f"= {actual_ratio:.1%}，低于下限 {min_ratio:.0%}"
                        "（provider 可能已退化为局部持续失败，为避免产出大半 "
                        "[Failed:] 的成品已中止，未渲染最终文档）"
                    )

    def _glossary_violations(self, seg: ContentSegment) -> List[tuple]:
        """检查单个已译段落是否违反术语表：原文（整词）出现某术语英文 key，
        但译文中缺失其规定的中文译名。返回 [(en, zh), ...]。

        整词匹配英文 key（ASCII 词边界）以降低误报；只考虑长度≥3 的 key。
        """
        if not self.glossary:
            return []
        import re as _re
        orig = seg.original_text or ""
        trans = seg.translated_text or ""
        violations = []
        for en, zh in self.glossary.items():
            if not en or not zh or len(en) < 3:
                continue
            if _re.search(r'(?<![A-Za-z0-9])' + _re.escape(en) + r'(?![A-Za-z0-9])', orig) and zh not in trans:
                violations.append((en, zh))
        return violations

    def _quality_check(self) -> None:
        """译后质检回路：定向重译硬失败段落（上限 1 轮），术语违例仅报告，生成 quality_report.json。

        复用 checkpoint 重入队与同步翻译路径，不重造翻译逻辑。属确定性扫描（零 LLM 成本）。

        重要：只重译「硬失败/空译文」段落——它们本就没有可用译文，重译只赚不赔。
        术语违例段落**只报告不重译**：它们本有合格可读译文，字面包含判定有误报，
        清空重译一旦失败反而毁掉好译文、且成本可能翻倍（见评审 MEDIUM-4/5）。"""
        if not getattr(self.settings.processing, 'enable_quality_check', True):
            logger.info("⏭️ 质检回路已禁用 (enable_quality_check=False)")
            return
        if not self.all_segments:
            return

        logger.info("🔍 质检：扫描失败标记与术语违例段落...")
        # 失败扫描不限 content_type：vision 模式下 image 段恰是最易产出 [Failed:] 的路径，
        # 跳过它们会让报告在成品嵌着失败页时仍打 ✅（可见性缺口，见评审 fallback-book-7）
        failed: SegmentList = []
        term_violations: List[tuple] = []  # (seg, [(en,zh),...])
        for seg in self.all_segments:
            tt = seg.translated_text or ""
            if not tt.strip() or contains_failed_marker(tt):
                failed.append(seg)
                continue
            if seg.content_type != "text":
                continue
            v = self._glossary_violations(seg)
            if v:
                term_violations.append((seg, v))

        # 重译队列仍只收 text：image 段失败只报告不重译（不引入新的视觉重译行为）
        retry_queue = [seg for seg in failed if seg.content_type == "text"]
        image_failed_count = len(failed) - len(retry_queue)

        if retry_queue:
            logger.info(
                "🔁 质检触发定向重译：{} 段硬失败/空译文（上限 1 轮）；另有 {} 段术语违例仅报告不重译".format(
                    len(retry_queue), len(term_violations)
                )
            )
            # 仅对硬失败段清空译文并移出 completed，使其重新入队（无好译文可毁）
            for seg in retry_queue:
                seg.translated_text = ""
                try:
                    self.checkpoint.remove_from_completed(seg.segment_id)
                except Exception as e:
                    # 不静默：移除失败时该段短暂处于"已完成但无译文"状态，
                    # get_pending_segments 的空译文判定会兜住它，但要留痕
                    logger.warning(f"⚠️ 段 {seg.segment_id} 移出 completed 失败（将由待翻译判定兜底）: {e}")
            try:
                self._run_sync_translation(list(retry_queue))
            except Exception as e:
                logger.error(f"❌ 质检定向重译失败（保留失败标记）: {e}")
            self._save_structure_map(self.all_segments)
            self.checkpoint.save_checkpoint()
        elif image_failed_count:
            logger.warning(
                "⚠️ 质检：{} 段非文本（image）翻译失败，仅报告不重译（见 quality_report.json）".format(
                    image_failed_count
                )
            )
        elif term_violations:
            logger.info("ℹ️ 质检：{} 段术语违例（仅报告，见 quality_report.json）".format(len(term_violations)))
        else:
            logger.info("✅ 质检未发现失败或术语违例段落")

        # 重译后仍失败的段落（全类型如实统计，image 失败段不再从报告里消失）
        still_failed = [
            seg for seg in self.all_segments
            if contains_failed_marker(seg.translated_text or "")
        ]
        self.still_failed_count = len(still_failed)
        self._write_quality_report(failed, term_violations, still_failed)

        if still_failed:
            logger.warning(
                "⚠️ 质检结束：仍有 {} 个段落翻译失败（详见 quality_report.json），已如实标记在输出中".format(
                    len(still_failed)
                )
            )
        else:
            logger.info("✅ 质检结束：无残留失败段落")

    def _write_quality_report(self, failed_before, term_violations_before, still_failed) -> None:
        """写出质检报告，替代‘标 [Failed] 就完事’的静默降级。"""
        try:
            report = {
                "generated_at": datetime.now().isoformat(),
                "document": self.file_path.name,
                "total_segments": len(self.all_segments) if self.all_segments else 0,
                "failed_before_retry": [seg.segment_id for seg in failed_before],
                # advisory：子串启发式检测，存在误报，按设计只报告不重译
                # （与 prompt 的 MANDATORY 措辞对齐说明，避免读报告的人误以为已强制执行）
                "term_violations_before_retry": [
                    {
                        "segment_id": seg.segment_id,
                        "type": "advisory",
                        "note": "substring heuristic; may contain false positives; not re-translated by design",
                        "missing_terms": [{"term": en, "expected": zh} for en, zh in vs],
                    }
                    for seg, vs in term_violations_before
                ],
                "still_failed_after_retry": [
                    {
                        "segment_id": seg.segment_id,
                        "page_index": getattr(seg, "page_index", None),
                        "original_excerpt": (seg.original_text or "")[:200],
                        "translated_excerpt": (seg.translated_text or "")[:200],
                    }
                    for seg in still_failed
                ],
            }
            out_path = self.project_dir / "quality_report.json"
            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            logger.info(f"🗂️ 质检报告已写入: {out_path}")
        except Exception as e:
            logger.error(f"❌ 写质检报告失败: {e}")

    def _run_sync_batches(
        self,
        pending_segments: SegmentList,
        on_progress: Optional[Callable[[], None]] = None,
    ) -> int:
        """同步翻译核心循环——rich/非 rich 共用的唯一实现。

        此前两分支各持一份逐行相同的循环，失败判定/保存节奏的修复被迫双写，
        漏一处即行为分叉；现收敛为一份，进度展示差异经 on_progress 回调注入
        （rich 分支传进度条更新回调，非 rich 传 None）。

        Returns:
            成功翻译的片段数
        """
        success_count = 0
        batch_size = self.settings.processing.batch_size
        total_batches = 0
        failed_batches = 0
        consecutive_failures = 0
        last_batch_exc: Optional[Exception] = None
        fatal_exc: Optional[Exception] = None
        # 连续失败熔断阈值：回退链耗尽时 fallback.py 抛的纯中文 APIError
        # （"所有翻译 provider 均不可用（回退链已耗尽）"）、本地 Ollama 中途
        # 断连时 engine.py 抛的 APITimeoutError（Connection refused）都不带
        # 状态码，classify_fatal 判不出 'hard'——不熔断的话会让剩余批次全部
        # continue 空转成 [Failed:]，正常返回后 execute 照常质检+渲染，
        # 产出一本大半 [Failed:] 的成品且退出码 0。
        CONSECUTIVE_FAILURE_THRESHOLD = 3

        for i in range(0, len(pending_segments), batch_size):
            batch = pending_segments[i:i+batch_size]
            batch_no = i // batch_size + 1
            total_batches += 1
            # 为当前batch的第一个segment获取上下文
            context = ""
            if batch:
                context = self._get_context_from_memory(
                    batch[0],
                    self.settings.processing.max_context_length
                )
            # 传入 glossary：正式阶段已在 system instruction（begin_formal_translation），
            # engine 内部的旁路兜底判定保证不会重复注入
            if i > 0 and self.settings.processing.rate_limit_delay > 0:
                # 批间限速（PROCESSING__RATE_LIMIT_DELAY，此前无任何消费者）；
                # 异步路径由 semaphore 控并发，不做逐批 sleep
                time.sleep(self.settings.processing.rate_limit_delay)

            try:
                results = self.translator.translate_batch(batch, context=context, glossary=self.glossary)
            except Exception as e:
                # 逐批隔离：tenacity 重试耗尽后的一次瞬态错误（429/超时/连接）此前
                # 直接上抛整晚任务，与异步路径 _process_single_batch 的 per-batch
                # try 语义不对齐——本地 Ollama 强制走同步路径最受影响。
                # 现只把该批标记失败、记日志、继续后续批次；failed 段不进
                # completed（support.py:117），下次运行 get_pending_segments 会重收。
                logger.error(f"❌ 批次 {batch_no} 翻译失败: {e}")
                failed_batches += 1
                consecutive_failures += 1
                last_batch_exc = e
                # 只把尚未成功的段标记失败，不覆写本批已算出的译文——batch 内
                # translate_batch 抛异常前个别 seg 可能已在上轮跑成功并落过盘，
                # 覆写会销毁已付费译文（与 _run_sync_translation 外层守卫同款）
                for seg in batch:
                    if not seg.translated_text or contains_failed_marker(seg.translated_text):
                        seg.translated_text = f"[Failed: {str(e)}]"
                        self.checkpoint.mark_segment_failed(seg.segment_id, str(e))
                    if on_progress:
                        on_progress()

                if batch_no % self.settings.processing.checkpoint_interval == 0:
                    self._save_structure_map(self.all_segments)
                    self.checkpoint.save_checkpoint()

                # 硬性致命错误（402 欠费/401 认证/404 模型下线）或连续失败达
                # 熔断阈值：裸引擎（无 fallback 链）/ 回退链已耗尽 / 本地
                # Ollama 断连都不会自愈，continue 会让剩余批次全部空转成
                # [Failed:]，execute 仍照常质检+渲染出一本几乎全失败的成品、
                # 退出码正常。本批已标记失败并落盘（保住进度）后立即 break，
                # 交给循环收尾统一 raise，让 execute 感知 provider 已不可用。
                from src.translator.fallback import classify_fatal
                if classify_fatal(e) == 'hard' or consecutive_failures >= CONSECUTIVE_FAILURE_THRESHOLD:
                    fatal_exc = e
                    break
                continue

            # 熔断盲区：engine 部分失败不抛异常，而是把单段/整批降级为
            # "[Failed: ...]" 字符串正常返回（如 vision 逐段隔离、DeepSeek
            # 缺失翻译兜底键等）。此前这里无条件清零 consecutive_failures，
            # 相当于回退链耗尽后每批都"成功返回"一堆 [Failed:] 字符串，
            # 连续失败熔断永远不会命中——只统计本批实际成功数，0 成功才计入
            # 连续失败（不清零），达阈值同样熔断；只要本批有成功才清零。
            batch_success_count = 0
            if results is not None and len(results) != len(batch):
                # engine 返回条数与批大小不一致：zip 会静默截断多出的段，
                # 这些段的 translated_text 保持不变（可能是空字符串）且不进
                # completed/failed 任一 checkpoint 分支，问题会一直潜伏到
                # 渲染阶段才以空白译文的形式暴露，此处先落一条日志留痕。
                logger.warning(
                    f"⚠️ 批次 {batch_no} engine 返回 {len(results)} 条结果，"
                    f"与批大小 {len(batch)} 不一致，多出的段将被 zip 静默丢弃"
                )
            for seg, trans in zip(batch, results):
                if trans and not contains_failed_marker(trans):
                    seg.translated_text = trans
                    self.checkpoint.mark_segment_completed(seg.segment_id)
                    success_count += 1
                    batch_success_count += 1
                else:
                    seg.translated_text = trans if trans else "[Failed: Empty response]"
                    self.checkpoint.mark_segment_failed(seg.segment_id, trans or "Empty response")
                    if isinstance(trans, str) and trans.startswith("[Failed: Blocked"):
                        try:
                            self._record_blocked_segments([seg], reason=trans)
                        except Exception:
                            logger.debug("Failed to record blocked segment")

                if on_progress:
                    on_progress()

            # 定期保存检查点
            if batch_no % self.settings.processing.checkpoint_interval == 0:
                self._save_structure_map(self.all_segments)
                self.checkpoint.save_checkpoint()

            if batch_success_count == 0:
                failed_batches += 1
                consecutive_failures += 1
                # last_batch_exc 用于收尾「全部批次都失败」时的整体 raise；
                # 这条路径没有真实异常对象（engine 降级为字符串未抛异常），
                # 补一个代表性 APIError。无条件覆写（而非 or 短路）：全败场景下
                # 每一批都会落到这里，覆写让收尾 raise 报告的是最新一批的批号，
                # 而不是永远指向第一批——排障时最新失败批次更接近真实故障点。
                last_batch_exc = APIError(
                    f"批次 {batch_no} 翻译 0 成功（engine 未抛异常，[Failed:] 字符串降级）"
                )
                if consecutive_failures >= CONSECUTIVE_FAILURE_THRESHOLD:
                    fatal_exc = APIError(
                        f"连续 {consecutive_failures} 批翻译 0 成功"
                        "（engine 未抛异常，[Failed:] 字符串降级），熔断终止"
                    )
                    break
            else:
                consecutive_failures = 0

        # fatal / 全败时不打「✅ 完成」误导排障——后面紧接着就会整体 raise，
        # 调用方（execute）看到 ✅ 却接着看到异常堆栈会误判是不是又出了别的问题。
        if fatal_exc:
            logger.warning(
                f"⚠️ 同步翻译熔断终止: {success_count}/{len(pending_segments)} 成功后触发致命错误: {fatal_exc}"
            )
        elif total_batches > 0 and failed_batches == total_batches:
            logger.warning(f"⚠️ 同步翻译全部批次失败: 0/{len(pending_segments)} 成功")
        else:
            logger.info(f"✅ 同步翻译完成: {success_count}/{len(pending_segments)} 成功")

        # 硬性致命错误短路：即便前面已有批次成功，也不能让 execute 把
        # 剩下全空转成 [Failed:] 的批次当正常结果去质检+渲染成品。修完
        # FallbackExhaustedError（classify_fatal 直接短路判 'hard'）后，
        # 这里与异步路径 _process_single_batch 的 fatal_holder 短路语义对齐，
        # 但两侧的短路时机不同：同步侧批次严格串行，判到 'hard' 当批立即
        # break，循环里尚未开始的批次天然不会被跑到；异步侧批次已并发建
        # task，'hard' 只能在当批异常处理内置位 fatal_holder，已在飞的
        # 其他批次仍会跑完，只有后续新批次在拿到 semaphore 入口时检查到
        # fatal_holder 才会跳过、不再发起真实请求。同步侧额外保留
        # CONSECUTIVE_FAILURE_THRESHOLD 连续失败熔断，兜底 classify_fatal
        # 判不出 'hard' 的持续性故障（如本地 Ollama 断连的 APITimeoutError）。
        # 先落盘保住已完成进度，再统一在这里 raise。
        if fatal_exc:
            raise fatal_exc

        # 全部批次都失败（且都是瞬态失败，无 hard fatal）才整体抛错；
        # 单批瞬态失败已就地标记并继续，不应终止整晚任务。
        if total_batches > 0 and failed_batches == total_batches:
            raise last_batch_exc

        return success_count

    def _run_sync_translation(self, pending_segments: SegmentList) -> int:
        """同步翻译模式（带进度条）

        返回本轮实际成功段数（供 _run_translation_loop 做整体成功率下限
        检查；_quality_check 的定向重译调用会忽略该返回值，不受影响）。
        """
        logger.info("🔄 使用同步模式翻译")
        logger.info(f"📝 开始同步翻译 {len(pending_segments)} 个片段...")

        try:
            # 尝试使用 rich 进度条，如果失败则回退到无进度条模式
            use_rich = self.settings.processing.use_rich_progress

            if use_rich:
                try:
                    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
                except ImportError:
                    logger.warning("⚠️ Rich 库未安装，使用简单模式（无进度条）")
                    use_rich = False

            if use_rich:
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[bold blue]{task.description}"),
                    BarColumn(),
                    TaskProgressColumn(),
                    console=None,  # 使用默认console
                ) as progress:
                    task = progress.add_task("[cyan]同步翻译中...", total=len(pending_segments))
                    success_count = self._run_sync_batches(
                        pending_segments,
                        on_progress=lambda: progress.update(task, advance=1),
                    )
            else:
                # 简单模式（无进度条）：同一实现，仅不传进度回调
                success_count = self._run_sync_batches(pending_segments, on_progress=None)

            # 最终保存
            self._save_structure_map(self.all_segments)
            self.checkpoint.save_checkpoint()

            return success_count

        except Exception as e:
            logger.error(f"❌ 同步翻译失败: {e}")
            # 只把尚未成功的段标记失败，不覆写本轮已成功的译文
            # （否则中途抛异常会把已完成批次的译文销毁并落盘，下次运行全部重新付费翻译）
            for seg in pending_segments:
                if not seg.translated_text or contains_failed_marker(seg.translated_text):
                    seg.translated_text = f"[Failed: {str(e)}]"
                    self.checkpoint.mark_segment_failed(seg.segment_id, str(e))
            self._save_structure_map(self.all_segments)
            self.checkpoint.save_checkpoint()
            raise
    
    def _run_async_translation(self, pending_segments: SegmentList) -> int:
        """异步翻译模式（多批次并发执行，真正的并行翻译）

        架构优化：
        - 之前：batch 串行执行，实际并发度 = 1
        - 现在：多个 batch 并发执行，实际并发度 = max_concurrent_batches

        调用链：
        workflow._run_async_translation (同步入口)
          └── asyncio.run(_run_concurrent_batches)  [单次调用]
                └── asyncio.gather(*batch_tasks)  [真正的并发]
                      └── _process_single_batch (每个 batch)
                            └── async_t.translate_vision_batch_async (批内含 image 段)
                                / async_t.translate_text_batch_async (纯文本批)

        返回本轮实际成功段数（供 _run_translation_loop 做整体成功率下限检查）。
        """
        logger.info("⚡ 使用异步模式翻译（多批次并发+即时保存）")

        # 检查translator是否支持异步
        if not hasattr(self.translator, 'async_translator') or self.translator.async_translator is None:
            logger.warning("⚠️ 当前translator不支持异步模式，降级到同步模式")
            return self._run_sync_translation(pending_segments)
        
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
        stats = {"success": 0, "processed": 0, "completed_batches": 0, "consecutive_zero_success": 0}

        # 连续 0 成功熔断阈值：与同步路径 _run_sync_batches 的
        # CONSECUTIVE_FAILURE_THRESHOLD 同款语义，兜底 engine 不抛异常、只把
        # 结果降级成 "[Failed: ...]" 字符串正常 return 的持续性故障（如结构化
        # 输出缺失 id 兜底键、Gemini 重试耗尽后返回占位字符串）——这类故障
        # classify_fatal 拿不到异常对象，下面的 fatal_holder 硬性致命错误
        # 短路永不命中，224 批全会照常发真实请求、每批还带内部重试，直到
        # _quality_check 才发现整本书都是 [Failed:]。批次并发执行、完成顺序
        # 不严格按派发顺序，这里的“连续”是按完成顺序统计的近似值，与同步路径
        # 严格串行的语义略有差异，但足以在持续性故障下及时止损。
        CONSECUTIVE_FAILURE_THRESHOLD = 3

        # provider 致命错误（欠费/认证/模型下线）的全书级短路点：
        # _process_single_batch 内部为了落盘失败状态会捕获所有异常，若不在此
        # 记录 fatal，gather 路径会继续把后续几十个批次逐一打成失败并静默收尾。
        # 这里只暂存，待 as_completed 循环收尾后统一 raise，保证已保存的
        # checkpoint/结构图不丢，同时让调用方（execute）感知 provider 已不可用。
        fatal_holder = {"exc": None}
        
        async def _process_single_batch(batch_idx: int, batch: SegmentList, semaphore: asyncio.Semaphore):
            """处理单个 batch（在 semaphore 控制下）"""
            async with semaphore:
                # 致命错误真短路：hard fatal 已由某个先完成的批次置位到
                # fatal_holder。同步侧遇 hard fatal 是当批 break，循环体天然
                # 不会再跑后续批次；异步侧批次在创建时已全部并发建好 task，
                # 只能在这里——真正拿到 semaphore 名额、即将发起请求之前——
                # 拦一次：还没跑到这一步说明本批尚未发出请求，直接短路标
                # 失败即可，不必再对已知报废的 provider 发一次注定失败的
                # 请求（省配额、省时间）。此前这里没有这道检查，_process_
                # _single_batch 每批都会照常发起真实请求，fatal 置位后剩余
                # 批次仍会全部空转打一遍失败才收尾。
                if fatal_holder["exc"]:
                    with lock:
                        for seg in batch:
                            if not seg.translated_text or contains_failed_marker(seg.translated_text):
                                seg.translated_text = "[Failed: 致命错误熔断，本批未发起请求]"
                                self.checkpoint.mark_segment_failed(
                                    seg.segment_id, "致命错误熔断，本批未发起请求"
                                )
                            stats["processed"] += 1
                        stats["completed_batches"] += 1
                    return batch_idx, False
                try:
                    # 获取上下文（读取当前已翻译的内容）
                    context = ""
                    if batch:
                        context = self._get_context_from_memory(
                            batch[0],
                            self.settings.processing.max_context_length
                        )
                    
                    # 执行翻译：整批路由视觉/文本通路（判定与同步 translate_batch 一致，
                    # 见 engine.py translate_batch 的 has_image 分流）。不拆批不合并——
                    # 混合批内文本段的降级由各 translate_vision_batch_async 实现内置，
                    # 否则 vision 模式下 image 段（original_text=""）会被当空文本送翻译。
                    async_t = self.translator.async_translator
                    has_image = any(seg.content_type == "image" for seg in batch)
                    if has_image:
                        batch_results = await async_t.translate_vision_batch_async(
                            batch, context, self.glossary
                        )
                    else:
                        batch_results = await async_t.translate_text_batch_async(
                            batch, context, self.glossary
                        )
                    
                    # 处理结果（线程安全）
                    batch_success = 0
                    with lock:
                        for seg, trans in zip(batch, batch_results):
                            # 与同步路径同源判定：手写 startswith/endswith 会漏检
                            # "[...翻译被截断]"，把截断段记成功后 checkpoint 就不再重译
                            if trans and not contains_failed_marker(trans):
                                seg.translated_text = trans
                                self.checkpoint.mark_segment_completed(seg.segment_id)
                                stats["success"] += 1
                                batch_success += 1
                            else:
                                seg.translated_text = trans if trans else "[Failed: Empty response]"
                                self.checkpoint.mark_segment_failed(seg.segment_id, trans or "Empty response")
                                if isinstance(trans, str) and trans.startswith("[Failed: Blocked"):
                                    try:
                                        self._record_blocked_segments([seg], reason=trans)
                                    except Exception:
                                        logger.debug("Failed to record blocked segment")
                            stats["processed"] += 1

                        stats["completed_batches"] += 1

                        # 连续 0 成功熔断：engine 把整批降级成 [Failed:] 字符串
                        # 正常 return，不会走下面的 except 分支，只能在这里统计。
                        # batch_success==0 才计入连续失败（不清零），只要本批
                        # 有成功即清零；达阈值时补一个代表性 APIError 写入
                        # fatal_holder，复用上面 1281 行现成的短路分支——后续
                        # 尚未拿到 semaphore 的批次会被拦掉，不必再对已知报废
                        # 的 provider 发真实请求。
                        if batch_success == 0:
                            stats["consecutive_zero_success"] += 1
                            if (
                                stats["consecutive_zero_success"] >= CONSECUTIVE_FAILURE_THRESHOLD
                                and not fatal_holder["exc"]
                            ):
                                fatal_holder["exc"] = APIError(
                                    f"连续 {stats['consecutive_zero_success']} 批翻译 0 成功"
                                    "（engine 未抛异常，[Failed:] 字符串降级），熔断终止"
                                )
                        else:
                            stats["consecutive_zero_success"] = 0

                        # 按 checkpoint_interval 节流（与同步路径对齐）。
                        # 此前每批全量落盘 2.7MB structure_map + fsync 且在锁内、
                        # 在事件循环线程上——一本书 ≈300MB 写入 + 224 次 fsync，
                        # 写盘期间整个并发调度停摆。结尾处仍有一次最终保存兜底。
                        interval = max(1, getattr(self.settings.processing, 'checkpoint_interval', 1))
                        if stats["completed_batches"] % interval == 0:
                            self._save_structure_map(self.all_segments)
                            self.checkpoint.save_checkpoint()

                    logger.info(f"✅ 批次 {batch_idx}/{total_batches} 完成 (本批成功: {batch_success}/{len(batch)}, 总进度: {stats['completed_batches']}/{total_batches})")
                    return batch_idx, True
                    
                except Exception as e:
                    logger.error(f"❌ 批次 {batch_idx} 失败: {e}")

                    # 硬性致命错误（classify_fatal=='hard'：402 欠费/401 认证/404 模型下线）
                    # 记入 fatal_holder：本批仍按现逻辑标记失败并保存（保住已有进度），
                    # 但收尾时必须整体短路，不能让后续批次继续空转烧配额。
                    from src.translator.fallback import classify_fatal
                    is_hard_fatal = classify_fatal(e) == 'hard'
                    if is_hard_fatal:
                        fatal_holder["exc"] = e

                    # 标记失败（线程安全）。与同步路径同款守卫：只把尚未成功的段
                    # 标记失败，不覆写本批已算出的译文（否则视觉批一段 429 会把
                    # 同批其余已付费译文销毁并落盘）
                    with lock:
                        for seg in batch:
                            if not seg.translated_text or contains_failed_marker(seg.translated_text):
                                seg.translated_text = f"[Failed: {str(e)}]"
                                self.checkpoint.mark_segment_failed(seg.segment_id, str(e))
                            stats["processed"] += 1

                        stats["completed_batches"] += 1

                        # 保存失败状态：按 checkpoint_interval 节流，与成功分支
                        # （上面 :1319-1326）对齐。此前失败分支无条件每批落盘，
                        # provider 挂掉后剩余批次逐一走到这里，224 批的书会写
                        # ~600MB（每批全量 structure_map + fsync）。置位
                        # fatal_holder 的那一批（provider 刚失效的瞬间）无条件
                        # 立即落盘兜底——一旦确认 provider 已挂，后续新批次会被
                        # 上面的短路分支拦掉，可能再也不会有机会走到常规的
                        # 按间隔落盘，必须保证这一刻的进度先落地。
                        interval = max(1, getattr(self.settings.processing, 'checkpoint_interval', 1))
                        if is_hard_fatal or stats["completed_batches"] % interval == 0:
                            try:
                                self._save_structure_map(self.all_segments)
                                self.checkpoint.save_checkpoint()
                            except Exception as save_exc:
                                logger.error(f"保存失败状态时出错: {save_exc}")

                    return batch_idx, False
        
        async def _run_concurrent_batches(on_batch_done: Optional[Callable[[], None]] = None):
            """并发执行所有 batch——rich/非 rich 共用的唯一驱动循环。

            此前 rich 分支另维护一份带进度条的驱动副本，fatal 短路等收尾语义
            靠注释手工"对齐"两处；现收敛为一份，进度条更新经 on_batch_done
            回调注入（rich 分支传回调，非 rich 传 None）。
            """
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
                    if on_batch_done:
                        on_batch_done()
                except Exception as e:
                    # 仅记录非致命的批次任务异常；fatal 已在 _process_single_batch
                    # 内部捕获并写入 fatal_holder，不会传导到这里，
                    # 故此 except 不会吞掉致命错误。
                    logger.error(f"批次任务异常: {e}")

            # 全书级短路：循环放到最后才 raise，是为了让已在飞的批次先落盘
            # （fatal 在 _process_single_batch 内部已捕获并置位，不会被上面的
            # except 拦掉），避免半途 raise 丢掉其他批次已保存的进度。
            if fatal_holder["exc"]:
                raise fatal_holder["exc"]

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

                def _update_progress():
                    progress.update(batch_task, completed=stats["completed_batches"])
                    progress.update(segment_task, completed=stats["processed"])

                # 执行并发翻译（与非 rich 路径同一驱动，仅注入进度条回调）
                asyncio.run(_run_concurrent_batches(on_batch_done=_update_progress))

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

        return stats["success"]

    def _cleanup_resources(self) -> None:
        """清理资源

        统一走 translator.cleanup()：FallbackTranslator 会逐 provider 清理各自的
        异步线程池。此前这里引用不存在的 cache_manager.cleanup_all_caches()
        （属性名/方法名双错，hasattr 恒 False，整段死代码），且只清顶层
        _async_translator、漏掉 fallback 链内各实例的线程池。
        Gemini context cache 不在此清理：按内容 hash 跨轮次复用，交给 TTL 过期。
        """
        try:
            if hasattr(self.translator, 'cleanup'):
                self.translator.cleanup()
            logger.info("✅ 资源清理完成")
        except Exception as e:
            logger.warning(f"⚠️ 清理资源时出现警告: {e}")
    
    def _render_output(self) -> None:
        """Render: 生成最终文档（Markdown + PDF + EPUB）"""
        from ..renderer.markdown import MarkdownRenderer
        
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
        
        # 检测源文件类型
        source_suffix = self.file_path.suffix.lower()
        is_epub_source = source_suffix == '.epub'
        is_pdf_source = source_suffix == '.pdf'
        
        # 1. 生成 Markdown
        md_renderer = MarkdownRenderer(self.settings)
        md_output_path = final_dir / f"{Path(self.file_path.name).stem}_Translated.md"
        md_renderer.render_to_file(
            self.all_segments, 
            md_output_path, 
            title=self.doc_title,
            translated_title=self.translated_doc_title or self.doc_title
        )
        logger.info(f"✅ Markdown 已保存到: {md_output_path}")
        self.produced_outputs.append(md_output_path)
        
        # 2. 生成 EPUB（默认）- 根据源文件类型选择不同策略
        epub_output_path = final_dir / f"{Path(self.file_path.name).stem}_Translated.epub"
        
        if is_epub_source:
            # EPUB 源文件：使用 EPUBRenderer 回填原文件
            try:
                from ..renderer.epub import EPUBRenderer
                epub_renderer = EPUBRenderer(self.settings)
                epub_renderer.render_to_file(
                    segments=self.all_segments,
                    original_epub_path=self.file_path,
                    output_path=epub_output_path,
                    title=self.doc_title,
                    translated_title=self.translated_doc_title or self.doc_title
                )
                logger.info(f"✅ EPUB 已保存到: {epub_output_path}")
                self.produced_outputs.append(epub_output_path)
            except ImportError:
                logger.info("ℹ️  跳过 EPUB 生成（未安装 ebooklib）")
            except Exception as e:
                logger.warning(f"⚠️  EPUB 生成失败: {e}")
                import traceback
                logger.debug(traceback.format_exc())
        else:
            # PDF/TXT 等其他源文件：使用 HTML → EPUB 转换
            try:
                from ..renderer.epub import render_html_to_epub
                import markdown2
                
                # 生成 HTML 内容（复用 PDF 的渲染逻辑）
                markdown_content = md_renderer.render_to_string(
                    self.all_segments,
                    title=self.doc_title,
                    translated_title=self.translated_doc_title or self.doc_title
                )
                
                # Markdown → HTML
                html_body = markdown2.markdown(
                    markdown_content,
                    extras=[
                        "fenced-code-blocks", 
                        "tables", 
                        "footnotes", 
                        "break-on-newline", 
                        "header-ids",
                        "code-friendly",
                        "cuddled-lists"
                    ]
                )
                
                # 构建完整 HTML
                display_title = self.translated_doc_title if self.translated_doc_title else self.doc_title
                html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>{display_title}</title>
</head>
<body>
    {html_body}
</body>
</html>"""
                
                # HTML → EPUB
                render_html_to_epub(
                    html_content=html_content,
                    output_path=epub_output_path,
                    settings=self.settings,
                    title=self.doc_title,
                    translated_title=self.translated_doc_title or self.doc_title
                )
                logger.info(f"✅ EPUB 已保存到: {epub_output_path}")
                self.produced_outputs.append(epub_output_path)
                
            except ImportError as e:
                logger.info(f"ℹ️  跳过 EPUB 生成（缺少依赖: {e}）")
            except Exception as e:
                logger.warning(f"⚠️  EPUB 生成失败: {e}")
                import traceback
                logger.debug(traceback.format_exc())
        
        # 3. 生成 PDF（可选，如果依赖可用）- 同时生成桌面版和移动版
        try:
            from ..renderer.pdf import PDFRenderer
            pdf_renderer = PDFRenderer(self.settings)
            
            pdf_path = final_dir / f"{Path(self.file_path.name).stem}_Translated.pdf"
            # 使用 generate_both=True 同时生成桌面版和移动版
            # 桌面版：{filename}_Translated.pdf
            # 移动版：{filename}_Translated_mobile.pdf
            produced = pdf_renderer.render_to_file(
                self.all_segments,
                pdf_path,
                title=self.doc_title,
                translated_title=self.translated_doc_title or self.doc_title,
                generate_both=True  # 生成两个版本
            )
            # 只对真正生成的文件打 ✅（渲染器内部失败会降级为日志、不上抛，
            # 此前这里无条件宣布成功，WeasyPrint 缺库时是假日志）
            for p in produced:
                logger.info(f"✅ PDF 已保存到: {p}")
                self.produced_outputs.append(p)
            if not produced:
                logger.warning("⚠️  本次未生成任何 PDF（详见上方渲染器日志），Markdown/EPUB 不受影响")
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
            
            # tmp+replace 原子写（与 merge_final.py 一致）：structure_map 是全部译文的
            # 唯一载体，写入中途被杀会留下截断 JSON、下次运行静默重解析丢弃译文。
            # tmp 放同目录，保证 os.replace 不跨设备。
            tmp_path = self.structure_path.with_suffix(self.structure_path.suffix + '.tmp')
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()  # 强制刷新缓冲区
                os.fsync(f.fileno())  # 强制同步到磁盘
            os.replace(tmp_path, self.structure_path)
            
            # 统计已翻译数量（用于进度显示）
            translated_count = sum(1 for seg in segments if seg.is_translated)
            logger.info(f"💾 Structure map 已保存到: {self.structure_path}")
            logger.info(f"💾 翻译进度: {translated_count}/{len(segments)} 个片段已完成")
            
        except Exception as e:
            logger.error(f"❌ 保存结构状态失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            raise

    def _record_blocked_segments(self, segments: List[ContentSegment], reason: str | None = None) -> None:
        """
        Persist blocked segments to output/<project>/blocked_segments.json for manual review.
        Each entry contains segment_id, page_index, chapter_title, original_text (truncated), and reason.
        """
        try:
            out_path = self.project_dir / "blocked_segments.json"
            existing = []
            if out_path.exists():
                try:
                    with open(out_path, 'r', encoding='utf-8') as f:
                        existing = json.load(f)
                except Exception:
                    existing = []

            ts = datetime.utcnow().isoformat() + 'Z'
            to_add = []
            for seg in segments:
                entry = {
                    'segment_id': seg.segment_id,
                    'page_index': seg.page_index,
                    'chapter_title': seg.chapter_title,
                    'original_text': (seg.original_text[:1000] + '...') if len(seg.original_text) > 1000 else seg.original_text,
                    'reason': reason or seg.translated_text,
                    'timestamp': ts
                }
                to_add.append(entry)

            merged = existing + to_add
            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)

            logger.info(f"💾 已记录 {len(to_add)} 个被阻断段落到: {out_path}")
        except Exception as e:
            logger.error(f"⚠️ 保存被阻断段落失败: {e}")
    
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
