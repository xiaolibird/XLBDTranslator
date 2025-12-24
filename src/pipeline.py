import os
import json
import re
from abc import ABC, abstractmethod
import csv
import logging
from typing import List, Dict, Any, Optional, Iterator, Tuple, Literal
from dataclasses import dataclass, asdict

# 第三方库
import fitz  # PyMuPDF
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup

# 本地模块
from .file_io import extract_text_from_epub_item
from .pdf_analyzer import detect_pdf_type
from .config import Settings
from .errors import DocumentParseError, FileSystemError

# ==============================================================================
# 0. 全局资源初始化
# ==============================================================================

logger = logging.getLogger(__name__)


# =====================================================
# 1. 文本段落数据结构定义
#    放在最上面，因为后面的函数都要用到它
# =====================================================
@dataclass
class ContentSegment:
    segment_id: int
    original_text: str
    translated_text: str = ""
    
    # --- 结构元数据 (关键修改) ---
    is_new_chapter: bool = False
    chapter_title: str = ""
    page_index: int = 0  # 对于 EPUB 可以是 0 或特定逻辑
    toc_level: int = 1 # TOC 层级 (1=H2, 2=H3, ...)，默认 1
    
    # --- 内容类型 ---
    content_type: Literal["text", "image"] = "text"
    image_path: Optional[str] = None

# =====================================================
# 2. 渲染器逻辑 
# =====================================================
class MarkdownRenderer:
    def __init__(self, settings: Settings):
        self.settings = settings
        
        # --- 样式模板 ---
        # 页面分隔符 (只在非章节开头显示)
        self.page_fmt = "\n\n###### --- 原文第 {page} 页 --- \n\n"
        
        # 图片格式
        self.image_fmt = "\n\n![Segment {id}]({path})"
        self.image_caption_fmt = "\n> 💡 **图注/内容译文**\n> {caption}"
        self.image_footer_fmt = "\n\n🔖 **Segment {id}** (Image)\n"

    def render_segment(self, seg: ContentSegment) -> str:
        """
        渲染单个 Segment 为 Markdown 字符串。
        
        Args:
            seg: 内容片段对象
        """
        parts = []

        # 1. 确定是否双语对照 (参数优先级 > 设置优先级)
        # 使用 getattr 提供默认值 False，防止 settings 没配报错
        
        retain_original = getattr(self.settings, 'retain_original', False)

        # =========================================================
        # A. 结构层：动态章节标题 (Dynamic Header Rendering)
        # =========================================================
        # 优先级最高：如果是新章节，显示 H1-H6 标题
        if seg.is_new_chapter and seg.chapter_title:
            # 1. 获取层级，默认为 1
            level = getattr(seg, 'toc_level', 1)
            
            # 2. 计算 Markdown 的 # 数量 (限制在 1-6 之间)
            # Level 1 -> ## (2) 因为通常 H1 是书名
            hash_count = min(level + 1, 6) 
            hashes = "#" * hash_count
            
            parts.append(f"\n\n{hashes} 📖 {seg.chapter_title}\n\n")
        
        # 结构层：页码标记 (Page Marker)
        # 仅在: 1.确实翻页了 2.不是新章节 3.全局配置允许显示 时才渲染
        elif seg.page_index is not None and not seg.is_new_chapter:
            show_marker = getattr(self.settings, 'render_page_markers', True)
            
            # 针对 Vision 模式的特殊优化：如果是图片，且没强制要求，可以不显示页码
            # (可选逻辑：如果你希望 Vision 模式也不显示页码，可以在这里加判断)
            # if seg.content_type == "image": show_marker = False

            if show_marker:
                parts.append(self.page_fmt.format(page=seg.page_index + 1))

        # =========================================================
        # B. 内容层：图片处理
        # =========================================================
        if seg.content_type == "image" and seg.image_path:
            parts.append(self.image_fmt.format(id=seg.segment_id, path=seg.image_path))
            
            if seg.translated_text:
                clean_trans = seg.translated_text.replace('\\n', '\n').strip()
                parts.append(self.image_caption_fmt.format(caption=clean_trans))
            
            parts.append(self.image_footer_fmt.format(id=seg.segment_id))
            parts.append("---")
            return "".join(parts)

        # =========================================================
        # C. 内容层：纯文本处理
        # =========================================================
        trans_text = (seg.translated_text or "").replace('\\n', '\n').strip()
        original_text = (seg.original_text or "").replace('\r', '').strip()
        
        # 段落 ID 标记
        parts.append(f"\n\n🔖 **Segment {seg.segment_id}**\n")
        
        # 辅助清洗函数
        clean_split = lambda t: [l.rstrip('\\').strip() for l in t.split('\n')]

        if retain_original:
            # --- 双语对照模式 ---
            orig_paras = [p for p in original_text.split('\n\n') if p.strip()]
            trans_paras = [p for p in trans_text.split('\n\n') if p.strip()]
            
            # 逐段对照渲染
            for i in range(max(len(orig_paras), len(trans_paras))):
                block = []
                p_orig = clean_split(orig_paras[i]) if i < len(orig_paras) else []
                p_trans = clean_split(trans_paras[i]) if i < len(trans_paras) else []

                if p_orig:
                    # 原文稍微缩进一点，或者加粗，看个人喜好
                    block.append(f"原文：{p_orig[0]}")
                    block.extend([f"      {line}" for line in p_orig[1:]])
                
                if p_trans:
                    for j, line in enumerate(p_trans):
                        # 处理 LLM 可能输出的 Markdown 标题，保持格式
                        if line.startswith('#'): 
                            block.append(f"\n{line}\n")
                        elif j == 0: 
                            block.append(f"> 译文：{line}")
                        else: 
                            block.append(f">       {line}")
                
                if block: parts.append("\n".join(block) + "\n")
        else:
            # --- 纯译文模式 ---
            lines = clean_split(trans_text)
            formatted = []
            for line in lines:
                if line.startswith('#'): 
                    formatted.append(f"\n{line}\n")
                else: 
                    # 纯译文模式统一加引用块，或者直接输出看你喜好
                    formatted.append(f"> {line}" if line else ">")
            parts.append("\n".join(formatted))

        parts.append("\n\n---")
        return "".join(parts)
