# -*- coding: utf-8 -*-
"""概念页的每周人读增量摘要。

概念页正文强调完整与可溯源，适合下钻，不适合每周通读。本模块在周度入库触发概念页
重合成后，把「上一版生成块 → 新版生成块」压成一份短报：本周总览、分页实质变化、
需要复核的自动质检信号，以及失败/冲突页。产物：

    output/scholar_notes/topics/weekly/<周一 YYYY-MM-DD>.md

纪律：
- 只比较生成块正文，剔除覆盖说明和证据表；否则 generated_at、证据编号重排会制造假变化。
- 摘要只是导航层，不新增证据、不替代原页；具体数字和强结论仍须回原页证据表。
- 无实质变化不调 LLM；已有同周报告时不拿「无变化」空报覆盖它。
- LLM 综合失败仍有确定性降级：列出原样抽出的新增/改写条目，不静默消失。
- 周报复用概念页哨兵与「我的批注」区，重跑不会清掉人的批注。
"""
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import coverage as cov
from . import topics as T
from .notes_index import write_if_changed
from .vault import GEN_END, split_frontmatter

WEEKLY_DIRNAME = "weekly"
VAULT_WEEKLY_DIR = "每周增量"
WEEKLY_GENERATOR = "scripts/build_topics.py"
DEFAULT_WEEKLY_PROMPT = "config/prompts/topic_weekly_digest_prompt.md"
_EVIDENCE_HEADING = "## 本页证据"
_MAX_OVERVIEW = 5
_MAX_CHANGES_PER_PAGE = 4


class WeeklyDigestError(RuntimeError):
    """周报输入、模型输出或写盘不可安全完成。"""


@dataclass
class WeeklyDigestOutcome:
    path: Path
    changed_pages: List[str]
    degraded: bool = False
    preserved_existing: bool = False
    detail: str = ""


def week_start(now: Optional[datetime] = None) -> str:
    """本地自然周的周一日期；跨周补跑也明确落在实际运行周。"""
    d = (now or datetime.now()).date()
    return (d - timedelta(days=d.weekday())).isoformat()


def weekly_digest_path(topics_dir, now: Optional[datetime] = None) -> Path:
    return Path(topics_dir) / WEEKLY_DIRNAME / "{}.md".format(week_start(now))


