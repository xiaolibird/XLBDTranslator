# -*- coding: utf-8 -*-
"""书籍精读的结构脊柱：PDF → 目录 → 章节切分 → 逐页文本 → book manifest。

与 pdf_ingest（论文链路）的三处根本差异，每一处都对应旧手工 digest 的一类翻车：

1. **切分按目录，不按固定页窗**。20 页硬切必然拦腰截断论证——旧 digest 里出现过
   「§6.4, pp.74，部分截断」这种自述。章节边界由出版方给定，免费且准确。
2. **逐页保留、不拼成一个大串**。论文链路把全页文本 join 成一个字符串再按字符切块，
   页码信息当场丢失，于是脚本草稿结构上无法标页码。这里按页存，页码是一等索引。
3. **manifest 是每次 LLM 调用的强制前缀**。旧 digest 每个 chunk 各自猜书目，同一本
   博士论文在 7 个 chunk 里被猜出 7 个不同标题——因为没有任何一个 chunk 见过封面。

页码术语（全文件统一，别混）：
  pdf_page    1-based PDF 物理页序
  printed     原书印刷页码 = pdf_page + page_offset（教科书的罗马数字前言让二者恒差十几页）
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..utils.logger import get_logger

logger = get_logger(__name__)

MANIFEST_NAME = "book.manifest.json"
MANIFEST_SCHEMA_VERSION = 1


# ---------------- 逐页文本 ----------------

def extract_pages(pdf_path: Path) -> List[str]:
    """PDF → 每页一个字符串的列表（下标 0 = PDF 第 1 页）。**不截断**。

    论文链路的 extract_pdf_text 有 100 万字符上限并静默丢尾——对 726 页的书
    等于砍掉后 43%。书籍链路按章喂 LLM，从来不需要一次性拼全书，故这里不设上限。
    """
    import fitz  # PyMuPDF
    pages: List[str] = []
    with fitz.open(str(pdf_path)) as doc:
        for page in doc:
            pages.append(page.get_text("text", sort=True) or "")
    return pages


# ---------------- 章节 ----------------

@dataclass
class Chapter:
    """一个章节单元（目录里 level<=split_level 的一项到下一项之前）。"""
    number: int                 # 1-based 顺序号（不是书上印的章号，见 label）
    title: str
    level: int
    pdf_start: int              # 1-based，闭区间
    pdf_end: int                # 1-based，闭区间
    label: str = ""             # 书上印的章号（如 "14"），抽不出则空
    subsections: List[str] = field(default_factory=list)

    @property
    def n_pages(self) -> int:
        return self.pdf_end - self.pdf_start + 1

    def printed_range(self, page_offset: int = 0) -> Tuple[int, int]:
        return (self.pdf_start + page_offset, self.pdf_end + page_offset)


# 目录标题里的章号："Chapter 14 · Harm" / "14 Harm" / "14. Harm" / "第14章"
_CHAP_LABEL_RE = re.compile(
    r"^\s*(?:chapter|chap\.?|ch\.?)\s*([0-9]+|[IVXLC]+)\b|^\s*([0-9]+)(?:[.．、]|\s)|^\s*第\s*([0-9]+)\s*章",
    re.I)


def chapter_label(title: str) -> str:
    """从目录标题抽出书上印的章号；抽不出返回空串。"""
    m = _CHAP_LABEL_RE.match(title or "")
    if not m:
        return ""
    return next((g for g in m.groups() if g), "")


# 前置/后置材料的章节标题。这些条目不是「章」，但会混在同一层级里：JAMA 的原生书签
# 把 Cover/Preface 与 Part 并列放在 L1，把 Glossary/Index 下的**索引字母**（A、B、C…）
# 放在 L2 —— 与真正的章同级。不滤掉的话 29 章会变成 75 个「章」，其中 46 个是字母，
# 分诊会对着索引字母逐个打分。
_MATTER_TITLE_RE = re.compile(
    r"^\s*(?:cover|title\s+page|copyright|contents?|table\s+of\s+contents|contributors?|"
    r"foreword|preface|acknowledge?ments?|dedication|about\s+the|glossary|index|"
    r"references?|bibliography|appendix|colophon|front\s+matter|back\s+matter|"
    r"封面|版权|目录|序言?|前言|致谢|附录|索引|参考文献)\b", re.I)
# 索引字母页（单个字母或字母对，如 "A" / "Mc"）
_INDEX_LETTER_RE = re.compile(r"^[A-Z][a-z]?$")


def is_matter(title: str) -> bool:
    """该目录标题是否为前/后置材料（而非正文章节）。"""
    t = (title or "").strip()
    return bool(_MATTER_TITLE_RE.match(t) or _INDEX_LETTER_RE.match(t))


def build_chapters(toc_items: Sequence[Dict[str, Any]], n_pages: int,
                   split_level: int = 1) -> List[Chapter]:
    """标准目录三元组（load_standardized_toc / parse_printed_toc 的产物）→ 章节列表。

    split_level = 「章」所在的**精确层级**，不是「不深于」：
      - Little & Rubin：level 1 就是章 → split_level=1
      - JAMA Users' Guides：level 1 是 Part（含 Cover/Preface 等前言项），level 2 才是
        真正的章 → split_level=2。若按「<=2」收，Part 会与章交错成伪章节，30 章会被
        5 个 Part 撕成碎片，分诊矩阵随之失去意义。

    每章的**止页**取下一个「同级或更高级」条目的起页减一：目录只给起点不给终点，
    而更高级条目（下一个 Part）同样意味着本章结束。
    """
    lv = int(split_level)

    def under_matter(pos: int) -> bool:
        """该条目所属的上级分部是否为前/后置材料（Glossary、Index…）。"""
        for t in reversed(toc_items[:pos]):
            if int(t.get("level", 1)) < lv:
                return is_matter(t.get("title") or "")
        return False

    def keep(pos: int, it: Dict[str, Any]) -> bool:
        title = it.get("title") or ""
        if is_matter(title):
            return False
        # 处在后置材料分部之下**且没有章号**才排除：索引字母（A、B、Mc）两条都占，
        # 而带章号的真章即便紧跟在 Preface 之后也应保留——只按父级判会把它一起误杀。
        return bool(chapter_label(title)) or not under_matter(pos)

    picked = [(i, it) for i, it in enumerate(toc_items)
              if int(it.get("level", 1)) == lv and keep(i, it)]
    if not picked:
        return []
    chapters: List[Chapter] = []
    for n, (pos, it) in enumerate(picked):
        start = int(it["key"]) + 1                      # key 是 0-based
        # 边界：其后第一个 level <= lv 的条目（同级章或上级 Part）
        nxt = next((t for t in toc_items[pos + 1:] if int(t.get("level", 1)) <= lv), None)
        end = int(nxt["key"]) if nxt else n_pages
        end = max(start, end)                           # 同页起的两条目录项：至少 1 页
        title = (it.get("title") or "").strip()
        subs = [(s.get("title") or "").strip() for s in toc_items
                if int(s.get("level", 1)) > lv and start - 1 <= int(s["key"]) < end]
        chapters.append(Chapter(number=n + 1, title=title, level=lv,
                                pdf_start=start, pdf_end=end,
                                label=chapter_label(title), subsections=subs))
    return chapters


# 印刷目录条目的**起始**行："1.2     Missingness Patterns and Mechanisms" / "14      Mixed Normal…"
_PRINTED_TOC_HEAD_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\s+(\S.*)$")
# 条目**结尾**（行尾即印刷页码）。长标题会折行，页码只出现在最后一行。
_PRINTED_TOC_TAIL_RE = re.compile(r"^(.*?)\s+(\d{1,4})\s*$")
# 目录里的 Part 分隔行："Part I  Overview and Basic Approaches  1"
_PRINTED_PART_RE = re.compile(r"^\s*Part\s+([IVXLC]+|\d+)\s+(.+?)\s+(\d{1,4})\s*$", re.I)
# 目录页自身的页眉/页脚（"vi   Contents" / "Contents    vii"），不是条目
_TOC_RUNNING_HEAD_RE = re.compile(r"^\s*(?:[ivxlc]+\s+)?Contents(?:\s+[ivxlc]+)?\s*$", re.I)
# 后置材料（无编号）："References  405" / "Author Index  429" / "Appendix A  399"。
# 必须认出来当**边界**：否则末章会一路吞到 PDF 末页——Rubin 第 15 章会因此多算 46 页
# 参考文献与索引，深读预算与覆盖率一起失真。
_PRINTED_BACKMATTER_RE = re.compile(
    r"^\s*((?:References|Bibliography|Author\s+Index|Subject\s+Index|Index|Glossary|"
    r"Appendi[xc]e?s?(?:\s+[A-Z0-9]+)?)(?:\s+[A-Za-z][\w\s\-]*?)?)\s+(\d{1,4})\s*$", re.I)

# 印刷目录里，「章」统一落在 level 2，Part 落在 level 1——与 JAMA 原生书签的
# Part(L1)/章(L2) 口径一致，于是两本书都用 split_level=2，不必各记一套。
_PRINTED_PART_LEVEL = 1
_PRINTED_CHAPTER_BASE_LEVEL = 2


def parse_printed_toc(pages: Sequence[str], toc_pdf_pages: Sequence[int],
                      page_offset: int) -> List[Dict[str, Any]]:
    """解析书**自己印的目录页**，产出与 load_standardized_toc 同型的三元组列表。

    存在的理由：Little & Rubin 这本 462 页的 PDF **没有任何书签**（`get_toc()` 返回空），
    而它的印刷目录排版规整、行尾就是印刷页码。没有这条分支就只能退回页窗盲切——
    正是本流水线要消灭的做法。

    折行必须合并，不能只取首行：实测 Rubin 的 15 章里有 7 章标题跨两行、页码在第二行，
    只认单行会**静默漏掉这 7 章**（第 3、7、8、9、11、13、14 章），漏掉的部分不会报错，
    只会在覆盖报告里表现为「这本书只有 8 章」。

    要求先知道 page_offset（printed = pdf + offset），才能把印刷页码换算回 PDF 页序；
    正文首页（印着 "1" 的那页）可由 detect_page_offset 探测或人工指定。
    """
    items: List[Dict[str, Any]] = []
    seen = set()

    def emit(num: str, title: str, printed: int, is_part: bool) -> None:
        key = printed - page_offset - 1                  # → 0-based PDF 页序
        if not (0 <= key < len(pages)):
            return
        level = (_PRINTED_PART_LEVEL if is_part
                 else _PRINTED_CHAPTER_BASE_LEVEL + num.count("."))
        title_full = ("Part {} {}".format(num, title) if is_part
                      else "{} {}".format(num, title)).strip()
        dedup = (level, title_full, key)
        if dedup not in seen:
            seen.add(dedup)
            items.append({"level": level, "title": title_full, "key": key})

    for p in toc_pdf_pages:
        idx = int(p) - 1
        if not (0 <= idx < len(pages)):
            continue
        pending_num, pending_title = None, ""
        for line in (pages[idx] or "").splitlines():
            if not line.strip() or _TOC_RUNNING_HEAD_RE.match(line):
                continue
            pm = _PRINTED_PART_RE.match(line)
            if pm:
                pending_num = None
                emit(pm.group(1), pm.group(2).strip(), int(pm.group(3)), True)
                continue
            bm = _PRINTED_BACKMATTER_RE.match(line)
            if bm:
                # 与 Part 同级（level 1）：本身不是章，但会给前一章封口
                pending_num = None
                key = int(bm.group(2)) - page_offset - 1
                if 0 <= key < len(pages):
                    title = bm.group(1).strip()
                    if (1, title, key) not in seen:
                        seen.add((1, title, key))
                        items.append({"level": 1, "title": title, "key": key})
                continue
            hm = _PRINTED_TOC_HEAD_RE.match(line)
            if hm:
                # 新条目开始：上一条若仍未收尾（页码没等到），直接丢弃——不猜页码
                pending_num, rest = hm.group(1), hm.group(2)
            else:
                if pending_num is None:
                    continue
                rest = line.strip()                      # 折行续接
            tm = _PRINTED_TOC_TAIL_RE.match(rest)
            if tm:
                title = (pending_title + " " + tm.group(1)).strip()
                emit(pending_num, title, int(tm.group(2)), False)
                pending_num, pending_title = None, ""
            else:
                pending_title = (pending_title + " " + rest).strip()
        # 页末未收尾的条目跨页续接的情况极少，且宁缺勿猜

    items.sort(key=lambda it: (it["key"], it["level"]))
    return items


def chapter_text(pages: Sequence[str], ch: Chapter) -> str:
    """取一章的正文（按页拼接，页间插入页码标记）。

    页码标记不是装饰：它让 LLM 在写引句时知道自己正读到第几页，也让 agent 亲读时
    能把「草稿说的 p.247」对回具体页。旧链路把全书拼成裸字符串，这一层信息当场丢失。
    """
    out: List[str] = []
    for p in range(ch.pdf_start, ch.pdf_end + 1):
        idx = p - 1
        if 0 <= idx < len(pages):
            out.append("\n[[PDF p.{}]]\n{}".format(p, pages[idx]))
    return "".join(out)


# ---------------- 页码偏移 ----------------

# 页脚/页眉里孤立成行的阿拉伯数字 = 该页印刷页码的最可能候选
_PAGE_NUM_LINE_RE = re.compile(r"^\s*(\d{1,4})\s*$")


def detect_page_offset(pages: Sequence[str], probe_start: int = 1,
                       probe_end: Optional[int] = None) -> Optional[int]:
    """探测 printed = pdf_page + offset 里的 offset（多数投票）。

    做法：在每页的首/末几行里找孤立数字，与该页 pdf 页序作差；取出现次数最多的差值。
    正文页的页脚页码是印刷体，与 pdf 页序恒差同一个数，故众数即答案；图表页、
    章首页常无页码或用不同版式，它们只会成为少数派。

    返回 None = 探测不可靠（少于 5 页给出同一差值），此时应由 manifest 手工指定——
    宁可要人填一次，也不要用一个错的 offset 把全书页码锚系统性地偏掉。
    """
    end = probe_end or len(pages)
    votes: Dict[int, int] = {}
    for p in range(probe_start, min(end, len(pages)) + 1):
        lines = [ln for ln in (pages[p - 1] or "").splitlines() if ln.strip()]
        # 首末各两行去重后再投票：页面只有 1-2 行时 lines[:2] 与 lines[-2:] 会重叠，
        # 同一个页码被计两票，于是「少于 5 页同意就不采信」的门槛被虚高的票数绕过。
        probe = lines[:2] + [ln for ln in lines[-2:] if ln not in lines[:2]]
        for ln in probe:
            m = _PAGE_NUM_LINE_RE.match(ln)
            if m:
                votes[int(m.group(1)) - p] = votes.get(int(m.group(1)) - p, 0) + 1
    if not votes:
        return None
    best, n = max(votes.items(), key=lambda kv: kv[1])
    return best if n >= 5 else None


# ---------------- manifest ----------------

@dataclass
class BookManifest:
    """一本书的单一事实源：书目 + 目录 + 页偏移 + 分诊矩阵 + ledger。

    entry_type 决定引用粒度，两者不可混：
      book    专著（Little & Rubin）：全书一个 citekey，章是 close_reading 的分节，
              引用时用 pandoc 页码定位符 [@little2020rubin, p. 247]
      chapter 编著文集（JAMA Users' Guides）：各章作者不同，章才是可引单元，
              每章一个 citekey、一条索引记录、CSL type=chapter
    """
    slug: str
    pdf_path: str
    entry_type: str                     # "book" | "chapter"
    title: str = ""
    authors: List[str] = field(default_factory=list)
    editors: List[str] = field(default_factory=list)
    publisher: str = ""
    edition: str = ""
    year: Optional[int] = None
    isbn: str = ""
    citekey: str = ""                   # 专著的 citekey；编著为空（各章自有）
    n_pages: int = 0
    page_offset: int = 0
    split_level: int = 2
    toc_source: str = ""                # native | printed（目录从哪来，覆盖报告要用）
    chapters: List[Dict[str, Any]] = field(default_factory=list)
    # 本书专属的分诊问题（追加在 config/topics.yaml 的 8 问之后）。
    # 存在的理由：topics.yaml 是**札记库的概念页轴**，全部围绕缺失机制；而一本书对论文的
    # 价值未必落在那条轴上。实测 JAMA Users' Guides 按 topics.yaml 分诊，Harm 章得 0 分、
    # Prognosis 得 1 分——裁决实质正确（那两章确实不谈缺失机制），但结论「整本不用读」是错的：
    # 用户旧手工 digest 正是拿它做**方法学评价框架**（偏倚判断、预后研究标准）。
    # 缺这一层，分诊就只会告诉你「这本书回答不了我问的问题」，而不会告诉你它能回答什么。
    # 每项形如 {"slug": "...", "title": "...", "question": "..."}。
    extra_questions: List[Dict[str, str]] = field(default_factory=list)
    triage: Dict[str, Any] = field(default_factory=dict)     # {chapter_number: {...}}
    ledger: Dict[str, Any] = field(default_factory=dict)     # {chapter_number: {...}}
    schema_version: int = MANIFEST_SCHEMA_VERSION

    # ---- 序列化 ----
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "BookManifest":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self, book_dir: Path) -> Path:
        from .notes_index import _atomic_write
        path = Path(book_dir) / MANIFEST_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps(self.to_dict(), ensure_ascii=False, indent=2))
        return path

    @classmethod
    def load(cls, book_dir: Path) -> "BookManifest":
        path = Path(book_dir) / MANIFEST_NAME
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    # ---- 章节 ----
    def chapter_objs(self) -> List[Chapter]:
        return [Chapter(**{k: v for k, v in c.items() if k in Chapter.__dataclass_fields__})
                for c in self.chapters]

    def chapter(self, number: int) -> Optional[Chapter]:
        return next((c for c in self.chapter_objs() if c.number == int(number)), None)

    # ---- 强制前缀（每次 LLM/agent 调用都必须带）----
    def prompt_prefix(self) -> str:
        """书目 + 目录概览。旧 digest 的元数据幻觉根因就是缺这一段。"""
        who = "、".join(self.authors[:4]) or "、".join(self.editors[:4]) or "（作者不详）"
        head = ["## 本书书目（权威，勿据正文猜测）",
                "标题：{}".format(self.title or self.slug),
                "{}：{}".format("编者" if self.entry_type == "chapter" else "作者", who)]
        if self.edition:
            head.append("版次：{}".format(self.edition))
        if self.publisher or self.year:
            head.append("出版：{} {}".format(self.publisher, self.year or "").strip())
        if self.isbn:
            head.append("ISBN：{}".format(self.isbn))
        head.append("总页数：{} 页（PDF）；印刷页码 = PDF 页序 {:+d}"
                    .format(self.n_pages, self.page_offset))
        head.append("")
        head.append("## 全书目录（{} 章）".format(len(self.chapters)))
        for c in self.chapter_objs():
            lo, hi = c.printed_range(self.page_offset)
            head.append("  {}. {} （原书 pp.{}-{}）".format(c.number, c.title, lo, hi))
        return "\n".join(head)


def _pdf_meta(pdf_path: Path) -> Dict[str, Any]:
    import fitz
    with fitz.open(str(pdf_path)) as doc:
        md = doc.metadata or {}
        return {"title": (md.get("title") or "").strip(),
                "author": (md.get("author") or "").strip(),
                "n_pages": doc.page_count}


def build_manifest(pdf_path: Path, slug: str, entry_type: str,
                   custom_toc_path: Optional[Path] = None,
                   split_level: int = 2,
                   page_offset: Optional[int] = None,
                   printed_toc_pages: Optional[Sequence[int]] = None,
                   overrides: Optional[Dict[str, Any]] = None) -> BookManifest:
    """PDF → BookManifest（目录 + 章节 + 页偏移探测）。

    目录梯子（三级，逐级降级）：
      1. CSV 自定义目录 / PDF 原生书签（load_standardized_toc）—— JAMA 走这条
      2. 书自己印的目录页（parse_printed_toc）—— Little & Rubin 走这条，其 PDF
         **完全没有书签**，而这本恰恰是最需要精读的
      3. 都没有 → 空章节列表，明确报错要人给 --toc-csv / --printed-toc-pages，
         而不是悄悄退回页窗盲切

    overrides 用于补 PDF 元数据给不出的书目字段（ISBN、编者、版次…）。这些字段
    **不猜**：书目错一个字，全书的引用就全错，而 PDF 元数据里恰恰经常是空的或错的
    （Rubin 这本的 PDF metadata 连标题都没有）。
    """
    from ..parser.formats import load_standardized_toc
    import fitz

    pdf_path = Path(pdf_path)
    info = _pdf_meta(pdf_path)
    pages = extract_pages(pdf_path)

    if page_offset is None:
        # 探测区间跳过前 20 页：罗马数字前言的页码与正文不同体系，会污染众数
        page_offset = detect_page_offset(pages, probe_start=min(21, len(pages)))
        if page_offset is None:
            logger.warning("⚠️ 页码偏移探测不可靠，暂按 0；请核对后在 manifest 里手工指定")
            page_offset = 0
        else:
            logger.info("页码偏移探测：printed = pdf {:+d}".format(page_offset))
    page_offset = int(page_offset)

    with fitz.open(str(pdf_path)) as doc:
        toc_items = load_standardized_toc(doc, custom_toc_path)
    toc_source = "native" if toc_items else ""

    if not toc_items and printed_toc_pages:
        toc_items = parse_printed_toc(pages, printed_toc_pages, page_offset)
        toc_source = "printed" if toc_items else ""
        logger.info("原生书签为空，改用印刷目录页 {}：解析出 {} 条"
                    .format(list(printed_toc_pages), len(toc_items)))

    chapters = build_chapters(toc_items, info["n_pages"], split_level=split_level)
    if not chapters:
        logger.warning("⚠️ 未得到章节切分（书签为空、且未给 --printed-toc-pages/--toc-csv）"
                       "——不会退回页窗盲切，请补目录来源后重跑")

    man = BookManifest(
        slug=slug, pdf_path=str(pdf_path.resolve()), entry_type=entry_type,
        title=info["title"] or slug,
        authors=[a for a in [info["author"]] if a],
        n_pages=info["n_pages"], page_offset=page_offset,
        split_level=split_level, toc_source=toc_source,
        chapters=[asdict(c) for c in chapters],
    )
    for k, v in (overrides or {}).items():
        if hasattr(man, k) and v not in (None, "", []):
            setattr(man, k, v)
    return man


def page_index_for(manifest: BookManifest, pages: Optional[Sequence[str]] = None):
    """构造引文回验用的 PageIndex（按原书印刷页码寻址）。"""
    from .quote_verify import PageIndex
    pgs = pages if pages is not None else extract_pages(Path(manifest.pdf_path))
    return PageIndex(pgs, offset=manifest.page_offset)