# ==============================================================================
# 3. 基类：定义流水线骨架
# ==============================================================================

class BaseDocPipeline(ABC):
    """
    文档处理流水线的抽象基类。
    负责将文档流转换为 List[ContentSegment] 对象流。
    """
    def __init__(self, file_path: str, cache_path: str, settings: Any):
        self.file_path = file_path
        self.cache_path = cache_path
        self.settings = settings
        
        # 结果容器
        self.all_segments: List[ContentSegment] = []
        self.global_id_counter: int = 0
        
        # 文本缓冲区
        self.rolling_buffer: List[str] = [] # 改用 List[str] 性能更好
        self.current_buffer_length: int = 0
        
        # --- 上下文状态 (Context State) ---
        # 这些状态随着遍历过程动态更新，决定了下一个 Segment 的元数据
        self.current_chapter_title: str = "前言/未命名章节"
        self.current_page_index: int = 0
        self.pending_new_chapter: bool = False # 标记下一个生成的 Segment 是否需要只有章节头
        self.current_toc_level: int = 1 
        # 章节映射表 {UnitKey: ChapterTitle}
        self.chapter_map: Dict[Any, str] = {} 

    def run(self) -> List[ContentSegment]:
        """主流程：迭代单元 -> 维护状态 -> 生成对象"""
        logger.info(f"Starting pipeline '{self.__class__.__name__}' for {os.path.basename(self.file_path)}")
        
        self._load_metadata()
        
        # 遍历内容单元 (UnitKey 通常是 页码 或 文件名)
        for unit_key, content, content_type in self._iter_content_units():
            
            # --- A. 视觉/图片模式处理 ---
            if content_type == "image":
                # 1. 先清空之前的文本缓冲区
                self._flush_buffer()
                
                # 2. 直接生成图片 Segment
                seg = ContentSegment(
                    segment_id=self.global_id_counter,
                    original_text="", 
                    content_type="image",
                    image_path=content, 
                    page_index=unit_key if isinstance(unit_key, int) else 0,
                    # 继承当前章节上下文
                    chapter_title=self.current_chapter_title, 
                    toc_level=self.current_toc_level, # 【新增】传入层级
                    is_new_chapter=False 
                )
                self.all_segments.append(seg)
                self.global_id_counter += 1
                continue

            # --- B. 纯文本模式处理 ---
            if not content or not content.strip():
                continue
            
            # 1. 检查章节变更
            # chap_info 可能是字符串(旧逻辑) 或 字典(新逻辑)
            chap_info = self.chapter_map.get(unit_key)
            
            if chap_info:
                # 预先解析出新标题
                new_title = ""
                new_level = 1
                
                if isinstance(chap_info, dict):
                    new_title = chap_info.get("title", "Untitled")
                    new_level = chap_info.get("level", 1)
                else:
                    new_title = str(chap_info)
                    new_level = 1

                # 【关键修正】: 只有当标题发生 *变化* 时，才触发新章节逻辑
                # 否则说明我们还在同一个章节文件的不同段落里
                if new_title != self.current_chapter_title:
                    
                    # 确实是新章节了 -> 结算旧账，开启新篇章
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
            if self.current_buffer_length >= self.settings.max_chunk_size:
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
        
        # 创建对象时传入当前状态
        seg = ContentSegment(
            segment_id=self.global_id_counter,
            original_text=full_text,
            content_type="text",
            # 这里传入的一定要是字符串，不能是字典
            chapter_title=self.current_chapter_title, 
            # 【新增】传入层级信息
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
        """保存为 JSON (需要将 dataclass 转 dict)"""
        data = [asdict(seg) for seg in self.all_segments]
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

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

# ==============================================================================
# 3. EPUB 实现类
# ==============================================================================

class EPUBPipeline(BaseDocPipeline):
    def _load_metadata(self):
        """
        解析 EPUB 元数据和目录结构。
        """
        logger.info("Parsing EPUB metadata.")
        # 1. 读取 EPUB
        self.book = epub.read_epub(self.file_path)
        
        # 2. 尝试从 NCX/NAV 获取目录 (Flatten)
        # 这里假设你已经实现了 _flatten_epub_to_standard
        standardized_items = self._flatten_epub_to_standard(self.book.toc)

        # 3. 兜底逻辑：如果目录为空，使用 Spine (阅读顺序)
        if not standardized_items:
            logger.warning("⚠️ EPUB TOC is empty. Falling back to Spine (linear reading order).")
            
            # 使用 Spine 遍历，它代表了书的真实阅读顺序
            # item_id 是 manifest 里的 ID, linear 表示是否线性阅读(yes/no)
            for item_id, linear in self.book.spine:
                item = self.book.get_item_with_id(item_id)
                
                if item:
                    # 过滤掉非 HTML 文档 (比如图片虽然在 spine 里但不是文档)
                    # 注意：需要 import ebooklib
                    if item.get_type() != ebooklib.ITEM_DOCUMENT:
                        continue
                        
                    # 过滤掉明显的导航文件 (Nav)
                    # 很多 nav 文件没什么可翻译的，容易产生空 seg
                    if 'nav' in (item.get_name() or "").lower():
                        continue

                    # 生成一个临时标题 (因为 Spine 里没有标题信息)
                    # item.get_name() 通常是 'Text/part001.xhtml'，作为标题很难看
                    # 我们可以用文件名，或者直接叫 "Section X"
                    file_name = item.get_name()
                    
                    standardized_items.append({
                        'level': 1,
                        'title': f"Section: {file_name}", # 或者用 item.title 如果有的话
                        'key': file_name
                    })

        # 4. 统一处理
        use_bc = getattr(self.settings, 'use_breadcrumb', True)
        self.chapter_map = process_unified_toc(standardized_items, use_breadcrumb=use_bc)
        logger.info(f"✅ Metadata loaded. Chapter Map size: {len(self.chapter_map)}")

    def _iter_content_units(self):
        """
        [核心修改] 按照 EPUB Spine 遍历，并解析 HTML 块级元素。
        不再返回一整块文本，而是 yield 单个段落/标题。
        """
        # 定义我们需要提取的块级标签
        # 排除 div，防止重复提取（因为 div 通常包含 p）
        BLOCK_TAGS = ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'blockquote', 'pre']

        for item_id, linear in self.book.spine:
            item = self.book.get_item_with_id(item_id)
            
            if not item: continue
            if item.get_type() != ebooklib.ITEM_DOCUMENT: continue
            
            # 1. 解析 HTML
            try:
                raw_content = item.get_content()
                soup = BeautifulSoup(raw_content, 'html.parser')
                
                # 获取文件名作为 Key (用于查章节标题)
                unit_key = item.get_name()

                # 2. 找到 Body (如果没 Body 就找全文档)
                root = soup.find('body') or soup

                # 3. 遍历所有块级元素
                # 这里使用 find_all 可能会遇到嵌套问题（比如 li 里面有 p），
                # 但对于大多数 EPUB，直接提取这些标签是最稳妥的策略。
                for tag in root.find_all(BLOCK_TAGS):
                    
                    # 4. 提取纯文本
                    # separator=' ' 处理 <p>Hello<br>World</p> -> "Hello World"
                    text = tag.get_text(separator=' ', strip=True)
                    
                    # 5. 过滤掉空标签或极短的噪音
                    if not text:
                        continue
                        
                    # 6. Yield 单个段落
                    # 这样 BaseDocPipeline 里的 rolling_buffer 就会一段一段地增加
                    yield unit_key, text, "text"

            except Exception as e:
                logger.error(f"Failed to parse HTML structure for {item_id}: {e}")
                continue

    def _flatten_epub_to_standard(self, toc, level=1):
        items = []
        for node in toc:
            # 兼容 ebooklib 的两种节点格式
            entry = node[0] if isinstance(node, (list, tuple)) else node
            children = node[1] if isinstance(node, (list, tuple)) and len(node) > 1 else []
            
            # 【微调】同时检查 hasattr 和 href 是否真的有值
            if hasattr(entry, 'href') and entry.href:
                items.append({
                    'level': level,
                    'title': entry.title or "Untitled",
                    'key': entry.href.split('#')[0] # key 是文件名
                })
                
            if children:
                items.extend(self._flatten_epub_to_standard(children, level + 1))
        return items