def _read_generated(path: Path) -> Tuple[Dict[str, Any], str]:
    """读生成块用于 diff；与 read_existing 不同，存根页也保留其实际文本。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, ""
    fm, body = split_frontmatter(raw)
    if not isinstance(body, str):
        return (fm if isinstance(fm, dict) else {}), ""
    end = body.find(GEN_END)
    gen = body[:end] if end >= 0 else body
    gen = re.sub(r"^<!-- BEGIN GENERATED.*?-->\s*", "", gen.strip(), flags=re.S)
    return (fm if isinstance(fm, dict) else {}), cov.strip_coverage_block(gen)


def _main_text(text: str) -> str:
    """只比较人读正文；证据表顺序/编号变化不等于概念页的论断变化。"""
    return (text or "").split(_EVIDENCE_HEADING, 1)[0].strip()


def capture_before(topics_dir, specs: Sequence[T.TopicSpec]) -> Dict[str, str]:
    """在合成前留快照；由调用方保证只在实际目标页集合上执行。"""
    out: Dict[str, str] = {}
    for spec in specs:
        _fm, gen = _read_generated(Path(topics_dir) / "{}.md".format(spec.slug))
        out[spec.slug] = gen
    return out


def _clip_for_prompt(text: str, head: int = 18000, tail: int = 4000) -> str:
    """限制摘要输入成本；保留开头总览/论断与尾部分歧/缺口，并显式声明截断。"""
    text = _main_text(text)
    if len(text) <= head + tail:
        return text
    return text[:head] + "\n\n……［为控制摘要成本，中段已截断］……\n\n" + text[-tail:]


def _clean_bullet(value: Any, *, max_chars: int = 320) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"\s+", " ", value).strip()
    text = re.sub(r"^[-•*]\s*", "", text).strip()
    if not text:
        return ""
    if len(text) > max_chars:
        text = text[:max_chars - 1].rstrip("，,、；; ") + "…"
    return text


def _extract_bullets(text: str) -> List[str]:
    """确定性降级只抽新版正文的论断行，不从证据表或覆盖说明里捞。"""
    out = []
    for line in _main_text(text).splitlines():
        if not line.startswith("- "):
            continue
        item = _clean_bullet(line)
        if item:
            out.append(item)
    return out


def fallback_changes(before: str, after: str) -> List[str]:
    """不依赖模型的保底：原样优先抽新版新增行；整页改写时明确 diff 的局限。"""
    old = set(_extract_bullets(before))
    added = [x for x in _extract_bullets(after) if x not in old]
    if added:
        return added[:3]
    if not before.strip() and after.strip():
        summary = next((ln.strip() for ln in after.splitlines()
                        if ln.strip() and not ln.startswith(("#", ">", "<"))), "")
        return [_clean_bullet(summary)] if summary else ["本周新建该概念页，请打开原页阅读。"]
    if _main_text(before) != _main_text(after):
        return ["本页正文有改写，但未能可靠拆出独立新增条目；请打开原页核对。"]
    return []


def parse_digest_synthesis(raw: str, valid_slugs: Sequence[str]) -> Dict[str, Any]:
    """只接受合法 JSON、已知 slug 与有界列表；模型不能凭摘要新增一页。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            start, end = text.find("{"), text.rfind("}")
            data = json.loads(text[start:end + 1]) if start >= 0 and end > start else None
        except json.JSONDecodeError:
            data = None
    if not isinstance(data, dict):
        raise WeeklyDigestError("增量摘要输出不是合法 JSON 对象")
    overview = [_clean_bullet(x) for x in (data.get("overview") or [])]
    overview = [x for x in overview if x][:_MAX_OVERVIEW]
    valid = set(valid_slugs)
    pages: Dict[str, List[str]] = {}
    for row in data.get("pages") or []:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("slug") or "")
        if slug not in valid or slug in pages:
            continue
        changes = [_clean_bullet(x) for x in (row.get("changes") or [])]
        pages[slug] = [x for x in changes if x][:_MAX_CHANGES_PER_PAGE]
    if not overview and not pages:
        raise WeeklyDigestError("增量摘要 JSON 没有可用的 overview/pages")
    return {"overview": overview, "pages": pages}


def _prompt_input(changes: Sequence[Dict[str, Any]]) -> str:
    blocks = []
    for item in changes:
        keys = "、".join(item["new_citekeys"]) or "（日历兜底或无明确新键）"
        blocks.append(
            "## {}｜{}\n本轮进入证据集的新论文：{}\n\n### 上一版正文\n{}\n\n### 新版正文\n{}".format(
                item["slug"], item["title"], keys,
                _clip_for_prompt(item["before"]) or "（此前不存在）",
                _clip_for_prompt(item["after"])))
    return "\n\n---\n\n".join(blocks)


def _synthesize(llm, template: str, changes: Sequence[Dict[str, Any]], *,
                week: str, model: Optional[str]) -> Dict[str, Any]:
    prompt = (template.replace("{{WEEK_START}}", week)
              .replace("{{PAGE_DIFFS}}", _prompt_input(changes)))
    raw = llm.call(prompt, model=model, max_tokens=3500, temperature=0.2,
                   json_mode=True, max_retries=2)
    return parse_digest_synthesis(raw, [x["slug"] for x in changes])


