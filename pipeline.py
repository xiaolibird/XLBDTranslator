import os
import json
import re
from abc import ABC, abstractmethod
import csv

# 第三方库
import fitz  # PyMuPDF
import spacy
from ebooklib import epub

# 本地工具 
import utils

# ==============================================================================
# 0. 全局资源初始化
# ==============================================================================

print("⚙️ Loading NLP Model (Spacy)...")
try:
    nlp = spacy.load("en_core_web_sm")
except:
    print("⚠️ 未检测到 Spacy 模型，请运行: python -m spacy download en_core_web_sm")
    nlp = None

# ==============================================================================
# 1. 基类：定义流水线骨架
# ==============================================================================

class BaseDocPipeline(ABC):
    def __init__(self, file_path, cache_path):
        self.file_path = file_path
        self.cache_path = cache_path
        
        # 内部状态
        self.all_segments = []
        self.global_id_counter = 0
        self.rolling_buffer = ""
        self.BATCH_SIZE = 1200  # 缓冲区阈值
        
        # 章节映射表
        self.chapter_map = {} 

    def run(self):
        """主流程 (Template Method)"""
        print(f"⚙️ [{self.__class__.__name__}] Start: {os.path.basename(self.file_path)}")
        
        # 1. 加载元数据
        self._load_metadata()
        
        # 2. 初始化容器
        self.all_segments = []
        self.rolling_buffer = ""  # 确保这里初始化了
        
        # 3. 开始遍历
        for unit_key, text in self._iter_content_units():
            if not text or not text.strip(): continue
            
            # =========================================================
            # 🟢 【修复核心】Vision Mode 直通逻辑
            # =========================================================
            if text.strip().startswith("<<IMAGE_PATH"):
                print(f"   📸 [Page {unit_key}] Image Token detected.")

                # A. 关键修正：检查 self.rolling_buffer 而不是 current_buffer
                # 如果缓冲区里有之前攒下的普通文字，先让它们“落袋为安”
                if self.rolling_buffer:
                    print(f"      💨 Flushing text buffer before image...")
                    self._flush_buffer() 
                
                # B. 将图片 Token 直接存为一个独立的 Segment
                # 保持 ID 的连续性 (1-based)
                self.all_segments.append({
                    "id": len(self.all_segments) + 1,
                    "text": text.strip()
                })
                
                # C. 直接跳过本次循环，不走下面的文本处理逻辑
                continue
            # =========================================================

            # --- 下面是 Text 模式的常规逻辑 ---

            # 语义注入
            chapter_title = self.chapter_map.get(unit_key)
            is_new_chapter = (chapter_title is not None)
            
            # 强制刷新缓冲区 (章节隔离)
            # 这里的 self.rolling_buffer 也要对应修改
            if is_new_chapter and self.rolling_buffer:
                print(f"   ✂️ Boundary detected: [{chapter_title}]. Flushing buffer...")
                self._flush_buffer()

            # 注入结构标记
            if is_new_chapter:
                # 修复：标题前后加双换行
                marker = f"\n\n## [Chapter: {chapter_title}]\n\n"
                print(f"   📍 Locate: {chapter_title}")
            else:
                # 弱标记：只是 Section (页码)
                marker = f"\n\n## [Section: {unit_key}]\n\n"
                
            self.rolling_buffer += marker + text
            
            # 容量检查
            if len(self.rolling_buffer) >= self.BATCH_SIZE:
                self._flush_buffer()
        
        # 4. 收尾：循环结束，把肚子里剩下的吐出来
        self._flush_buffer()
        
        # 5. 保存
        self._save_cache()
        
        return self.all_segments

    def _flush_buffer(self):
        if not self.rolling_buffer: return
        
        new_segs = self._semantic_chunking(self.rolling_buffer, max_chars=1200)
        
        for seg in new_segs:
            seg['id'] = self.global_id_counter
            self.all_segments.append(seg)
            self.global_id_counter += 1
            
        self.rolling_buffer = ""

    def _save_cache(self):
        print(f"   💾 Freezing structure to: {os.path.basename(self.cache_path)}")
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(self.all_segments, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _semantic_chunking(text, max_chars=1500, method="paragraph_aware"):
        """[NLP Tool] 通用字符串切分逻辑"""
        
        # =========== 🟢 核心修复区域 ===========
        # 1. 修复字面转义符：把 "\n" (两个字符) 变成真正的换行
        text = text.replace('\\n', '\n')
        # 2. 统一 Windows/Unix 换行
        text = text.replace('\r\n', '\n')
        # 3. 【新逻辑】使用正则 "挤压" 换行符
        # 含义：无论原本是 1 个、2 个还是 10 个换行符，统统变成标准的 Markdown 分段 (\n\n)
        text = re.sub(r'\n+', '\n\n', text)
        # =====================================

        if not nlp:
             return [{"text": text}]

        if method == "paragraph_aware":
            paragraphs = re.split(r'\n\s*\n+', text)
            paragraphs = [p.strip() for p in paragraphs if p.strip()]
            
            segments = []
            current_chunk = []
            current_len = 0
            
            for para in paragraphs:
                para_len = len(para)
                
                # 情况 A: 超长段落，按句子切
                if para_len > max_chars:
                    if current_chunk:
                        segments.append({"text": "\n\n".join(current_chunk)})
                        current_chunk = []
                        current_len = 0
                    
                    doc = nlp(para[:100000]) 
                    sents = [s.text.strip() for s in doc.sents if s.text.strip()]
                    
                    temp_chunk = []
                    temp_len = 0
                    for sent in sents:
                        sl = len(sent)
                        if temp_len + sl > max_chars and temp_chunk:
                            segments.append({"text": " ".join(temp_chunk)})
                            temp_chunk = [sent]
                            temp_len = sl
                        else:
                            temp_chunk.append(sent)
                            temp_len += sl
                    if temp_chunk:
                        segments.append({"text": " ".join(temp_chunk)})
                
                # 情况 B: 普通段落，合并
                else:
                    if current_len + para_len + 2 > max_chars and current_chunk:
                        segments.append({"text": "\n\n".join(current_chunk)})
                        current_chunk = [para]
                        current_len = para_len
                    else:
                        current_chunk.append(para)
                        current_len += para_len + 2
            
            if current_chunk:
                segments.append({"text": "\n\n".join(current_chunk)})
            
            return segments
        
        return [{"text": text}]

    @abstractmethod
    def _load_metadata(self):
        pass

    @abstractmethod
    def _iter_content_units(self):
        pass


# ==============================================================================
# 2. EPUB 实现类
# ==============================================================================

class EPUBPipeline(BaseDocPipeline):
    def _load_metadata(self):
        print("   📖 Parsing EPUB TOC...")
        self.book = epub.read_epub(self.file_path)
        self.chapter_map = utils.flatten_toc(self.book.toc)
        print(f"   🗺️  Mapped {len(self.chapter_map)} TOC entries.")

    def _iter_content_units(self):
            for item_id, linear in self.book.spine:
                item = self.book.get_item_with_id(item_id)
                if item:
                    # 🟢 正确代码：调用 HTML 清洗函数
                    raw_text = utils.extract_text_from_epub_item(item)
                    file_name = item.get_name()
                    yield file_name, raw_text


# ==============================================================================
# 3. PDF 实现类 
# ==============================================================================

class PDFPipeline(BaseDocPipeline):
    """
    基于 PyMuPDF 的原生 PDF 提取器。
    继承自 BaseDocPipeline。
    保留了高级布局分析功能：边距切除、段落间距判定、连字符修复。
    """
    
    def __init__(self, file_path, cache_path, extra_config=None):
        super().__init__(file_path, cache_path)
        self.doc = None
        # 接收外部配置 (main.py 传进来的 PDF_CONFIG)
        self.config = extra_config or {}
        self.custom_toc_path = extra_config.get("custom_toc_path") # 保存自定义章节信息路径
        """        
        Page,Title,Level
        1,Title1,1
        5,Title2,1
        5,Subtitle2-1,2
        20,Title3,1
        """
    
    def _load_metadata(self):
        """[必须实现] 打开 PDF 并解析目录"""
        print(f"   📕 Opening PDF: {os.path.basename(self.file_path)}")
        self.doc = fitz.open(self.file_path)
        
        # 🟢 2. 优先读取 CSV TOC
        if self.custom_toc_path and os.path.exists(self.custom_toc_path):
            print(f"   📥 Loading Custom TOC from: {self.custom_toc_path}")
            try:
                with open(self.custom_toc_path, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        # CSV Page 1 -> PDF Index 0
                        p_idx = int(row['Page']) - 1 
                        title = row['Title']
                        # level = row['Level'] # 暂时没用到层级，后续可扩展
                        self.chapter_map[p_idx] = title
                print(f"   ✅ Overloaded {len(self.chapter_map)} chapters from CSV.")
                return # 成功读取后，直接返回，不再读 PDF 内置目录
            except Exception as e:
                print(f"   ⚠️ Failed to load CSV TOC: {e}")

        # 尝试提取书签作为章节映射
        try:
            toc = self.doc.get_toc(simple=False)
            for item in toc:
                lvl, title, page_num = item[0], item[1], item[2]
                if page_num > 0:
                    # PDF 页码是从 1 开始的，转换为 0-based 索引
                    self.chapter_map[page_num - 1] = title
            print(f"   🗺️  Mapped {len(self.chapter_map)} bookmarks from PDF TOC.")
        except Exception as e:
            print(f"   ⚠️  TOC extraction failed: {e}")

    def _iter_content_units(self):
        """[必须实现] 按页面遍历内容 (带调试日志)"""
        
        # 1. 处理页面范围
        start_page = 0
        end_page = len(self.doc)
        
        if "page_range" in self.config and self.config["page_range"]:
            r_start, r_end = self.config["page_range"]
            start_page = max(0, r_start)
            end_page = min(len(self.doc), r_end)
            print(f"   📄 [Scope] Processing specific range: {start_page} to {end_page}")
        else:
            print(f"   📄 [Scope] Processing ALL pages: 0 to {end_page}")

        # 2. 循环遍历
        for i in range(start_page, end_page):
            page = self.doc[i]
            
            # 调用核心提取逻辑
            # 💡 这里的 page_text 可能是文本，也可能是 "<<IMAGE_PATH...>>"
            page_text = self._extract_text_from_page(page, page_idx=i)
            
            # 🛡️ [防呆修正] 防止返回 None 导致后续报错
            if page_text is None:
                page_text = ""
                
            # 🕵️‍♂️ [调试日志] 这一步能救命！
            # 让你在终端直接看到每一页到底提取到了什么
            content_preview = page_text[:50].replace('\n', '\\n')
            if not page_text.strip():
                print(f"      ⚠️ [Page {i}] Extracted EMPTY content! (Check Config/OCR)")
            elif "<<IMAGE_PATH" in page_text:
                print(f"      📸 [Page {i}] Vision Token: {content_preview}...")
            else:
                print(f"      📝 [Page {i}] Text Length: {len(page_text)} chars")

            # Yield 出去
            yield i, page_text

    def _extract_text_from_page(self, page, page_idx=0):
        """
        [核心逻辑移植] 单页布局分析与文本提取。
        """

        if self.config.get("use_vision_mode", False):
            # 直接跳去处理图片，不再往下执行文本提取
            return self._extract_image_for_vision(page, page_idx)   

        h = page.rect.height
        
        # 1. 获取配置 (优先绝对值，没有则用百分比)
        cfg_top = self.config.get("margin_top", None)
        cfg_bottom = self.config.get("margin_bottom", None)
        cfg_top_pct = self.config.get("margin_top_pct", 0.08)
        cfg_bottom_pct = self.config.get("margin_bottom_pct", 0.08)
        min_gap = self.config.get("min_gap", 8.0) # 段落间距阈值

        # 计算切除阈值
        m_top = cfg_top if (cfg_top is not None and cfg_top > 1) else h * cfg_top_pct
        m_bottom = cfg_bottom if (cfg_bottom is not None and cfg_bottom > 1) else h * cfg_bottom_pct
        
        # (可选) 打印第一页的参数供调试
        if page_idx == 0:
            print(f"   📐 Layout Config: H={h:.1f} | Top Cut={m_top:.1f} | Bottom Cut={m_bottom:.1f}")

        # 2. 获取所有文本块
        blocks = page.get_text("dict").get("blocks", [])
        
        page_paragraphs = []     # 存放本页提取出的所有完整段落
        current_para_lines = []  # 正在拼接的当前段落行
        prev_block_bottom = None # 上一个块的底边位置
        
        for block in blocks:
            # 过滤非文本块 (如图片)
            if "lines" not in block: continue
            
            bbox = block.get("bbox", [0,0,0,0]) # [x0, y0, x1, y1]
            
            # A. 核心过滤：切除页眉页脚
            if bbox[3] < m_top or bbox[1] > (h - m_bottom): 
                continue
            
            # B. 提取文本内容
            block_text = ""
            for line in block["lines"]:
                for span in line.get("spans", []):
                    block_text += span.get("text", "") + " "
            block_text = block_text.strip()
            
            # C. 过滤噪点 (页码、太短的乱码)
            if not block_text or (block_text.isdigit() and len(block_text) < 5) or len(block_text) < 3:
                continue
            
            # D. 段落判定 (基于垂直间距 min_gap)
            block_top = bbox[1]
            is_new_para = False
            
            if prev_block_bottom is not None:
                # 如果当前块的顶部 - 上一个块的底部 > 阈值，认为是新段落
                if (block_top - prev_block_bottom) > min_gap:
                    is_new_para = True
            
            # 如果是新段落，先结算上一段
            if is_new_para and current_para_lines:
                full_para = " ".join(current_para_lines).strip()
                # E. 连字修复 (Hyphenation Repair)
                full_para = self._repair_hyphenation(full_para)
                if full_para: page_paragraphs.append(full_para)
                current_para_lines = []
            
            # 加入当前累积
            current_para_lines.append(block_text)
            prev_block_bottom = bbox[3]
            
        # 处理本页最后剩下的段落缓存
        if current_para_lines:
            full_para = " ".join(current_para_lines).strip()
            full_para = self._repair_hyphenation(full_para)
            if full_para: page_paragraphs.append(full_para)
            
        # 返回本页文本，段落之间用双换行隔开
        return "\n\n".join(page_paragraphs)

    def _extract_image_for_vision(self, page, page_idx):
        """
        📸 [Vision Mode Core]
        将当前 PDF 页面渲染为高分辨率图片，保存到缓存目录，并返回路径暗号。
        """
        try:
            # 1. 渲染图片 (DPI=200 是性价比之选，Gemini 看得清且体积不过大)
            # matrix=fitz.Matrix(2, 2) 等效于 zoom=2
            pix = page.get_pixmap(dpi=200)
            
            # 2. 准备存放图片的文件夹
            # 存放在跟 structure_map.json 同一级的 "page_images" 文件夹里
            base_dir = os.path.dirname(self.cache_path)
            img_dir = os.path.join(base_dir, "page_images")
            os.makedirs(img_dir, exist_ok=True)
            
            # 3. 构造文件名 (按页码排序，方便查找)
            img_filename = f"page_{page_idx:04d}.jpg"
            img_path = os.path.join(img_dir, img_filename)
            
            # 4. 保存到硬盘
            pix.save(img_path)
            print(f"      📸 Vision Capture: Page {page_idx} saved.")
            
            # 5. 【关键】返回“暗号”
            # 这个格式必须和 translator.py 里识别的格式完全一致！
            return f"<<IMAGE_PATH::{img_path}>>"
            
        except Exception as e:
            print(f"      ❌ Vision Capture Failed on Page {page_idx}: {e}")
            return "" # 返回空字符串，跳过此页

    @staticmethod
    def _repair_hyphenation(text):
        """辅助：修复跨行连字符 (ex- ample -> example)"""
        # 匹配逻辑：单词 + 连字符 + 空格/换行 + 小写字母
        return re.sub(r'-\s*\n?\s*([a-z])', r'\1', text)


# ==============================================================================
# 4. 工厂入口
# ==============================================================================

def compile_structure(file_path, cache_path, project_config):
    """
    智能工厂：全自动决策中心。
    决定是用 PDFPipeline 还是 EPUBPipeline，是 Vision 模式还是 Native 模式。
    """
    
    ext = os.path.splitext(file_path)[1].lower()
    
    # 准备配置容器 (如果用户没传，就新建一个空字典)
    # final_config = project_config.copy() if project_config else {}
    if project_config is None: project_config = {}
    final_config = project_config 
    pipeline = None

    if ext == '.epub':
        pipeline = EPUBPipeline(file_path, cache_path)

    elif ext == '.pdf':
        # =========== 🕵️‍♀️ 侦探逻辑 (自动决策) ===========
        
        # 1. 决策：是否开启 Vision 模式？
        # 只有当用户没有显式指定时，我们才去自动检测
        if "use_vision_mode" not in final_config:
            print("   🔍 Auto-detecting PDF type...")
            try:
                # 调用 utils 进行诊断
                pdf_type = utils.detect_pdf_type(file_path)
                
                if pdf_type == "image_only":
                    print("   ⚠️ Diagnosis: Image-only/Scanned PDF. 🟢 Switching to VISION mode.")
                    final_config["use_vision_mode"] = True
                else:
                    print("   ✅ Diagnosis: Native Text PDF. 🔵 Using TEXT extraction.")
                    final_config["use_vision_mode"] = False
            except Exception as e:
                print(f"   ⚠️ Detection failed ({e}). Defaulting to Text mode.")
                final_config["use_vision_mode"] = False

        # 2. 决策：如果是 Native 模式，需要切边距吗？
        # 无论是否是 Vision 模式，如果配置中没有边距，都尝试自动扫描
        if final_config.get("margin_top") is None:
            print("   🔍 Auto-scanning layout margins...")
            try:
                margins = utils.analyze_pdf_margins_by_scan(file_path)
                final_config.update({
                    "margin_top": margins.get("suggested_margin_top", 0),
                    "margin_bottom": margins.get("suggested_margin_bottom", 0)
                })
                print(f"      ✅ Margins set: Top={final_config['margin_top']} / Bottom={final_config['margin_bottom']}")
            except:
                print("      ⚠️ Margin scan failed. Using defaults.")

        # =========== 🏭 实例化 ===========
        pipeline = PDFPipeline(
            file_path, 
            cache_path, 
            extra_config=final_config
        )

    else:
        raise ValueError(f"❌ Unsupported file format: {ext}")
        
    return pipeline.run()