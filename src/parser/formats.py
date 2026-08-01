"""
文档解析器集合
包含所有文档格式的解析器实现：BaseDocPipeline, PDFParser, EPUBParser
"""
import os
import csv
import json
import fitz  # PyMuPDF
import ebooklib
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Dict, Any, Iterator, Tuple

from ebooklib import epub
from bs4 import BeautifulSoup

from ..core.schema import ContentSegment, Settings
from ..core.exceptions import DocumentParseError
from .helpers import process_unified_toc, parse_epub_toc
from ..utils.logger import get_logger

logger = get_logger(__name__)


# ============================================================================
# 基础抽象类
# ============================================================================

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
            # 1. 检查章节变更 (必须在过滤逻辑和空文本检查之前)
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

            # 2. 视觉/图片模式处理
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
                    is_new_chapter=self.pending_new_chapter
                )
                self.all_segments.append(seg)
                self.global_id_counter += 1
                self.pending_new_chapter = False
                continue

            # 3. 纯文本模式处理
            if not content or not content.strip():
                continue

            # 4. 更新当前页码 (针对 PDF)
            if isinstance(unit_key, int):
                self.current_page_index = unit_key

            # 5. 累积文本
            self.rolling_buffer.append(content)
            self.current_buffer_length += len(content)

            # 6. 检查是否需要分块
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


# ============================================================================
# PDF 解析器
# ============================================================================

