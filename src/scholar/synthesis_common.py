# -*- coding: utf-8 -*-
"""概念页（topics）与问答页（qa）共享的防幻觉、引用回译、哨兵合并与渲染原语。

从 topics.py 下沉至此，消除两套 2500 行模块之间的镜像同步负担；
topics.py 保留全套同名 re-export，保证既有测试与外部导入零改动。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..utils.logger import get_logger
from .grounding import build_pool, check_numbers
from .vault import (
    ROLE_LABEL,
    _SAFE_KEY,
    _YAML_WORDS,
    _y,
)

logger = get_logger(__name__)

GEN_BEGIN = (
    "<!-- BEGIN GENERATED v{} h={} · 由 {} 生成 · "
    "此块会被重建覆盖，请写在下面的「我的批注」里 -->"
)
USER_HEADING = "## 我的批注"
DEFAULT_USER_ZONE = "\n{}\n\n".format(USER_HEADING)
ROLE_EMOJI = {"citable": "🟪", "refutable": "🟦", "method": "🟩"}

_REF_RE = re.compile(r"^[Ee](\d+)$")
_BRACKET_CITE_RE = re.compile(r"\[@[^\]]+\]")
_BARE_CITE_ONLY_RE = re.compile(r"(?<![A-Za-z0-9_.\-])@[A-Za-z0-9][A-Za-z0-9_.\-:]*")
_BARE_CITE_RE = re.compile(_BRACKET_CITE_RE.pattern + "|" + _BARE_CITE_ONLY_RE.pattern)
_INLINE_REF_RE = re.compile(r"(?<![A-Za-z0-9])\[?[Ee](\d+)\]?(?![0-9])")
_SCHEMA_TERM_RE = re.compile(r"(?<![A-Za-z0-9_])(position|evidence)_([ab])(?![A-Za-z0-9_])")
_EVIDENCE_ROW_RE = re.compile(
    r"^- ([●○]) \*\*(E\d+)\*\* `\[@([^\]]+)\]`.*?\n(?:  <small>.*?</small>\n)?  > (.*)$",
    re.M,
)
_CITE_RE = re.compile(r"\[@([^\],\s]+)(?:,[^\]]*)?\]")
_CITE_LOCATOR_RE = re.compile(r"\[@([^\],\s]+)((?:,[^\]]*)?)\]")


@dataclass
class Evidence:
    ref: str
    citekey: str
    text: str
    role: Optional[str]
    section: Optional[str]
    note_file: Optional[str]
    note_line: Optional[int]
    score: float
    year: Optional[int] = None
    tier: Optional[str] = None
    title: Optional[str] = None
    pages: Optional[str] = None
    chapter: Optional[str] = None
    series: Optional[str] = None
    bucket_boost: float = 0.0


@dataclass
class ValidationReport:
    """校验战果。落进页面 frontmatter，异常值一眼可见。"""
    kept_claims: int = 0
    dropped_claims: int = 0
    invalid_refs: int = 0
    stripped_cites: int = 0
    dropped_disputes: int = 0
    used_refs: int = 0
    numbers_checked: int = 0
    numbers_derived: int = 0
    ungrounded_numbers: int = 0
    ungrounded_claims: int = 0
    ungrounded_details: List[Dict[str, Any]] = field(default_factory=list)


def flatten_linebreaks(s: str) -> str:
    """把一切会被 `str.splitlines()` 断行的字符归一成空格。"""
    return re.sub(r"[\r\n\x0b\x0c\u2028\u2029]+", " ", s or "")


def _flatten_optional_locator(v):
    return flatten_linebreaks(str(v)) if v else None


def render_evidence_block(evidences: Sequence[Evidence]) -> str:
    """喂给 LLM 的证据块。只给编号，不给 citekey。"""
    lines = []
    for e in evidences:
        meta = []
        if e.role:
            meta.append(ROLE_LABEL.get(e.role, e.role))
        if e.section:
            meta.append(e.section)
        if e.year:
            meta.append(str(e.year))
        head = "[{}]".format(e.ref)
        if meta:
            head += "（{}）".format(" · ".join(meta))
        lines.append("{} {}".format(head, e.text.strip()))
    return "\n".join(lines)


def _clean_text(s: Any, report: ValidationReport) -> str:
    """正文清洗：剥掉模型自写的引用标记（[@key] 与裸 @key）。"""
    text = str(s or "").strip()
    if not text:
        return ""
    cleaned, n = _BARE_CITE_RE.subn("", text)
    if n:
        report.stripped_cites += n
    cleaned = flatten_linebreaks(cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip().rstrip("，,、 ")


def _resolve_refs(refs: Any, ev_map: Dict[str, Evidence],
                  report: ValidationReport) -> List[str]:
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, (list, tuple)):
        return []
    out: List[str] = []
    for r in refs:
        key = str(r).strip()
        m = _REF_RE.match(key)
        if m:
            key = "E{}".format(int(m.group(1)))
        if key in ev_map:
            if key not in out:
                out.append(key)
        else:
            report.invalid_refs += 1
    return out


def _check_grounding(text: str, refs: Sequence[str], ev_map: Dict[str, Evidence],
                     report: ValidationReport, n_evidence: int,
                     where: str = "") -> None:
    if not text or not refs:
        return
    pool = build_pool(*(
        part for r in refs for part in (ev_map[r].text, ev_map[r].title)
    ))
    chk = check_numbers(text, pool, exempt=[n_evidence])
    report.numbers_checked += chk.total
    report.numbers_derived += len(chk.derived)
    if chk.ungrounded:
        report.ungrounded_numbers += len(chk.ungrounded)
        report.ungrounded_claims += 1
        report.ungrounded_details.append({
            "where": where, "numbers": list(chk.ungrounded), "text": text,
        })
        logger.warning(
            "  ⚠️ 数字未接地{}：{} · 引用 {} · {}",
            "（{}）".format(where) if where else "",
            "、".join(chk.ungrounded),
            " ".join(ev_map[r].citekey for r in refs[:4]),
            text[:90],
        )


def backtranslate_inline_refs(text: str, ev_map: Dict[str, Any],
                              report: ValidationReport,
                              used: Optional[set] = None) -> str:
    """把正文里模型顺手写的证据编号回译成 `[@citekey]`，越界的直接剔除。"""
    def _sub(m):
        ref = "E{}".format(int(m.group(1)))
        ev = ev_map.get(ref)
        if ev is None:
            report.invalid_refs += 1
            return ""
        if used is not None:
            used.add(ref)
        pages = getattr(ev, "pages", None)
        return "[@{}, p. {}]".format(ev.citekey, pages) if pages else "[@{}]".format(ev.citekey)

    out = _INLINE_REF_RE.sub(_sub, text)
    return re.sub(r"\s{2,}", " ", out).strip()


def _scrub_schema_terms(text: str) -> str:
    return _SCHEMA_TERM_RE.sub(lambda m: "一方" if m.group(2) == "a" else "另一方", text)


def _json_safe(v, _depth: int = 0):
    if _depth > 32:
        return str(v)
    if isinstance(v, dict):
        out = {}
        n_coll = n_long = 0
        for k, x in v.items():
            if isinstance(k, str):
                jk = k
            elif k is None or isinstance(k, (int, float, bool)):
                jk = json.dumps(k)
            else:
                jk = str(k)
            if len(jk) > 512:
                n_long += 1
                continue
            if jk in out:
                n_coll += 1
            out[jk] = _json_safe(x, _depth + 1)
        if n_coll:
            logger.warning("frontmatter 嵌套键净化后发生 {} 处同形碰撞，后写的覆盖先写的", n_coll)
        if n_long:
            logger.warning("frontmatter 嵌套键过长（>512 字符）{} 个，已跳过", n_long)
        return out
    if isinstance(v, (list, tuple)):
        return [_json_safe(x, _depth + 1) for x in v]
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def _render_frontmatter(items: Dict[str, Any]) -> str:
    lines = ["---"]
    for k, v in items.items():
        ks = str(k)
        if len(ks) > 512:
            logger.warning("frontmatter 键过长（{} 字符 > 512），已跳过", len(ks))
            continue
        key = ks if (_SAFE_KEY.match(ks) and ks.lower() not in _YAML_WORDS) else _y(ks)
        if isinstance(v, list):
            if not v:
                lines.append("{}: []".format(key))
            else:
                lines.append("{}:".format(key))
                lines += ["  - {}".format(_y(x)) for x in v]
        elif isinstance(v, dict):
            lines.append("{}: {}".format(key, json.dumps(_json_safe(v), ensure_ascii=False)))
        else:
            lines.append("{}: {}".format(key, _y(v)))
    lines.append("---")
    return "\n".join(lines)


def to_wiki_links(text: str, filenames: Dict[str, str]) -> str:
    """`[@citekey]` -> `[[文件名|citekey]]`，让概念页/问答页连进 Obsidian 关系图。"""
    def _sub(m):
        key, loc = m.group(1), m.group(2)
        fname = filenames.get(key)
        if not fname:
            return m.group(0)
        display = key + loc
        return "[[{}|{}]]".format(fname, display) if fname != display else "[[{}]]".format(key)
    return _CITE_LOCATOR_RE.sub(_sub, text)
