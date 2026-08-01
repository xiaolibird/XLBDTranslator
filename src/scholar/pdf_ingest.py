# -*- coding: utf-8 -*-
"""
手动 PDF 深度精读 ingest：给一个 PDF → 抽全文 + 拉权威元数据 → 分块通读 → CloseReading 草稿。

与流水线 top-N 精读（closereading.close_read_segments）的区别：
  - 全文不截断（pdf_to_text max_chars 调到极大），分块逐块通读后汇总，而非单跳；
  - 输出扩展分节的 CloseReading（研究问题/方法/结果与效应量/图表/局限/联想/逐节要点）；
  - 是 agent（Claude Code）交叉核验的「脚本草稿」——最终稿由 agent 亲读 PDF 后合并。

落盘 bundle（output/scholar_notes/manual/YYYY-MM/<paper_id>.paper.json，status=draft）供 agent 接手。
复用：closereading.pdf_to_text/parse_closeread、crossref.crossref_by_doi/crossref_lookup、
academic_search.fetch_arxiv_by_id、fulltext.ipv4_client、schema.PaperSegment/PaperMetadata。
"""
import hashlib
import json
import re
import threading
# CancelledError 自 py3.8 起继承 BaseException，except Exception 接不住，须显式捕获
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .schema import (
    PaperSegment, PaperMetadata, PaperField, DigestStatus,
    FilterDecision, CloseReading,
)
from .closereading import pdf_to_text, parse_closeread
from ..utils.logger import get_logger
from ..utils.json_tools import strip_code_fences as _strip_json

logger = get_logger(__name__)

BUNDLE_SCHEMA_VERSION = 1
BUNDLE_SUFFIX = ".paper.json"

# DOI：10.xxxx/yyyy，末尾常粘标点/括号，剥掉
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"<>]+", re.I)
# arXiv id：2401.12345(v2) 或旧式 math.GT/0309136
_ARXIV_RE = re.compile(r"\b(\d{4}\.\d{4,5}(?:v\d+)?|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})\b")
_ARXIV_CTX = re.compile(r"arxiv", re.I)


def _generate_paper_id(title: str, authors: Optional[List[str]] = None) -> str:
    """与 academic_search._generate_paper_id 一致（title + 前3作者 md5），保证跨模块同一篇同 id。"""
    content = (title or "").lower().strip()
    if authors:
        content += '|' + '|'.join(a.lower().strip() for a in authors[:3])
    return hashlib.md5(content.encode('utf-8')).hexdigest()


# ---------------- PDF 文本 + 标识符抽取 ----------------

def extract_pdf_text(path: Path, max_chars: int = 1_000_000) -> str:
    """抽 PDF 全文纯文本（默认上限极大 ≈ 不截断）。复用 closereading.pdf_to_text。"""
    return pdf_to_text(Path(path), max_chars=max_chars)


def pdf_page_count(path: Path) -> Optional[int]:
    """PDF 页数；读不出返回 None。

    这个数必须传给 agent：亲读协议是「20 页窗口读到尾」，agent 不知道总页数就无从判断
    自己有没有读完。实际踩过——只读到 12 页就认定草稿引用的 Table 15/21/24 是编造的，
    而那些表在第 13 页之后的附录里，每个数都对（PDF 共 31 页）。
    """
    try:
        import fitz  # PyMuPDF
        with fitz.open(str(path)) as doc:
            return doc.page_count
    except Exception as e:
        logger.warning("  ⚠️ 读取 PDF 页数失败: {}".format(e))
        return None


def read_windows(n_pages: Optional[int], size: int = 20) -> List[Tuple[int, int]]:
    """把总页数切成 agent 亲读用的 [(起页, 止页)] 窗口（1-based 闭区间）。"""
    if not n_pages or n_pages < 1:
        return []
    return [(s, min(s + size - 1, n_pages)) for s in range(1, n_pages + 1, size)]


def _clean_doi(raw: str) -> str:
    d = (raw or "").strip().rstrip(").,;")
    # 去掉误粘的尾随标记（如 doi 后跟 "Received"）
    return d


# PDF 抽文常把 DOI 与紧邻正文粘连（无空格换行），如
# "10.1177/09622802231165001development of ..."。真 DOI 后缀也可能含字母，
# 故不硬猜边界：生成候选让 Crossref 当裁判（见 resolve_metadata）。
_DOI_GLUE_RE = re.compile(r"(?<=\d)[A-Za-z][A-Za-z\-]*$")


