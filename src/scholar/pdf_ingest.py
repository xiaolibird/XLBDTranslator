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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .schema import (
    PaperSegment, PaperMetadata, PaperField, DigestStatus,
    FilterDecision, CloseReading,
)
from .closereading import pdf_to_text, parse_closeread
from ..utils.logger import get_logger

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


def _strip_json(text: str) -> str:
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


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


_CHUNK_PROMPT = """你在为一位研究者逐块精读一篇论文（这是第 {idx}/{total} 块，可能承接上一块）。
研究者的研究主线：
{research_interests}

请从**这一块文本**里，抽取以下要点（只记这一块里确有的，宁缺勿造），输出 JSON：
{{
  "method_details": ["方法/建模/统计细节，带原文关键词或数值依据"],
  "key_numbers": ["关键数字/效应量/样本量/指标，含上下文（如 AUC=0.87, n=1200）"],
  "claims": ["作者的结论主张"],
  "limitations": ["局限/威胁/边界条件"],
  "relevance": ["与研究主线的关联点（方法可借鉴/发现可引用/背景）"]
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
                     research_interests: str) -> List[Dict[str, Any]]:
    """每块一次强模型调用，产出结构化块笔记。失败块记 error 并继续（不中断整篇）。

    额度/鉴权类失败额外标 `_api_error`，供上层决定是否回退到 subagent 对抗生成。
    """
    notes: List[Dict[str, Any]] = []
    total = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        prompt = _CHUNK_PROMPT.format(
            idx=i, total=total,
            research_interests=research_interests or "（未提供）",
            chunk=chunk)
        try:
            resp = llm.call(prompt, model=model, max_tokens=8192, json_mode=True)
            data = _loads_lenient(resp)
            if isinstance(data, dict):
                data["_chunk"] = i
                notes.append(data)
                logger.info("  📖 通读块 {}/{} ✓".format(i, total))
                continue
            notes.append({"_chunk": i, "_error": True})
        except Exception as e:
            logger.warning("  ⚠️ 通读块 {}/{} 失败: {}".format(i, total, e))
            notes.append({"_chunk": i, "_error": True, "_api_error": is_credit_error(e)})
    return notes


_SYNTH_PROMPT = """你在把一篇论文的逐块通读笔记汇总成一份结构化的深度精读，供研究者引用。
研究者的研究主线：
{research_interests}

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
                 draft_status: str = "ok", draft_note: str = "") -> Path:
    """落盘/更新 bundle JSON。

    draft_status：脚本深读草稿状态——"ok"（有草稿）/ "api_error"（LLM 无额度/鉴权失败，
    应回退到 subagent 对抗生成）/ "degraded"（部分块失败）/ "empty"（无可用块）。
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


def ingest_pdf(pdf_path: Path, notes_dir: Path, month: str, llm, *,
               model: Optional[str] = None, email: str = "",
               research_interests: str = "", title_override: str = "",
               index_path: Optional[Path] = None) -> Dict[str, Any]:
    """端到端 ingest 一篇 PDF，落 draft bundle。返回 {bundle, paper_id, meta_source, dup, chunks, ...}。"""
    pdf_path = Path(pdf_path)
    logger.info("📄 ingest: {}".format(pdf_path.name))
    full_text = extract_pdf_text(pdf_path)
    if not full_text.strip():
        raise ValueError("PDF 抽不出文本（可能是扫描件/加密）：{}".format(pdf_path))
    first_pages = full_text[:12000]
    ids = extract_pdf_ids(pdf_path, first_pages)
    meta, meta_source = resolve_metadata(
        ids, llm=llm, email=email, first_pages_text=first_pages,
        title_override=title_override)
    logger.info("  元数据来源: {} | {} | DOI={}".format(meta_source, meta.title[:50], meta.doi))

    abstract_en, abstract_zh = extract_abstract(full_text, llm)
    if not abstract_en:
        abstract_en = meta.title  # 兜底不留空

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

    # 查已有索引里是否已有同文（不阻断，提示 agent）
    dup = None
    if index_path and Path(index_path).exists():
        try:
            from .notes_index import dedup_key_fields
            key = dedup_key_fields(meta.doi, meta.arxiv_id, meta.title, fallback=meta.paper_id)
            data = json.loads(Path(index_path).read_text(encoding="utf-8"))
            for e in data.get("papers", []):
                if e.get("dedup_key") == key and not e.get("duplicate_of"):
                    dup = {"month": e.get("month"), "note_file": e.get("note_file"),
                           "citekey": e.get("citekey")}
                    break
        except Exception:
            pass

    bundle = bundle_path(notes_dir, month, meta.paper_id)
    write_bundle(bundle, status="draft", month=month, pdf_path=str(pdf_path.resolve()),
                 metadata_source=meta_source, segment=seg, close_reading_script=cr,
                 draft_status=draft_status, draft_note=draft_note)
    return {
        "bundle": str(bundle), "paper_id": meta.paper_id, "title": meta.title,
        "meta_source": meta_source, "doi": meta.doi, "arxiv_id": meta.arxiv_id,
        "chunks": len(chunks), "chunk_ok": n_ok,
        "has_close_reading": cr is not None, "draft_status": draft_status,
        "pdf_path": str(pdf_path.resolve()), "duplicate": dup, "month": month,
    }
