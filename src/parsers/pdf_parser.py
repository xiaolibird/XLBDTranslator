"""
PDF 文档解析器
负责将 PDF 文档解析为 ContentSegment 列表
"""
import os
import csv
import fitz  # PyMuPDF
from pathlib import Path
from typing import List, Dict, Any, Iterator, Tuple

from ..core.schema import ContentSegment, Settings
from ..core.exceptions import DocumentParseError
from .tools import process_unified_toc
from ..utils.logger import get_logger
from .base import BaseDocPipeline

logger = get_logger(__name__)


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
            logger.info("No TOC found. Falling back to Page-as-Chapter mode.")
            is_fallback_mode = True
            # 每一页都作为一个 Level 1 的章节
            for i in range(len(self.doc)):
                standardized_items.append({
                    'level': 1,
                    'title': f"Page {i+1}",
                    'key': i
                })

        # =========================================================
        # 2. 统一调用核心策略
        # =========================================================

        # 获取面包屑开关 (默认开启)
        use_bc = self.settings.processing.use_breadcrumb

        # 特殊处理：如果是纯页码回退模式，强制关闭面包屑
        # 否则会变成 "Page 1 > Page 2 > Page 3..." 这种荒谬的层级
        if is_fallback_mode:
            use_bc = False

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
