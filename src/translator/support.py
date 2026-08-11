"""
翻译核心管理模块
整合：断点续传、缓存持久化管理、Prompt 管理
"""
import json
import os
import time
import hashlib
import threading
from pathlib import Path
from typing import Dict, Any, Optional, List, Set, Tuple, TYPE_CHECKING
from datetime import datetime, timedelta

from ..core.schema import ContentSegment, SegmentList, contains_failed_marker
from ..utils.logger import get_logger

if TYPE_CHECKING:
    from ..core.schema import Settings, TranslationMode

logger = get_logger(__name__)


# ========================================================================
# 1. 断点续传管理
# ========================================================================

class CheckpointManager:
    """翻译进度检查点管理器"""
    
    def __init__(self, settings: 'Settings'):
        """
        Args:
            settings: 全局设置对象（从document_path自动计算doc_hash）
        """
        self.settings = settings
        
        # 从settings自动计算doc_hash
        from ..utils.file import get_file_hash
        doc_hash = get_file_hash(settings.files.document_path) if settings.files.document_path else "unknown"
        
        # 从 settings 中获取项目目录
        base_dir = Path(settings.files.output_base_dir) if isinstance(settings.files.output_base_dir, str) else settings.files.output_base_dir
        self.project_dir = base_dir / doc_hash
        self.checkpoint_file = self.project_dir / "checkpoint.json"
        self.checkpoint_data: Dict = {}
        
        # enable_checkpoint=False 时整体降级为 no-op（此前该配置无任何消费者）
        self.enabled = bool(getattr(settings.processing, 'enable_checkpoint', True))
        if not self.enabled:
            logger.warning("⚠️ 断点续传已禁用（PROCESSING__ENABLE_CHECKPOINT=False）：中断即需重头翻译")

        # 记录checkpoint文件路径，便于排查
        logger.info(f"📍 Checkpoint文件路径: {self.checkpoint_file.absolute()}")
        
        self._load_checkpoint()
    
    def _load_checkpoint(self):
        """加载现有的检查点文件"""
        if self.enabled and self.checkpoint_file.exists():
            try:
                with open(self.checkpoint_file, 'r', encoding='utf-8') as f:
                    self.checkpoint_data = json.load(f)
                # 内部用 set（O(1) 成员判断；此前 list 上 in/append 是 O(n)，
                # 全书标记完成合计 O(n²)），仅在落盘时转有序 list
                self._completed = set(self.checkpoint_data.get('completed_segments', []))
                logger.info(f"📂 加载检查点: 已完成 {len(self._completed)} 个段落")
            except Exception as e:
                logger.warning(f"⚠️ 加载检查点失败: {e}")
                self.checkpoint_data = {}
        else:
            logger.info("🆕 未发现检查点文件，将从头开始翻译")
            self.checkpoint_data = {
                'start_time': datetime.now().isoformat(),
                'completed_segments': [],
                'failed_segments': [],
                'total_segments': 0,
                'last_update': None,
                'original_filename': None,
                'translated_filename': None,
                'title_translations': {}  # {原标题: 译标题}
            }
        self._completed = set(self.checkpoint_data.get('completed_segments', []))
    
    def save_checkpoint(self):
        """保存当前检查点到文件"""
        if not self.enabled:
            return
        try:
            self.project_dir.mkdir(parents=True, exist_ok=True)
            self.checkpoint_data['last_update'] = datetime.now().isoformat()
            # 落盘前把内部 set 序列化为有序 list
            self.checkpoint_data['completed_segments'] = sorted(self._completed)
            
            # tmp+replace 原子写：checkpoint 被截断会让续传状态整体作废，
            # tmp 与目标同目录规避 os.replace 跨设备问题
            tmp_file = self.checkpoint_file.with_suffix(self.checkpoint_file.suffix + '.tmp')
            with open(tmp_file, 'w', encoding='utf-8') as f:
                json.dump(self.checkpoint_data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_file, self.checkpoint_file)
            
            completed = len(self._completed)
            total = self.checkpoint_data.get('total_segments', 0)
            logger.debug(f"💾 检查点已保存: {completed}/{total}")
        except Exception as e:
            logger.error(f"❌ 保存检查点失败: {e}")
    
    def mark_segment_completed(self, segment_id: int):
        """标记一个段落为已完成"""
        self._completed.add(segment_id)

    def remove_from_completed(self, segment_id: int):
        """从已完成列表中移除一个段落（用于重新翻译）"""
        self._completed.discard(segment_id)

    def mark_segment_failed(self, segment_id: int, error_msg: str = ""):
        """标记一个段落为失败（同段去重，保留最新错误——否则跨轮次重试会让
        checkpoint.json 的 failed_segments 无界增长）"""
        if 'failed_segments' not in self.checkpoint_data:
            self.checkpoint_data['failed_segments'] = []
        self.checkpoint_data['failed_segments'] = [
            rec for rec in self.checkpoint_data['failed_segments']
            if rec.get('segment_id') != segment_id
        ]
        self.checkpoint_data['failed_segments'].append({
            'segment_id': segment_id,
            'error': error_msg,
            'timestamp': datetime.now().isoformat()
        })
    
    def is_segment_completed(self, segment_id: int) -> bool:
        """检查段落是否已完成"""
        return segment_id in self._completed

    def get_completed_segment_ids(self) -> Set[int]:
        """获取所有已完成的段落ID"""
        return set(self._completed)
    
    def get_title_translation(self, original_title: str) -> Optional[str]:
        """获取已缓存的标题翻译"""
        return self.checkpoint_data.get('title_translations', {}).get(original_title)
    
    def save_title_translation(self, original_title: str, translated_title: str):
        """保存标题翻译到缓存"""
        if 'title_translations' not in self.checkpoint_data:
            self.checkpoint_data['title_translations'] = {}
        self.checkpoint_data['title_translations'][original_title] = translated_title
    
    def save_filenames(self, original_filename: str, translated_filename: str = None):
        """保存文件名信息"""
        self.checkpoint_data['original_filename'] = original_filename
        if translated_filename:
            self.checkpoint_data['translated_filename'] = translated_filename
    
    def get_pending_segments(self, all_segments: SegmentList) -> SegmentList:
        """获取所有未完成的段落
        
        筛选条件（满足任一即为待翻译）：
        1. segment_id 不在 completed_segments 中
        2. translated_text 为空或包含失败标记
        """
        completed_ids = self.get_completed_segment_ids()
        
        # 使用集合去重，避免重复添加
        pending_ids = set()
        pending = []
        
        for seg in all_segments:
            # 已经添加过的跳过
            if seg.segment_id in pending_ids:
                continue
            
            # 条件1: 不在已完成列表中
            not_completed = seg.segment_id not in completed_ids
            
            # 条件2: 翻译结果为空或包含失败/不完整标记（统一词表，见 core.schema.FAILED_MARKERS）
            has_failed_content = (
                not seg.translated_text or
                contains_failed_marker(seg.translated_text)
            )
            
            # 满足任一条件即为待翻译
            if not_completed or has_failed_content:
                pending.append(seg)
                pending_ids.add(seg.segment_id)
                # 如果是失败的segment但在completed列表中，需要移除
                if has_failed_content and seg.segment_id in completed_ids:
                    self.remove_from_completed(seg.segment_id)
        
        if pending:
            logger.info(f"🔄 检测到 {len(pending)} 个待翻译段落 (共 {len(all_segments)} 个)")
        else:
            logger.info(f"✅ 所有 {len(all_segments)} 个段落均已完成")
        return pending
    
    def update_total_segments(self, total: int):
        """更新总段落数"""
        self.checkpoint_data['total_segments'] = total
    