class PDFParser(BaseDocPipeline):
    """PDF 文档解析器"""

    def __init__(self, file_path: Path, cache_path: Path, settings: Settings):
        super().__init__(file_path, cache_path, settings)
        self.doc: fitz.Document = None

    def _load_metadata(self):
        """
        加载元数据并适配 process_unified_toc 架构。
        支持：CSV 自定义 -> PDF 原生 TOC -> 纯页码回退。
        """
        self.doc = fitz.open(str(self.file_path))

        # 1. 定义中间层：标准三元组列表
        # 每一项结构: {'level': int, 'title': str, 'key': int}
        standardized_items = []

        # =========================================================
        # 分支 A: 尝试加载 CSV 自定义目录 (优先级最高)
        # =========================================================
        # 修正: 从 settings.document 读取最终生效的 TOC 路径
        if self.settings.document.custom_toc_path and self.settings.document.custom_toc_path.exists():
            logger.info(f"Loading custom TOC from CSV: {self.settings.document.custom_toc_path}")
            try:
                # utf-8-sig 兼容 Excel 保存的 CSV
                with open(self.settings.document.custom_toc_path, 'r', encoding='utf-8-sig') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        # 健壮性读取：处理 CSV 列名大小写或空格
                        # 假设标准列名: Page, Title, Level (可选)
                        row_lower = {k.lower().strip(): v for k, v in row.items()}

                        page_str = row_lower.get('page') or row_lower.get('页码')
                        if not page_str: continue

                        p_idx = int(page_str) - 1 # 用户习惯 1-based, 内部逻辑 0-based

                        title = row_lower.get('title') or row_lower.get('标题') or f"Page {p_idx+1}"
                        level_str = row_lower.get('level') or row_lower.get('层级') or "1"

                        if p_idx >= 0:
                            standardized_items.append({
                                'level': int(level_str),
                                'title': title.strip(),
                                'key': p_idx
                            })
            except Exception as e:
                logger.error(f"Failed to parse CSV TOC: {e}. Falling back to native TOC.")
                standardized_items = [] # 解析失败，清空以触发回退

        # =========================================================
        # 分支 B: 尝试加载 PDF 原生 TOC (如果 CSV 为空)
        # =========================================================
        if not standardized_items:
            # fitz.get_toc() 返回: [[lvl, title, page, ...], ...]
            raw_toc = self.doc.get_toc()
            if raw_toc:
                logger.info("Loading native PDF TOC.")
                for item in raw_toc:
                    lvl = item[0]
                    title = item[1]
                    page_num = item[2]

                    p_idx = page_num - 1
                    if p_idx >= 0:
                        standardized_items.append({
                            'level': lvl,
                            'title': title,
                            'key': p_idx
                        })

        # =========================================================
        # 分支 C: 纯页码回退模式 (如果以上都为空)
        # =========================================================
        is_fallback_mode = False
        if not standardized_items:
            logger.info("No TOC found. Using page-index-only mode (no chapter structure).")
            is_fallback_mode = True
            # 纯页码回退模式：不创建任何章节结构
            # chapter_map 保持为空，所有页面都是 is_new_chapter=False
            # 页码将通过 page_index 渲染为 h6 标记（不进入章节信息）

        # =========================================================
        # 2. 统一调用核心策略
        # =========================================================

        # 纯页码回退模式：不创建 chapter_map，所有页面保持 is_new_chapter=False
        if is_fallback_mode:
            self.chapter_map = {}
            logger.info("📄 纯页码模式：所有页面将通过 page_index 渲染为 h6 标记（不作为章节）")
        else:
            # 获取面包屑开关 (默认开启)
            use_bc = self.settings.processing.use_breadcrumb

            # 调用 process_unified_toc 生成最终 Map
            # 结果格式: { 0: {"title": "...", "level": 1}, 5: {"title": "...", "level": 2} }
            self.chapter_map = process_unified_toc(standardized_items, use_breadcrumb=use_bc)

        # (可选) 保存 raw items 供 process_flow 进行预翻译使用
        self.raw_toc_entries = standardized_items

        logger.info(f"Metadata loaded. Chapter Map contains {len(self.chapter_map)} entries.")

    def _iter_content_units(self) -> Iterator[Tuple[int, str, str]]:
        """
        根据模式 Yield 内容。
        Vision 模式 -> type='image', content=path
        Text 模式 -> type='text', content=string
        """
        use_vision = self.settings.processing.use_vision_mode
        # --- Text 模式 ---
        # 根据 settings.document.page_range 进行页面切割
        actual_start_page = 0
        actual_end_page = len(self.doc) # 总页数

        if self.settings.document.page_range:
            user_start, user_end = self.settings.document.page_range
            
            # 将用户输入的 1-based 转换为 0-based 索引
            potential_start_idx = user_start - 1
            potential_end_idx = user_end # range 是 exclusive 的，所以直接用 user_end
            
            # 确保范围不超出文档实际页数
            actual_start_page = max(0, potential_start_idx)
            actual_end_page = min(len(self.doc), potential_end_idx)

            logger.info(f"📄 页面范围切割: 用户请求 {user_start}-{user_end}，实际处理 {actual_start_page + 1}-{actual_end_page}")
            if actual_start_page >= actual_end_page:
                logger.warning(f"⚠️ 设定的页面范围 {user_start}-{user_end} 无效或超出文档范围，将跳过页面解析。")
                return # 范围无效，不生成任何内容

        for i in range(actual_start_page, actual_end_page):
            page = self.doc[i]

            if use_vision:
                # --- Vision 模式 ---
                img_path = self._save_page_image(page, i)
                if img_path:
                    # yield (页码, 图片路径, 类型)
                    yield i, img_path, "image"
            else:
                # --- Text 模式 ---
                text = self._extract_text(page, i)
                # yield (页码, 文本内容, 类型)
                yield i, text, "text"

    def _save_page_image(self, page, page_idx) -> str:
        """
        以 200 DPI 渲染裁切后的页面图片。
        直接在渲染阶段根据 Settings 里的 Margin 参数移除页眉页脚。
        """
        try:
            # 1. 准备目录
            project_dir = self.cache_path.parent

            # 在项目目录下创建 images 文件夹
            image_dir = project_dir / "images"
            image_dir.mkdir(parents=True, exist_ok=True)

            # 2. 生成文件名
            filename = f"page_{page_idx + 1:04d}.jpg"
            full_path = image_dir / filename

            if full_path.exists():
                return str(full_path.resolve())

            # 4. 计算裁切区域
            clip_rect = self._get_crop_rect(page)

            # 5. 渲染图片
            zoom = 200 / 72  # 200 DPI
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, clip=clip_rect, alpha=False)

            # 6. 保存
            pix.save(str(full_path))
            return str(full_path.resolve())

        except Exception as e:
            logger.error(f"Failed to render image for page {page_idx}: {e}")
            return ""

    def _extract_text(self, page, page_idx) -> str:
        """
        提取页面文本，根据 settings 里的百分比参数进行裁切 (Clip)。
        """
        try:
            # 计算裁切区域
            clip_rect = self._get_crop_rect(page)

            # 提取文本
            text = page.get_text("text", clip=clip_rect, sort=True)
            return text.strip()

        except Exception as e:
            logger.error(f"Failed to extract text from page {page_idx}: {e}")
            return ""

    def _get_crop_rect(self, page: fitz.Page) -> fitz.Rect:
        """计算裁切矩形"""
        w = page.rect.width
        h = page.rect.height

        # 获取边距设置
        m_top = self.settings.document.margin_top or 0.0
        m_bottom = self.settings.document.margin_bottom or 0.0
        m_left = self.settings.document.margin_left or 0.0
        m_right = self.settings.document.margin_right or 0.0

        # 计算坐标
        x0 = w * m_left
        y0 = h * m_top
        x1 = w * (1.0 - m_right)
        y1 = h * (1.0 - m_bottom)

        # 安全检查
        if x0 >= x1 or y0 >= y1:
            return page.rect  # 返回全页

        return fitz.Rect(x0, y0, x1, y1)