def doi_candidates(raw: str) -> List[str]:
    """由抽取到的 DOI 原串生成候选（原样优先，其次剥去数字后粘连的字母尾）。"""
    d = _clean_doi(raw)
    if not d:
        return []
    out = [d]
    trimmed = _DOI_GLUE_RE.sub("", d).rstrip("-.")
    if trimmed and trimmed != d and re.match(r"^10\.\d{4,9}/\S+$", trimmed):
        out.append(trimmed)
    return out


def extract_pdf_ids(path: Path, first_pages_text: str = "") -> Dict[str, Optional[str]]:
    """从 fitz metadata + 前几页文本提取 doi / arxiv_id / 标题候选。任何缺失为 None。"""
    ids: Dict[str, Optional[str]] = {"doi": None, "arxiv_id": None, "title": None}
    meta_title = None
    try:
        import fitz  # PyMuPDF
        with fitz.open(str(path)) as doc:
            meta_title = (doc.metadata or {}).get("title") or None
            if not first_pages_text:
                parts = []
                for pi, page in enumerate(doc):
                    if pi >= 3:
                        break
                    parts.append(page.get_text("text") or "")
                first_pages_text = "\n".join(parts)
    except Exception as e:
        logger.warning("  ⚠️ 读取 PDF metadata 失败: {}".format(e))

    text = first_pages_text or ""
    dm = _DOI_RE.search(text)
    if dm:
        ids["doi"] = _clean_doi(dm.group(0))
    # arXiv：优先 "arXiv:" 上下文附近的 id，避免误抓正文里的数字
    for m in _ARXIV_RE.finditer(text):
        window = text[max(0, m.start() - 12):m.start()]
        if _ARXIV_CTX.search(window):
            ids["arxiv_id"] = m.group(1)
            break
    title = (meta_title or "").strip()
    if not title:
        # 兜底：首页第一段较长的非全大写行作为标题候选
        for line in text.splitlines():
            s = line.strip()
            if len(s) >= 20 and not s.isupper() and "@" not in s:
                title = s
                break
    ids["title"] = title or None
    return ids


# ---------------- 元数据权威链 ----------------

_META_EXTRACT_PROMPT = """从下面这篇论文的首页文本抽取书目元数据，只输出 JSON：
{{
  "title": "论文完整标题",
  "authors": ["First Last", ...],
  "journal": "期刊或会议名（无则空串）",
  "year": 2024,
  "abstract": "英文摘要原文（无则空串）"
}}

首页文本：
{text}"""


def _meta_from_crossref(hit: Dict[str, Any]) -> PaperMetadata:
    authors = hit.get("authors") or []
    return PaperMetadata(
        paper_id=_generate_paper_id(hit.get("title", ""), authors),
        title=hit.get("title") or "",
        authors=authors,
        publication_date=hit.get("publication_date"),
        journal=hit.get("journal"),
        doi=hit.get("doi"),
        volume=hit.get("volume"),
        issue=hit.get("issue"),
        pages=hit.get("pages"),
        url=hit.get("url"),
        field=PaperField.OTHER,
        source_type="manual-pdf",
    )


