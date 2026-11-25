# -*- coding: utf-8 -*-
"""概念页的每周人读增量摘要。

概念页正文强调完整与可溯源，适合下钻，不适合每周通读。本模块在周度入库触发概念页
重合成后，把「周初基线 → 最新生成块」压成一份**累计**短报：本周总览、分页实质变化、
需要复核的自动质检信号，以及失败/冲突/缺失页。产物：

    output/scholar_notes/topics/weekly/<周一 YYYY-MM-DD>.md

纪律：
- 只比较生成块正文，剔除覆盖说明和证据表；否则 generated_at、证据编号重排会制造假变化。
- 报告是**本周累计视图**：以本周第一次运行前的正文为基线，同周后续运行继续与同一
  基线比较。第二次入库不会把第一次的变化挤出报告，也不会重复调用模型。
- 摘要只是导航层，不新增证据、不替代原页；具体数字和强结论仍须回原页证据表。
- 无实质变化不调 LLM；已有同周报告时不拿「无变化」空报覆盖它。
- LLM 综合失败仍有确定性降级：列出原样抽出的新增/改写条目，不静默消失。
- 周报复用概念页哨兵与「我的批注」区，重跑不会清掉人的批注。
- 只有哨兵完好的页面才参与 diff：生成块被手改/哨兵缺失/页面被删的页不作为变化源
  （否则用户私有批注会被当成本周变化渲染进报告），只列进「未完成更新」。
- 状态文件 `weekly/<周一>.state.json` 保存周基线、累计触发键、失败/缺失记录与质检
  细节缓存；它损坏时按空状态重建，并把同周旧报告的正文迁进「本周更早的更新」，
  不静默丢内容。页面回退到周初基线时，旧报告中失真的描述同样迁进该节。
"""
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import coverage as cov
from . import topics as T
from .notes_index import write_if_changed
from .vault import GEN_END, split_frontmatter
from ..utils.logger import get_logger

logger = get_logger(__name__)

WEEKLY_DIRNAME = "weekly"
VAULT_WEEKLY_DIR = "每周增量"
WEEKLY_GENERATOR = "scripts/build_topics.py"
DEFAULT_WEEKLY_PROMPT = "config/prompts/topic_weekly_digest_prompt.md"
_STATE_VERSION = 1
_STATE_SUFFIX = ".state.json"
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


def weekly_digest_path(topics_dir, now: Optional[datetime] = None, *,
                       week: Optional[str] = None) -> Path:
    """周报路径。传 week 时以它为权威：跨午夜/跨周运行必须和基线落在同一周。"""
    return Path(topics_dir) / WEEKLY_DIRNAME / "{}.md".format(week or week_start(now))