def _review_lines(item: Dict[str, Any]) -> List[str]:
    """质检信号只从程序报告/frontmatter 生成，不让模型编。"""
    fm = item.get("frontmatter") or {}
    result = item.get("result")
    report = getattr(result, "report", None)
    lines = []
    n_ungrounded = int(fm.get("ungrounded_numbers") or 0)
    if n_ungrounded:
        lines.append("复核：{} 个数字未通过自动接地检查（不等于错误，写稿前须回证据原句）。".format(
            n_ungrounded))
        details = getattr(report, "ungrounded_details", None) or []
        if details:
            first = details[0]
            numbers = "、".join(first.get("numbers") or [])
            claim = _clean_bullet(first.get("text"), max_chars=120)
            if numbers and claim:
                lines.append("首个未接地位置：{}；所在论断「{}」。".format(numbers, claim))
    invalid = int(fm.get("invalid_refs") or 0)
    dropped = int(fm.get("dropped_claims") or 0)
    if invalid or dropped:
        lines.append("复核：本轮剔除非法引用编号 {} 个、无效论断 {} 条。".format(invalid, dropped))
    n_claims = int(fm.get("n_claims") or 0)
    n_single = int(fm.get("single_source_claims") or 0)
    if n_claims and n_single:
        lines.append("证据强度：{}/{} 条论断标为单一来源。".format(n_single, n_claims))
    return lines


def _render_body(week: str, source_note: str, changes: Sequence[Dict[str, Any]],
                 synthesis: Dict[str, Any], results: Sequence[T.TopicResult], *,
                 degraded: bool, unchanged_count: int) -> str:
    lines = ["# 概念页每周增量 · {} 周".format(week), "",
             "> 这是给人快速导航的增量层，不替代概念页与证据原句。具体数字、强结论和写稿引用仍请点回原页核查。", ""]
    if source_note:
        lines += ["本周入库札记：[{}](../../{})".format(Path(source_note).name, Path(source_note).name), ""]
    lines += ["## 本周速览", ""]
    overview = synthesis.get("overview") or []
    if not overview:
        overview = ["本周 {} 个概念页发生实质正文变化。".format(len(changes))]
    lines += ["- {}".format(x) for x in overview[:_MAX_OVERVIEW]]
    if unchanged_count:
        lines.append("- 另有 {} 个触发页重新合成后正文无实质变化。".format(unchanged_count))
    if degraded:
        lines.append("- 自动综合本轮不可用或部分不可用；相应页面使用原文差分降级，未补写推断。")
    lines.append("")

    lines += ["## 分页变化", ""]
    synth_pages = synthesis.get("pages") or {}
    for item in changes:
        slug, title = item["slug"], item["title"]
        lines += ["### [{}](../{}.md)".format(title, slug), ""]
        bullets = synth_pages.get(slug) or fallback_changes(item["before"], item["after"])
        lines += ["- {}".format(x) for x in bullets[:_MAX_CHANGES_PER_PAGE]]
        if item.get("new_citekeys"):
            lines.append("- 本轮触发论文：{}。".format("、".join("`{}`".format(k) for k in item["new_citekeys"])))
        for warning in _review_lines(item):
            lines.append("- ⚠️ {}".format(warning))
        lines.append("")

    bad = [r for r in results if r.status in ("failed", "conflict")]
    if bad:
        lines += ["## 未完成更新", ""]
        for r in bad:
            label = "冲突，未覆盖" if r.status == "conflict" else "失败"
            lines.append("- `{}`：{}。{}".format(r.slug, label, r.error or "详见运行日志"))
        lines.append("")
    lines += ["---", "",
              "自动质检只负责发现可疑点：未接地不等于错误，单一来源不等于虚假；它们也不能证明其余转述已经人工核验。", ""]
    return "\n".join(lines)


def _render_frontmatter_for_digest(week: str, now: datetime, source_note: str,
                                   changes: Sequence[Dict[str, Any]], degraded: bool) -> str:
    return T._render_frontmatter({
        "title": "概念页每周增量 · {} 周".format(week),
        "type": "topic_weekly_digest",
        "weekly": week,
        "generated_at": now.isoformat(timespec="seconds"),
        "source_note": Path(source_note).name if source_note else "",
        "changed_pages": [x["slug"] for x in changes],
        "degraded": degraded,
        "tags": ["札记/概念页", "札记/每周增量"],
    })


