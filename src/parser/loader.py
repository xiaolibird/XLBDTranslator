"""
文档加载器 (DocumentLoader)
统一入口，负责根据文件类型选择合适的解析器并管理缓存
"""

import json
from pathlib import Path

from ..core.exceptions import DocumentFormatError
from ..core.schema import ContentSegment, SegmentList, Settings
from ..utils.logger import get_logger
from .formats import EPUBParser, PDFParser

logger = get_logger(__name__)


class DocumentLoader:
    """文档加载器 - 工厂模式入口"""

    def __init__(self, settings: Settings):
        self.settings = settings

    def load_document(self, file_path: Path, cache_path: Path) -> SegmentList:
        """
        加载文档的主入口

        Args:
            file_path: 文档文件路径
            cache_path: 缓存文件路径

        Returns:
            ContentSegment 列表
        """
        ext = file_path.suffix.lower()

        # 检查缓存
        if self.settings.processing.enable_cache and cache_path.exists():
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
                    segments = [ContentSegment(**item) for item in raw_data]
                logger.info(f"✅ Loaded {len(segments)} segments from cache.")
                return segments
            except Exception as e:
                logger.warning(f"⚠️ Cache file corrupted: {e}. Will re-parse document.")

        # 根据文件类型选择解析器
        if ext == ".pdf":
            parser = PDFParser(file_path, cache_path, self.settings)
        elif ext == ".epub":
            parser = EPUBParser(file_path, cache_path, self.settings)
        else:
            raise DocumentFormatError(f"Unsupported file format: {ext}")

        # 解析文档
        segments = parser.run()

        # 保存缓存
        self._save_cache(cache_path, segments)

        return segments

    def _save_cache(self, cache_path: Path, segments: SegmentList):
        """保存解析结果到缓存"""
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            data = [seg.model_dump() for seg in segments]
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info(f"💾 Cache saved: {len(segments)} segments.")
        except Exception as e:
            logger.error(f"⚠️ Failed to save cache: {e}")


# 便捷函数
def load_document_structure(
    file_path: Path, cache_path: Path, settings: Settings
) -> SegmentList:
    """
    加载文档结构的便捷函数

    Args:
        file_path: 文档路径
        cache_path: 缓存路径
        settings: 设置

    Returns:
        ContentSegment 列表
    """
    loader = DocumentLoader(settings)
    return loader.load_document(file_path, cache_path)