# ==============================================================================
# 5. PDF 实现类 
# ==============================================================================

class PDFPipeline(BaseDocPipeline):
    def __init__(self, file_path: str, cache_path: str, settings: Any, extra_config: dict = None):
        super().__init__(file_path, cache_path, settings)
        self.doc: Optional[fitz.Document] = None
        self.config = extra_config or {}

    def _load_metadata(self):
        """
        加载元数据并适配 process_unified_toc 架构。
        支持：CSV 自定义 -> PDF 原生 TOC -> 纯页码回退。
        """
        self.doc = fitz.open(self.file_path)
        
        # 1. 定义中间层：标准三元组列表
        # 每一项结构: {'level': int, 'title': str, 'key': int}
        standardized_items = []
        
        # =========================================================
        # 分支 A: 尝试加载 CSV 自定义目录 (优先级最高)
        # =========================================================
        if self.settings.custom_toc_path and os.path.exists(self.settings.custom_toc_path):
            logger.info(f"Loading custom TOC from CSV: {self.settings.custom_toc_path}")
            try:
                # utf-8-sig 兼容 Excel 保存的 CSV
                with open(self.settings.custom_toc_path, 'r', encoding='utf-8-sig') as f:
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
        use_bc = getattr(self.settings, 'use_breadcrumb', True)
        
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
        use_vision = self.config.get("use_vision_mode", False)
        start_page = 0
        end_page = len(self.doc)

        for i in range(start_page, end_page):
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
            project_dir = os.path.dirname(os.path.abspath(self.cache_path))

            # 在项目目录下创建 images 文件夹
            image_dir = os.path.join(project_dir, "images")
            os.makedirs(image_dir, exist_ok=True)
            # 2. 生成文件名
            filename = f"page_{page_idx + 1:04d}.jpg"
            full_path = os.path.join(image_dir, filename)

            if os.path.exists(full_path):
                return full_path

            # 3. 计算裁切区域 (Clip Rect) - 逻辑同 _extract_text
            w = page.rect.width
            h = page.rect.height

            # 获取设置 (百分比)
            m_top = getattr(self.settings, 'MARGIN_TOP', 0.0)
            m_bottom = getattr(self.settings, 'MARGIN_BOTTOM', 0.0)
            m_left = getattr(self.settings, 'MARGIN_LEFT', 0.0)
            m_right = getattr(self.settings, 'MARGIN_RIGHT', 0.0)

            # 计算坐标
            x0 = w * m_left
            y0 = h * m_top
            x1 = w * (1.0 - m_right)
            y1 = h * (1.0 - m_bottom)

            # 安全检查：如果裁切参数有问题（比如把图切没了），回退到全页
            if x0 >= x1 or y0 >= y1:
                logger.warning(f"Page {page_idx}: Invalid margins for image. Rendering full page.")
                clip_rect = page.rect
            else:
                clip_rect = fitz.Rect(x0, y0, x1, y1)

            # 4. 设置缩放 (锁定 200 DPI)
            target_dpi = 200
            zoom = target_dpi / 72
            mat = fitz.Matrix(zoom, zoom)

            # 5. 渲染 (关键点：传入 clip 参数)
            # fitz 会只渲染 clip_rect 指定的区域，并应用 matrix 缩放
            pix = page.get_pixmap(matrix=mat, clip=clip_rect, alpha=False)
            
            # 6. 保存
            pix.save(full_path)
            
            return full_path

        except Exception as e:
            logger.error(f"Failed to render image for page {page_idx}: {e}")
            return ""

    def _extract_text(self, page, page_idx) -> str:
        """
        提取页面文本，根据 settings 里的百分比参数进行裁切 (Clip)。
        """
        try:
            # 1. 获取页面原始尺寸
            w = page.rect.width
            h = page.rect.height

            # 2. 获取裁切比例 (默认为 0，即不裁切)
            # 容错处理：使用 getattr 避免 settings 缺少字段报错
            m_top = getattr(self.settings, 'MARGIN_TOP', 0.0)
            m_bottom = getattr(self.settings, 'MARGIN_BOTTOM', 0.0)
            m_left = getattr(self.settings, 'MARGIN_LEFT', 0.0)
            m_right = getattr(self.settings, 'MARGIN_RIGHT', 0.0)

            # 3. 计算裁切矩形 (Clip Rect)
            # 左上角 (x0, y0) -> 右下角 (x1, y1)
            x0 = w * m_left
            y0 = h * m_top
            x1 = w * (1.0 - m_right)
            y1 = h * (1.0 - m_bottom)

            # 安全性检查：防止 margin 设置过大导致区域重叠或无效
            if x0 >= x1 or y0 >= y1:
                logger.warning(f"Page {page_idx}: Margins match or overlap content area. Returning raw text.")
                return page.get_text("text", sort=True).strip()

            clip_rect = fitz.Rect(x0, y0, x1, y1)

            # 4. 提取文本 (带 Clip)
            # sort=True 尝试按阅读顺序重新排列文本块
            text = page.get_text("text", clip=clip_rect, sort=True)
            
            return text.strip()

        except Exception as e:
            logger.error(f"Failed to extract text from page {page_idx}: {e}")
            return ""

