# -*- coding: utf-8 -*-
"""导入旧的手工书籍 digest（thesis-project/books/digest/<Book>/*.md）为**草稿轨**。

关键裁决（2026-08-26 实测后修正原计划）：旧 digest 只进 `close_reading_script`，
不进 `close_reading_final`，status 保持 draft —— 即扮演论文链路里「脚本草稿」的角色，
仍然要由 agent 亲读该章后写终稿。

原计划是「回验旧引句后直接转成章记录，免去 agent 重读」。实测否掉了它：
18 份 Rubin 旧 digest（约 230KB）里**逐字英文引句总共只有 7 条**，153 处页码引用
绝大多数是章节/公式指针（「§6.3」「Eq. 6.46」），不附在可引用断言上。也就是说
旧产物几乎没有可回验的东西——而它们恰恰是分块 LLM 通读的产物，正是双轨协议
要拿亲读去校的那一轨。把它们当终稿入库，等于把未经核验的归纳直接变成可引用文献。

它们仍然有价值，只是价值在「草稿」而不在「证据」：agent 亲读时可以拿它当基线做
逐条对照（同 read-paper 协议第 3 步：独立在先、对照在后）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..utils.logger import get_logger
from .book_ingest import BookManifest, Chapter

logger = get_logger(__name__)

# 文件名里的 PDF 页范围："Rubin_143_160.md" / "101_120.md" / "ch4_53_72.md"
_FNAME_RANGE_RE = re.compile(r"(?:^|_)(\d{1,4})_(\d{1,4})(?=\.|$)")
# 正文里的印刷页码引用："pp.143–160" / "p. 4" / "（pp. 8-12）"
_PAGE_REF_RE = re.compile(r"pp?\.\s?(\d{1,4})(?:\s?[–—-]\s?(\d{1,4}))?")


@dataclass
class LegacyDigest:
    """一份旧 digest 文件的解析结果。"""
    path: Path
    pdf_start: Optional[int] = None
    pdf_end: Optional[int] = None
    printed_start: Optional[int] = None
    printed_end: Optional[int] = None
    text: str = ""
    quotes: List[Tuple[str, Optional[str]]] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name


def _first_lines(text: str, n: int = 12) -> str:
    return "\n".join(text.splitlines()[:n])


def parse_legacy_file(path: Path) -> LegacyDigest:
    """解析一份旧 digest：文件名给 PDF 页范围，头部若干行给印刷页范围。"""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    d = LegacyDigest(path=path, text=text)

    m = _FNAME_RANGE_RE.search(path.stem)
    if m:
        d.pdf_start, d.pdf_end = int(m.group(1)), int(m.group(2))

    # 头部的印刷页范围（"第6章 pp.143–160 摘要" / "本文件覆盖原书 pp. 86-106"）
    pm = _PAGE_REF_RE.search(_first_lines(text))
    if pm:
        d.printed_start = int(pm.group(1))
        d.printed_end = int(pm.group(2)) if pm.group(2) else int(pm.group(1))

    d.quotes = extract_legacy_quotes(text)
    return d


# 逐字英文引句：整段被英文双引号包住、且以大写字母开头、足够长。
# 中文归纳里也常用中文引号「」和成对英文引号包中文——用「首字符是 ASCII 大写字母」
# 把它们排除，否则回验分母里全是本就不该在英文原书里逐字出现的中文句。
_EN_QUOTE_RE = re.compile(r'"([A-Z][^"]{39,})"')


def extract_legacy_quotes(text: str) -> List[Tuple[str, Optional[str]]]:
    """抽出 (逐字引句, 页码锚) 对。页码锚取该引句所在行/上一行里的 p./pp. 引用。"""
    out: List[Tuple[str, Optional[str]]] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        for qm in _EN_QUOTE_RE.finditer(line):
            ctx = "\n".join(lines[max(0, i - 2):i + 2])
            pm = _PAGE_REF_RE.search(ctx)
            anchor = None
            if pm:
                anchor = pm.group(1) if not pm.group(2) else "{}-{}".format(pm.group(1),
                                                                           pm.group(2))
            out.append((qm.group(1).strip(), anchor))
    return out


def infer_offset(digests: Sequence[LegacyDigest]) -> Optional[int]:
    """由「文件名 PDF 页 vs 头部印刷页」推 page_offset，并要求各文件一致。

    旧 digest 每份各自手推一次这个偏移，且同书不同文件推出过不同值——所以这里
    不取某一份的值，而是取众数并把分歧报出来。
    """
    votes: Dict[int, int] = {}
    for d in digests:
        if d.pdf_start and d.printed_start:
            off = d.printed_start - d.pdf_start
            votes[off] = votes.get(off, 0) + 1
    if not votes:
        return None
    best = max(votes.items(), key=lambda kv: kv[1])
    if len(votes) > 1:
        logger.warning("⚠️ 旧 digest 的页偏移不一致：{}（取众数 {:+d}）"
                       .format({k: v for k, v in sorted(votes.items())}, best[0]))
    return best[0]


def map_to_chapters(digests: Sequence[LegacyDigest], manifest: BookManifest
                    ) -> Dict[int, List[LegacyDigest]]:
    """把旧 digest（按 PDF 页窗切）映射到章：页窗与章区间有重叠即归入该章。

    一对多是常态——20 页窗横跨两章正是旧做法的病灶，所以一份 digest 可能进入多个章的草稿。
    """
    out: Dict[int, List[LegacyDigest]] = {}
    for ch in manifest.chapter_objs():
        for d in digests:
            if d.pdf_start is None or d.pdf_end is None:
                continue
            if d.pdf_start <= ch.pdf_end and d.pdf_end >= ch.pdf_start:
                out.setdefault(ch.number, []).append(d)
    return out


def coverage_gaps(digests: Sequence[LegacyDigest], manifest: BookManifest
                  ) -> List[Tuple[int, int]]:
    """旧 digest 未覆盖的 PDF 页区间（只算正文章节范围内的）。"""
    covered = set()
    for d in digests:
        if d.pdf_start and d.pdf_end:
            covered.update(range(d.pdf_start, d.pdf_end + 1))
    body = set()
    for ch in manifest.chapter_objs():
        body.update(range(ch.pdf_start, ch.pdf_end + 1))
    missing = sorted(body - covered)
    gaps: List[Tuple[int, int]] = []
    for p in missing:
        if gaps and p == gaps[-1][1] + 1:
            gaps[-1] = (gaps[-1][0], p)
        else:
            gaps.append((p, p))
    return gaps


def audit_legacy_quotes(digests: Sequence[LegacyDigest], page_index) -> Dict[str, Any]:
    """对旧 digest 里的逐字引句跑回验（只为体检，不决定入库）。"""
    from .quote_verify import verify_quote
    checks = []
    for d in digests:
        for q, anchor in d.quotes:
            chk = verify_quote(q, anchor, page_index, section=d.name)
            checks.append({"file": d.name, "quote": q[:160], "anchor": anchor,
                           "ok": chk.ok, "reason": chk.reason,
                           "found_page": chk.found_page})
    total = len(checks)
    passed = sum(1 for c in checks if c["ok"])
    return {"total": total, "passed": passed,
            "pass_rate": round(passed / total, 4) if total else 1.0,
            "checks": checks}


def legacy_script_note(chapter: Chapter, digests: Sequence[LegacyDigest],
                       max_chars: int = 40000) -> str:
    """把归入某章的旧 digest 拼成该章的「脚本草稿」文本，供 agent 对照。"""
    parts = ["> 以下为**旧手工 digest**（20 页盲切时代的产物），仅作亲读时的对照基线。",
             "> 它没有经过引文回验，且是分块 LLM 通读的产物——正是双轨协议要校的那一轨。",
             "> 与原文冲突时，以 PDF 为准。", ""]
    for d in digests:
        parts.append("<!-- 来源：{}（PDF pp.{}-{}） -->".format(d.name, d.pdf_start, d.pdf_end))
        parts.append(d.text)
        parts.append("")
    text = "\n".join(parts)
    return text[:max_chars]