def resolve_metadata(ids: Dict[str, Optional[str]], llm=None, email: str = "",
                     first_pages_text: str = "",
                     title_override: str = "") -> Tuple[PaperMetadata, str]:
    """按 DOI→arXiv→标题 Crossref→LLM 首页抽取 的权威链解析元数据。

    返回 (PaperMetadata, metadata_source)。source ∈ crossref-doi/arxiv/crossref-title/pdf-llm/pdf-only。
    """
    from .crossref import crossref_by_doi, crossref_lookup
    from .fulltext import ipv4_client

    doi = ids.get("doi")
    arxiv_id = ids.get("arxiv_id")
    title = (title_override or ids.get("title") or "").strip()

    # 1) DOI 直查（最权威）；PDF 抽文可能把 DOI 与正文粘连 → 候选逐个查，Crossref 命中即真
    if doi:
        cands = doi_candidates(doi)
        for cand in cands:
            hit = crossref_by_doi(cand, email=email)
            if hit:
                meta = _meta_from_crossref(hit)
                if arxiv_id and not meta.arxiv_id:
                    meta.arxiv_id = arxiv_id
                return meta, "crossref-doi"
        if len(cands) > 1:
            # 检出粘连但无一被 Crossref 证实 → 该串不可信，别污染下游元数据。
            # 无粘连迹象时保留原值（此处查不中多半只是网络不通）。
            doi = None

    # 2) arXiv 精确查
    if arxiv_id:
        try:
            from .academic_search import fetch_arxiv_by_id
            with ipv4_client(timeout=20) as xc:
                it = fetch_arxiv_by_id(arxiv_id, xc)
            if it:
                meta = PaperMetadata(
                    paper_id=_generate_paper_id(it.get("title", ""), it.get("authors")),
                    title=it.get("title") or "",
                    authors=it.get("authors") or [],
                    publication_date=it.get("published"),
                    journal=it.get("journal"),
                    doi=it.get("doi") or doi,
                    arxiv_id=arxiv_id,
                    url=it.get("url"),
                    field=PaperField.OTHER,
                    source_type="manual-pdf",
                )
                return meta, "arxiv"
        except Exception as e:
            logger.warning("  ⚠️ arXiv 精确查失败（{}）: {}".format(arxiv_id, e))

    # 3) 标题查 Crossref（内建 0.85 相似度门槛）
    if title:
        hit = crossref_lookup(title, email=email)
        if hit:
            meta = _meta_from_crossref(hit)
            if arxiv_id and not meta.arxiv_id:
                meta.arxiv_id = arxiv_id
            return meta, "crossref-title"

    # 4) LLM 从首页抽（全 miss 降级）
    if llm is not None and first_pages_text.strip():
        try:
            resp = llm.call(_META_EXTRACT_PROMPT.format(text=first_pages_text[:8000]),
                            max_tokens=1024, json_mode=True)
            data = json.loads(_strip_json(resp))
            authors = [a for a in (data.get("authors") or []) if isinstance(a, str)]
            year = data.get("year")
            pub = None
            if isinstance(year, int) and 1900 < year < 2100:
                from datetime import date
                pub = date(year, 1, 1)
            t = (data.get("title") or title or "").strip()
            # LLM 抽出的标题比「首页第一行够长的行」这种启发式干净得多（后者常抓到页眉、
            # 会议名、作者行）。拿它再查一次 Crossref：命中就能补齐作者/年份/卷期页，
            # 直接避免这篇落成 anon* 兜底键、bibliography 里缺卷期页。
            if t and t.lower() != title.lower():
                try:
                    hit = crossref_lookup(t, email=email)
                except Exception:
                    hit = None
                if hit:
                    meta = _meta_from_crossref(hit)
                    if arxiv_id and not meta.arxiv_id:
                        meta.arxiv_id = arxiv_id
                    return meta, "crossref-title"
            meta = PaperMetadata(
                paper_id=_generate_paper_id(t, authors),
                title=t, authors=authors,
                publication_date=pub,
                journal=(data.get("journal") or None) or None,
                doi=doi, arxiv_id=arxiv_id,
                field=PaperField.OTHER, source_type="manual-pdf",
            )
            return meta, "pdf-llm"
        except Exception as e:
            logger.warning("  ⚠️ LLM 元数据抽取失败: {}".format(e))

    # 5) 兜底：只有 PDF 抓到的零散信息
    meta = PaperMetadata(
        paper_id=_generate_paper_id(title or str(path_hint(ids)), None),
        title=title or "(未知标题)", authors=[],
        doi=doi, arxiv_id=arxiv_id,
        field=PaperField.OTHER, source_type="manual-pdf",
    )
    return meta, "pdf-only"


def path_hint(ids: Dict[str, Optional[str]]) -> str:
    return ids.get("doi") or ids.get("arxiv_id") or ids.get("title") or "unknown"



def _close_suffix(head: str) -> Optional[str]:
    """给一段 JSON 前缀算出闭合后缀（按真实的括号栈，忽略字符串内与转义）。栈非法返回 None。"""
    stack = []
    in_str = False
    esc = False
    for c in head:
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c in "{[":
            stack.append(c)
        elif c in "}]":
            if not stack or {"}": "{", "]": "["}[c] != stack.pop():
                return None
    if in_str:
        return None
    return "".join("}" if b == "{" else "]" for b in reversed(stack))