def _read_generated_checked(path: Path) -> Tuple[Dict[str, Any], str, bool]:
    """读生成块并校验哨兵合法性，供 diff/基线使用。

    ok=False（哨兵缺失 / 生成块被手改 / 无法读取）的页面**绝不能作为 diff 源**：
    否则用户手写在生成块里的批注会被当成本周实质变化，降级路径还会把它原样抽进
    周报正文。ok=False 时的 gen 为空串。页面不存在（新页）返回 ok=True + 空正文。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, "", True
    except OSError:
        return {}, "", False
    fm, body = split_frontmatter(raw)
    if not isinstance(fm, dict) or not isinstance(body, str):
        return {}, "", False
    if body.find(GEN_END) < 0:                 # 哨兵缺失：GEN_END 之后的用户区必须隔离
        return fm, "", False
    if T.generated_block_tampered(body):       # 生成块内被手改：内容不可信
        return fm, "", False
    gen = body[:body.find(GEN_END)]
    gen = re.sub(r"^<!-- BEGIN GENERATED.*?-->\s*", "", gen.strip(), flags=re.S)
    return fm, cov.strip_coverage_block(gen), True


def _read_generated(path: Path) -> Tuple[Dict[str, Any], str]:
    """宽松读生成块（仅用于展示历史，如旧报告迁移——哨兵异常的旧报告正文仍要保留，
    内容不丢原则优先）。作为 diff 源一律用 _read_generated_checked。"""
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


@dataclass
class WeeklyState:
    """本周的累计状态：基线只在本周第一次见到某页时写入，后续运行不覆盖。

    resolved：本进程本轮观察到「已成功合成」从而清除的失败 slug。它表达的是一次
    **写盘意图**（merge 时据此从盘上 failures 剔除），不持久化——dict.update 表达
    不了删除，没有它，pop 掉的失败记录每次落盘都会从盘上复活成僵尸。
    """
    week: str
    baseline: Dict[str, str] = field(default_factory=dict)
    citekeys: Dict[str, List[str]] = field(default_factory=dict)
    failures: Dict[str, Dict[str, str]] = field(default_factory=dict)
    review_notes: Dict[str, str] = field(default_factory=dict)
    runs: List[str] = field(default_factory=list)
    legacy_body: str = ""
    rendered_signature: str = ""
    resolved: set = field(default_factory=set)


def _state_path(topics_dir, week: str) -> Path:
    return Path(topics_dir) / WEEKLY_DIRNAME / "{}{}".format(week, _STATE_SUFFIX)


def _str_map(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if isinstance(v, str)}


def _list_map(value: Any) -> Dict[str, List[str]]:
    if not isinstance(value, dict):
        return {}
    out: Dict[str, List[str]] = {}
    for key, items in value.items():
        if isinstance(items, list):
            out[str(key)] = [str(x) for x in items if isinstance(x, str)]
    return out


def _failure_map(value: Any) -> Dict[str, Dict[str, str]]:
    if not isinstance(value, dict):
        return {}
    out: Dict[str, Dict[str, str]] = {}
    for key, item in value.items():
        if isinstance(item, dict) and item.get("status") in ("failed", "conflict", "missing"):
            out[str(key)] = {"status": str(item["status"]),
                             "error": str(item.get("error") or "")}
    return out


def _review_note_map(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if isinstance(v, str)}


_LEGACY_HEADING = "## 本周更早的更新"


def _canonicalize_legacy(text: str) -> str:
    """把旧报告正文规范化为可重复迁移的形态，防层层嵌套。

    - 已含的「本周更早的更新」段头剥掉（头由 _legacy_section 统一加），内容保留；
    - 旧「本周速览」段丢弃：它是分页变化的摘要，分页变化/未完成更新/更早的更新
      这些实质内容都保留，速览反复叠加只会让一份报告里出现三份速览；
    - 页脚（与本轮页脚重复）剥掉。
    """
    text = (text or "").strip()
    if not text:
        return ""
    idx = text.find(_LEGACY_HEADING)
    if idx >= 0:
        head = text[:idx].rstrip()
        tail = text[idx + len(_LEGACY_HEADING):].lstrip("\n")
        tail = re.sub(r"^> [^\n]*\n+", "", tail)          # 旧引语由渲染时统一写
        text = (head + "\n\n" + tail) if head else tail
    m1, m2 = text.find("## 本周速览"), text.find("## 分页变化")
    if 0 <= m1 and m2 > m1:
        text = (text[:m1].rstrip() + "\n\n" + text[m2:]).strip()
    elif 0 <= m1 and m2 < 0:
        text = text[:m1].rstrip()
    foot = text.rfind("\n---\n")
    if foot >= 0 and "自动质检" in text[foot:]:
        text = text[:foot].rstrip()
    return text


def _legacy_generated_body(topics_dir, week: str) -> str:
    """没有可用状态时，保留同周既有报告的正文作为「更早的更新」。

    既覆盖修复前的老报告，也覆盖「状态文件被删/损坏」的恢复场景：此时重建的
    基线只能反映当前正文，早先变化只能靠旧报告原文保留。changed_pages 为空但
    正文含失败清单的报告同样要迁——失败记录也是本周的历史，不能静默消失。
    """
    path = weekly_digest_path(topics_dir, week=week)
    try:
        fm, gen = _read_generated(path)
    except OSError as exc:
        logger.warning("   ⚠️ 旧周报读取失败，无法迁移历史：{}".format(exc))
        return ""
    if not ((fm.get("changed_pages") or [])
            or "## 分页变化" in gen or "## 未完成更新" in gen
            or _LEGACY_HEADING in gen):
        return ""                                          # 无变化存根：没有内容可迁
    return _canonicalize_legacy(gen).strip()


def load_week_state(topics_dir, *, now: Optional[datetime] = None) -> WeeklyState:
    """读取本周状态；文件不存在、损坏或版本不符时按空状态重建。

    重建时若同周已有旧版报告，把它当作「更早的更新」原文保留，而不是让第一次
    新运行把整份旧报告覆盖掉。
    """
    now = now or datetime.now()
    week = week_start(now)
    state = WeeklyState(week=week)
    data = None
    path = _state_path(topics_dir, week)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = None
    except (OSError, ValueError) as exc:
        logger.warning("   ⚠️ 周报状态读取失败，按空状态重建：{}（{}）".format(exc, path))
    if (isinstance(data, dict) and data.get("version") == _STATE_VERSION
            and data.get("week") == week):
        state.baseline = _str_map(data.get("baseline"))
        state.citekeys = _list_map(data.get("citekeys"))
        state.failures = _failure_map(data.get("failures"))
        state.review_notes = _review_note_map(data.get("review_notes"))
        runs = data.get("runs")
        state.runs = [str(x) for x in runs if isinstance(x, str)] if isinstance(runs, list) else []
        state.legacy_body = data.get("legacy_body") if isinstance(
            data.get("legacy_body"), str) else ""
        state.rendered_signature = data.get("rendered_signature") if isinstance(
            data.get("rendered_signature"), str) else ""
    else:
        state.legacy_body = _legacy_generated_body(topics_dir, week)
    return state


def _valid_state_payload(topics_dir, week: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(_state_path(topics_dir, week).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("version") == _STATE_VERSION and data.get("week") == week:
        return data
    return None


def _save_state(topics_dir, state: WeeklyState, *, strict: bool, merge: bool = True) -> None:
    """原子写回状态；strict=False 用于报告已落盘后的收尾，失败只告警。

    merge=True 时先读盘并与内存状态合并：周度 launchd 与手动 build_topics 若同周
    重叠，后写者不能把先写者已经记录的基线、触发键或失败页整段抹掉。合并规则里
    failures 会剔除 state.resolved（本轮观察到已成功合成的页）——否则「清除」这个
    动作永远过不了合并（dict.update 不表达删除），失败记录会从盘上复活成僵尸。
    注意这不是锁：两个进程同时处在「读盘→写回」的极小窗口时仍是 lost-update，
    只缓解常见交错，不承诺并发安全。
    """
    baseline = dict(state.baseline)
    citekeys = {k: list(v) for k, v in state.citekeys.items()}
    failures = {k: dict(v) for k, v in state.failures.items()}
    review_notes = dict(state.review_notes)
    runs = list(state.runs)
    legacy_body = state.legacy_body
    signature = state.rendered_signature
    if merge:
        disk = _valid_state_payload(topics_dir, state.week)
        if disk:
            # 同一个页以盘上更早的基线为准（先捕获者才是真正的「周初」）。
            baseline = {**baseline, **_str_map(disk.get("baseline"))}
            merged_keys = {k: list(v) for k, v in _list_map(disk.get("citekeys")).items()}
            for slug, keys in citekeys.items():
                dst = merged_keys.setdefault(slug, [])
                for key in keys:
                    if key not in dst:
                        dst.append(key)
            citekeys = merged_keys
            # 盘上的失败记录是既有事实，先收下；再叠加本轮观察，最后剔除本轮
            # 明确观察到「已成功」的页——清除必须能穿透合并，否则僵尸复活。
            merged_failures = _failure_map(disk.get("failures"))
            merged_failures.update(failures)
            merged_failures = {k: v for k, v in merged_failures.items()
                                if k not in state.resolved}
            failures = merged_failures
            review_notes = {**_review_note_map(disk.get("review_notes")), **review_notes}
            old_runs = disk.get("runs")
            if isinstance(old_runs, list):
                runs = [str(x) for x in old_runs if isinstance(x, str)] + [
                    x for x in runs if x not in old_runs]
            if not legacy_body and isinstance(disk.get("legacy_body"), str):
                legacy_body = disk["legacy_body"]
            if not signature and isinstance(disk.get("rendered_signature"), str):
                signature = disk["rendered_signature"]
    payload = {
        "version": _STATE_VERSION,
        "week": state.week,
        "baseline": baseline,
        "citekeys": citekeys,
        "failures": failures,
        "review_notes": review_notes,
        "runs": runs[-50:],
        "legacy_body": legacy_body,
        "rendered_signature": signature,
    }
    try:
        write_if_changed(_state_path(topics_dir, state.week),
                         json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True))
    except OSError as exc:
        if strict:
            raise
        logger.warning("   ⚠️ 周报状态写回失败（报告本身已生成，下周基线可能退化为当前正文）：{}".format(exc))


def _reload_merged(topics_dir, state: WeeklyState) -> WeeklyState:
    """渲染前重读盘上状态并合并进内存态（基线/触发键/失败页都以合并后为准）。

    begin_week 与 generate 之间隔着整段概念页合成（数分钟）：并发运行的另一个
    进程在此期间写入的基线/触发键，以及它刚渲染过的报告对应的 signature，本进程
    都看不见——不合并就渲染，会用陈旧的内存快照产出一个子集报告，把对方刚写进
    报告的页变化覆盖掉。signature 双方不一致时置空（宁可多渲染一次，不可误跳过）。
    """
    disk = _valid_state_payload(topics_dir, state.week)
    if disk is None:
        return state
    state.baseline = {**state.baseline, **_str_map(disk.get("baseline"))}
    for slug, keys in _list_map(disk.get("citekeys")).items():
        dst = state.citekeys.setdefault(slug, [])
        for key in keys:
            if key not in dst:
                dst.append(key)
    state.failures = {**_failure_map(disk.get("failures")), **state.failures}
    state.review_notes = {**_review_note_map(disk.get("review_notes")), **state.review_notes}
    if not state.legacy_body and isinstance(disk.get("legacy_body"), str):
        state.legacy_body = disk["legacy_body"]
    disk_sig = disk.get("rendered_signature")
    if isinstance(disk_sig, str) and disk_sig and disk_sig != state.rendered_signature:
        state.rendered_signature = ""       # 盘上是别人渲染的：本次不许再按旧指纹跳过
    return state


def begin_week(topics_dir, specs: Sequence[T.TopicSpec], *,
               now: Optional[datetime] = None) -> WeeklyState:
    """合成前建立/读取本周状态，并为本次目标页记录「周基线」。

    只有本周第一次见到某页时才写基线；同周第二次运行绝不拿运行前正文顶掉基线，
    否则本周早先的变化会从报告里消失。哨兵缺失/被手改的页不记基线（手写内容不可
    充当比较基线），但记入冲突清单——周报必须让读者知道这页本周没法跟踪，而不是
    静默跳过。
    """
    state = load_week_state(topics_dir, now=now)
    dirty = False
    for spec in specs:
        if spec.slug in state.baseline:
            continue
        _fm, gen, ok = _read_generated_checked(Path(topics_dir) / "{}.md".format(spec.slug))
        if not ok:
            state.failures[spec.slug] = {
                "status": "conflict",
                "error": "生成块哨兵缺失或被手改，周报无法跟踪该页（页面修复后自动恢复）"}
            dirty = True
            continue
        state.baseline[spec.slug] = _main_text(gen)
        dirty = True
    if dirty:
        _save_state(topics_dir, state, strict=True)
    return state


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
            "## {}｜{}\n本周累计进入证据集的论文：{}\n\n### 周初正文\n{}\n\n### 最新正文\n{}".format(
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


def _review_lines(item: Dict[str, Any], cached_note: str = "") -> Tuple[List[str], str]:
    """质检信号只从程序报告/frontmatter 生成，不让模型编。

    返回 (行列表, 首个未接地位置行)。位置行依赖本轮内存中的 ValidationReport，
    累计重渲染时早先变化页拿不到 report——由调用方传入 state 里持久化的缓存行，
    细节行不能因为换了一轮就消失。
    """
    fm = item.get("frontmatter") or {}
    result = item.get("result")
    report = getattr(result, "report", None)
    lines = []
    note = ""
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
                note = "首个未接地位置：{}；所在论断「{}」。".format(numbers, claim)
        if not note:
            note = cached_note
        if note:
            lines.append(note)
    invalid = int(fm.get("invalid_refs") or 0)
    dropped = int(fm.get("dropped_claims") or 0)
    if invalid or dropped:
        lines.append("复核：该页最近一次合成剔除非法引用编号 {} 个、无效论断 {} 条。".format(invalid, dropped))
    n_claims = int(fm.get("n_claims") or 0)
    n_single = int(fm.get("single_source_claims") or 0)
    if n_claims and n_single:
        lines.append("证据强度：{}/{} 条论断标为单一来源。".format(n_single, n_claims))
    return lines, note


def _legacy_section(body: str) -> str:
    """把旧报告正文降级成历史块；只做结构变换，不改写内容。

    输入约定：legacy_body 不含「本周更早的更新」头（规范化时已剥），头与引语由
    这里统一加——否则状态反复损坏迁移时会出现层层嵌套的重复段头。
    """
    text = (body or "").strip()
    if not text:
        return ""
    kept = []
    for line in text.splitlines():
        if line.startswith("# "):
            continue                                  # 旧 H1 与前面重复
        if line.startswith("> ") and ("导航" in line or "以下为" in line):
            continue                                  # 旧版免责声明/旧引语
        kept.append(line)
    text = "\n".join(kept).strip()
    # 去掉旧页脚，避免与新版页脚重复；只认我们自己的固定文案。
    idx = text.rfind("\n---\n")
    if idx >= 0 and "自动质检" in text[idx:]:
        text = text[:idx].rstrip()
    # 整体降一级标题，挂在「本周更早的更新」下面。
    text = re.sub(r"^(#{1,6}) ", lambda m: "#" + m.group(0), text, flags=re.M)
    return ("{}\n\n"
            "> 以下为本周更早运行的原样记录（周报累计状态不可用或页面回退前的过渡归档）；"
            "新变化见上。\n\n" + text + "\n").format(_LEGACY_HEADING)


def _render_body(week: str, source_note: str, changes: Sequence[Dict[str, Any]],
                 synthesis: Dict[str, Any], failures: Sequence[T.TopicResult], *,
                 degraded: bool, unchanged_count: int, legacy_body: str = "") -> str:
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
        lines.append("- 另有 {} 个本轮触发页相对周初无实质变化。".format(unchanged_count))
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
            lines.append("- 本周触发论文：{}。".format("、".join("`{}`".format(k) for k in item["new_citekeys"])))
        warnings = item.get("review_warnings") or []
        for warning in warnings:
            lines.append("- ⚠️ {}".format(warning))
        lines.append("")

    if failures:
        lines += ["## 未完成更新", ""]
        labels = {"conflict": "冲突，未覆盖", "failed": "失败",
                  "missing": "页面缺失或生成块不可读"}
        for r in failures:
            lines.append("- `{}`：{}。{}".format(
                r.slug, labels.get(r.status, r.status), r.error or "详见运行日志"))
        lines.append("")
    legacy = _legacy_section(legacy_body)
    if legacy:
        lines += [legacy.rstrip(), ""]
    lines += ["---", "",
              "自动质检只负责发现可疑点：未接地不等于错误，单一来源不等于虚假；它们也不能证明其余转述已经人工核验。", ""]
    return "\n".join(lines)


def _render_frontmatter_for_digest(week: str, now: datetime, source_note: str,
                                   changes: Sequence[Dict[str, Any]], degraded: bool) -> str:
    # state_version 是「本报告由累计状态生成」的标记：旧版报告没有它，迁移时
    # 才会被当作「更早的更新」保留，不会被后来的空跑误认为己方产物。
    return T._render_frontmatter({
        "title": "概念页每周增量 · {} 周".format(week),
        "type": "topic_weekly_digest",
        "weekly": week,
        "state_version": _STATE_VERSION,
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
                    detail: str = "本周没有概念页正文的实质变化。",
                    week: Optional[str] = None) -> WeeklyDigestOutcome:
    """无变化也留一张周报；同周已有内容 richer 的报告时绝不拿空报覆盖。"""
    now = now or datetime.now()
    week = week or week_start(now)
    path = weekly_digest_path(topics_dir, week=week)
    if path.exists():
        return WeeklyDigestOutcome(path=path, changed_pages=[], preserved_existing=True,
                                   detail="同周报告已存在，保留原报告")
    body = "# 概念页每周增量 · {} 周\n\n{}\n".format(week, detail)
    fm = _render_frontmatter_for_digest(week, now, source_note, [], False)
    _write_report(Path(topics_dir), path, fm, body)
    return WeeklyDigestOutcome(path=path, changed_pages=[], detail=detail)


def _signature(week: str, changes: Sequence[Dict[str, Any]],
               citekeys: Dict[str, List[str]], failures: Sequence[T.TopicResult],
               template: str, model: Optional[str]) -> str:
    """内容指纹：同周重复触发但没有真实新内容时，既不调模型也不重写文件。"""
    payload = {
        "week": week,
        "template": hashlib.sha1((template or "").encode("utf-8")).hexdigest(),
        "model": model or "",
        "pages": {item["slug"]: hashlib.sha1(item["after"].encode("utf-8")).hexdigest()
                  for item in changes},
        "citekeys": {item["slug"]: list(citekeys.get(item["slug"]) or [])
                     for item in changes},
        "failures": [[r.slug, r.status, r.error or ""] for r in failures],
    }
    return hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


_PAGE_HEAD_RE = re.compile(r"^### \[.*\]\(\.\./([\w.-]+)\.md\)", re.M)


def _extract_page_sections(body: str, slugs: Sequence[str]) -> str:
    """从旧报告正文按页提取「分页变化」段落（### 标题到下一标题之间）。"""
    matches = list(_PAGE_HEAD_RE.finditer(body))
    picks = []
    for i, m in enumerate(matches):
        if m.group(1) not in slugs:
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        picks.append(body[m.start():end].rstrip())
    return "\n\n".join(picks)


