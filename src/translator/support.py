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
        
        # 记录checkpoint文件路径，便于排查
        logger.info(f"📍 Checkpoint文件路径: {self.checkpoint_file.absolute()}")
        
        self._load_checkpoint()
    
    def _load_checkpoint(self):
        """加载现有的检查点文件"""
        if self.checkpoint_file.exists():
            try:
                with open(self.checkpoint_file, 'r', encoding='utf-8') as f:
                    self.checkpoint_data = json.load(f)
                completed_count = len(self.checkpoint_data.get('completed_segments', []))
                logger.info(f"📂 加载检查点: 已完成 {completed_count} 个段落")
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
    
    def save_checkpoint(self):
        """保存当前检查点到文件"""
        try:
            self.project_dir.mkdir(parents=True, exist_ok=True)
            self.checkpoint_data['last_update'] = datetime.now().isoformat()
            
            # tmp+replace 原子写：checkpoint 被截断会让续传状态整体作废，
            # tmp 与目标同目录规避 os.replace 跨设备问题
            tmp_file = self.checkpoint_file.with_suffix(self.checkpoint_file.suffix + '.tmp')
            with open(tmp_file, 'w', encoding='utf-8') as f:
                json.dump(self.checkpoint_data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_file, self.checkpoint_file)
            
            completed = len(self.checkpoint_data.get('completed_segments', []))
            total = self.checkpoint_data.get('total_segments', 0)
            logger.debug(f"💾 检查点已保存: {completed}/{total}")
        except Exception as e:
            logger.error(f"❌ 保存检查点失败: {e}")
    
    def mark_segment_completed(self, segment_id: int):
        """标记一个段落为已完成"""
        if 'completed_segments' not in self.checkpoint_data:
            self.checkpoint_data['completed_segments'] = []
        if segment_id not in self.checkpoint_data['completed_segments']:
            self.checkpoint_data['completed_segments'].append(segment_id)
    
    def remove_from_completed(self, segment_id: int):
        """从已完成列表中移除一个段落（用于重新翻译）"""
        if 'completed_segments' in self.checkpoint_data:
            if segment_id in self.checkpoint_data['completed_segments']:
                self.checkpoint_data['completed_segments'].remove(segment_id)

    def mark_segment_failed(self, segment_id: int, error_msg: str = ""):
        """标记一个段落为失败"""
        if 'failed_segments' not in self.checkpoint_data:
            self.checkpoint_data['failed_segments'] = []
        self.checkpoint_data['failed_segments'].append({
            'segment_id': segment_id,
            'error': error_msg,
            'timestamp': datetime.now().isoformat()
        })
    
    def is_segment_completed(self, segment_id: int) -> bool:
        """检查段落是否已完成"""
        return segment_id in self.checkpoint_data.get('completed_segments', [])
    
    def get_completed_segment_ids(self) -> Set[int]:
        """获取所有已完成的段落ID"""
        return set(self.checkpoint_data.get('completed_segments', []))
    
    def get_title_translation(self, original_title: str) -> Optional[str]:
        """获取已缓存的标题翻译"""
        return self.checkpoint_data.get('title_translations', {}).get(original_title)
    
    def save_title_translation(self, original_title: str, translated_title: str):
        """保存标题翻译到缓存"""
        if 'title_translations' not in self.checkpoint_data:
            self.checkpoint_data['title_translations'] = {}
        self.checkpoint_data['title_translations'][original_title] = translated_title
    
    def get_filenames(self) -> Tuple[Optional[str], Optional[str]]:
        """获取原文件名和翻译后文件名"""
        return (
            self.checkpoint_data.get('original_filename'),
            self.checkpoint_data.get('translated_filename')
        )
    
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
    
    def get_progress_stats(self) -> Dict:
        """获取进度统计信息"""
        completed = len(self.checkpoint_data.get('completed_segments', []))
        failed = len(self.checkpoint_data.get('failed_segments', []))
        total = self.checkpoint_data.get('total_segments', 0)
        progress_pct = (completed / total * 100) if total > 0 else 0
        
        return {
            'completed': completed,
            'failed': failed,
            'total': total,
            'pending': total - completed,
            'progress_percentage': progress_pct,
            'start_time': self.checkpoint_data.get('start_time'),
            'last_update': self.checkpoint_data.get('last_update')
        }
    
    def reset_checkpoint(self):
        """重置检查点（重新开始翻译）"""
        logger.warning("🗑️  重置检查点，将从头开始翻译")
        self.checkpoint_data = {
            'start_time': datetime.now().isoformat(),
            'completed_segments': [],
            'failed_segments': [],
            'total_segments': 0,
            'last_update': None
        }
        if self.checkpoint_file.exists():
            self.checkpoint_file.unlink()