class CachePersistenceManager:
    """缓存持久化管理器 - 管理Gemini缓存与本地文件的映射关系"""
    
    def __init__(self, settings: 'Settings'):
        """
        Args:
            settings: 全局设置对象（从document_path自动计算doc_hash）
        """
        self.settings = settings
        
        # 从settings自动计算doc_hash
        from ..utils.file import get_file_hash
        doc_hash = get_file_hash(settings.files.document_path) if settings.files.document_path else "unknown"
        
        # 存储为实例属性，供后续方法使用
        self.doc_hash = doc_hash
        
        # 从 settings 中获取项目目录
        base_dir = Path(settings.files.output_base_dir) if isinstance(settings.files.output_base_dir, str) else settings.files.output_base_dir
        # `output_base_dir` 在不同调用方中可能已经指向 {doc_hash} 目录。
        # 为了避免产生 {doc_hash}/{doc_hash} 的重复嵌套，这里做一次智能归一化。
        self.project_dir = base_dir if base_dir.name == doc_hash else (base_dir / doc_hash)
        # 遵循 test_standards.md：将缓存元数据持久化到 `.cache/cache_metadata.json`
        self.cache_metadata_file = self.project_dir / ".cache" / "cache_metadata.json"
        self.cache_metadata: Dict[str, Dict[str, Any]] = {
            "system_instruction": {},
            "glossary": {},
            "context": {},
            "uploaded_files": {}
        }
        
        # ========== 线程安全机制 ==========
        # 用于保护缓存创建操作的锁（防止异步模式下的竞态条件）
        self._cache_creation_lock = threading.Lock()
        # 记录正在创建的缓存（key=content_hash, value=True）
        self._pending_cache_creation: Dict[str, bool] = {}
        # 用于等待缓存创建完成的条件变量
        self._cache_created_condition = threading.Condition(self._cache_creation_lock)
        
        self._load_metadata()
    
    def _load_metadata(self):
        """从磁盘加载缓存元数据"""
        if self.cache_metadata_file.exists():
            try:
                with open(self.cache_metadata_file, 'r', encoding='utf-8') as f:
                    loaded_data = json.load(f)
                    self.cache_metadata.update(loaded_data)
                self._cleanup_expired_caches()
                logger.info(f"✅ 已加载缓存元数据: {self.cache_metadata_file}")
            except Exception as e:
                logger.warning(f"⚠️ 加载缓存元数据失败: {e}")
    
    def _save_metadata(self):
        """保存缓存元数据到磁盘"""
        try:
            self.cache_metadata_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_metadata_file, 'w', encoding='utf-8') as f:
                json.dump(self.cache_metadata, f, indent=2, ensure_ascii=False)
            logger.debug(f"💾 缓存元数据已保存: {self.cache_metadata_file}")
        except Exception as e:
            logger.error(f"❌ 保存缓存元数据失败: {e}")
    
    def _cleanup_expired_caches(self):
        """清理过期的缓存记录"""
        current_time = time.time()
        cleaned_count = 0
        
        for cache_type in self.cache_metadata:
            if not isinstance(self.cache_metadata[cache_type], dict):
                continue
            expired_keys = [
                k for k, v in self.cache_metadata[cache_type].items()
                if current_time > v.get('expiry_time', 0)
            ]
            for key in expired_keys:
                del self.cache_metadata[cache_type][key]
                cleaned_count += 1
        
        if cleaned_count > 0:
            logger.info(f"🧹 已清理 {cleaned_count} 个过期缓存记录")
            self._save_metadata()
    
    def register_system_cache(
        self,
        cache_name: str,
        content_hash: str,
        ttl_hours: float = 1.0
    ) -> bool:
        """注册System Instruction缓存"""
        try:
            # 使用日期+doc_hash+内容hash生成缓存键
            date_str = datetime.now().strftime("%Y%m%d")
            doc_hash_short = self.doc_hash[:8] if self.doc_hash else "nodoc"
            cache_key = f"sys_{date_str}_{doc_hash_short}_{content_hash[:8]}"
            
            self.cache_metadata["system_instruction"][cache_key] = {
                "cache_name": cache_name,
                "content_hash": content_hash,
                "created_at": time.time(),
                "expiry_time": time.time() + (ttl_hours * 3600),
                "ttl_hours": ttl_hours,
                "type": "system_instruction"
            }
            self._save_metadata()
            logger.info(f"📌 已注册System Instruction缓存: {cache_key}")
            return True
        except Exception as e:
            logger.error(f"❌ 注册System缓存失败: {e}")
            return False
    
    def get_system_cache(self, content_hash: str) -> Optional[str]:
        """获取System Instruction缓存名称（通过内容hash查找）
        
        增强版：提前10分钟视为过期，主动删除过期记录
        """
        current_time = time.time()
        buffer_seconds = 600  # 10分钟缓冲时间
        
        expired_keys = []
        for cache_key, cache_info in self.cache_metadata["system_instruction"].items():
            expiry_time = cache_info.get('expiry_time', 0)
            
            if cache_info.get('content_hash') == content_hash:
                # 检查是否已过期或即将过期（提前10分钟）
                if current_time > (expiry_time - buffer_seconds):
                    logger.warning(f"⏰ 缓存 {cache_key} 已过期或即将过期，删除本地记录")
                    expired_keys.append(cache_key)
                    continue
                
                logger.debug(f"♻️  复用System缓存: {cache_key}")
                return cache_info.get('cache_name')
        
        # 清理过期记录
        if expired_keys:
            for key in expired_keys:
                del self.cache_metadata["system_instruction"][key]
            self._save_metadata()
            logger.info(f"🗑️  已删除 {len(expired_keys)} 个过期缓存记录")
        
        return None
    
    def remove_invalid_cache(self, cache_name: str) -> bool:
        """删除失效的缓存记录（用于降级处理）
        
        Args:
            cache_name: 要删除的缓存名称
            
        Returns:
            是否找到并删除了记录
        """
        removed = False
        for cache_type in ["system_instruction", "glossary", "context"]:
            keys_to_remove = [
                k for k, v in self.cache_metadata[cache_type].items()
                if v.get('cache_name') == cache_name
            ]
            for key in keys_to_remove:
                del self.cache_metadata[cache_type][key]
                removed = True
                logger.info(f"🗑️  已删除失效缓存记录: {key} ({cache_type})")
        
        if removed:
            self._save_metadata()
        return removed
    
    def get_or_create_system_cache(
        self,
        system_instruction: str,
        model_name: str,
        display_name: Optional[str] = None
    ) -> Optional[str]:
        """
        统一的System Instruction缓存获取或创建方法（线程安全版本）
        
        使用双重检查锁定模式防止异步模式下的竞态条件：
        - 第一次检查：无锁快速路径，如果缓存已存在直接返回
        - 加锁保护：确保同一时间只有一个线程创建缓存
        - 第二次检查：防止在等待锁期间其他线程已创建缓存
        - 等待机制：如果缓存正在创建中，等待完成而不是重复创建
        
        Args:
            system_instruction: 系统指令内容
            model_name: 模型名称
            display_name: 缓存显示名称（可选）
            
        Returns:
            缓存名称（cache_name），如果创建失败则返回None
        """
        # 计算内容哈希
        content_hash = self.compute_content_hash(system_instruction)
        
        # ========== 第一次检查（无锁快速路径）==========
        existing_cache = self.get_system_cache(content_hash)
        if existing_cache:
            logger.info(f"♻️  复用已有System Instruction缓存: {existing_cache[:50]}...")
            return existing_cache
        
        # ========== 加锁保护创建过程 ==========
        with self._cache_creation_lock:
            # ========== 第二次检查（防止重复创建）==========
            existing_cache = self.get_system_cache(content_hash)
            if existing_cache:
                logger.debug(f"🔒 锁内检测到缓存已创建: {existing_cache[:30]}...")
                return existing_cache
            
            # ========== 检查是否有其他线程正在创建 ==========
            if content_hash in self._pending_cache_creation:
                logger.info(f"⏳ 检测到其他 worker 正在创建缓存，等待完成...")
                # 等待缓存创建完成（最多等待 30 秒）
                wait_start = time.time()
                while content_hash in self._pending_cache_creation:
                    timeout_remaining = 30.0 - (time.time() - wait_start)
                    if timeout_remaining <= 0:
                        logger.warning("⚠️  等待缓存创建超时（30秒），继续尝试创建")
                        break
                    # 释放锁并等待通知
                    self._cache_created_condition.wait(timeout=min(1.0, timeout_remaining))
                
                # 等待完成后再次检查缓存
                existing_cache = self.get_system_cache(content_hash)
                if existing_cache:
                    logger.info(f"✅ 等待完成，缓存已就绪: {existing_cache[:30]}...")
                    return existing_cache
            
            # ========== 标记正在创建 ==========
            self._pending_cache_creation[content_hash] = True
            logger.debug(f"🔨 开始创建缓存 (hash: {content_hash[:8]}...)")
        
        # ========== 创建缓存（释放锁，允许其他线程等待）==========
        cache_name = None
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=self.settings.api.gemini_api_key)
            ttl_seconds = int(self.settings.processing.cache_ttl_hours * 3600)
            
            # 使用日期和hash生成显示名称
            if display_name is None:
                date_str = datetime.now().strftime("%Y%m%d")
                display_name = f"sys_{date_str}_{content_hash[:8]}"

            cache = client.caches.create(
                model=model_name,
                config=types.CreateCachedContentConfig(
                    display_name=display_name,
                    system_instruction=system_instruction,
                    ttl=f"{ttl_seconds}s",
                ),
            )

            cache_name = cache.name
            logger.info(f"✅ System Instruction缓存已创建: {cache_name[:50]}...")
            logger.debug(f"   缓存TTL: {self.settings.processing.cache_ttl_hours}小时")
            logger.debug(f"   缓存内容长度: {len(system_instruction):,} 字符")
            
            # ========== 立即保存元数据（加锁保护）==========
            with self._cache_creation_lock:
                self.register_system_cache(
                    cache_name=cache_name,
                    content_hash=content_hash,
                    ttl_hours=self.settings.processing.cache_ttl_hours
                )
                # 强制立即写入磁盘
                self._save_metadata()
            
            return cache_name
            
        except ImportError:
            logger.warning("⚠️  google.genai 模块不可用，跳过缓存创建")
            return None
        except Exception as e:
            logger.warning(f"⚠️  创建 System Instruction 缓存失败: {e}")
            logger.debug(f"   继续无缓存模式...")
            return None
        finally:
            # ========== 清除正在创建标记并通知等待线程 ==========
            with self._cache_creation_lock:
                self._pending_cache_creation.pop(content_hash, None)
                # 通知所有等待的线程
                self._cache_created_condition.notify_all()
                logger.debug(f"🔓 缓存创建流程结束，已通知等待线程")
    
    @staticmethod
    def compute_content_hash(content: str) -> str:
        """计算内容哈希值"""
        return hashlib.md5(content.encode('utf-8')).hexdigest()