def _stale_slugs_in_report(topics_dir, path: Path, changes_slugs: set,
                           state: WeeklyState) -> List[str]:
    """盘上报告里宣称过、但本次 changes 已不再包含的页。

    单进程累计下 changes 只会多不会少，出现差集只有两种情况：页面回退到了周初基线
    （报告描述已失真，须迁进「更早的更新」）；或并发进程刚把报告写成、对方的页本
    进程还没看到（同样须迁移，不能被子集报告覆盖掉）。
    """
    try:
        fm, gen = _read_generated(path)
    except OSError:
        return []
    declared = {str(x) for x in (fm.get("changed_pages") or [])}
    return sorted(declared - changes_slugs - set(state.failures))


def _migrate_stale_pages(topics_dir, path: Path, stale_slugs: Sequence[str],
                         state: WeeklyState) -> None:
    """把盘上报告中这些页的段落追加进 legacy——旧报告描述不丢，报告主体反映现状。

    已在 legacy 里出现过的页跳过：状态损坏重建时 `_legacy_generated_body` 可能
    已经收下盘上报告的同一批段落，再迁一次会让同一变化在 legacy 里出现两份。
    """
    try:
        _fm, gen = _read_generated(path)
    except OSError:
        return
    additions = _extract_page_sections(gen, stale_slugs)
    if not additions:
        return
    # 按页去重：legacy 中已引用过 (../<slug>.md) 的页不再追加。
    kept = []
    for chunk in re.split(r"\n\n(?=### )", additions):
        m = re.search(r"\]\(\.\./([\w.-]+)\.md\)", chunk)
        if not m:
            continue
        slug = m.group(1)
        if "](../{}.md)".format(slug) in (state.legacy_body or ""):
            continue
        kept.append(chunk)
    if not kept:
        return
    additions = "\n\n".join(kept)
    additions = re.sub(r"^(#{1,6}) ", lambda m: "#" + m.group(0), additions, flags=re.M)
    state.legacy_body = (state.legacy_body.rstrip() + "\n\n" + additions).strip() \
        if state.legacy_body else additions