# ==============================================================================
# 6. 工厂入口
# ==============================================================================

def compile_structure(
    file_path: str,
    cache_path: str,
    settings: Any, # 你的 Settings 对象
    project_config: Optional[Dict[str, Any]] = None
) -> List[ContentSegment]:  # <--- 注意：返回值变强类型了
    """
    智能工厂函数：根据文件类型实例化对应的 Pipeline 并执行。
    """
    ext = os.path.splitext(file_path)[1].lower()
    final_config = project_config or {}
    
    pipeline: Optional[BaseDocPipeline] = None

    if ext == '.epub':
        pipeline = EPUBPipeline(file_path, cache_path, settings)

    elif ext == '.pdf':
        # --- 自动决策逻辑 (保留原逻辑) ---
        # 如果用户没有强制指定模式，且我们需要自动检测
        if "use_vision_mode" not in final_config:
            logger.info("Auto-detecting PDF type for vision mode decision.")
            try:
                pdf_type = detect_pdf_type(file_path)
                is_image_only = (pdf_type == "image_only")
                # is_image_only = False # 临时占位
                
                final_config["use_vision_mode"] = is_image_only
                logger.info(f"Vision mode set to {is_image_only} based on detection.")
            except Exception as e:
                logger.warning(f"Detection failed: {e}. Defaulting to text mode.")
                final_config["use_vision_mode"] = False

        pipeline = PDFPipeline(file_path, cache_path, settings, extra_config=final_config)

    else:
        raise ValueError(f"Unsupported file format: {ext}")
        
    # 执行 Pipeline，返回对象列表
    return pipeline.run()