def render_weekly_index(weekly_dir) -> str:
    rows = []
    for path in Path(weekly_dir).glob("*.md"):
        if path.name == "INDEX.md":
            continue
        try:
            fm, _body = split_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if not isinstance(fm, dict) or not fm.get("weekly"):
            continue
        pages = fm.get("changed_pages") or []
        rows.append((str(fm["weekly"]), path.name, len(pages), str(fm.get("generated_at") or "")))
    rows.sort(reverse=True)
    lines = ["# 概念页每周人读增量", "",
             "每周只读这一层即可掌握概念页变化；需要证据与完整语境时再点进具体概念页。", "",
             "| 周 | 有实质变化的页面 | 生成时间 |", "|---|---:|---|"]
    lines += ["| [{}]({}) | {} | {} |".format(week, name, n, generated) for week, name, n, generated in rows]
    lines.append("")
    return "\n".join(lines)


def _write_report(topics_dir, path: Path, fm: str, body: str) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else None
    content, _status = T.merge_topic_page(fm, body, existing, generator=WEEKLY_GENERATOR)
    if content is None:
        raise WeeklyDigestError("周报哨兵缺失或生成块被手改，未覆盖：{}".format(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    write_if_changed(path, content)
    write_if_changed(path.parent / "INDEX.md", render_weekly_index(path.parent))


def write_no_change(topics_dir, *, source_note: str = "", now: Optional[datetime] = None,
                    detail: str = "本周没有概念页正文的实质变化。") -> WeeklyDigestOutcome:
    """无变化也留一张周报；同周已有内容 richer 的报告时绝不拿空报覆盖。"""
    now = now or datetime.now()
    path = weekly_digest_path(topics_dir, now)
    if path.exists():
        return WeeklyDigestOutcome(path=path, changed_pages=[], preserved_existing=True,
                                   detail="同周报告已存在，保留原报告")
    body = "# 概念页每周增量 · {} 周\n\n{}\n".format(week_start(now), detail)
    fm = _render_frontmatter_for_digest(week_start(now), now, source_note, [], False)
    _write_report(Path(topics_dir), path, fm, body)
    return WeeklyDigestOutcome(path=path, changed_pages=[], detail=detail)


def generate_weekly_digest(llm, topics_dir, results: Sequence[T.TopicResult],
                           before: Dict[str, str], specs: Sequence[T.TopicSpec], *,
                           hit_map: Optional[Dict[str, List[str]]] = None,
                           source_note: str = "", template: str,
                           model: Optional[str] = None,
                           now: Optional[datetime] = None) -> WeeklyDigestOutcome:
    """对本轮真正写盘的目标页生成增量；模型失败时逐页降级，不阻断概念页主流程。"""
    now = now or datetime.now()
    topics_dir = Path(topics_dir)
    by_slug = {s.slug: s for s in specs}
    hit_map = hit_map or {}
    changes: List[Dict[str, Any]] = []
    unchanged_count = 0
    for result in results:
        if result.status not in ("new", "merged", "insufficient") or not result.path:
            continue
        fm, after = _read_generated(result.path)
        old = before.get(result.slug, "")
        if _main_text(old) == _main_text(after):
            unchanged_count += 1
            continue
        spec = by_slug.get(result.slug)
        changes.append({
            "slug": result.slug,
            "title": (spec.title if spec else result.slug),
            "before": old,
            "after": after,
            "frontmatter": fm,
            "result": result,
            "new_citekeys": list(hit_map.get(result.slug) or []),
        })

    path = weekly_digest_path(topics_dir, now)
    if not changes:
        if path.exists():
            return WeeklyDigestOutcome(path=path, changed_pages=[], preserved_existing=True,
                                       detail="本周正文无新增变化，保留既有周报")
        bad = [r for r in results if r.status in ("failed", "conflict")]
        if bad:
            synthesis = {"overview": ["本轮没有成功更新的概念页正文；{} 页失败或冲突。".format(len(bad))],
                         "pages": {}}
            body = _render_body(week_start(now), source_note, [], synthesis, results,
                                degraded=False, unchanged_count=unchanged_count)
            fm = _render_frontmatter_for_digest(week_start(now), now, source_note, [], False)
            _write_report(topics_dir, path, fm, body)
            return WeeklyDigestOutcome(path=path, changed_pages=[],
                                       detail="{} 页失败或冲突".format(len(bad)))
        return write_no_change(topics_dir, source_note=source_note, now=now,
                               detail="本轮没有概念页正文的实质变化。")

    synthesis: Dict[str, Any] = {"overview": [], "pages": {}}
    degraded = False
    try:
        synthesis = _synthesize(llm, template, changes, week=week_start(now), model=model)
    except Exception:
        degraded = True
    # 模型漏页不丢掉真实变化：该页退回确定性差分，并在总览声明降级。
    if any(not (synthesis.get("pages") or {}).get(x["slug"]) for x in changes):
        degraded = True
    body = _render_body(week_start(now), source_note, changes, synthesis, results,
                        degraded=degraded, unchanged_count=unchanged_count)
    fm = _render_frontmatter_for_digest(week_start(now), now, source_note, changes, degraded)
    _write_report(topics_dir, path, fm, body)
    return WeeklyDigestOutcome(path=path, changed_pages=[x["slug"] for x in changes],
                               degraded=degraded)


# ---------------------------------------------------------------------------
# Obsidian 同步：与问答归档同样不做 prune——周报是历史记录。
# ---------------------------------------------------------------------------


def is_weekly_digest_file(path) -> bool:
    return Path(path).name.endswith(".md") and Path(path).name != "INDEX.md"


def sync_weekly_to_vault(notes_dir, vault_dir, filenames: Dict[str, str], *,
                         dry_run: bool = False) -> Dict[str, Any]:
    src_dir = Path(notes_dir) / T.TOPICS_DIRNAME / WEEKLY_DIRNAME
    out_dir = Path(vault_dir) / T.VAULT_TOPICS_DIR / VAULT_WEEKLY_DIR
    report: Dict[str, Any] = {"synced": 0, "new": 0, "merged": 0, "unchanged": 0,
                              "conflicts": [], "skipped": []}
    if not src_dir.exists():
        return report
    for src in sorted(src_dir.glob("*.md")):
        if not is_weekly_digest_file(src):
            continue
        try:
            raw = src.read_text(encoding="utf-8")
        except OSError as exc:
            report["skipped"].append("{}（读取失败：{}）".format(src.name, type(exc).__name__))
            continue
        fm, body = split_frontmatter(raw)
        if not isinstance(fm, dict) or not fm.get("weekly") or not isinstance(body, str):
            report["skipped"].append(src.name)
            continue
        end = body.find(GEN_END)
        gen = body[:end] if end >= 0 else body
        gen = re.sub(r"^<!-- BEGIN GENERATED.*?-->\s*", "", gen.strip(), flags=re.S)
        if not gen:
            report["skipped"].append(src.name)
            continue
        fm_v = dict(fm)
        fm_v["cssclasses"] = fm.get("cssclasses") or ["topic-weekly-digest"]
        dst = out_dir / src.name
        existing = dst.read_text(encoding="utf-8") if dst.exists() else None
        content, status = T.merge_topic_page(
            T._render_frontmatter(fm_v), T.to_wiki_links(gen, filenames), existing,
            generator=WEEKLY_GENERATOR)
        if content is None:
            report["conflicts"].append(src.stem)
            continue
        if dry_run:
            report[status if status in ("new", "merged") else "unchanged"] += 1
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        if write_if_changed(dst, content):
            report["synced"] += 1
            report["new" if status == "new" else "merged"] += 1
        else:
            report["unchanged"] += 1
    if not dry_run and src_dir.joinpath("INDEX.md").exists():
        out_dir.mkdir(parents=True, exist_ok=True)
        write_if_changed(out_dir / "INDEX.md", src_dir.joinpath("INDEX.md").read_text(encoding="utf-8"))
    return report
