# -*- coding: utf-8 -*-
"""
Google Scholar 邮件解析器
从邮件内容中提取论文信息
"""
import copy
import re
import hashlib
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, date
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs, unquote

from .schema import (
    PaperMetadata, 
    PaperSegment, 
    PaperField,
    EmailMetadata,
    DigestStatus
)
from ..utils.logger import get_logger

logger = get_logger(__name__)


class ScholarEmailParser:
    """
    Google Scholar 邮件解析器
    
    功能：
    - 解析 Google Scholar Alert 邮件的 HTML 内容
    - 提取论文标题、作者、摘要、链接等信息
    - 尝试解析 DOI 和其他标识符
    - 将论文按篇分离并封装为 PaperSegment
    """
    
    # Google Scholar 链接特征
    SCHOLAR_LINK_PATTERNS = [
        r'scholar\.google\.com',
        r'scholar\.google\.co\.',
    ]
    
    # DOI 正则表达式
    # 长度上限 {1,2000} 防 ReDoS：DOI 实际最长约 255 字符，2000 留足余量避免
    # 恶意超长输入触发 [^\s<>"']+ 的灾难性回溯
    DOI_PATTERN = re.compile(
        r'(?:doi[:\s]*)?10\.\d{4,}/[^\s<>"\']{1,2000}',
        re.IGNORECASE
    )
    
    # arXiv ID 正则表达式
    ARXIV_PATTERN = re.compile(
        r'arxiv[:\s]*(\d{4}\.\d{4,5}(?:v\d+)?)',
        re.IGNORECASE
    )
    
    # 常见学术领域关键词映射
    FIELD_KEYWORDS = {
        PaperField.ARTIFICIAL_INTELLIGENCE: [
            'artificial intelligence', 'ai', 'neural network', 'deep learning',
            'machine learning', 'nlp', 'natural language', 'computer vision',
            'reinforcement learning', 'transformer', 'llm', 'large language model'
        ],
        PaperField.MACHINE_LEARNING: [
            'machine learning', 'ml', 'supervised learning', 'unsupervised',
            'classification', 'regression', 'clustering', 'feature extraction'
        ],
        PaperField.COMPUTER_SCIENCE: [
            'computer science', 'algorithm', 'software', 'programming',
            'database', 'distributed', 'parallel computing', 'security'
        ],
        PaperField.BIOLOGY: [
            'biology', 'biological', 'gene', 'protein', 'cell', 'organism',
            'evolution', 'ecology', 'genomic', 'molecular biology'
        ],
        PaperField.MEDICINE: [
            'medicine', 'medical', 'clinical', 'patient', 'treatment',
            'disease', 'therapy', 'diagnosis', 'healthcare', 'drug'
        ],
        PaperField.NEUROSCIENCE: [
            'neuroscience', 'brain', 'neuron', 'cognitive', 'neural',
            'cortex', 'hippocampus', 'synapse', 'neuroimaging'
        ],
        PaperField.PHYSICS: [
            'physics', 'quantum', 'particle', 'relativity', 'mechanics',
            'thermodynamics', 'electromagnetic', 'optics'
        ],
        PaperField.CHEMISTRY: [
            'chemistry', 'chemical', 'molecule', 'reaction', 'synthesis',
            'compound', 'catalyst', 'polymer'
        ],
        PaperField.MATHEMATICS: [
            'mathematics', 'mathematical', 'theorem', 'proof', 'algebra',
            'geometry', 'calculus', 'statistics', 'probability'
        ],
        PaperField.ENGINEERING: [
            'engineering', 'mechanical', 'electrical', 'civil', 'structural',
            'control system', 'robotics', 'automation'
        ],
        PaperField.ENVIRONMENTAL_SCIENCE: [
            'environment', 'climate', 'pollution', 'sustainability',
            'ecosystem', 'carbon', 'renewable', 'conservation'
        ],
        PaperField.ECONOMICS: [
            'economics', 'economic', 'market', 'financial', 'investment',
            'trade', 'monetary', 'fiscal'
        ],
        PaperField.PSYCHOLOGY: [
            'psychology', 'psychological', 'behavior', 'mental',
            'emotion', 'perception', 'memory', 'attention'
        ],
    }
    
    def __init__(self):
        """初始化解析器"""
        self._segment_counter = 0
    
    def generate_paper_id(self, title: str, authors: List[str] = None) -> str:
        """
        生成论文唯一ID
        
        Args:
            title: 论文标题
            authors: 作者列表（可选）
            
        Returns:
            32位MD5哈希ID
        """
        content = title.lower().strip()
        if authors:
            content += '|' + '|'.join(a.lower().strip() for a in authors[:3])
        
        return hashlib.md5(content.encode('utf-8')).hexdigest()
    
    def parse_email(
        self, 
        html_content: str, 
        email_metadata: EmailMetadata
    ) -> List[PaperSegment]:
        """
        解析 Google Scholar 邮件，提取论文信息
        
        Args:
            html_content: 邮件 HTML 内容
            email_metadata: 邮件元数据
            
        Returns:
            PaperSegment 列表
        """
        if not html_content:
            logger.warning("⚠️ 邮件内容为空")
            return []
        
        soup = BeautifulSoup(html_content, 'html.parser')
        papers = []
        
        # Google Scholar Alert 邮件的论文通常在特定的 HTML 结构中
        # 尝试多种解析策略
        
        # 策略1: 查找 class 包含特定关键词的元素
        paper_blocks = self._find_paper_blocks(soup)
        
        if paper_blocks:
            logger.debug(f"📄 找到 {len(paper_blocks)} 个论文块")
            for block in paper_blocks:
                paper = self._parse_paper_block(block, email_metadata)
                if paper:
                    papers.append(paper)
        else:
            # 策略2: 通过链接和文本模式识别论文
            papers = self._parse_by_links(soup, email_metadata)
        
        logger.info(f"📚 从邮件 {email_metadata.email_id[:8]}... 提取了 {len(papers)} 篇论文")
        return papers
    
    def _find_paper_blocks(self, soup: BeautifulSoup) -> List[Any]:
        """
        查找论文信息块
        
        Google Scholar Alert 邮件结构可能因版本不同而变化，
        这里尝试多种选择器，包括对引用（Citations）邮件的支持
        """
        # 策略0（优先）：Scholar Alert 的标准结构是每篇论文一个 <h3><a>标题</a></h3>，
        # 其后的兄弟节点为作者行与摘要。按 h3 锚点切分能拿到全部论文；
        # 泛型 div 选择器常常只命中一个无关容器，导致整封邮件只提取出 1 篇。
        h3_blocks = self._split_by_h3_anchors(soup)
        if len(h3_blocks) >= 2:
            return h3_blocks

        # 常见论文容器选择器
        selectors = [
            # 新版及引用邮件格式
            'div[style*="margin-bottom"]',
            'div[class*="paper"]',
            'table[class*="paper"]',
            'tr[class*="paper"]',
            # 搜索结果/引用列表项
            'div[style*="padding"]',
            # 基于表格的结构
            'table table tr',
        ]
        
        blocks = []
        for selector in selectors:
            found = soup.select(selector)
            for b in found:
                text = b.get_text(strip=True)
                # 论文块通常包含标题且长度适中，且包含学术特征（如链接或 [PDF] 标记）
                if 40 < len(text) < 2000:
                    # 检查是否有链接，论文块至少应该有一个标题链接
                    if b.find('a', href=True) and b not in blocks:
                        # 进一步检查父子关系，避免嵌套重复添加
                        if not any(b in existing or existing in b for existing in blocks):
                            blocks.append(b)
        
        if blocks:
            return blocks
        
        # 如果没找到明确的块，尝试基于 <h3> 或 <font size> 分割
        headers = soup.find_all(['h3', 'font', 'b'])
        if headers:
            return self._split_by_headers(soup, headers)
        
        return []
    
    def _split_by_h3_anchors(self, soup: BeautifulSoup) -> List[Any]:
        """按 <h3><a>（Scholar 论文标题的标准标记）切分邮件为独立论文片段。

        每个片段包含 h3 本身及其后直到下一个 h3 之前的所有兄弟节点
        （作者行、期刊行、摘要片段），供 _parse_paper_block 解析。
        """
        anchors = [h for h in soup.find_all('h3') if h.find('a', href=True)]
        if len(anchors) < 2:
            return []

        blocks = []
        rich_count = 0
        for anchor in anchors:
            frag = BeautifulSoup('<div></div>', 'html.parser')
            container = frag.div
            container.append(copy.copy(anchor))
            sibling = anchor.next_sibling
            while sibling is not None:
                if getattr(sibling, 'name', None) == 'h3':
                    break
                container.append(copy.copy(sibling))
                sibling = sibling.next_sibling
            if len(container.get_text(strip=True)) > len(anchor.get_text(strip=True)) + 20:
                rich_count += 1
            blocks.append(container)

        # 嵌套表格布局（h3 与摘要不在同一层级）下兄弟遍历只能拿到标题：
        # 若多数片段没带出正文，放弃本策略，回退到容器选择器路径
        if rich_count * 2 < len(blocks):
            return []
        return blocks

    def _split_by_headers(
        self, 
        soup: BeautifulSoup, 
        headers: List[Any]
    ) -> List[Any]:
        """基于标题元素分割内容"""
        blocks = []
        
        for header in headers:
            # 检查是否像是论文标题
            text = header.get_text(strip=True)
            if len(text) > 20 and len(text) < 500:
                # 获取相关的父容器
                parent = header.find_parent(['div', 'td', 'tr'])
                if parent and parent not in blocks:
                    blocks.append(parent)
        
        return blocks
    
    def _parse_paper_block(
        self, 
        block: Any, 
        email_metadata: EmailMetadata
    ) -> Optional[PaperSegment]:
        """
        解析单个论文块
        
        Args:
            block: BeautifulSoup 元素
            email_metadata: 邮件元数据
            
        Returns:
            PaperSegment 或 None
        """
        try:
            # 提取标题（通常是第一个链接或加粗文本）
            title, url = self._extract_title_and_url(block)
            if not title:
                return None
            
            # 提取作者（传入干净标题，剥除粘连的标题尾巴）
            authors = self._extract_authors(block, title)
            
            # 提取摘要/描述
            abstract = self._extract_abstract(block)
            
            # 提取发布信息
            journal, pub_date = self._extract_publication_info(block)
            
            # 尝试提取 DOI
            doi = self._extract_doi(block, url)
            
            # 尝试提取 arXiv ID
            arxiv_id = self._extract_arxiv_id(block, url)
            
            # 提取引用次数
            citation_count = self._extract_citation_count(block)
            
            # 推断领域
            field = self._infer_field(title, abstract, email_metadata.alert_query)
            
            # 生成 paper_id
            paper_id = self.generate_paper_id(title, authors)
            
            # 创建元数据
            metadata = PaperMetadata(
                paper_id=paper_id,
                title=title,
                authors=authors,
                publication_date=pub_date,
                journal=journal,
                doi=doi,
                arxiv_id=arxiv_id,
                url=url,
                field=field,
                citation_count=citation_count,
                source_email_id=email_metadata.email_id,
                email_received_at=email_metadata.received_at,
                extracted_at=datetime.now()
            )
            
            # 创建片段
            self._segment_counter += 1
            segment = PaperSegment(
                segment_id=self._segment_counter,
                paper_id=paper_id,
                original_abstract=abstract,
                metadata=metadata,
                status=DigestStatus.PENDING
            )
            
            return segment
            
        except Exception as e:
            logger.warning(f"⚠️ 解析论文块失败: {e}")
            return None
    
    def _extract_title_and_url(self, block: Any) -> Tuple[str, Optional[str]]:
        """提取标题和链接"""
        title = ""
        url = None
        
        # 查找链接
        links = block.find_all('a', href=True)
        for link in links:
            href = link.get('href', '')
            text = link.get_text(strip=True)
            
            # 跳过太短或明显不是标题的链接
            if len(text) < 10 or text.lower() in ['pdf', 'html', 'cite', 'cited by']:
                continue
            
            # 检查是否是 Scholar 链接
            if any(pattern in href for pattern in self.SCHOLAR_LINK_PATTERNS) or 'http' in href:
                title = text
                url = self._clean_scholar_url(href)
                break
        
        # 如果没找到链接中的标题，尝试其他方式
        if not title:
            # 查找加粗文本
            bold = block.find(['b', 'strong', 'h3', 'h4'])
            if bold:
                title = bold.get_text(strip=True)
            else:
                # 使用第一段有意义的文本
                texts = block.stripped_strings
                for text in texts:
                    if len(text) > 20 and not text.startswith('http'):
                        title = text
                        break
        
        return title, url
    
    def _clean_scholar_url(self, url: str) -> str:
        """
        清理 Scholar 跳转链接，提取真实 URL
        
        Google Scholar 邮件中的链接通常是跳转链接，
        需要提取实际的论文链接
        """
        if 'scholar.google' in url and 'url=' in url:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            if 'url' in params:
                return unquote(params['url'][0])
        
        return url
    
    # 作者 token 的排除关键词（标题尾巴/邮件样板文字混入的信号）
    _AUTHOR_JUNK_KEYWORDS = (
        "google scholar", "following", "new articles", "related to research",
        "http", "arxiv", "preprint", "[pdf]", "[html]", "et al", "journal",
    )

    @staticmethod
    def _strip_title_bleed(author_text: str, title: str) -> str:
        """剥掉粘在作者串开头的标题尾巴。

        Scholar 邮件里 get_text 常把标题末段与作者行粘在一起（如
        "…Multi-Agent LLM Systems?J Bai, L Shi" 或无分隔的 "…RecordsD Pant"）。
        标题是单独干净提取的，故找“title 的最长后缀恰是 author_text 的前缀”并截掉。
        """
        if not author_text or not title:
            return author_text
        a = re.sub(r"\s+", " ", author_text).strip()
        t = re.sub(r"\s+", " ", title).strip()
        al, tl = a.lower(), t.lower()
        best = 0
        # 找 title 的最长后缀，使其为 author_text 的前缀（字符级，忽略大小写）
        for i in range(len(tl)):
            suffix = tl[i:]
            if len(suffix) <= best:
                break  # 后面更短，无需再试
            if al.startswith(suffix):
                best = len(suffix)
                break  # 从最左 i 起即最长后缀
        if best >= 6:  # 至少 6 字符才认定是标题尾巴，避免误伤短姓名
            return a[best:].lstrip(" ,:?[]-–—")
        return author_text

    @classmethod
    def _clean_author_list(cls, raw_text: str, title: str = "") -> List[str]:
        """把原始作者串清洗为作者列表（纯函数，可离线单测）。

        步骤：剥标题尾巴 → 截到 " - "（作者 - 期刊, 年）前 → 逗号分隔 → 过滤噪声 token。
        """
        text = cls._strip_title_bleed(raw_text or "", title)
        # 截断到 " - "（Scholar 引用行的作者/期刊分隔），去掉期刊年份尾巴
        text = re.split(r"\s[-–—]\s", text)[0]
        out: List[str] = []
        for tok in text.split(","):
            t = tok.strip()
            if not t:
                continue
            if any(ch.isdigit() for ch in t):      # 排除年份/卷期等
                continue
            if len(t) > 40:                          # 过长 → 标题残留
                continue
            if any(ch in t for ch in "?:[]{}"):     # 标题标点混入
                continue
            low = t.lower()
            if any(k in low for k in cls._AUTHOR_JUNK_KEYWORDS):
                continue
            out.append(t)
        return out[:10]

    def _extract_authors(self, block: Any, title: str = "") -> List[str]:
        """提取作者列表（传入干净标题以剥除粘连的标题尾巴）"""
        raw = ""

        # Google Scholar 邮件中作者通常是灰色/绿色着色文本
        gray_texts = block.find_all(['font', 'span'],
            attrs={'color': re.compile(r'#[0-9a-fA-F]{6}|gray', re.I)})
        for elem in gray_texts:
            text = elem.get_text(strip=True)
            if ',' in text and len(text) < 500:
                raw = text
                break

        # 回退：从 "Author - Journal, Year" 或 "by Author1, Author2" 中取
        if not raw:
            text = block.get_text()
            meta_match = re.search(r'([^-\n]+)\s+-\s+[^-\n]+,\s+\d{4}', text)
            if meta_match:
                raw = meta_match.group(1).strip()
            else:
                by_match = re.search(r'by\s+([^-\n]+)', text, re.IGNORECASE)
                if by_match:
                    raw = by_match.group(1)

        return self._clean_author_list(raw, title)
    
    def _extract_abstract(self, block: Any) -> str:
        """提取摘要/描述"""
        # 获取块中的所有文本（用换行符连接节点，让后续 split('\n') 能按行拆分）
        full_text = block.get_text(separator='\n', strip=True)
        
        # 移除标题和作者（通常在开头）
        lines = full_text.split('\n')
        abstract_lines = []
        
        for line in lines:
            line = line.strip()
            # 跳过太短的行或明显的元数据
            if len(line) < 30:
                continue
            if any(skip in line.lower() for skip in ['cited by', 'related articles', 'save']):
                continue
            abstract_lines.append(line)
        
        abstract = ' '.join(abstract_lines)
        
        # 清理多余空格
        abstract = re.sub(r'\s+', ' ', abstract).strip()
        
        # 截取合理长度
        if len(abstract) > 2000:
            abstract = abstract[:2000] + '...'
        
        return abstract
    
    def _extract_publication_info(
        self, 
        block: Any
    ) -> Tuple[Optional[str], Optional[date]]:
        """提取期刊和发布日期"""
        journal = None
        pub_date = None
        
        text = block.get_text()
        
        # 查找年份
        year_match = re.search(r'\b(19|20)\d{2}\b', text)
        if year_match:
            try:
                year = int(year_match.group())
                pub_date = date(year, 1, 1)  # 默认1月1日
            except:
                pass
        
        # 尝试提取期刊名（通常在斜体或特定格式中）
        italic = block.find('i')
        if italic:
            journal_text = italic.get_text(strip=True)
            if len(journal_text) > 3 and len(journal_text) < 100:
                journal = journal_text
        
        return journal, pub_date
    
    def _extract_doi(self, block: Any, url: Optional[str]) -> Optional[str]:
        """提取 DOI"""
        # 从文本中查找
        text = block.get_text()
        doi_match = self.DOI_PATTERN.search(text)
        if doi_match:
            doi = doi_match.group()
            # 清理 DOI
            doi = re.sub(r'^doi[:\s]*', '', doi, flags=re.IGNORECASE)
            return doi.strip()
        
        # 从 URL 中查找
        if url and 'doi.org' in url:
            parsed = urlparse(url)
            if parsed.path:
                return parsed.path.lstrip('/')
        
        return None
    
    def _extract_arxiv_id(self, block: Any, url: Optional[str]) -> Optional[str]:
        """提取 arXiv ID"""
        text = block.get_text()
        
        # 从文本中查找
        arxiv_match = self.ARXIV_PATTERN.search(text)
        if arxiv_match:
            return arxiv_match.group(1)
        
        # 从 URL 中查找
        if url and 'arxiv.org' in url:
            match = re.search(r'(\d{4}\.\d{4,5}(?:v\d+)?)', url)
            if match:
                return match.group(1)
        
        return None
    
    def _extract_citation_count(self, block: Any) -> Optional[int]:
        """提取引用次数"""
        text = block.get_text()
        # 匹配 "Cited by 123" 或 "被引用次数：123" 或 "引用次数：123"
        match = re.search(r'(?:Cited by|被引用次数|引用次数|被引用)[:：\s]+(\d+)', text, re.IGNORECASE)
        if match:
            try:
                return int(match.group(1))
            except:
                pass
        return None
    
    def _infer_field(
        self, 
        title: str, 
        abstract: str, 
        alert_query: Optional[str]
    ) -> PaperField:
        """根据内容推断论文领域"""
        text = f"{title} {abstract} {alert_query or ''}".lower()
        
        # 计算每个领域的匹配分数
        field_scores = {}
        for field, keywords in self.FIELD_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in text)
            if score > 0:
                field_scores[field] = score
        
        if field_scores:
            # 返回得分最高的领域
            return max(field_scores, key=field_scores.get)
        
        return PaperField.OTHER
    
    def _parse_by_links(
        self, 
        soup: BeautifulSoup, 
        email_metadata: EmailMetadata
    ) -> List[PaperSegment]:
        """
        通过链接模式解析论文
        
        当无法识别明确的论文块时，基于链接进行解析
        """
        papers = []
        seen_titles = set()
        
        # 查找所有可能是论文标题的链接
        for link in soup.find_all('a', href=True):
            href = link.get('href', '')
            text = link.get_text(strip=True)
            
            # 过滤条件
            if len(text) < 15 or len(text) > 500:
                continue
            if text.lower() in ['pdf', 'html', 'cite', 'cited by', 'related articles']:
                continue
            if text in seen_titles:
                continue
            
            # 检查是否是学术链接
            if not any(pattern in href for pattern in ['scholar.google', 'doi.org', 'arxiv.org', 'http']):
                continue
            
            seen_titles.add(text)
            
            # 获取链接周围的上下文作为摘要
            parent = link.find_parent(['div', 'td', 'tr', 'p'])
            abstract = ""
            if parent:
                abstract = parent.get_text(separator=' ', strip=True)
                # 移除标题本身
                abstract = abstract.replace(text, '').strip()
                abstract = re.sub(r'\s+', ' ', abstract)
            
            # 创建论文
            url = self._clean_scholar_url(href)
            doi = self._extract_doi_from_text(abstract) or self._extract_doi_from_url(url)
            
            paper_id = self.generate_paper_id(text)
            
            metadata = PaperMetadata(
                paper_id=paper_id,
                title=text,
                url=url,
                doi=doi,
                field=self._infer_field(text, abstract, email_metadata.alert_query),
                source_email_id=email_metadata.email_id,
                email_received_at=email_metadata.received_at,
                extracted_at=datetime.now()
            )
            
            self._segment_counter += 1
            segment = PaperSegment(
                segment_id=self._segment_counter,
                paper_id=paper_id,
                original_abstract=abstract[:1500] if abstract else "",
                metadata=metadata,
                status=DigestStatus.PENDING
            )
            
            papers.append(segment)
        
        return papers
    
    def _extract_doi_from_text(self, text: str) -> Optional[str]:
        """从文本中提取 DOI"""
        match = self.DOI_PATTERN.search(text)
        if match:
            doi = match.group()
            doi = re.sub(r'^doi[:\s]*', '', doi, flags=re.IGNORECASE)
            return doi.strip()
        return None
    
    def _extract_doi_from_url(self, url: str) -> Optional[str]:
        """从 URL 中提取 DOI"""
        if url and 'doi.org' in url:
            parsed = urlparse(url)
            if parsed.path:
                return parsed.path.lstrip('/')
        return None
    
    def reset_counter(self):
        """重置片段计数器"""
        self._segment_counter = 0