# ============================================================================
# EPUB 解析器
# ============================================================================

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

        # 2. 尝试从 NCX/NAV 获取目录
        # 使用 helpers 中的统一解析器 (带 unquote 和路径规整)
        standardized_items = parse_epub_toc(self.book.toc)

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

        # 4. 统一处理核心策略
        use_bc = self.settings.processing.use_breadcrumb
        self.chapter_map = process_unified_toc(standardized_items, use_breadcrumb=use_bc)
        logger.info(f"✅ Metadata loaded. Chapter Map size: {len(self.chapter_map)}")

    def _iter_content_units(self):
        """按照 EPUB Spine 遍历，并解析 HTML 块级元素"""
        BLOCK_TAGS = ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'blockquote', 'pre']

        # 统计解析失败的 spine item，迭代结束后汇总告警一条，避免逐条日志被淹没
        failed_item_count = 0

        for item_id, linear in self.book.spine:
            item = self.book.get_item_with_id(item_id)

            if not item: continue
            if item.get_type() != ebooklib.ITEM_DOCUMENT: continue

            try:
                # 1. 解析 HTML
                raw_content = item.get_content()
                # 防御：部分 EPUB 中 item 存在但 content 为 None（EPUBRenderer 侧已证实），
                # 显式抛出以走下方统一的占位通道，而不是让 BeautifulSoup 行为不定
                if raw_content is None:
                    raise ValueError(f"{item_id} content is None")
                soup = BeautifulSoup(raw_content, 'html.parser')

                # 获取文件名作为 Key
                unit_key = item.get_name()

                # 2. 找到 Body
                root = soup.find('body') or soup

                # 3. 遍历所有块级元素 (+ 处理图片占位以触发 TOC 更新)
                tags = root.find_all(BLOCK_TAGS + ['img'])
                
                # 即使没有标签，也 yield 一个空行以维护 unit_key 的流转
                if not tags:
                    yield unit_key, "", "text"
                    continue

                for tag in tags:
                    if tag.name == 'img':
                        # 尝试提取 alt 作为可翻译文本，否则 yield 空信号
                        alt_text = tag.get('alt', '').strip()
                        if alt_text:
                            yield unit_key, f"[插图: {alt_text}]", "text"
                        else:
                            yield unit_key, "", "text"
                    else:
                        # 4. 提取纯文本
                        text = tag.get_text(separator=' ', strip=True)
                        # 注意：BaseDocPipeline 会过滤掉纯空格/空字符串
                        # 但 yield 它们可以确保 chap_info 被 check 到
                        yield unit_key, text, "text"

            except Exception as e:
                logger.error(f"Failed to parse HTML structure for {item_id}: {e}")
                failed_item_count += 1
                # 不再静默丢章：注入一个显式占位段，让失败在 structure_map 及
                # markdown/PDF 成品的对应章节位置可见（EPUB 渲染器是原文替换式，
                # 占位文本不会出现在 EPUB 成品里，可见性靠 structure_map/日志/质检层）。
                # unit_key 必须用真实键 item.get_name()（文件名）而非 item_id：
                # BaseDocPipeline.run() 用 unit_key 查 chapter_map，其键正是
                # get_name()——用合成键必然 miss，章节切换与 _flush_buffer 不触发，
                # 占位文本会被错误并入前一章的 rolling buffer。
                # 失败标记只放在文本内容里，不污染 key。
                yield item.get_name(), (
                    f"[EPUB 章节解析失败 {item_id}: {e}] 本章原文未能提取，请检查源文件。"
                ), "text"

        if failed_item_count > 0:
            logger.error(f"⚠️ {failed_item_count} 个章节解析失败，占位段已注入")