def _loads_lenient(text: str):
    """解析 LLM JSON；被 max_tokens 截断时抢救：回退到最后一个完整元素边界，按括号栈闭合后重试。"""
    s = _strip_json(text)
    try:
        return json.loads(s)
    except Exception:
        pass
    for cut in range(len(s) - 1, 0, -1):
        if s[cut] not in ",]}":
            continue
        head = s[:cut] if s[cut] == "," else s[:cut + 1]
        suffix = _close_suffix(head)
        if suffix is None:
            continue
        try:
            return json.loads(head + suffix)
        except Exception:
            continue
    return None


# ---------------- 摘要抽取 + 翻译 ----------------

_ABSTRACT_PROMPT = """下面是一篇论文的全文前段。抽取它的英文摘要（Abstract），并给出准确的中文翻译。
只输出 JSON：{{"abstract_en": "英文摘要原文", "abstract_zh": "中文翻译"}}
若找不到明确摘要，用引言首段代替。

全文前段：
{text}"""


def extract_abstract(full_text: str, llm) -> Tuple[str, str]:
    """一次 LLM 调用抽英文摘要 + 中文翻译。失败返回 ("","")。"""
    if not full_text.strip() or llm is None:
        return "", ""
    try:
        resp = llm.call(_ABSTRACT_PROMPT.format(text=full_text[:6000]),
                        max_tokens=2048, json_mode=True)
        data = json.loads(_strip_json(resp))
        return (data.get("abstract_en") or "").strip(), (data.get("abstract_zh") or "").strip()
    except Exception as e:
        logger.warning("  ⚠️ 摘要抽取失败: {}".format(e))
        return "", ""


# ---------------- 分块通读 + 汇总 ----------------

def chunk_text(full_text: str, size: int = 12000, overlap: int = 600) -> List[str]:
    """把全文切成带重叠的块（重叠避免跨块句子断裂丢信息）。"""
    text = full_text or ""
    if len(text) <= size:
        return [text] if text.strip() else []
    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        chunks.append(text[start:end])
        if end >= n:
            break
        start = end - overlap
    return chunks


# 注意：这一步**故意不提供**研究者画像。
# 2026-07 实测教训：画像放在通读 prompt 顶部会形成强锚定，模型把"这段与读者相关"写成
# "原文在讲这个"——65 篇里至少 7 次把原文的 "non-random"/"违反 MAR" 升格成 MNAR，
# 甚至把读者自己的方法构想（"缺失条件依赖结构"）写进论文的方法节。分块通读只做客观抽取，
# 与读者课题的关联留到 _SYNTH_PROMPT 汇总阶段再做，让"复述原文"和"主观联想"在流程上分开。
_CHUNK_PROMPT = """你在逐块通读一篇论文（这是第 {idx}/{total} 块，可能承接上一块）。

请从**这一块文本**里，抽取以下要点，输出 JSON。
硬要求：**只记这一块原文里确有的内容，宁缺勿造**；用原文自己的措辞，
不要把原文的表述换成更强或更专业的同义术语（例如原文写 "non-random" 就记 "non-random"，
不要写成 "MNAR"；原文写"违反某假设"就照记，不要替它归类到某个理论框架）。
{{
  "method_details": ["方法/建模/统计细节，带原文关键词或数值依据"],
  "key_numbers": ["关键数字/效应量/样本量/指标，含上下文（如 AUC=0.87, n=1200）"],
  "claims": ["作者的结论主张"],
  "limitations": ["局限/威胁/边界条件"]
}}

这一块文本：
{chunk}"""


def is_credit_error(exc) -> bool:
    """判断异常是否为 API 额度/鉴权类（402 余额不足 / 401 / quota）——这类重试无用，应回退。"""
    s = str(exc).lower()
    return any(k in s for k in (
        "402", "payment required", "insufficient", "balance", "quota",
        "401", "unauthorized", "invalid api key", "no api key"))


