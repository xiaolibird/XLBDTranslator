# -*- coding: utf-8 -*-
"""入库确认：待确认队列 + 原生弹窗会话 + 确认标签落盘 + ingest 闸门（可信度加固 WO-8a）。

设计：docs/decisions/trust_hardening_design_2026-10-13.md §12（用户 2026-10-21 拍板）。

背景一句话：筛选器（filter-v3）的 EXCLUDE 从未被人逐条核对过，用户自己的标注也不是金标准、
研究兴趣还在漂移，所以**不设固定放行门槛**，改为每周入库前请用户在系统弹窗里过一遍——
确认结果本身就是持续产生的标签（stream_labels.jsonl，WO-8b 并入评测，WO-8c 看漂移）。

流程（改造后）：

    digest(周一 09:00, 不变) ──► 写 digest_*.json + _archive/excluded/*_excluded.json
                                    │
    confirm_ingest.py (每天 10:00) ─┤ sync_queue：把近 N 天未入队的 run 读成待确认条目
                                    │            output/scholar_notes/_pending/confirm_<run日期>.json
                                    ├ 弹窗：开场询问 →（看摘要）→ 分页 choose from list
                                    │        纳入/待定默认勾选；低置信排除、抽查排除默认不勾
                                    ├ 各页勾选只暂存（内存 + _draft.json 草稿，草稿不算确认）；全部点完后弹
                                    │   汇总核对框（默认按钮=返回修改），点「确认提交」才落盘：
                                    │   队列 state=keep|drop + stream_labels.jsonl 追加
                                    └ 对有新增 keep 的周调 ingest_notes.py --week <周一> --auto
                                    │
    ingest_notes.py (周一 09:30)  ──┴► gate_segments：只放行 state=keep 的；勾上的原判 EXCLUDE
                                       从 sidecar 重建 segment，走 apply_manual_override（WO-2 语义）

未确认（取消 / 超时 / 无人应答 / 无 GUI）一律**挂起**：state 仍是 pending，不入库、不精读，
下一天再提醒，直到确认。取消不是「全部丢弃」。

本模块**不调 LLM、不联网**；弹窗经注入的 dialogs 对象（`utils.osa_dialog.OsaDialogs` 或测试桩）。
"""
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import thresholds as TH
from ._citekey_utils import dedup_key_fields
from ..utils.logger import get_logger

logger = get_logger("confirm")

SCHEMA = 1
PENDING_DIRNAME = "_pending"
STATE_FILE = "_state.json"
LABELS_FILE = "stream_labels.jsonl"
SUMMARY_FILE = "summary_{}.md"

CATEGORIES = ("include", "maybe", "lowconf_exclude", "audit")
CATEGORY_LABEL = {"include": "纳入", "maybe": "待定", "lowconf_exclude": "低置信排除", "audit": "抽查排除"}
# 筛选器想收的默认勾选；想漏判的（EXCLUDE）默认不勾，勾上才是「推翻」
CATEGORY_DEFAULT_CHECKED = {"include": True, "maybe": True, "lowconf_exclude": False, "audit": False}
ABSTRACT_KEEP = 800                      # 队列里存的摘要长度（与 filter_eval.ABSTRACT_MAX_CHARS 同口径）
_RUN_RE = re.compile(r"^digest_(\d{8})_(\d{6})$")


# ---------------------------------------------------------------- 路径

def pending_dir(notes_dir) -> Path:
    return Path(notes_dir) / PENDING_DIRNAME


def queue_path(notes_dir, run_date) -> Path:
    return pending_dir(notes_dir) / "confirm_{}.json".format(run_date)


def labels_path(notes_dir) -> Path:
    """stream_labels.jsonl 与 filter_eval 的其它评测产物同目录（<notes_dir>/_eval/filter/）。"""
    return Path(notes_dir) / "_eval" / "filter" / LABELS_FILE


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True)


# ---------------------------------------------------------------- 队列读写