# ========================================================================
# 2. 缓存持久化管理
# ========================================================================

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
    
    def register_glossary_cache(
        self,
        cache_name: str,
        glossary_hash: str,
        term_count: int,
        ttl_hours: float = 2.0
    ) -> bool:
        """注册术语表缓存"""
        try:
            # 使用日期+doc_hash+术语表hash生成缓存键
            date_str = datetime.now().strftime("%Y%m%d")
            doc_hash_short = self.doc_hash[:8] if self.doc_hash else "nodoc"
            cache_key = f"glo_{date_str}_{doc_hash_short}_{glossary_hash[:8]}"
            
            self.cache_metadata["glossary"][cache_key] = {
                "cache_name": cache_name,
                "glossary_hash": glossary_hash,
                "term_count": term_count,
                "created_at": time.time(),
                "expiry_time": time.time() + (ttl_hours * 3600),
                "ttl_hours": ttl_hours,
                "type": "glossary"
            }
            self._save_metadata()
            logger.info(f"📌 已注册术语表缓存: {cache_key} ({term_count}项)")
            return True
        except Exception as e:
            logger.error(f"❌ 注册术语表缓存失败: {e}")
            return False
    
    def register_context_cache(
        self,
        cache_name: str,
        context_hash: str,
        segment_range: str,
        ttl_hours: float = 1.0
    ) -> bool:
        """注册上下文缓存"""
        try:
            cache_key = f"context_{segment_range}_{context_hash[:8]}"
            self.cache_metadata["context"][cache_key] = {
                "cache_name": cache_name,
                "context_hash": context_hash,
                "segment_range": segment_range,
                "created_at": time.time(),
                "expiry_time": time.time() + (ttl_hours * 3600),
                "ttl_hours": ttl_hours,
                "type": "context"
            }
            self._save_metadata()
            logger.info(f"📌 已注册上下文缓存: {cache_key}")
            return True
        except Exception as e:
            logger.error(f"❌ 注册上下文缓存失败: {e}")
            return False
    
    def register_uploaded_file(
        self,
        file_path: str,
        file_uri: str,
        file_hash: str,
        mime_type: str = "image/jpeg"
    ) -> bool:
        """注册已上传文件（Gemini Developer API专用）"""
        try:
            cache_key = f"file_{file_hash[:12]}"
            self.cache_metadata["uploaded_files"][cache_key] = {
                "file_path": file_path,
                "file_uri": file_uri,
                "file_hash": file_hash,
                "mime_type": mime_type,
                "uploaded_at": time.time(),
                "type": "uploaded_file"
            }
            self._save_metadata()
            logger.debug(f"📌 已注册上传文件: {Path(file_path).name}")
            return True
        except Exception as e:
            logger.error(f"❌ 注册上传文件失败: {e}")
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
    
    def get_glossary_cache(self, glossary_hash: str) -> Optional[str]:
        """获取术语表缓存名称（增强版：提前过期检查）"""
        cache_key = f"glossary_{glossary_hash[:8]}"
        cache_info = self.cache_metadata["glossary"].get(cache_key)
        
        if cache_info:
            current_time = time.time()
            expiry_time = cache_info.get('expiry_time', 0)
            buffer_seconds = 600  # 10分钟缓冲
            
            # 检查是否已过期或即将过期
            if current_time > (expiry_time - buffer_seconds):
                logger.warning(f"⏰ 术语表缓存 {cache_key} 已过期，删除记录")
                del self.cache_metadata["glossary"][cache_key]
                self._save_metadata()
                return None
            
            logger.debug(f"♻️  复用术语表缓存: {cache_key}")
            return cache_info.get('cache_name')
        return None
    
    def get_context_cache(self, context_hash: str) -> Optional[str]:
        """获取上下文缓存名称（增强版：提前过期检查）"""
        current_time = time.time()
        buffer_seconds = 600  # 10分钟缓冲
        expired_keys = []
        
        for cache_key, cache_info in self.cache_metadata["context"].items():
            expiry_time = cache_info.get('expiry_time', 0)
            
            if cache_info.get('context_hash') == context_hash:
                # 检查是否已过期或即将过期
                if current_time > (expiry_time - buffer_seconds):
                    logger.warning(f"⏰ 上下文缓存 {cache_key} 已过期，删除记录")
                    expired_keys.append(cache_key)
                    continue
                
                logger.debug(f"♻️  复用上下文缓存: {cache_key}")
                return cache_info.get('cache_name')
        
        # 清理过期记录
        if expired_keys:
            for key in expired_keys:
                del self.cache_metadata["context"][key]
            self._save_metadata()
        
        return None
    
    def get_uploaded_file_uri(self, file_hash: str) -> Optional[str]:
        """获取已上传文件的URI"""
        cache_key = f"file_{file_hash[:12]}"
        cache_info = self.cache_metadata["uploaded_files"].get(cache_key)
        if cache_info:
            logger.debug(f"♻️  复用上传文件: {cache_key}")
            return cache_info.get('file_uri')
        return None
    
    def list_all_caches(self) -> Dict[str, List[Dict[str, Any]]]:
        """列出所有缓存"""
        result = {}
        current_time = time.time()
        
        for cache_type, caches in self.cache_metadata.items():
            if not isinstance(caches, dict):
                continue
            active_caches = []
            for cache_key, cache_info in caches.items():
                expiry_time = cache_info.get('expiry_time', 0)
                is_expired = current_time > expiry_time
                cache_info_copy = cache_info.copy()
                cache_info_copy['key'] = cache_key
                cache_info_copy['is_expired'] = is_expired
                if not is_expired or cache_type == "uploaded_files":
                    active_caches.append(cache_info_copy)
            result[cache_type] = active_caches
        return result
    
    def get_statistics(self) -> Dict[str, Any]:
        """获取缓存统计信息"""
        stats = {
            "total_caches": 0,
            "active_caches": 0,
            "expired_caches": 0,
            "by_type": {}
        }
        current_time = time.time()
        
        for cache_type, caches in self.cache_metadata.items():
            if not isinstance(caches, dict):
                continue
            total = len(caches)
            active = sum(1 for c in caches.values() 
                        if time.time() < c.get('expiry_time', float('inf')))
            expired = total - active
            stats["by_type"][cache_type] = {
                "total": total,
                "active": active,
                "expired": expired
            }
            stats["total_caches"] += total
            stats["active_caches"] += active
            stats["expired_caches"] += expired
        return stats
    
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
    
    def clear_all_caches(self):
        """清除所有缓存记录"""
        self.cache_metadata = {
            "system_instruction": {},
            "glossary": {},
            "context": {},
            "uploaded_files": {}
        }
        self._save_metadata()
        logger.info("🧹 已清除所有缓存记录")
    
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
    
    def get_or_create_glossary_cache(
        self,
        glossary: Dict[str, str],
        model_name: str
    ) -> Optional[str]:
        """
        统一的术语表缓存获取或创建方法
        
        Args:
            glossary: 术语表字典
            model_name: 模型名称
            
        Returns:
            缓存名称（cache_name），如果创建失败则返回None
        """
        if not glossary:
            return None
        
        # 计算术语表哈希
        glossary_text = json.dumps(glossary, ensure_ascii=False, sort_keys=True)
        glossary_hash = self.compute_content_hash(glossary_text)
        
        # 检查是否已有可复用的缓存
        existing_cache = self.get_glossary_cache(glossary_hash)
        if existing_cache:
            logger.info(f"♻️  复用已有术语表缓存: {existing_cache[:50]}...")
            return existing_cache
        
        # 创建新缓存
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=self.settings.api.gemini_api_key)
            ttl_seconds = int(self.settings.processing.cache_ttl_hours * 2 * 3600)
            
            # 格式化术语表内容
            glossary_content = "\n".join([
                f"- **{k}**: Must be translated as **{v}**" 
                for k, v in glossary.items()
            ])

            cache = client.caches.create(
                model=model_name,
                config=types.CreateCachedContentConfig(
                    display_name=f"glossary_{glossary_hash[:8]}",
                    contents=[
                        types.Content(
                            role="user",
                            parts=[types.Part.from_text(text=glossary_content)],
                        )
                    ],
                    ttl=f"{ttl_seconds}s",
                ),
            )

            cache_name = cache.name
            logger.info(f"✅ 术语表缓存已创建: {cache_name[:50]}... ({len(glossary)}项)")
            
            # 注册到持久化管理器
            self.register_glossary_cache(
                cache_name=cache_name,
                glossary_hash=glossary_hash,
                term_count=len(glossary),
                ttl_hours=self.settings.processing.cache_ttl_hours * 2
            )
            
            return cache_name
            
        except ImportError:
            logger.warning("⚠️  google.genai 模块不可用，跳过术语表缓存创建")
            return None
        except Exception as e:
            logger.warning(f"⚠️  创建术语表缓存失败: {e}")
            return None
    
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
        # claude 系列也算云端：主 provider 为 claude-agent 时，回退链中的
        # gemini/deepseek 翻译器不能因此被降级到简化版 prompt
        # （ClaudeAgentTranslator 自身通过 force_simple=True 显式选简化版）
        provider = getattr(settings.api, 'translator_provider', 'gemini').lower()
        is_cloud_provider = provider in {
            'deepseek', 'openai', 'openai-compatible', 'openai_compatible', 'gemini',
            'claude-agent', 'claude_agent', 'agent', 'claude',
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
            return "Translate the following text: {input_json}"
    
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

    def get_mode_prefix(self) -> str:
        """获取 Mode 配置作为 User message 的前缀（文本路径已并入 system instruction，
        视觉路径仍在 user message 中拼接此前缀）。"""
        if not self.mode_entity:
            return ""

        role_desc = self.mode_entity.role_desc
        style = self.mode_entity.style
        mode_name = self.mode_entity.name

        return f"""{'='*80}
⚠️ ACTIVE TRANSLATION MODE: {mode_name}
{'='*80}

Your Role:
{role_desc}

Your Style & Approach:
{style}

**CRITICAL**: Follow THIS mode's philosophy for the translation below.
{'='*80}

"""

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
        style = self.mode_entity.style if self.mode_entity else "Professional and accurate"
        
        return f"""You are a professional translator. Translate the following list of document headers/titles into Chinese.

Your style: {style}

Input JSON: {text_list}

**You MUST OBEY THE FOLLOWING RULE!!!!!!**
Output JSON format: A flat JSON Dictionary where keys are the source text and values are the translation.
Example: {{"Chapter 1": "第一章", "Index": "索引"}}

Return ONLY the JSON object."""