def deep_read_chunks(chunks: List[str], llm, model: Optional[str],
                     research_interests: str = "",
                     max_workers: int = 4) -> List[Dict[str, Any]]:
    """每块一次强模型调用，产出结构化块笔记。失败块记 error 并继续（不中断整篇）。

    额度/鉴权类失败额外标 `_api_error`，供上层决定是否回退到 subagent 对抗生成。

    `research_interests` 保留在签名里只为兼容既有调用，**本步刻意不使用**——
    见 `_CHUNK_PROMPT` 上方注释：画像进通读 prompt 会让模型把主观关联写成原文事实。

    各块相互独立，用线程池并发（`max_workers=4`，与 llm_client 的 _AGENT_SEMAPHORE
    对齐；回退串行传 1 即可）。结果按块序号回填后依序拼装，输出与串行版逐块一致。
    遇 402/401 类致命错误时取消未开始的块避免空转，但被取消块仍按块号补
    `{"_chunk": i, "_error": True, "_api_error": True}` 占位——下游 ingest_pdf 靠
    notes 长度与 n_ok/n_api 计数判 draft_status="api_error"，占位保证判定不漂移
    （串行版遇 402 会把每一块都试一遍并逐块记 _api_error，语义等价）。
    """
    total = len(chunks)
    fatal_evt = threading.Event()  # 402/401 类致命错误：所有后续调用只会重复烧钱空转

    def _read_one(i: int, chunk: str) -> Dict[str, Any]:
        # 主线程的 cancel() 与线程池取下一个任务之间有竞态（快调用时 cancel 常常
        # 赶不上）；worker 自己查事件短路，保证致命错误后不再发起任何 LLM 调用
        if fatal_evt.is_set():
            return {"_chunk": i, "_error": True, "_api_error": True}
        prompt = _CHUNK_PROMPT.format(idx=i, total=total, chunk=chunk)
        try:
            resp = llm.call(prompt, model=model, max_tokens=8192, json_mode=True)
        except Exception as e:
            logger.warning("  ⚠️ 通读块 {}/{} 失败: {}".format(i, total, e))
            api = is_credit_error(e)
            if api:
                fatal_evt.set()
            return {"_chunk": i, "_error": True, "_api_error": api}
        data = _loads_lenient(resp)
        if isinstance(data, dict):
            data["_chunk"] = i
            logger.info("  📖 通读块 {}/{} ✓".format(i, total))
            return data
        return {"_chunk": i, "_error": True}

    results: Dict[int, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_read_one, i, chunk): i
                   for i, chunk in enumerate(chunks, 1)}
        cancelled = False
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                results[i] = fut.result()
            except CancelledError:
                continue  # 被取消的块收尾统一补占位
            if not cancelled and results[i].get("_api_error"):
                cancelled = True  # 未开始的 future 直接取消（事件短路兜底竞态窗口）
                for f in futures:
                    f.cancel()

    notes: List[Dict[str, Any]] = []
    for i in range(1, total + 1):
        # 被取消/未运行的块补占位条目（见 docstring 的致命路径契约）
        notes.append(results.get(i) or {"_chunk": i, "_error": True, "_api_error": True})
    return notes


_SYNTH_PROMPT = """你在把一篇论文的逐块通读笔记汇总成一份结构化的深度精读，供研究者引用。

研究者的研究主线（**仅用于写「对我研究的联想」一节**）：
{research_interests}

⚠️ 画像的使用边界（违反会让札记库长期失真，务必遵守）：
1. 除「对我研究的联想」外，**其余各节只能复述块笔记里确有的内容**，不得引入画像里的术语、
   框架或方法构想。原文没出现的词（如 MNAR、缺失指纹、跨中心迁移等）就不许出现在
   研究问题/方法与数据/结果与效应量/图表要点/局限各节里。
2. **不要把原文措辞升格**：原文写 "non-random"、"违反 MAR 假设"、"非完全随机缺失"，
   就照原样写，不要替它归类成 MNAR 或别的理论框架。
3. **不要把研究者的想法写成论文提出的东西**。「对我研究的联想」里的每一句都要能看出
   是"我由此想到"，而不是"该文提出"。
4. 若这篇论文其实与研究主线关系不大，「对我研究的联想」写一两句实话即可，**不要硬凑关联**。

逐块笔记（JSON 数组）：
{chunk_notes}

请汇总为如下分节的精读，句级组织。按**对后续工作流的用途**给句子打 tag：
- "可引用证据"：含具体数字/效应量/可溯源结果，写作可直接取证
- "可反驳观点"：作者的主张/立场/可质疑处，是写 critique 的靶子
- "方法论借鉴"：方法/建模思路有方法学借鉴价值
其余句子 tag 置 null（如纯背景/动机）。

只输出 JSON（不要额外文字）：
{{
  "one_line": "一句话说清这篇对研究者的用处（≤30字）",
  "sections": [
    {{"heading": "研究问题", "sentences": [{{"text": "…", "tag": null}}]}},
    {{"heading": "方法与数据", "sentences": [{{"text": "…", "tag": "方法论借鉴"}}]}},
    {{"heading": "结果与效应量", "sentences": [{{"text": "…（保留关键数值）", "tag": "可引用证据"}}]}},
    {{"heading": "图表与补充材料要点", "sentences": [{{"text": "…", "tag": null}}]}},
    {{"heading": "局限与可质疑点", "sentences": [{{"text": "…", "tag": "可反驳观点"}}]}},
    {{"heading": "对我研究的联想", "sentences": [{{"text": "…", "tag": "方法论借鉴"}}]}},
    {{"heading": "逐节通读要点", "sentences": [{{"text": "…", "tag": null}}]}}
  ]
}}"""


