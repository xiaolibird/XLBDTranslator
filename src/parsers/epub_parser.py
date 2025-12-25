"""
EPUB 文档解析器
负责将 EPUB 文档解析为 ContentSegment 列表
"""
import os
import json
from pathlib import Path
from typing import List, Dict, Any, Iterator, Tuple
from abc import ABC, abstractmethod

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup

from ..core.schema import ContentSegment, Settings
from ..core.exceptions import DocumentParseError
from .tools import process_unified_toc, extract_text_from_html
from ..utils.logger import get_logger
from .base import BaseDocPipeline

logger = get_logger(__name__)


class EPUBParser(BaseDocPipeline):
    """EPUB 文档解析器"""

    def __init__(self, file_path: Path, cache_path: Path, settings: Settings):
        super().__init__(file_path, cache_path, settings)
        self.book: epub.EpubBook = None

    def _load_metadata(self):
        """解析 EPUB 元数据和目录结构"""
        logger.info("Parsing EPUB metadata.")
        # 1. 读取 EPUB
        self.book = epub.read_epub(str(self.file_path))

        # 2. 尝试从 NCX/NAV 获取目录 (Flatten)
        standardized_items = self._flatten_epub_to_standard(self.book.toc)

        # 3. 兜底逻辑：如果目录为空，使用 Spine
        if not standardized_items:
            logger.warning("⚠️ EPUB TOC is empty. Falling back to Spine (linear reading order).")

            for item_id, linear in self.book.spine:
                item = self.book.get_item_with_id(item_id)

                if item:
                    if item.get_type() != ebooklib.ITEM_DOCUMENT:
                        continue

                    if 'nav' in (item.get_name() or "").lower():
                        continue

                    file_name = item.get_name()
                    standardized_items.append({
                        'level': 1,
                        'title': f"Section: {file_name}",
                        'key': file_name
                    })

        # 4. 统一处理
        use_bc = self.settings.processing.use_breadcrumb
        self.chapter_map = process_unified_toc(standardized_items, use_breadcrumb=use_bc)
        logger.info(f"✅ Metadata loaded. Chapter Map size: {len(self.chapter_map)}")

    def _iter_content_units(self):
        """按照 EPUB Spine 遍历，并解析 HTML 块级元素"""
        BLOCK_TAGS = ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'blockquote', 'pre']

        for item_id, linear in self.book.spine:
            item = self.book.get_item_with_id(item_id)

            if not item: continue
            if item.get_type() != ebooklib.ITEM_DOCUMENT: continue

            try:
                # 1. 解析 HTML
                raw_content = item.get_content()
                soup = BeautifulSoup(raw_content, 'html.parser')

                # 获取文件名作为 Key
                unit_key = item.get_name()

                # 2. 找到 Body
                root = soup.find('body') or soup

                # 3. 遍历所有块级元素
                for tag in root.find_all(BLOCK_TAGS):
                    # 4. 提取纯文本
                    text = tag.get_text(separator=' ', strip=True)

                    # 5. 过滤掉空标签
                    if not text:
                        continue

                    # 6. Yield 单个段落
                    yield unit_key, text, "text"

            except Exception as e:
                logger.error(f"Failed to parse HTML structure for {item_id}: {e}")
                continue

    def _flatten_epub_to_standard(self, toc, level=1):
        """解析 EPUB 目录结构"""
        items = []
        for node in toc:
            # 兼容 ebooklib 的两种节点格式
            entry = node[0] if isinstance(node, (list, tuple)) else node
            children = node[1] if isinstance(node, (list, tuple)) and len(node) > 1 else []

            if hasattr(entry, 'href') and entry.href:
                items.append({
                    'level': level,
                    'title': entry.title or "Untitled",
                    'key': entry.href.split('#')[0]
                })

            if children:
                items.extend(self._flatten_epub_to_standard(children, level + 1))
        return items