def load_queue(path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def list_queues(notes_dir) -> List[Tuple[Path, Dict[str, Any]]]:
    """所有队列文件（按 run 日期升序）。损坏的文件跳过并告警——不能因为一份坏队列挡住其余。"""
    out = []
    for p in sorted(pending_dir(notes_dir).glob("confirm_*.json")):
        try:
            out.append((p, load_queue(p)))
        except (OSError, ValueError) as e:
            logger.warning("  ⚠️ 待确认队列读取失败，跳过 {}: {}".format(p.name, e))
    return out


def all_queued_keys(queues: Sequence[Tuple[Path, Dict[str, Any]]]) -> Set[str]:
    return {it["dedup_key"] for _, q in queues for it in q.get("items", [])}


def pending_items(notes_dir, run_dates: Optional[Set[str]] = None) -> List[Tuple[Path, Dict[str, Any]]]:
    """全部 state=pending 的条目，附其所在队列路径。排序：类别（纳入→待定→低置信→抽查）→ run 日期 → 标题。

    run_dates 非空时只取这些 run 日期（confirm_ingest --date）。
    """
    order = {c: i for i, c in enumerate(CATEGORIES)}
    rows = []
    for p, q in list_queues(notes_dir):
        if run_dates and q.get("run_date") not in run_dates:
            continue
        for it in q.get("items", []):
            if it.get("state") == "pending":
                rows.append((p, q.get("run_date", ""), it))
    rows.sort(key=lambda r: (order.get(r[2]["category"], 9), r[1], r[2].get("title", "")))
    return [(p, it) for p, _, it in rows]


# ---------------------------------------------------------------- 建队列（digest 产物 → 待确认条目）

def key_of(meta: Dict[str, Any], paper_id: str) -> str:
    """与 ingest.dedup_key(PaperMetadata) 同一规则（同参数），保证队列键 == 入库闸门键。"""
    return dedup_key_fields(meta.get("doi"), meta.get("arxiv_id"), meta.get("title"),
                            fallback=paper_id, url=meta.get("url"))


def _venue(meta: Dict[str, Any]) -> str:
    return (meta.get("journal") or meta.get("container_title") or meta.get("source_type") or "").strip()


def _item(run_id: str, source: str, category: str, paper_id: str, meta: Dict[str, Any], dec: Dict[str, Any],
          abstract: str, dkey: str, weight: float, pool_size: int) -> Dict[str, Any]:
    return {
        "dedup_key": dkey, "paper_id": paper_id, "run_id": run_id, "source": source,
        "title": meta.get("title") or dec.get("title") or "", "venue": _venue(meta),
        "doi": meta.get("doi"), "arxiv_id": meta.get("arxiv_id"), "url": meta.get("url"),
        "abstract": re.sub(r"\s+", " ", abstract or "").strip()[:ABSTRACT_KEEP],
        "category": category, "default_checked": CATEGORY_DEFAULT_CHECKED[category],
        "decision": dec.get("decision"), "confidence": dec.get("confidence"),
        "bucket": list(dec.get("bucket") or []), "exclude_reason": dec.get("exclude_reason"),
        "one_line": dec.get("one_line") or "", "role": dec.get("role"),
        "prompt_version": dec.get("prompt_version"), "provider": dec.get("provider"),
        "served_model": dec.get("served_model"), "model": dec.get("model"),
        "weight": weight, "pool_size": pool_size,
        "state": "pending", "confirmed_at": None, "ingested_at": None,
    }


def _audit_quota(pool_n: int) -> int:
    k = TH.CONFIRM_AUDIT_MIN + pool_n // max(1, TH.CONFIRM_AUDIT_POOL_PER_EXTRA)
    return min(pool_n, max(TH.CONFIRM_AUDIT_MIN, min(TH.CONFIRM_AUDIT_MAX, k)))


def _rank(run_id: str, dkey: str) -> str:
    return hashlib.sha256("{}|audit|{}".format(run_id, dkey).encode("utf-8")).hexdigest()


def items_for_run(run_id: str, digest_json: Path, sidecar: Optional[Path],
                  skip_keys: Set[str]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """读一个 digest run：入选的全进（纳入/待定）+ 低置信 EXCLUDE 全进 + 高置信 EXCLUDE 随机抽查。

    skip_keys：已在库 / 已入过队列的键，不再问（问「库里已有的」是骚扰；问第二遍是重复）。
    只收 stage=llm_judge 的 EXCLUDE：黑名单关键词命中（确定性规则）不是模型判断，不在抽查范围。
    """
    d = json.loads(Path(digest_json).read_text(encoding="utf-8"))
    items: List[Dict[str, Any]] = []
    taken: Set[str] = set(skip_keys)
    n_skipped = 0
    for s in d.get("segments", []):
        fd = s.get("filter_decision")
        if not isinstance(fd, dict) or fd.get("verdict") != "included":
            continue
        meta, pid = s.get("metadata") or {}, s.get("paper_id") or ""
        k = key_of(meta, pid)
        if k in taken:
            n_skipped += 1
            continue
        taken.add(k)
        cat = "maybe" if fd.get("decision") == "MAYBE" else "include"
        items.append(_item(run_id, "digest", cat, pid, meta, fd, s.get("original_abstract"), k, 1.0, 0))

    low, high = [], []
    if sidecar and Path(sidecar).exists():
        sc = json.loads(Path(sidecar).read_text(encoding="utf-8"))
        for p in sc.get("papers", []):
            dec = p.get("decision")
            if not isinstance(dec, dict) or dec.get("stage") != "llm_judge" or dec.get("decision") != "EXCLUDE":
                continue
            meta, pid = p.get("metadata") or {}, p.get("paper_id") or ""
            k = key_of(meta, pid)
            if k in taken:
                n_skipped += 1
                continue
            taken.add(k)
            conf = dec.get("confidence")
            row = (k, pid, meta, dec, p.get("original_abstract"))
            (low if (conf is None or float(conf) <= TH.CONFIRM_LOWCONF_MAX) else high).append(row)
    for k, pid, meta, dec, ab in low:
        items.append(_item(run_id, "excluded", "lowconf_exclude", pid, meta, dec, ab, k, 1.0, len(low)))
    quota = _audit_quota(len(high))
    chosen = sorted(high, key=lambda r: _rank(run_id, r[0]))[:quota]
    w = (len(high) / quota) if quota else 0.0
    for k, pid, meta, dec, ab in chosen:
        items.append(_item(run_id, "excluded", "audit", pid, meta, dec, ab, k, w, len(high)))
    sampling = {"include": sum(i["category"] == "include" for i in items),
                "maybe": sum(i["category"] == "maybe" for i in items),
                "lowconf_exclude": len(low), "audit_pool": len(high), "audit_taken": len(chosen),
                "skipped_known": n_skipped}
    return items, sampling


@dataclass
class SyncReport:
    runs_seen: int = 0
    runs_new: int = 0
    items_added: int = 0
    queue_files: List[str] = field(default_factory=list)


def sync_queue(notes_dir, digest_dir, since: Optional[date] = None, until: Optional[date] = None, *,
               seen: Optional[Set[str]] = None, now: Optional[datetime] = None) -> SyncReport:
    """把窗口内**尚未入队**的 digest run 转成待确认条目，写进 _pending/confirm_<run 日期>.json。

    幂等：run_id 已登记在某个队列的 runs 里 → 跳过；键已在任何队列 / 在库里 → 条目级跳过。
    只处理 stats.json 已存在的 run（它是 workflow Step 4 最后写的文件；缺它说明 run 还没跑完或中途死了，
    此时 excluded sidecar 可能不全，抽查会漏——宁可明天再来）。
    """
    from .ingest import digest_runs
    now = now or datetime.now()
    rep = SyncReport()
    queues = {p: q for p, q in list_queues(notes_dir)}
    registered_runs = {rid for q in queues.values() for rid in (q.get("runs") or {})}
    skip = set(seen or ()) | all_queued_keys(list(queues.items()))
    digest_dir = Path(digest_dir)
    for ts, path in digest_runs(digest_dir):
        d = ts.date()
        if (since and d < since) or (until and d > until):
            continue
        rep.runs_seen += 1
        run_id = path.stem
        if run_id in registered_runs:
            continue
        if not (digest_dir / "{}_stats.json".format(run_id)).exists():
            logger.info("  跳过 {}：stats.json 尚未生成（run 未完成？）".format(run_id))
            continue
        sidecar = digest_dir / "_archive" / "excluded" / "{}_excluded.json".format(run_id)
        try:
            items, sampling = items_for_run(run_id, path, sidecar, skip)
        except (OSError, ValueError) as e:
            logger.warning("  ⚠️ 读取 {} 失败，跳过本 run：{}".format(run_id, e))
            continue
        skip |= {i["dedup_key"] for i in items}
        qp = queue_path(notes_dir, d.isoformat())
        q = queues.get(qp) or {"schema": SCHEMA, "run_date": d.isoformat(),
                               "created_at": now.isoformat(timespec="seconds"), "runs": {}, "items": []}
        q["runs"][run_id] = sampling
        q["items"].extend(items)
        _atomic_write(qp, _dump(q))
        queues[qp] = q
        rep.runs_new += 1
        rep.items_added += len(items)
        if str(qp) not in rep.queue_files:
            rep.queue_files.append(str(qp))
    return rep


# ---------------------------------------------------------------- 确认结果落盘

SOURCE_USER = "user"                    # 用户相对筛选器默认动过手（补勾/取消）：主动判断
SOURCE_DEFAULT = "default_accepted"     # 用户没动、照单全收默认勾选：不当作主动判断（S-stream/漂移默认排除）
DECISION_RETRACTED = "retracted"        # 撤销行（--reopen）：读取侧让同一 dedup_key 的此前标签失效


def label_row(item: Dict[str, Any], decision: str, run_date: str, now: datetime, *,
              source: str = SOURCE_USER, committed_at: Optional[datetime] = None) -> Dict[str, Any]:
    """stream_labels.jsonl 的一行（设计 §12.1）。

    confirmed_at = 该条所在页**实际点「确认本页」的时刻**（now 由调用方按页给）；committed_at = 汇总核对后
    点「确认提交」的时刻。user_decision_source：user / default_accepted（见上）。"""
    from .filter_eval import item_id_of
    cat = item["category"]
    return {
        "schema": SCHEMA, "confirmed_at": now.isoformat(timespec="seconds"), "run_date": run_date,
        "run_id": item.get("run_id"), "dedup_key": item["dedup_key"], "item_id": item_id_of(item["dedup_key"]),
        "paper_id": item.get("paper_id"), "title": item.get("title"), "abstract": item.get("abstract") or "",
        "category": cat, "default_checked": bool(item.get("default_checked")),
        "user_decision": decision,
        # 用户推翻筛选器：勾上原判 EXCLUDE（补勾）/ 取消筛选器想收的（取消）
        "override": (decision == "keep") != (cat in ("include", "maybe")),
        "filter_decision": item.get("decision"), "filter_confidence": item.get("confidence"),
        "filter_bucket": item.get("bucket") or [], "exclude_reason": item.get("exclude_reason"),
        "one_line": item.get("one_line") or "",
        "prompt_version": item.get("prompt_version"), "provider": item.get("provider"),
        "served_model": item.get("served_model"), "model": item.get("model"),
        "sample_weight": item.get("weight", 1.0), "pool_size": item.get("pool_size", 0),
        "user_decision_source": source,
        "committed_at": (committed_at or now).isoformat(timespec="seconds"),
    }


def record_decisions(notes_dir, decisions: Sequence[Tuple[Path, str, str]], *,
                     now: Optional[datetime] = None, sources: Optional[Dict[str, str]] = None,
                     times: Optional[Dict[str, datetime]] = None) -> int:
    """落盘一批确认：[(队列路径, dedup_key, 'keep'|'drop')]。

    顺序：先追加 stream_labels（重复行可容忍，读取时按 dedup_key 取最晚），再改队列状态。
    反过来的话，崩在两步之间会永久丢一条标签；这个顺序最坏只多一行重复。
    只处理仍是 pending 的条目（重复调用幂等）。返回实际落盘条数。
    sources：dedup_key → user/default_accepted（缺省 user）；times：dedup_key → 该条所在页的确认时刻
    （缺省 now）。now 同时作为 committed_at。
    """
    now = now or datetime.now()
    sources, times = sources or {}, times or {}
    by_q: Dict[Path, Dict[str, str]] = {}
    for qp, k, dec in decisions:
        if dec not in ("keep", "drop"):
            raise ValueError("user_decision 只能是 keep/drop：{!r}".format(dec))
        by_q.setdefault(Path(qp), {})[k] = dec
    lines, updates = [], []
    for qp, kd in by_q.items():
        q = load_queue(qp)
        for it in q["items"]:
            if it["dedup_key"] in kd and it.get("state") == "pending":
                k = it["dedup_key"]
                lines.append(label_row(it, kd[k], q.get("run_date", ""), times.get(k, now),
                                       source=sources.get(k, SOURCE_USER), committed_at=now))
                updates.append((q, it, kd[k], qp))
    if not lines:
        return 0
    lp = labels_path(notes_dir)
    lp.parent.mkdir(parents=True, exist_ok=True)
    with open(lp, "a", encoding="utf-8") as f:
        for r in lines:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    touched: Dict[Path, Dict[str, Any]] = {}
    for q, it, dec, qp in updates:
        it["state"] = dec
        it["confirmed_at"] = times.get(it["dedup_key"], now).isoformat(timespec="seconds")
        it["decision_source"] = sources.get(it["dedup_key"], SOURCE_USER)
        touched[qp] = q
    for qp, q in touched.items():
        _atomic_write(qp, _dump(q))
    return len(lines)


def load_stream_labels(notes_dir, *, since: Optional[datetime] = None,
                       until: Optional[datetime] = None, include_retracted: bool = False) -> List[Dict[str, Any]]:
    """读 stream_labels.jsonl：坏行跳过；同一 dedup_key 取 confirmed_at 最晚的一行（同刻取后写的）；
    最晚一行是撤销行（--reopen）则该键整体失效，除非 include_retracted；可按确认时间过滤。

    default_accepted 行在此**保留**（lint/审计要看）；评测与漂移各自按开关排除。"""
    p = labels_path(notes_dir)
    if not p.exists():
        return []
    by_key: Dict[str, Dict[str, Any]] = {}
    for ln in p.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            r = json.loads(ln)
            r["dedup_key"], r["confirmed_at"]
        except (ValueError, KeyError, TypeError):
            continue
        o = by_key.get(r["dedup_key"])
        if o is None or r["confirmed_at"] >= o["confirmed_at"]:
            by_key[r["dedup_key"]] = r
    out = []
    for r in by_key.values():
        if r.get("user_decision") == DECISION_RETRACTED and not include_retracted:
            continue
        try:
            t = datetime.fromisoformat(r["confirmed_at"])
        except ValueError:
            continue
        if (since and t < since) or (until and t > until):
            continue
        out.append(r)
    return sorted(out, key=lambda r: (r["confirmed_at"], r["dedup_key"]))


def reopen_run(notes_dir, run_date: str, *, now: Optional[datetime] = None) -> Tuple[str, int]:
    """撤销某 run 日期的确认：未入库 → 队列已确认条目复位 pending，并追加撤销行；已入库 → 拒绝。

    返回 (status, n)：status = ok / not_found / ingested（n = 受影响条数；ingested 时 n=已入库条数）。
    顺序同 record_decisions：先追加撤销行，再改队列（崩在中间最坏是多一行撤销，队列仍是已确认，可重跑）。
    """
    now = now or datetime.now()
    qp = queue_path(notes_dir, run_date)
    if not qp.exists():
        return "not_found", 0
    q = load_queue(qp)
    n_ing = sum(1 for it in q["items"] if it.get("ingested_at"))
    if n_ing:
        return "ingested", n_ing
    todo = [it for it in q["items"] if it.get("state") in ("keep", "drop")]
    if not todo:
        return "ok", 0
    lp = labels_path(notes_dir)
    lp.parent.mkdir(parents=True, exist_ok=True)
    with open(lp, "a", encoding="utf-8") as f:
        for it in todo:
            row = label_row(it, DECISION_RETRACTED, run_date, now, source=SOURCE_USER)
            row["override"] = False
            row["retracted_decision"] = it["state"]
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    for it in todo:
        it["state"], it["confirmed_at"] = "pending", None
        it.pop("decision_source", None)
        it.pop("ingest_attempts", None)
    _atomic_write(qp, _dump(q))
    return "ok", len(todo)


# ---------------------------------------------------------------- 弹窗会话

def short_title(title: str, n: int = TH.CONFIRM_TITLE_CHARS) -> str:
    from ..utils.osa_dialog import clean
    t = clean(title) or "（无标题）"
    return t if len(t) <= n else t[:n - 1] + "…"


def item_label(idx: int, item: Dict[str, Any]) -> str:
    """一行条目：`007 [低置信排除] 标题…`。序号前缀保证同标题条目也互不相同、可反查。"""
    return "{:03d} [{}] {}".format(idx, CATEGORY_LABEL[item["category"]], short_title(item.get("title", "")))


def paginate(seq: Sequence, size: int = TH.CONFIRM_PAGE_SIZE) -> List[List]:
    size = max(1, size)
    return [list(seq[i:i + size]) for i in range(0, len(seq), size)]


def write_summary_md(notes_dir, rows: Sequence[Tuple[Path, Dict[str, Any]]], now: datetime) -> Path:
    """「看摘要」入口：标题 + 筛选器一句话 + 摘要前 400 字，一份 md，交系统默认程序打开。"""
    L = ["# 待确认入库速览（{}）".format(now.strftime("%Y-%m-%d %H:%M")), "",
         "共 {} 篇。序号与弹窗一致；类别含义：纳入/待定=筛选器想收（默认勾选）；"
         "低置信排除/抽查排除=筛选器想丢（默认不勾，勾上才是推翻它）。".format(len(rows)), ""]
    for i, (_, it) in enumerate(rows, 1):
        conf = it.get("confidence")
        L.append("## {:03d} [{}] {}".format(i, CATEGORY_LABEL[it["category"]], (it.get("title") or "").strip()))
        meta = "筛选器 {}{}".format(it.get("decision") or "-",
                                 "（置信 {:.2f}）".format(conf) if conf is not None else "")
        if it.get("bucket"):
            meta += " · 维度 " + "/".join(it["bucket"])
        if it.get("exclude_reason"):
            meta += " · 排除原因 " + str(it["exclude_reason"])
        if it.get("venue"):
            meta += " · " + it["venue"]
        L.append("- " + meta)
        if it.get("one_line"):
            L.append("- 一句话：" + it["one_line"])
        if it.get("doi") or it.get("url"):
            L.append("- 链接：" + (("https://doi.org/" + it["doi"]) if it.get("doi") else it["url"]))
        ab = (it.get("abstract") or "")[:TH.CONFIRM_SUMMARY_CHARS]
        L.append("- 摘要：" + (ab + ("…" if len(it.get("abstract") or "") > TH.CONFIRM_SUMMARY_CHARS else "")
                              if ab else "（无摘要）"))
        L.append("")
    p = pending_dir(notes_dir) / SUMMARY_FILE.format(now.strftime("%Y-%m-%d"))
    _atomic_write(p, "\n".join(L))
    return p


DRAFT_FILE = "_draft.json"


def _load_draft(notes_dir) -> Dict[str, bool]:
    """会话草稿：上次没提交完的勾选（崩溃/取消/超时后续用）。**草稿不算确认**，只当预勾状态；坏文件当没有。"""
    try:
        d = json.loads((pending_dir(notes_dir) / DRAFT_FILE).read_text(encoding="utf-8"))
        return {k: bool(v) for k, v in (d.get("selection") or {}).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def _save_draft(notes_dir, sel: Dict[str, bool], now: datetime) -> None:
    _atomic_write(pending_dir(notes_dir) / DRAFT_FILE,
                  _dump({"saved_at": now.isoformat(timespec="seconds"), "selection": sel}))


def _clear_draft(notes_dir) -> None:
    try:
        (pending_dir(notes_dir) / DRAFT_FILE).unlink()
    except OSError:
        pass


@dataclass
class SessionResult:
    """status: nothing（无待确认）/ done（核对后提交，全部处理完）/ suspended（未提交：稍后/超时/取消/无 GUI）。
    提交是整批原子的，不再有 partial。keep_runs = 本次新增 keep 所在的 run 日期。
    n_changed = 相对筛选器默认改动的条数（n_added 补勾 + n_removed 取消）。"""
    status: str
    n_pending_before: int = 0
    n_keep: int = 0
    n_drop: int = 0
    n_still_pending: int = 0
    reason: str = ""
    keep_run_dates: List[str] = field(default_factory=list)
    n_added: int = 0
    n_removed: int = 0

    @property
    def n_changed(self) -> int:
        return self.n_added + self.n_removed


INTRO_HINT = "提示：列表页回车 = 确认本页；全部点完后还会再让你核对一次，核对后才真正生效。"
BTN_BACK, BTN_COMMIT = "返回修改", "确认提交"


def selection_stats(rows: Sequence[Tuple[Path, Dict[str, Any]]], sel: Dict[str, bool]) -> Dict[str, int]:
    """保留/丢弃数，以及相对筛选器默认（default_checked）的改动：补勾 = 默认不勾却勾了，取消 = 默认勾却没勾。"""
    keep = sum(1 for _, it in rows if sel[it["dedup_key"]])
    added = sum(1 for _, it in rows if sel[it["dedup_key"]] and not it.get("default_checked"))
    removed = sum(1 for _, it in rows if not sel[it["dedup_key"]] and it.get("default_checked"))
    return {"keep": keep, "drop": len(rows) - keep, "added": added, "removed": removed,
            "changed": added + removed}


def commit_message(st: Dict[str, int]) -> str:
    msg = "保留 {keep} / 丢弃 {drop}，其中相对筛选器默认改动了 {changed} 处（补勾 {added}、取消 {removed}）。".format(**st)
    tail = ("你完全接受了筛选器默认，这批只放行入库，不会记为你的标签。" if st["changed"] == 0
            else "只有这 {} 处会记为你的主动标签；没动的按「默认接受」记录。".format(st["changed"]))
    return msg + "\n" + tail + "\n\n「确认提交」后才入库、精读并落盘；「返回修改」回到第 1 页，保留你已改的勾选。"


def run_session(notes_dir, dialogs, *, now: Optional[datetime] = None,
                page_size: int = TH.CONFIRM_PAGE_SIZE, ask_timeout_s: int = TH.CONFIRM_ASK_TIMEOUT_S,
                page_timeout_s: int = TH.CONFIRM_PAGE_TIMEOUT_S, require_gui: bool = True,
                run_dates: Optional[Set[str]] = None,
                clock: Optional[Callable[[], datetime]] = None) -> SessionResult:
    """弹窗会话。各页「确认本页」只**暂存**勾选（内存 + 草稿文件）；全部点完后弹汇总核对框
    （默认按钮=返回修改，连按回车不会提交），点「确认提交」才整批落盘（队列 state + stream_labels）。
    任何一步取消/超时/崩溃 = 一条都不生效，全部仍是 pending。

    时间：每页确认的时刻取 clock()（缺省：给了 now 就固定为 now——测试用；否则 datetime.now）。
    ——旧版整场共用开场时的 now，导致一场里所有标签的 confirmed_at 都是同一个入队时刻（2026-10-21 实测）。
    """
    if clock is None:
        clock = (lambda: now) if now is not None else datetime.now
    now = now or clock()
    rows = pending_items(notes_dir, run_dates)
    res = SessionResult("nothing", n_pending_before=len(rows), n_still_pending=len(rows))
    if not rows:
        return res
    cnt = {c: sum(1 for _, it in rows if it["category"] == c) for c in CATEGORIES}
    summary = "纳入 {} · 待定 {} · 低置信排除 {} · 抽查排除 {}".format(
        cnt["include"], cnt["maybe"], cnt["lowconf_exclude"], cnt["audit"])
    if require_gui and not dialogs.available():
        res.status, res.reason = "suspended", "无图形会话，无法弹窗"
        return res

    msg = ("有 {} 篇论文待确认入库。\n{}\n\n未确认的不会入库，也不会花钱精读；"
           "选「稍后」或不理会，明天同一时间再提醒。\n\n{}").format(len(rows), summary, INTRO_HINT)
    while True:
        a = dialogs.ask(msg, ["稍后", "先看摘要", "开始确认"], "开始确认", title="Scholar 入库确认",
                        giving_up_s=ask_timeout_s, timeout_s=ask_timeout_s + 60)
        if a.status != "ok":
            res.status, res.reason = "suspended", "无人应答（{}）".format(a.status)
            return res
        if a.button == "先看摘要":
            dialogs.open_path(write_summary_md(notes_dir, rows, now))
            continue
        if a.button == "开始确认":
            break
        res.status, res.reason = "suspended", "用户选择稍后"
        return res

    sel = {it["dedup_key"]: bool(it.get("default_checked")) for _, it in rows}
    sel.update({k: v for k, v in _load_draft(notes_dir).items() if k in sel})     # 续上次没提交完的勾选
    page_time: Dict[str, datetime] = {}
    pages = paginate(rows, page_size)
    while True:
        gidx = 0
        for pno, page in enumerate(pages, 1):
            labels = [item_label(gidx + i + 1, it) for i, (_, it) in enumerate(page)]
            by_label = dict(zip(labels, page))
            gidx += len(page)
            defaults = [lb for lb, (_, it) in by_label.items() if sel[it["dedup_key"]]]
            c = dialogs.choose(
                labels, defaults, title="Scholar 入库确认 {}/{}".format(pno, len(pages)),
                prompt="勾选 = 入库并精读；不勾 = 丢弃。前两类已按筛选器意见预勾，后两类默认不勾（勾上即推翻筛选器）。"
                       "「确认本页」只是暂存，全部点完后还会再核对一次；「暂不确认」= 整场挂起、一条都不生效。",
                ok="确认本页", cancel="暂不确认", timeout_s=page_timeout_s)
            if c.status != "ok":
                res.status = "suspended"
                res.reason = "第 {} 页{}".format(
                    pno, {"cancel": "点了暂不确认", "timeout": "无人应答"}.get(c.status, "出错：" + c.detail))
                return res
            chosen = {s_ for s_ in c.selected if s_ in by_label}
            t = clock()
            for lb, (_, it) in by_label.items():
                sel[it["dedup_key"]] = lb in chosen
                page_time[it["dedup_key"]] = t
            _save_draft(notes_dir, sel, t)
        st = selection_stats(rows, sel)
        a = dialogs.ask(commit_message(st), [BTN_BACK, BTN_COMMIT], BTN_BACK, title="Scholar 入库确认：提交前核对",
                        giving_up_s=ask_timeout_s, timeout_s=ask_timeout_s + 60)
        if a.status != "ok":
            res.status, res.reason = "suspended", "汇总核对框无人应答（{}），未提交".format(a.status)
            return res
        if a.button == BTN_COMMIT:
            break

    t_commit = clock()
    dec, sources = [], {}
    keep_dates: Set[str] = set()
    for qp, it in rows:
        k = it["dedup_key"]
        dec.append((qp, k, "keep" if sel[k] else "drop"))
        changed = sel[k] != bool(it.get("default_checked"))
        sources[k] = SOURCE_USER if changed else SOURCE_DEFAULT
        if sel[k]:
            keep_dates.add(load_queue(qp).get("run_date", ""))
    record_decisions(notes_dir, dec, now=t_commit, sources=sources, times=page_time)
    _clear_draft(notes_dir)
    st = selection_stats(rows, sel)
    res.n_keep, res.n_drop, res.n_added, res.n_removed = st["keep"], st["drop"], st["added"], st["removed"]
    res.keep_run_dates = sorted(d for d in keep_dates if d)
    res.n_still_pending = 0
    res.status = "done"
    return res


# ---------------------------------------------------------------- 每日提醒状态

def _load_state(notes_dir) -> Dict[str, Any]:
    try:
        return json.loads((pending_dir(notes_dir) / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def mark_prompted(notes_dir, today: date) -> None:
    st = _load_state(notes_dir)
    st["last_prompted"] = today.isoformat()
    _atomic_write(pending_dir(notes_dir) / STATE_FILE, _dump(st))


def prompted_today(notes_dir, today: date) -> bool:
    return _load_state(notes_dir).get("last_prompted") == today.isoformat()


# ---------------------------------------------------------------- 入库后标记 / ingest 闸门

def mark_ingested(notes_dir, run_date: str, now: Optional[datetime] = None) -> int:
    """某 run 日期所在队列里 state=keep 且未入库的条目打 ingested_at。返回条数。"""
    now = now or datetime.now()
    qp = queue_path(notes_dir, run_date)
    if not qp.exists():
        return 0
    q = load_queue(qp)
    n = 0
    for it in q["items"]:
        if it.get("state") == "keep" and not it.get("ingested_at"):
            it["ingested_at"] = now.isoformat(timespec="seconds")
            n += 1
    if n:
        _atomic_write(qp, _dump(q))
    return n


MAX_INGEST_ATTEMPTS = 3      # ingest 退出码无法区分「全已在库（无新论文）」与失败：试满此次数就不再自动重试


def _awaiting_ingest(it: Dict[str, Any]) -> bool:
    return (it.get("state") == "keep" and not it.get("ingested_at")
            and int(it.get("ingest_attempts") or 0) < MAX_INGEST_ATTEMPTS)


def keep_run_dates_uningested(notes_dir) -> List[str]:
    """还有「已确认保留但未入库」条目的 run 日期（升序）。confirm_ingest 每天据此补跑入库，覆盖上次入库失败。"""
    return sorted({q.get("run_date", "") for _, q in list_queues(notes_dir)
                   if any(_awaiting_ingest(i) for i in q.get("items", []))} - {""})


def bump_ingest_attempts(notes_dir, run_date: str) -> int:
    """一次入库未成功：该 run 日期队列里等待入库的条目 ingest_attempts+1。返回达到上限的条数。"""
    qp = queue_path(notes_dir, run_date)
    if not qp.exists():
        return 0
    q = load_queue(qp)
    capped = 0
    for it in q["items"]:
        if _awaiting_ingest(it):
            it["ingest_attempts"] = int(it.get("ingest_attempts") or 0) + 1
            capped += it["ingest_attempts"] >= MAX_INGEST_ATTEMPTS
    _atomic_write(qp, _dump(q))
    return capped


def segment_from_excluded(item: Dict[str, Any], digest_dir, segment_id: int):
    """从 excluded sidecar 重建被用户勾上的原判 EXCLUDE 论文（PaperSegment），并套手选覆盖语义。

    sidecar 里存的就是送审时的元数据 + 摘要 + 完整裁决，无需联网、无需再调 LLM。
    找不到 / 校验失败返回 None（调用方告警，不静默）。
    """
    from .ingest import apply_manual_override
    from .schema import FilterDecision, PaperMetadata, PaperSegment
    sc = Path(digest_dir) / "_archive" / "excluded" / "{}_excluded.json".format(item["run_id"])
    try:
        data = json.loads(sc.read_text(encoding="utf-8"))
        rec = next(p for p in data.get("papers", []) if p.get("paper_id") == item["paper_id"])
        seg = PaperSegment(segment_id=segment_id, paper_id=item["paper_id"],
                           original_abstract=rec.get("original_abstract") or "",
                           metadata=PaperMetadata(**rec["metadata"]),
                           filter_decision=FilterDecision(**rec["decision"]))
    except (OSError, ValueError, StopIteration, KeyError, TypeError) as e:
        logger.warning("  ⚠️ 无法从 sidecar 重建 {}（{}: {}）".format((item.get("title") or "")[:48], type(e).__name__, e))
        return None
    apply_manual_override(seg)
    # 用户亲手勾上的，精读不该因为 priority_score=0（被排除的论文没走过打分）排到末尾
    seg.priority_score = max(seg.priority_score or 0.0, 0.9)
    return seg


@dataclass
class GateResult:
    segs: List[Any]
    n_confirmed: int = 0       # 窗口内 state=keep 的队列条目
    n_pending: int = 0
    n_dropped: int = 0
    n_override: int = 0        # 其中由原判 EXCLUDE 重建的
    n_rebuild_failed: int = 0
    pending_titles: List[str] = field(default_factory=list)


def gate_segments(segs: Sequence[Any], notes_dir, digest_dir, since: Optional[date], until: Optional[date], *,
                  seen: Optional[Set[str]] = None, sync: bool = True) -> GateResult:
    """ingest 的入库闸门：digest 里的 segs 只放行 state=keep 的；原判 EXCLUDE 被勾上的由 sidecar 重建补入。

    先 sync_queue：保证即使只有 ingest 这条 job 在跑（确认 job 没装），本周条目也会进队列——
    此时全是 pending → 放行 0 篇 → 无人值守的 ingest 自然变成「只通知、不入库」。
    """
    from .ingest import dedup_key
    if sync:        # --list / --dry-run 是零写盘入口，调用方传 sync=False（只读现有队列）
        sync_queue(notes_dir, digest_dir, since, until, seen=seen)
    keep_dig: Set[str] = set()
    keep_exc: List[Dict[str, Any]] = []
    res = GateResult(segs=[])
    for _, q in list_queues(notes_dir):
        try:
            d = date.fromisoformat(q.get("run_date", ""))
        except ValueError:
            continue
        if (since and d < since) or (until and d > until):
            continue
        for it in q.get("items", []):
            st = it.get("state")
            if st == "keep":
                res.n_confirmed += 1
                (keep_dig.add(it["dedup_key"]) if it["source"] == "digest" else keep_exc.append(it))
            elif st == "pending":
                res.n_pending += 1
                res.pending_titles.append(it.get("title") or "")
            elif st == "drop":
                res.n_dropped += 1
    out = [s for s in segs if dedup_key(s.metadata) in keep_dig]
    sid = 900000
    for it in keep_exc:
        sid += 1
        seg = segment_from_excluded(it, digest_dir, sid)
        if seg is None:
            res.n_rebuild_failed += 1
            continue
        out.append(seg)
        res.n_override += 1
    out.sort(key=lambda s: -(s.priority_score or 0))
    res.segs = out
    return res