def synthesize_deep_read(chunk_notes: List[Dict[str, Any]], llm, model: Optional[str],
                         research_interests: str) -> Tuple[Optional[CloseReading], str, bool]:
    """把块笔记汇总为扩展分节 CloseReading。返回 (CloseReading|None, one_line, api_error)。"""
    usable = [n for n in chunk_notes if not n.get("_error")]
    if not usable:
        return None, "", False
    prompt = _SYNTH_PROMPT.format(
        research_interests=research_interests or "（未提供）",
        chunk_notes=json.dumps(usable, ensure_ascii=False)[:60000])
    try:
        resp = llm.call(prompt, model=model, max_tokens=8192, json_mode=True)
    except Exception as e:
        logger.warning("  ⚠️ 汇总精读 LLM 调用失败: {}".format(e))
        return None, "", is_credit_error(e)
    one_line = ""
    try:
        one_line = (json.loads(_strip_json(resp)).get("one_line") or "").strip()
    except Exception:
        pass
    cr = parse_closeread(resp)  # 复用 closereading 的宽松解析（忽略 one_line 字段）
    if cr is not None:
        cr.from_full_text = True
        cr.model = model
        cr.source = "manual-pdf"
    return cr, one_line, False


# ---------------- 组装 segment + bundle ----------------

def build_segment(meta: PaperMetadata, abstract_en: str, abstract_zh: str,
                  close_reading: Optional[CloseReading], one_line: str) -> PaperSegment:
    """把元数据 + 摘要 + 精读组装为 PaperSegment（手动选入即 INCLUDE、最高优先级）。"""
    fd = FilterDecision(
        paper_id=meta.paper_id, title=meta.title,
        verdict="included", decision="INCLUDE",
        stage="llm_judge", reason="手动选入深度精读",
        one_line=one_line or "手动深度精读文献", confidence=1.0,
    )
    seg = PaperSegment(
        segment_id=1, paper_id=meta.paper_id,
        original_abstract=abstract_en, translated_abstract=abstract_zh,
        metadata=meta, status=DigestStatus.COMPLETED,
        priority_score=1.0, priority_reason="手动深度精读",
        filter_decision=fd,
    )
    if close_reading is not None:
        seg.close_reading = close_reading
    return seg


def bundle_path(notes_dir: Path, month: str, paper_id: str) -> Path:
    return Path(notes_dir) / "manual" / month / "{}{}".format(paper_id[:16], BUNDLE_SUFFIX)


def write_bundle(bundle_file: Path, *, status: str, month: str, pdf_path: str,
                 metadata_source: str, segment: PaperSegment,
                 close_reading_script: Optional[CloseReading],
                 close_reading_final: Optional[dict] = None,
                 cross_check_report: Optional[dict] = None,
                 draft_status: str = "ok", draft_note: str = "",
                 n_pages: Optional[int] = None) -> Path:
    """落盘/更新 bundle JSON。

    draft_status：脚本深读草稿状态——"ok"（有草稿）/ "api_error"（LLM 无额度/鉴权失败，
    应回退到 subagent 对抗生成）/ "degraded"（部分块失败）/ "empty"（无可用块）。

    n_pages：PDF 总页数，给 agent 定亲读窗口用（见 pdf_page_count 的说明）。
    """
    bundle_file = Path(bundle_file)
    bundle_file.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "status": status,
        "draft_status": draft_status,
        "draft_note": draft_note,
        "month": month,
        "pdf_path": pdf_path,
        "n_pages": n_pages,
        "metadata_source": metadata_source,
        "paper_id": segment.paper_id,
        "segment": segment.model_dump(mode="json"),
        "close_reading_script": (close_reading_script.model_dump(mode="json")
                                 if close_reading_script else None),
        "close_reading_final": close_reading_final,
        "cross_check_report": cross_check_report,
    }
    bundle_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return bundle_file


