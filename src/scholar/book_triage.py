# -*- coding: utf-8 -*-
"""章节分诊：给每一章对论文各研究问题打 0–3 分，产出覆盖热力图与深读队列。

为什么要有这一层：JAMA Users' Guides 726 页 29 章，与「EHR 缺失机制」直接相关的
大概只有几章；旧做法要么全书盲切（成本不可承受，实际只读了 4% 就停了），要么凭印象
挑章（挑漏了不会有人发现）。分诊让「不读哪些章」成为一个**有记录、可复核**的决定，
而不是一次沉默的放弃。

分数口径（写进 prompt，别改一处忘另一处）：
  3 = 直接回答该问题：有可直接引用的定义/定理/数据/反例
  2 = 实质相关：提供该问题所需的方法或前提，但不直接回答
  1 = 边缘相关：术语或背景重叠，引用价值低
  0 = 无关

只喂章标题 + 子节标题 + 首尾若干页正文，不喂全章：分诊要的是「值不值得深读」，
把全章喂进去等于先付一遍深读的钱。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..utils.json_tools import loads_lenient
from ..utils.logger import get_logger
from .book_ingest import BookManifest, Chapter

logger = get_logger(__name__)

# 深读门槛：≥ 此分数的章进深读队列。2 = 「实质相关」，1 = 「术语重叠」——
# 把 1 也读进来等于放弃分诊（教科书里几乎每章都能蹭上一个术语）。
DEEP_READ_MIN_SCORE = 2

SCORE_LEGEND = {
    3: "直接回答：有可直接引用的定义/定理/数据/反例",
    2: "实质相关：提供所需方法或前提，但不直接回答",
    1: "边缘相关：术语或背景重叠，引用价值低",
    0: "无关",
}

# 分诊时喂进去的章内正文页数（首 N 页 + 末 N 页）。首页有本章的问题陈述，
# 末页常是小结——这两处最能判定主题，中间的推导对「值不值得读」没有增量。
PROBE_HEAD_PAGES = 3
PROBE_TAIL_PAGES = 2
# 中段每隔几页抽一页。8 页步长下：14 页的章多看 1 页、54 页的章多看 6 页，
# 探针覆盖率从 9% 升到 20%，而 prompt 长度只增不到一倍。
PROBE_MID_STRIDE = 8
PROBE_PAGE_CHARS = 2500


_TRIAGE_PROMPT = """{manifest_prefix}

# 任务
你在为一位研究者做**章节分诊**：判断这一章对他论文的各个研究问题是否值得深读。
只做相关性判断，不要写读书笔记。

## 研究者的研究主线
{interests}

## 待评分的研究问题
{questions}

## 本章信息
章号：{number}（书上印为 {label}）
标题：{title}
原书页码：pp.{p_start}-{p_end}（共 {n_pages} 页）
子节：{subsections}

## 本章正文节选（首尾若干页 + 中段等距抽样；**不是全章**）
{probe}

# 评分标准（严格按此口径，不要通货膨胀）
3 = 直接回答该问题：有可直接引用的定义/定理/数据/反例
2 = 实质相关：提供该问题所需的方法或前提，但不直接回答
1 = 边缘相关：术语或背景重叠，引用价值低
0 = 无关

多数章对多数问题应当是 0 或 1。若给 3，必须在 why 里指出具体是哪个定义/结论/数据。