def generate_weekly_digest(llm, topics_dir, results: Sequence[T.TopicResult],
                           state: WeeklyState, specs: Sequence[T.TopicSpec], *,
                           hit_map: Optional[Dict[str, List[str]]] = None,
                           source_note: str = "", template: str,
                           model: Optional[str] = None,
                           now: Optional[datetime] = None) -> WeeklyDigestOutcome:
    """把「周基线 → 最新正文」压成累计增量；模型失败时逐页降级，不阻断概念页主流程。

    state 必须来自同一次运行的 begin_week。报告周与路径以 state.week 为准：运行跨
    午夜时，基线所在周不能被 datetime.now() 悄悄换成下一周。渲染前会重读并合并盘上
    状态：begin 与 generate 之间隔着整段概念页合成，并发进程在此期间写入的基线/
    触发键必须可见，否则本进程会拿陈旧快照产出子集报告、覆盖掉对方刚写入的内容。
    """
    now = now or datetime.now()
    topics_dir = Path(topics_dir)
    state = _reload_merged(topics_dir, state)
    week = state.week
    by_slug = {s.slug: s for s in specs}
    hit_map = hit_map or {}

    # 触发键与失败页都按周累计：同周第二次运行不能只带自己这一轮的信息。
    # 清除同样要持久（resolved）：本进程观察到「已成功合成」的页，写盘时必须
    # 从盘上 failures 里剔除，否则僵尸失败记录会在下一轮报告里复活。
    for slug, keys in hit_map.items():
        merged = state.citekeys.setdefault(slug, [])
        for key in keys:
            if key not in merged:
                merged.append(key)
    for result in results:
        if not result.slug:
            continue
        if result.status in ("failed", "conflict"):
            state.failures[result.slug] = {"status": result.status,
                                           "error": result.error or ""}
        elif result.status in ("new", "merged", "insufficient"):
            state.failures.pop(result.slug, None)
            state.resolved.add(result.slug)

    results_map = {r.slug: r for r in results if r.slug}
    candidates = set(state.baseline)
    for result in results:
        if result.status in ("new", "merged", "insufficient") and result.slug:
            candidates.add(result.slug)
            # 理论上 begin_week 已覆盖本次全部目标页；这里兜住调用方漏传的页，
            # 把它当作“本周首次见到”（与 begin_week 的口径一致）。
            state.baseline.setdefault(result.slug, "")
    changes: List[Dict[str, Any]] = []
    unchanged_count = 0
    for slug in sorted(candidates):
        fm, after, ok = _read_generated_checked(topics_dir / "{}.md".format(slug))
        old = state.baseline.get(slug, "")
        if not ok:
            # 哨兵缺失/被手改的页不能作为 diff 源：手写内容一旦被当成本周变化
            # 抽进周报，等于把用户的私有批注公开渲染。改记进未完成更新清单。
            state.failures[slug] = {
                "status": "conflict",
                "error": "生成块哨兵缺失或被手改，无法安全比较（页面修复后自动恢复）"}
            logger.warning("   ⚠️ 概念页 {} 哨兵异常，本周不参与周报比较".format(slug))
            continue
        if not after.strip() and old.strip():
            # 基线有、页面已删/生成块为空：不能伪装成「结论全部消失」，
            # 也不能只留一条日志让删除事件对周报读者不可见。
            state.failures[slug] = {
                "status": "missing",
                "error": "页面文件已从 topics/ 消失或生成块为空（若为有意删除请忽略）"}
            logger.warning("   ⚠️ 概念页 {} 已消失或生成块为空，无法比较".format(slug))
            continue
        if _main_text(old) == _main_text(after):
            # 页面读得到且哨兵正常：此前因「缺失/哨兵异常」挂上的观察记录自动清除
            #（failed 不在此清——合成成败由 results 层观察，diff 层无权裁决）。
            if state.failures.get(slug, {}).get("status") in ("missing", "conflict"):
                state.failures.pop(slug, None)
                state.resolved.add(slug)
            result = results_map.get(slug)
            if result is not None and result.status in ("new", "merged", "insufficient"):
                unchanged_count += 1
            continue
        if state.failures.get(slug, {}).get("status") in ("missing", "conflict"):
            state.failures.pop(slug, None)
            state.resolved.add(slug)
        spec = by_slug.get(slug)
        item = {
            "slug": slug,
            "title": (spec.title if spec else (fm.get("title") or slug)),
            "before": _main_text(old),
            "after": _main_text(after),
            "frontmatter": fm,
            "result": results_map.get(slug),
            "new_citekeys": list(state.citekeys.get(slug) or []),
        }
        warnings, note = _review_lines(item, state.review_notes.get(slug, ""))
        item["review_warnings"] = warnings
        if note:
            state.review_notes[slug] = note
        changes.append(item)

    failures = [T.TopicResult(slug=slug, status=v["status"], error=v.get("error") or "")
                for slug, v in sorted(state.failures.items())]
    signature = _signature(week, changes, state.citekeys, failures, template, model)
    path = weekly_digest_path(topics_dir, week=week)

    # 盘上报告宣称过、本次 changes 已不含的页（回退/并发）：段落迁进「更早的更新」，
    # 不让重写的报告把历史描述静默挤掉。
    if path.exists():
        stale = _stale_slugs_in_report(topics_dir, path, {x["slug"] for x in changes}, state)
        if stale:
            _migrate_stale_pages(topics_dir, path, stale, state)

    if not changes:
        existing_report = path.exists()
        stale_overview = []
        if existing_report:
            try:
                fm_ex, gen_ex = _read_generated(path)
            except OSError:
                fm_ex, gen_ex = {}, ""
            declared = [x for x in (fm_ex.get("changed_pages") or [])]
            # 页面回退到周初基线（declared 现已全部不在 changes）或失败清单与盘上
            # 报告不一致时，旧报告的宣称已失真——preserved 只会把它锁死整周。
            open_mismatch = ("## 未完成更新" in gen_ex) != bool(failures)
            if declared:
                stale_overview.append(
                    "此前报告记录的 {} 个页面已回退到周初基线或不再变化，相关描述已移至「本周更早的更新」。".format(len(declared)))
            elif open_mismatch:
                stale_overview.append("此前报告的未完成更新清单已过时，报告已按当前状态重写。")
        if failures or stale_overview:
            overview = (["本周没有成功更新的概念页正文；{} 页失败或冲突。".format(len(failures))]
                        if failures else [])
            overview += stale_overview
            synthesis = {"overview": overview, "pages": {}}
            body = _render_body(week, source_note, [], synthesis, failures,
                                degraded=False, unchanged_count=unchanged_count,
                                legacy_body=state.legacy_body)
            fm = _render_frontmatter_for_digest(week, now, source_note, [], False)
            _write_report(topics_dir, path, fm, body)
            state.rendered_signature = signature
            _save_state(topics_dir, state, strict=False)
            return WeeklyDigestOutcome(path=path, changed_pages=[],
                                       detail="{} 页失败或冲突".format(len(failures))
                                       if failures else "报告已按页面现状更新")
        _save_state(topics_dir, state, strict=False)
        if existing_report:
            return WeeklyDigestOutcome(path=path, changed_pages=[], preserved_existing=True,
                                       detail="本周正文无新增变化，保留既有周报")
        return write_no_change(topics_dir, source_note=source_note, now=now, week=week,
                               detail="本周没有概念页正文的实质变化。")

    if path.exists() and state.rendered_signature == signature:
        _save_state(topics_dir, state, strict=False)
        return WeeklyDigestOutcome(path=path, changed_pages=[x["slug"] for x in changes],
                                   preserved_existing=True,
                                   detail="本周报告已是最新，未重复调用模型")

    synthesis: Dict[str, Any] = {"overview": [], "pages": {}}
    degraded = False
    try:
        synthesis = _synthesize(llm, template, changes, week=week, model=model)
    except Exception as exc:
        degraded = True
        logger.warning("   ⚠️ 周报自动综合失败，改用原文差分：{}: {}".format(
            type(exc).__name__, str(exc)[:200]))
    # 模型漏页不丢掉真实变化：该页退回确定性差分，并在总览声明降级。
    if any(not (synthesis.get("pages") or {}).get(x["slug"]) for x in changes):
        degraded = True
    body = _render_body(week, source_note, changes, synthesis, failures,
                        degraded=degraded, unchanged_count=unchanged_count,
                        legacy_body=state.legacy_body)
    fm = _render_frontmatter_for_digest(week, now, source_note, changes, degraded)
    _write_report(topics_dir, path, fm, body)
    state.runs = (state.runs + [now.isoformat(timespec="seconds")])[-50:]
    # 降级报告不记指纹：下次同内容运行仍要重试自动综合，不能把一次模型故障
    # 固化成本周的最终版本。
    state.rendered_signature = "" if degraded else signature
    _save_state(topics_dir, state, strict=False)
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