def load_bundle(bundle_file: Path) -> Dict[str, Any]:
    return json.loads(Path(bundle_file).read_text(encoding="utf-8"))


def segment_from_bundle(data: Dict[str, Any]) -> PaperSegment:
    """从 bundle 复原 PaperSegment，并用 close_reading_final（有则）覆盖精读。

    agent 可在 bundle 顶层填 `one_line`（一句话用处，≤30字）覆盖 ingest 时的兜底值——
    脚本 synthesize 失败时该值仍是占位符，agent 亲读后给一句更有检索价值的说明。
    """
    seg = PaperSegment.model_validate(data["segment"])
    final = data.get("close_reading_final")
    if final:
        seg.close_reading = CloseReading.model_validate(final)
    elif data.get("close_reading_script"):
        seg.close_reading = CloseReading.model_validate(data["close_reading_script"])
    one_line = (data.get("one_line") or "").strip()
    if one_line and seg.filter_decision is not None:
        seg.filter_decision.one_line = one_line
    return seg


def find_duplicate(index_path: Optional[Path], meta: PaperMetadata) -> Optional[Dict[str, Any]]:
    """索引里是否已有同文（按 dedup_key）。不阻断 ingest，只把结果交给上层提示。

    读索引失败**必须出声**：静默 None 与「确实没有重复」不可区分，而这条提示正是
    发现「这篇几个月前已经精读过」的唯一途径——吞掉就等于白读一遍。
    """
    if not index_path or not Path(index_path).exists():
        return None
    try:
        from .notes_index import dedup_key_fields
        key = dedup_key_fields(meta.doi, meta.arxiv_id, meta.title, fallback=meta.paper_id)
        data = json.loads(Path(index_path).read_text(encoding="utf-8"))
        for e in data.get("papers", []):
            if e.get("dedup_key") == key and not e.get("duplicate_of"):
                return {"month": e.get("month"), "note_file": e.get("note_file"),
                        "citekey": e.get("citekey")}
    except Exception as e:
        logger.warning("  ⚠️ 查重失败（{}: {}）——本篇是否与索引已有文献重复**未知**，请手动确认"
                       .format(type(e).__name__, e))
    return None


def find_final_bundle(notes_dir: Path, month: str, pdf_path: Path,
                      paper_id: str) -> Optional[Path]:
    """本月是否已有一个 final bundle 在保护这篇 PDF。没有返回 None。

    两条判据缺一不可：
      · paper_id 命中——O(1)，覆盖绝大多数情况；
      · **同一个 PDF 路径**命中——paper_id 是「标题+前三作者」的哈希，而元数据每次都要重新解析：
        Crossref 超时、DOI 抽取粘连、LLM 抽出的标题措辞不同，都会让同一个 PDF 算出不同的
        paper_id、落到不同的文件名。此时 paper_id 判据完全失效，靠这条兜住。
    """
    mdir = Path(notes_dir) / "manual" / month
    by_id = bundle_path(notes_dir, month, paper_id)
    if by_id.exists():
        try:
            if load_bundle(by_id).get("status") == "final":
                return by_id
        except Exception:
            pass
    if not mdir.is_dir():
        return None
    try:
        target = Path(pdf_path).resolve()
    except Exception:
        return None
    for bf in sorted(mdir.glob("*{}".format(BUNDLE_SUFFIX))):
        if bf == by_id:
            continue
        try:
            d = load_bundle(bf)
            if d.get("status") != "final" or not d.get("pdf_path"):
                continue
            if Path(d["pdf_path"]).resolve() == target:
                return bf
        except Exception:
            continue
    return None