# ==============================================================================
# 7. TOC 统一处理函数 
# ==============================================================================
def process_unified_toc(
    raw_toc_items: List[Dict[str, Any]], 
    use_breadcrumb: bool = True
) -> Dict[Any, Dict[str, Any]]:
    """
    [通用核心] 统一处理 TOC。
    Args:
        raw_toc_items: List of {'level': int, 'title': str, 'key': Any}
        use_breadcrumb: True or False 是否启用面包屑导航格式而非层级格式
    Returns:
        { key: {"title": "Final Title", "level": int} }
    """
    chapter_map = {}
    title_stack = [] # 路径栈
    
    for item in raw_toc_items:
        level = item['level']
        raw_title = item['title'].strip()
        key = item['key']
        
        # 1. 维护栈：保留父级路径
        if len(title_stack) >= level:
            title_stack = title_stack[:level-1]
        title_stack.append(raw_title)
        
        # 2. 策略应用
        if use_breadcrumb:
            final_title = " > ".join(title_stack)
            final_level = 1 # 面包屑强制层级为 1 (H2)
        else:
            final_title = raw_title
            final_level = level # 保留原始语义层级
            
        # 3. 写入 Map (防覆盖：保留第一次出现)
        if key not in chapter_map:
            chapter_map[key] = {
                "title": final_title,
                "level": final_level
            }
            
    return chapter_map