# 输出（只输出 JSON，不要解释）
{{
  "scores": {{ {score_keys} }},
  "why": {{ "问题slug": "一句话依据（给 0 分可留空）" }},
  "chapter_gist": "本章在做什么，一句话（中文，不超过 60 字）",
  "worth_reading": true/false
}}"""


@dataclass
class ChapterTriage:
    """一章的分诊结果。"""
    number: int
    title: str
    scores: Dict[str, int] = field(default_factory=dict)
    why: Dict[str, str] = field(default_factory=dict)
    gist: str = ""
    error: str = ""

    @property
    def max_score(self) -> int:
        return max(self.scores.values()) if self.scores else 0

    @property
    def selected(self) -> bool:
        """是否进深读队列。出错的章按「选中」处理——分诊失败不等于不相关，
        宁可多读一章，也不要让一次 LLM 故障静默地把一章从视野里抹掉。"""
        return bool(self.error) or self.max_score >= DEEP_READ_MIN_SCORE

    def to_dict(self) -> Dict[str, Any]:
        return {"number": self.number, "title": self.title, "scores": self.scores,
                "why": self.why, "gist": self.gist, "error": self.error,
                "max_score": self.max_score, "selected": self.selected}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ChapterTriage":
        return cls(number=int(d["number"]), title=d.get("title", ""),
                   scores={k: int(v) for k, v in (d.get("scores") or {}).items()},
                   why=d.get("why") or {}, gist=d.get("gist", ""),
                   error=d.get("error", ""))


def chapter_probe(pages: Sequence[str], ch: Chapter,
                  head: int = PROBE_HEAD_PAGES, tail: int = PROBE_TAIL_PAGES,
                  stride: int = PROBE_MID_STRIDE) -> str:
    """取一章的首尾 + 中段等距抽样页作为分诊探针（每页截断，避免病态页吃光预算）。

    中段抽样不是可选的锦上添花：首尾各几页对 6 页的章覆盖率是 83%，对 54 页的章
    只有 9%。实测代价——Little & Rubin 第 15 章（印刷 pp.351-404）在 shadow-variable
    轴上被判 0 分，而 proxy pattern-mixture（p.378）、subsample ignorable likelihood
    （pp.379-380）、tipping point 分析（pp.400-402）全都在那一章的中段，正是本项目
    最需要的内容。首尾探针**结构上**看不见它们。
    """
    covered = list(range(ch.pdf_start, min(ch.pdf_start + head, ch.pdf_end + 1)))
    tail_start = max(ch.pdf_end - tail + 1, (covered[-1] + 1) if covered else ch.pdf_start)
    covered += list(range(tail_start, ch.pdf_end + 1))
    mid_lo = (covered[head - 1] + 1) if len(covered) >= head else ch.pdf_start
    mid = [p for p in range(mid_lo, tail_start, max(1, stride))]
    idxs = sorted(set(covered) | set(mid))
    out = []
    for p in idxs:
        i = p - 1
        if 0 <= i < len(pages):
            out.append("[[PDF p.{}]] {}".format(p, (pages[i] or "")[:PROBE_PAGE_CHARS]))
    return "\n\n".join(out)


def _questions_block(specs) -> str:
    return "\n".join("- {}（slug: {}）：{}".format(s.title, s.slug, s.question)
                     for s in specs)


def triage_chapter(ch: Chapter, manifest: BookManifest, pages: Sequence[str],
                   specs, llm, interests: str, model: Optional[str] = None) -> ChapterTriage:
    """对一章跑分诊。LLM 失败时返回带 error 的结果（selected=True，见该属性文档）。"""
    slugs = [s.slug for s in specs]
    p_start, p_end = ch.printed_range(manifest.page_offset)
    prompt = _TRIAGE_PROMPT.format(
        manifest_prefix=manifest.prompt_prefix(),
        interests=interests,
        questions=_questions_block(specs),
        number=ch.number, label=ch.label or "无", title=ch.title,
        p_start=p_start, p_end=p_end, n_pages=ch.n_pages,
        subsections="；".join(ch.subsections[:25]) or "（无子节）",
        probe=chapter_probe(pages, ch),
        score_keys=", ".join('"{}": 0'.format(s) for s in slugs))
    try:
        raw = llm.call(prompt, model=model, json_mode=True, max_tokens=1500)
        data = loads_lenient(raw)
    except Exception as exc:                      # noqa: BLE001
        logger.warning("  ⚠️ 第 {} 章分诊失败：{}".format(ch.number, exc))
        return ChapterTriage(number=ch.number, title=ch.title, error=str(exc)[:200])

    raw_scores = (data or {}).get("scores") or {}
    scores: Dict[str, int] = {}
    for slug in slugs:                            # 按配置的 slug 收，LLM 编出来的键丢弃
        try:
            scores[slug] = max(0, min(3, int(raw_scores.get(slug, 0))))
        except (TypeError, ValueError):
            scores[slug] = 0
    return ChapterTriage(number=ch.number, title=ch.title, scores=scores,
                         why={k: str(v)[:300] for k, v in ((data or {}).get("why") or {}).items()
                              if k in scores},
                         gist=str((data or {}).get("chapter_gist") or "")[:200])


def triage_book(manifest: BookManifest, pages: Sequence[str], specs, llm,
                interests: str, model: Optional[str] = None,
                only: Optional[Sequence[int]] = None,
                max_workers: int = 4) -> Dict[int, ChapterTriage]:
    """对全书逐章分诊（并发）。返回 {章号: ChapterTriage}。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    chapters = [c for c in manifest.chapter_objs()
                if only is None or c.number in set(only)]
    out: Dict[int, ChapterTriage] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(triage_chapter, c, manifest, pages, specs, llm, interests, model): c
                for c in chapters}
        for fut in as_completed(futs):
            c = futs[fut]
            try:
                res = fut.result()
            except Exception as exc:              # noqa: BLE001
                res = ChapterTriage(number=c.number, title=c.title, error=str(exc)[:200])
            out[res.number] = res
            logger.info("  分诊 ch{:>2} max={} {}".format(
                res.number, res.max_score, res.title[:48]))
    return out