def ingest_pdf(pdf_path: Path, notes_dir: Path, month: str, llm, *,
               model: Optional[str] = None, email: str = "",
               research_interests: str = "", title_override: str = "",
               index_path: Optional[Path] = None,
               force: bool = False) -> Dict[str, Any]:
    """端到端 ingest 一篇 PDF，落 draft bundle。返回 {bundle, paper_id, meta_source, dup, chunks, ...}。

    force=False 时**拒绝覆盖已 final 的 bundle**：bundle 路径由 paper_id（标题+作者的哈希）决定，
    同一个 PDF 重跑必然落回同一个文件，而 write_bundle 会把 status 写回 draft 且
    close_reading_final / cross_check_report 归 None——agent 亲读核验的成果就此静默消失。
    拦截点放在元数据解析之后、摘要与分块通读之前，顺带省掉整篇的 LLM 开销。
    """
    pdf_path = Path(pdf_path)
    logger.info("📄 ingest: {}".format(pdf_path.name))
    full_text = extract_pdf_text(pdf_path)
    if not full_text.strip():
        raise ValueError("PDF 抽不出文本（可能是扫描件/加密）：{}".format(pdf_path))
    n_pages = pdf_page_count(pdf_path)
    first_pages = full_text[:12000]
    ids = extract_pdf_ids(pdf_path, first_pages)
    meta, meta_source = resolve_metadata(
        ids, llm=llm, email=email, first_pages_text=first_pages,
        title_override=title_override)
    logger.info("  元数据来源: {} | {} | DOI={}".format(meta_source, meta.title[:50], meta.doi))

    bundle = bundle_path(notes_dir, month, meta.paper_id)
    if not force:
        guard = find_final_bundle(notes_dir, month, pdf_path, meta.paper_id)
        if guard is not None:
            old = load_bundle(guard)
            logger.warning("  ⛔ 已有 final bundle，跳过（要重跑加 --force，会丢弃已有核验成果）: {}"
                           .format(guard))
            return {
                "bundle": str(guard), "paper_id": old.get("paper_id") or meta.paper_id,
                "title": meta.title,
                "meta_source": old.get("metadata_source") or meta_source,
                "doi": meta.doi, "arxiv_id": meta.arxiv_id,
                "authors_n": len(meta.authors or []), "n_pages": old.get("n_pages") or n_pages,
                "chunks": 0, "chunk_ok": 0, "has_close_reading": True,
                "draft_status": old.get("draft_status") or "ok",
                "pdf_path": str(pdf_path.resolve()),
                "duplicate": find_duplicate(index_path, meta), "month": month,
                "skipped": "final",
            }

    abstract_en, abstract_zh = extract_abstract(full_text, llm)
    if not abstract_en:
        # 不拿标题冒充摘要：占位值与标题一字不差、无任何标记，读者无从分辨降级。
        # schema 里 original_abstract 默认空串是安全的，notes.py 渲染层会显式落成 *摘要暂无*。
        logger.warning("  摘要抽取失败，札记将显示摘要暂无")

    chunks = chunk_text(full_text)
    logger.info("  分块通读: {} 块（全文 {} 字符）".format(len(chunks), len(full_text)))
    chunk_notes = deep_read_chunks(chunks, llm, model, research_interests)
    cr, one_line, synth_api_err = synthesize_deep_read(chunk_notes, llm, model, research_interests)

    # 草稿状态：区分「API 无额度（应回退 subagent 对抗生成）」与普通降级
    n_ok = sum(1 for n in chunk_notes if not n.get("_error"))
    n_api = sum(1 for n in chunk_notes if n.get("_api_error"))
    if cr is not None:
        draft_status, draft_note = "ok", ""
    elif synth_api_err or (n_api and n_ok == 0):
        draft_status = "api_error"
        draft_note = "LLM 无额度/鉴权失败（如 402），脚本草稿不可用——应回退 subagent 对抗生成"
    elif n_ok == 0:
        draft_status, draft_note = "empty", "全文分块通读均失败，无脚本草稿"
    else:
        draft_status, draft_note = "degraded", "汇总步失败但部分块可用"

    seg = build_segment(meta, abstract_en, abstract_zh, cr, one_line)
    dup = find_duplicate(index_path, meta)

    write_bundle(bundle, status="draft", month=month, pdf_path=str(pdf_path.resolve()),
                 metadata_source=meta_source, segment=seg, close_reading_script=cr,
                 draft_status=draft_status, draft_note=draft_note, n_pages=n_pages)
    return {
        "bundle": str(bundle), "paper_id": meta.paper_id, "title": meta.title,
        "meta_source": meta_source, "doi": meta.doi, "arxiv_id": meta.arxiv_id,
        "authors_n": len(meta.authors or []), "n_pages": n_pages,
        "chunks": len(chunks), "chunk_ok": n_ok,
        "has_close_reading": cr is not None, "draft_status": draft_status,
        "pdf_path": str(pdf_path.resolve()), "duplicate": dup, "month": month,
        "skipped": None,
    }
