# -*- coding: utf-8 -*-
"""引文回验：把 LLM/agent 产出的逐字引句 grep 回 PDF 原文的被引页。

为什么必须是确定性检查、而不是再叫一个 LLM 当裁判：FABLES（arXiv 2404.01261）
在书级摘要上实测，**所有** LLM 忠实性裁判都无法可靠识别不忠实断言，而书级摘要的
主要错误类别恰恰是「需要全书上下文才能发现的不忠实引用」。字符串比对不聪明，
但它不会说谎，且成本为零。

口径：
- 归一化（NFKC / 连字 ﬁ ﬂ / 断行连字符 / 花引号 / 空白折叠）后做**精确子串**匹配。
  不做模糊匹配——模糊阈值一旦放开，「改了一个数字的引句」会被判通过，而那正是最该抓的错。
- 匹配范围是被引页 ±slack 页（默认 1）：版面跨页与页码偏移各差一页是常态，
  但放到全书就等于不验页码了。
- 不匹配**只标记不静默丢**：产出 flagged 清单交人抽查。静默丢会让「引句被悄悄删掉」
  和「引句本来就没有」看起来一模一样。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

# 归一化替换表。PDF 抽出的文本里这些字符与键盘字符不等价，但引句里人（或 LLM）
# 往往写成键盘字符，不归一会造成大批假阴性。
_LIGATURES = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
    "ﬅ": "st", "ﬆ": "st",
}
_QUOTES = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "′": "'", "″": '"',
}
_DASHES = {"‐": "-", "‑": "-", "‒": "-", "–": "-",
           "—": "-", "―": "-", "−": "-"}

# 断行连字符：`impu-\ntation` → `imputation`。只在「小写字母-换行-小写字母」时合并，
# 避免把真正的复合词连字符（missing-data\nmechanism）也吃掉。
_LINEBREAK_HYPHEN_RE = re.compile(r"([a-z])-\s*\n\s*([a-z])")
_WS_RE = re.compile(r"\s+")


def normalize(text: Optional[str]) -> str:
    """归一到可比对形态：NFKC → 断行连字符合并 → 连字/引号/破折号替换 → 空白折叠。"""
    s = unicodedata.normalize("NFKC", text or "")
    s = _LINEBREAK_HYPHEN_RE.sub(r"\1\2", s)
    for table in (_LIGATURES, _QUOTES, _DASHES):
        for src, dst in table.items():
            if src in s:
                s = s.replace(src, dst)
    return _WS_RE.sub(" ", s).strip()


# 页码锚形态："247" / "241-259" / "247,249"。取其中最小与最大页作为检索区间端点。
_PAGE_NUM_RE = re.compile(r"\d+")


def parse_page_anchor(anchor: Optional[str]) -> List[int]:
    """页码锚串 → 页号列表（升序去重）。认不出返回空列表。"""
    nums = [int(n) for n in _PAGE_NUM_RE.findall(str(anchor or ""))]
    return sorted(set(nums))


@dataclass
class QuoteCheck:
    """单条引句的回验结果。"""
    quote: str
    anchor: Optional[str]
    ok: bool
    reason: str = ""
    found_page: Optional[int] = None      # 实际命中的**原书**页码（可与 anchor 差一页）
    section: str = ""
    tag: Optional[str] = None


@dataclass
class ChapterQuoteReport:
    """一章的回验汇总。pass_rate 是 finalize 硬门的判据。"""
    chapter: Optional[int] = None
    total: int = 0
    passed: int = 0
    checks: List[QuoteCheck] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return (self.passed / self.total) if self.total else 1.0

    @property
    def flagged(self) -> List[QuoteCheck]:
        return [c for c in self.checks if not c.ok]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chapter": self.chapter,
            "total": self.total,
            "passed": self.passed,
            "pass_rate": round(self.pass_rate, 4),
            "flagged": [
                {"quote": c.quote, "anchor": c.anchor, "reason": c.reason,
                 "section": c.section, "tag": c.tag}
                for c in self.flagged
            ],
        }


class PageIndex:
    """按**原书印刷页码**寻址的归一化页文本。

    printed_page = pdf_page_index + 1 + offset。offset 由 manifest 给出（正文首页的
    印刷页码与 PDF 页序之差），因为教科书的罗马数字前言会让两者恒差十几页——
    旧手工 digest 每份文件各自手推一次这个偏移，且同书不同文件推出过不同值。
    """

    def __init__(self, pages: Sequence[str], offset: int = 0):
        self._norm = [normalize(p) for p in pages]
        self.offset = int(offset)

    def __len__(self) -> int:
        return len(self._norm)

    @property
    def printed_range(self):
        """(最小, 最大) 可寻址的印刷页码。"""
        return (1 + self.offset, len(self._norm) + self.offset)

    def printed(self, printed_page: int) -> str:
        """取某印刷页的归一化文本；越界返回空串。"""
        idx = int(printed_page) - 1 - self.offset
        return self._norm[idx] if 0 <= idx < len(self._norm) else ""

    def find(self, needle: str, pages: Sequence[int]) -> Optional[int]:
        """在给定印刷页集合里找精确子串，返回命中页；未命中返回 None。"""
        for p in pages:
            if needle and needle in self.printed(p):
                return p
        return None

    def find_anywhere(self, needle: str) -> Optional[int]:
        """全书扫描（只用于给未命中的引句写出「其实在第 N 页」的可操作提示）。"""
        for i, text in enumerate(self._norm):
            if needle and needle in text:
                return i + 1 + self.offset
        return None


# 太短的引句在任何一页都可能偶然命中，验了等于没验（"the model" 必然通过）。
# 32 字符是经验下限：够放下一个完整的从句，又不至于把术语定义式短引句全挡掉。
MIN_QUOTE_CHARS = 32


def verify_quote(quote: str, anchor: Optional[str], index: PageIndex,
                 slack: int = 1, section: str = "", tag: Optional[str] = None) -> QuoteCheck:
    """回验单条引句。slack=允许的页码偏差（默认 ±1 页）。"""
    needle = normalize(quote)
    if len(needle) < MIN_QUOTE_CHARS:
        return QuoteCheck(quote, anchor, False, "引句过短（<{} 字符），无法有效回验"
                          .format(MIN_QUOTE_CHARS), section=section, tag=tag)
    anchored = parse_page_anchor(anchor)
    if not anchored:
        hit = index.find_anywhere(needle)
        if hit is not None:
            return QuoteCheck(quote, anchor, False,
                              "缺页码锚（原文在 p.{}）".format(hit), found_page=hit,
                              section=section, tag=tag)
        return QuoteCheck(quote, anchor, False, "缺页码锚，且全书未找到该引句",
                          section=section, tag=tag)

    lo, hi = anchored[0] - slack, anchored[-1] + slack
    hit = index.find(needle, range(lo, hi + 1))
    if hit is not None:
        return QuoteCheck(quote, anchor, True, "", found_page=hit, section=section, tag=tag)

    elsewhere = index.find_anywhere(needle)
    if elsewhere is not None:
        return QuoteCheck(quote, anchor, False,
                          "页码锚错误：标注 p.{}，实际在 p.{}".format(anchor, elsewhere),
                          found_page=elsewhere, section=section, tag=tag)
    return QuoteCheck(quote, anchor, False,
                      "原文 p.{}±{} 未找到该引句（可能被改写或杜撰）".format(anchor, slack),
                      section=section, tag=tag)


# 只回验**逐字原文**引句：中文归纳句是读者的转述，本就不该逐字出现在英文原书里，
# 拿去 grep 只会产出一堆无意义的 flagged。判据是「句中含成对英文引号包裹的片段」。
_VERBATIM_RE = re.compile(r'"([^"]{%d,})"' % MIN_QUOTE_CHARS)


def extract_verbatim(text: str) -> List[str]:
    """从一句札记里抽出被英文双引号包裹的逐字引文片段（可有多段）。"""
    return [m.group(1).strip() for m in _VERBATIM_RE.finditer(normalize(text))]


def verify_close_reading(cr, index: PageIndex, slack: int = 1,
                         chapter: Optional[int] = None) -> ChapterQuoteReport:
    """回验一份 CloseReading 里所有带逐字引文的句子。

    只对「含逐字引文」的句子计数：全中文的归纳句既不进分母也不进分子，
    否则 pass_rate 会被大量不该验的句子稀释成一个没有判别力的数字。
    """
    report = ChapterQuoteReport(chapter=chapter)
    for sec in (getattr(cr, "sections", None) or []):
        for st in (getattr(sec, "sentences", None) or []):
            for frag in extract_verbatim(getattr(st, "text", "") or ""):
                chk = verify_quote(frag, getattr(st, "page", None), index, slack=slack,
                                   section=getattr(sec, "heading", ""),
                                   tag=getattr(st, "tag", None))
                report.checks.append(chk)
                report.total += 1
                report.passed += 1 if chk.ok else 0
    return report