# ========================================================================
# 3. Prompt 管理器
# ========================================================================

class PromptManager:
    """Prompt 模板管理器，在初始化时加载所有模板和配置"""
    
    def __init__(self, settings: 'Settings', force_simple: bool = False):
        """
        初始化 Prompt 管理器

        Args:
            settings: 全局设置对象，包含 translation_mode_entity
            force_simple: 强制使用简化版 prompt（claude-agent 用：完整版的
                ❌/✅ 对照表会触发 Claude API 输出内容过滤器的误报）
        """
        self.settings = settings
        self.mode_entity = settings.processing.translation_mode_entity

        # 根据translator provider选择prompt版本
        # claude/ollama 系列也算"默认云端"：主 provider 为 claude-agent 或
        # ollama 时，回退链中的 gemini/deepseek 等云端翻译器不能因此被降级到
        # 简化版 prompt——is_cloud_provider 只反映"主 provider 是否倾向本地"，
        # 具体某个 Translator 实例是否真的本地，由该实例自己通过
        # force_simple 显式声明（ClaudeAgentTranslator 用 force_simple=False
        # 强制留在完整版；OpenAICompatibleTranslator 的 ollama 分支用
        # force_simple=self.is_local 强制降级到简化版）。
        provider = getattr(settings.api, 'translator_provider', 'gemini').lower()
        is_cloud_provider = provider in {
            'deepseek', 'openai', 'openai-compatible', 'openai_compatible', 'gemini',
            'claude-agent', 'claude_agent', 'agent', 'claude', 'ollama',
        }

        if is_cloud_provider and not force_simple:
            # 云端API使用完整版本的prompt（更好的翻译质量）
            self.system_instruction_base = self._load_prompt_template("system_instruction.md")
            self.text_translation_prompt = self._load_prompt_template("text_translation_prompt.md")
            logger.info("🌐 云端API模式：使用完整版prompt（高质量翻译）")
        else:
            # 本地模型使用简化版本（节省token）
            self.system_instruction_base = self._load_prompt_template("system_instruction_simple.md")
            self.text_translation_prompt = self._load_prompt_template("text_translation_prompt_simple.md")
            logger.info("🏠 本地模式：使用简化版prompt（节省资源）")
        
        # 视觉 prompt 保持不变（JSON 修复已由结构化输出取代，不再加载 json_repair_prompt）
        self.vision_translation_prompt = self._load_prompt_template("vision_translation_prompt.md")
    
    def _load_prompt_template(self, template_name: str) -> str:
        """从文件加载 Prompt 模板"""
        path = Path(__file__).parent.parent.parent / "config" / "prompts" / template_name
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        except FileNotFoundError:
            logger.warning(f"⚠️ Prompt template not found: {path}, using fallback")
            # 中性兜底指令：不带花括号占位符（模板从不做 .format()，占位符会以
            # 字面量进 prompt 并与 user message 里的真实输入自相矛盾）
            return (
                "You are a professional translator. Translate the JSON segments "
                "provided in the user message into Chinese and return a JSON object "
                'of the form {"translations": [{"id": ..., "translation": ...}]}.'
            )
    
    def get_system_instruction(
        self, 
        use_vision: bool = False,
        include_mode: bool = False,
        include_glossary: bool = False,
        glossary_text: str = ""
    ) -> str:
        """
        获取融合了prompt模板的system instruction
        
        缓存策略：
        - 预翻译阶段：只包含 base + prompt_template（无 mode、无 glossary）
        - 正式翻译阶段：包含 base + prompt_template + mode + glossary
        
        Args:
            use_vision: 是否使用视觉模式
            include_mode: 是否包含翻译模式配置（正式翻译时为 True）
            include_glossary: 是否包含术语表（正式翻译时为 True）
            glossary_text: 格式化的术语表文本
        
        Returns:
            完整的 system instruction
        """
        parts = [self.system_instruction_base]
        
        # 添加 prompt 模板
        if use_vision:
            parts.append(f"\n\n---\n\n# TRANSLATION PROMPT TEMPLATE\n\n{self.vision_translation_prompt}\n")
        else:
            parts.append(f"\n\n---\n\n# TRANSLATION PROMPT TEMPLATE\n\n{self.text_translation_prompt}\n")
        
        # 添加翻译模式（正式翻译时）
        if include_mode and self.mode_entity:
            mode_section = f"""
---

# ACTIVE TRANSLATION MODE

**Mode Name**: {self.mode_entity.name}

**Your Role**:
{self.mode_entity.role_desc}

**Your Style & Approach**:
{self.mode_entity.style}

**CRITICAL**: You MUST follow this mode's philosophy for ALL translations.
"""
            few_shot = getattr(self.mode_entity, 'few_shot', None)
            if few_shot:
                mode_section += f"""
**Exemplar (few-shot)** — the quality bar for this mode; imitate its handling, not its content:

{few_shot}
"""
            parts.append(mode_section)
        
        # 添加术语表（正式翻译时）
        if include_glossary and glossary_text:
            glossary_section = f"""
---

# MANDATORY GLOSSARY

The following terms MUST be translated exactly as specified. These are non-negotiable:

<glossary>
{glossary_text}
</glossary>

**CRITICAL**: Always check the glossary before translating any term. If a term appears in the glossary, you MUST use the specified translation.
"""
            parts.append(glossary_section)
        
        return "".join(parts)

    @staticmethod
    def format_glossary(glossary: Optional[Dict[str, str]]) -> str:
        """术语表的唯一权威文本格式。

        统一为 `- **term**: translation`（与 text_translation_prompt.md 的输入契约
        一致）；强制语义由 MANDATORY GLOSSARY 段头承担，逐行重复 "Must be
        translated as" 是纯 token 浪费。空/None 返回空串。
        """
        if not glossary:
            return ""
        return "\n".join(f"- **{k}**: {v}" for k, v in glossary.items())

    def format_text_prompt(
        self,
        context: str,
        input_json: str,
        glossary: str = ""
    ) -> str:
        """
        格式化文本翻译的完整提示（用户消息部分）
        
        新设计：
        - glossary 和 mode 已经在 system instruction 缓存中
        - 这里只提供动态内容：context 和 input_json
        - 预翻译阶段：glossary 为空
        - 正式翻译阶段：glossary 已在 system instruction 中，这里可以不传
        
        Args:
            context: 上下文文本（前一个 batch 的翻译结果或原文）
            input_json: 输入的 JSON 数据
            glossary: 术语表文本（可选，用于非缓存模式）
        
        Returns:
            格式化的完整提示
        """
        parts = []
        
        # 添加上下文（动态内容，每次请求都不同）
        if context and context.strip():
            parts.append(f"# Context from Previous Segments\n<previous_context>\n{context}\n</previous_context>\n")
        else:
            parts.append("# Context from Previous Segments\n<previous_context>\n(Beginning of document - no previous context)\n</previous_context>\n")
        
        # 如果术语表在消息中提供（非缓存模式或预翻译阶段）
        if glossary and glossary.strip():
            parts.append(f"# Glossary Reference\n<glossary>\n{glossary}\n</glossary>\n")
        
        # 添加输入数据
        parts.append(f"# Input Data\n{input_json}")
        
        return "\n".join(parts)
    
    def format_vision_prompt(self, context: str, glossary: str = "") -> str:
        """
        格式化视觉翻译的完整提示（user message 部分）

        正式翻译阶段的 mode 与 glossary 已并入 system instruction
        （见 get_system_instruction / 各 Translator 的 _build_system_instruction），
        这里只装动态内容。

        Args:
            context: 上下文文本
            glossary: 旁路兜底用术语表文本（正式阶段调用方应传空串）

        Returns:
            格式化的完整提示
        """
        parts = []

        # 旁路兜底：仅当调用方未走正式阶段钩子却直接传术语表时注入
        if glossary and glossary.strip() and glossary.strip() != "N/A":
            parts.append(
                "# MANDATORY GLOSSARY\n<glossary>\n"
                f"{glossary}\n</glossary>\n"
                "**CRITICAL**: If any term in the image matches the glossary, "
                "you MUST use the specified translation."
            )

        # 添加上下文
        if context and context.strip():
            parts.append(f"# Context from Previous Page\n<previous_context>\n{context}\n</previous_context>")
        else:
            parts.append("# Context from Previous Page\nNo previous context.")

        return "\n".join(parts)
    
    def format_title_prompt(self, text_list: str) -> str:
        """
        格式化标题翻译提示
        
        Args:
            text_list: JSON 格式的标题列表
        
        Returns:
            格式化的标题翻译提示
        """
        # 不再自建第二个角色人设、不再整段重复注入 mode.style——角色与模式已在
        # system instruction 中（正式阶段含 mode），这里只给任务契约
        return f"""# Task: Title Translation

Translate the following list of document headers/titles into Chinese, following the active translation mode and terminology conventions from the system instruction. Titles should be concise and consistent.

Input JSON: {text_list}

Output JSON shape (task-level, takes precedence per Task Precedence): a flat JSON object where keys are the source titles and values are the translations.
Example: {{"Chapter 1": "第一章", "Index": "索引"}}

Return ONLY the JSON object."""