# ---------------- 覆盖热力图 ----------------

_HEAT = {0: "·", 1: "░", 2: "▒", 3: "█"}


def heatmap_md(manifest: BookManifest, triage: Dict[int, ChapterTriage], specs) -> str:
    """章 × 研究问题的覆盖热力图（markdown）。

    这张表的价值不在被选中的章，而在**空列**：某个研究问题在全书没有一章 ≥2，
    说明这本书回答不了它——那是一条该去别处找证据的结论，而不是这本书读得不够。
    """
    slugs = [s.slug for s in specs]
    lines = ["# {} · 章节分诊热力图".format(manifest.title or manifest.slug), "",
             "> 分数：{}".format(" / ".join("{}={}".format(k, v)
                                            for k, v in sorted(SCORE_LEGEND.items(), reverse=True))),
             "> 深读门槛：max ≥ {}".format(DEEP_READ_MIN_SCORE), ""]
    lines.append("| 章 | 标题 | " + " | ".join(s.title for s in specs) + " | max | 深读 |")
    lines.append("|---:|---|" + "|".join([":--:"] * len(specs)) + "|:--:|:--:|")
    for ch in manifest.chapter_objs():
        t = triage.get(ch.number)
        cells = []
        for slug in slugs:
            sc = (t.scores.get(slug, 0) if t else 0)
            cells.append("{} {}".format(_HEAT.get(sc, "·"), sc))
        lo, hi = ch.printed_range(manifest.page_offset)
        mark = "❗" if (t and t.error) else ("✅" if (t and t.selected) else "")
        lines.append("| {} | {} <br><sub>pp.{}-{}</sub> | {} | {} | {} |".format(
            ch.number, ch.title.replace("|", "/")[:60], lo, hi,
            " | ".join(cells), (t.max_score if t else 0), mark))
    lines.append("")

    # 空列体检：没有任何一章能回答的问题
    empty = [s.title for s in specs
             if not any((triage.get(c.number) or ChapterTriage(0, "")).scores.get(s.slug, 0)
                        >= DEEP_READ_MIN_SCORE for c in manifest.chapter_objs())]
    lines.append("## 覆盖缺口")
    lines.append("")
    if empty:
        lines.append("以下研究问题在本书**没有任何一章**达到深读门槛——结论是「这本书回答不了它」，"
                     "应去别处取证，而不是把门槛调低：")
        lines.extend("- {}".format(e) for e in empty)
    else:
        lines.append("每个研究问题都至少有一章达到深读门槛。")
    lines.append("")

    sel = [c for c in manifest.chapter_objs() if (triage.get(c.number) or ChapterTriage(0, "")).selected]
    total_pages = sum(c.n_pages for c in manifest.chapter_objs())
    sel_pages = sum(c.n_pages for c in sel)
    lines.extend(["## 深读队列", "",
                  "{} / {} 章，{} / {} 页（{:.0%}）".format(
                      len(sel), len(manifest.chapters), sel_pages, total_pages,
                      (sel_pages / total_pages) if total_pages else 0), ""])
    for c in sel:
        t = triage.get(c.number)
        lines.append("- ch{} {} — max {} · {}".format(
            c.number, c.title[:60], t.max_score if t else "?", (t.gist if t else "")))
    lines.append("")
    errs = [t for t in triage.values() if t.error]
    if errs:
        lines.extend(["## 分诊失败（按选中处理，勿当作已裁决）", ""])
        lines.extend("- ch{} {}：{}".format(t.number, t.title[:50], t.error) for t in errs)
        lines.append("")
    return "\n".join(lines)


def selected_chapters(manifest: BookManifest,
                      triage: Dict[int, ChapterTriage]) -> List[Chapter]:
    """深读队列（按章号）。"""
    return [c for c in manifest.chapter_objs()
            if (triage.get(c.number) or ChapterTriage(c.number, c.title)).selected]
