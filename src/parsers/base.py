import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Dict, Any, Iterator, Tuple

from ..core.schema import ContentSegment, Settings
from ..utils.logger import get_logger

logger = get_logger(__name__)


class BaseDocPipeline(ABC):
    """
    文档处理流水线的抽象基类。
    负责将文档流转换为 List[ContentSegment] 对象流。
    """
    def __init__(self, file_path: Path, cache_path: Path, settings: Settings):
        self.file_path = file_path
        self.cache_path = cache_path
        self.settings = settings

        # 结果容器
        self.all_segments: List[ContentSegment] = []
        self.global_id_counter: int = 0

        # 文本缓冲区
        self.rolling_buffer: List[str] = []
        self.current_buffer_length: int = 0

        # 上下文状态
        self.current_chapter_title: str = "前言/未命名章节"
        self.current_page_index: int = 0
        self.pending_new_chapter: bool = False
        self.current_toc_level: int = 1
        # 章节映射表 {UnitKey: ChapterTitle}
        self.chapter_map: Dict[Any, Dict[str, Any]] = {}

    def run(self) -> List[ContentSegment]:
        """主流程：迭代单元 -> 维护状态 -> 生成对象"""
        logger.info(f"Starting pipeline '{self.__class__.__name__}' for {self.file_path.name}")

        self._load_metadata()

        # 遍历内容单元 (UnitKey 通常是 页码 或 文件名)
        for unit_key, content, content_type in self._iter_content_units():

            # A. 视觉/图片模式处理
            if content_type == "image":
                self._flush_buffer()

                seg = ContentSegment(
                    segment_id=self.global_id_counter,
                    original_text="",
                    content_type="image",
                    image_path=content,
                    page_index=unit_key if isinstance(unit_key, int) else 0,
                    chapter_title=self.current_chapter_title,
                    toc_level=self.current_toc_level,
                    is_new_chapter=False
                )
                self.all_segments.append(seg)
                self.global_id_counter += 1
                continue

            # B. 纯文本模式处理
            if not content or not content.strip():
                continue

            # 1. 检查章节变更
            chap_info = self.chapter_map.get(unit_key)

            if chap_info:
                new_title = chap_info.get("title", "Untitled")
                new_level = chap_info.get("level", 1)

                if new_title != self.current_chapter_title:
                    self._flush_buffer()
                    self.current_chapter_title = new_title
                    self.current_toc_level = new_level
                    logger.debug(f"New chapter detected: {new_title}")
                    self.pending_new_chapter = True

            # 2. 更新当前页码 (针对 PDF)
            if isinstance(unit_key, int):
                self.current_page_index = unit_key

            # 3. 累积文本
            self.rolling_buffer.append(content)
            self.current_buffer_length += len(content)

            # 4. 检查是否需要分块
            if self.current_buffer_length >= self.settings.processing.max_chunk_size:
                self._flush_buffer()

        # 处理剩余内容
        self._flush_buffer()

        self._save_cache()
        logger.info(f"Pipeline finished. Generated {len(self.all_segments)} segments.")
        return self.all_segments

    def _flush_buffer(self):
        """将当前缓冲区打包成一个 Segment"""
        if not self.rolling_buffer:
            return

        full_text = "\n\n".join(self.rolling_buffer)

        seg = ContentSegment(
            segment_id=self.global_id_counter,
            original_text=full_text,
            content_type="text",
            chapter_title=self.current_chapter_title,
            toc_level=self.current_toc_level,
            is_new_chapter=self.pending_new_chapter,
            page_index=self.current_page_index
        )

        self.all_segments.append(seg)
        self.global_id_counter += 1

        # 重置状态
        self.rolling_buffer = []
        self.current_buffer_length = 0
        self.pending_new_chapter = False

    def _save_cache(self):
        """保存为 JSON"""
        try:
            data = [seg.model_dump() for seg in self.all_segments]
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save cache: {e}")

    @abstractmethod
    def _load_metadata(self):
        pass

    @abstractmethod
    def _iter_content_units(self) -> Iterator[Tuple[Any, str, str]]:
        """
        Yields: (unit_key, content, content_type)
        content_type: 'text' | 'image'
        """
        pass
