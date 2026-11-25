# -*- coding: utf-8 -*-
"""周 digest 自动筛选：替代入库确认弹窗，由 LLM 按固定口径代用户勾选，事后只发一份报告。

背景（2026-11-02 用户决定）：每周待确认 100~170 篇，用户没有时间逐篇看。此前人工跟 Claude
对话完成过两轮这个筛选（口径见下方 CRITERIA），效果是：纳入/待定里真正值得留的约 15%，
且大半已被月度回填收进库。本模块把那套做法固化：

    pending(纳入/待定) ─► 库内标题去重 ─► LLM 逐篇研判(标题+摘要+筛选器一句话) ─► keep/drop
    pending(低置信排除/抽查排除) ───────────────────────────────────────────► 直接 drop

与弹窗流程共用同一套队列、record_decisions 与 ingest 闸门，只是决策者换成 LLM：
  * 标签写 user_decision_source=auto_agent，**不是人的判断**——filter_eval / profile_drift 默认排除，
    否则筛选器会拿 LLM 的口径给自己判分、画像漂移也会被 LLM 的偏好牵着走。
  * LLM 调用失败 / 漏判 / 解析失败的条目**保持 pending 不落盘**（不是默认丢弃），次日重试。
  * 报告列出每条 keep 的理由与被丢弃的纳入/待定条目，覆盖用 `confirm_ingest.py --reopen <日期>`。
"""
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import confirm as C
from ..utils.logger import get_logger

logger = get_logger("auto_triage")

SOURCE_AGENT = C.SOURCE_AGENT
BATCH_SIZE = 10
ABSTRACT_CHARS = 800
LLM_CATEGORIES = ("include", "maybe")          # 其余类别不送审，直接丢弃

CRITERIA = """\
你在替一位做 ICU/EHR 缺失机制研究的博士生筛每周文献。目标：判断一篇论文值不值得**入库并做全文精读**。
他的方向：(a) 缺失数据：MNAR/信息性缺失、影子变量、自删失、观测过程、插补及其失真、缺失感知建模、
跨院迁移；(b) ICU 合成数据/数字孪生，因此需要真实的 ICU 临床现象与处置逻辑作对照。

【留 keep】满足任一：
 1. 缺失/观测过程/插补/MNAR/因果识别/跨院迁移相关的方法或理论（真的在处理缺失机制才算；
    只是"缺失"二字沾边、实际把缺失当噪声抹掉的，降为 drop 或标 low）。
 2. ICU 临床现象描述：表型、亚型、轨迹、并发症过程、病程演变、多库/多中心的描述性发现。
 3. ICU 前沿操作、认知与争论：处置时机/阈值/策略、新评分与定义、有信息量的综述或指南。
 4. 处置混杂于病情、测量频率依赖病情、测量方式不一致的实例（含 target-trial emulation、
    landmark 设计、immortal time 处理）。
即使论文没有方法学贡献，只要是上面 2~4 类的 ICU 研究，也要留。

【丢 drop】：
 - 非 ICU 专科（肿瘤、眼科、消化、骨科、皮肤、产科、精神、牙科等）；
 - 影像/波形/ECG/EEG/病理/组学等非表格数据为主；
 - 与上述方向无关的通用 ML、扩散语言模型、联邦学习图像任务、LLM 基准；
 - 无信息量的评论/社论/来信/会议摘要/试验方案；
 - 摘要里没有任何可判断的实质内容且标题也不明确指向以上方向；
 - 动物/体外/猪模型实验；护理、心理社会、康复、营养肌肉、家属体验等 ICU 周边研究（不涉及病程或处置逻辑）；
 - 儿科或新生儿研究（用户的合成目标是成人 ICU），除非直接是缺失/观测过程方法；
 - 只报告单个生物标志物/评分与预后的关联、没有新的病程现象或处置逻辑的预测/关联研究，标 low，
   仅当它是跨库/多中心验证或明确讨论测量时机与缺失时才升到 normal。
拿不准时：成人 ICU 病程/处置相关倾向 keep(low)，其余倾向 drop。

priority：high=直接命中缺失机制或跨库/跨院设计或处置混杂的典型实例；normal=普通；low=边缘。
reason：一句中文，说明依据（点名方法/数据/发现）；若仅凭截断摘要，在末尾加"（仅凭截断摘要）"。
"""

OUTPUT_SPEC = """\
只输出一个 JSON 对象，不要任何其他文字：
{"verdicts":[{"id":"<原样照抄的 id>","verdict":"keep"|"drop","priority":"high"|"normal"|"low","reason":"…"}]}
必须对每个 id 恰好给一条；不要新增、遗漏或改写 id。"""


@dataclass(frozen=True)
class DigestHealthAlert:
    batch_date: str
    message: str


def _valid_rolling_weekly_run(digest_dir: Path, run_path: Path) -> bool:
    """JSON 与 stats 都可消费、且确实覆盖滚动近 ≥7 天，才算一个周批。"""
    try:
        stats_path = digest_dir / "{}_stats.json".format(run_path.stem)
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
        digest = json.loads(run_path.read_text(encoding="utf-8"))
        if stats.get("run_id") != run_path.stem or digest.get("digest_id") != run_path.stem:
            return False
        if stats.get("run_kind") != "rolling" or not isinstance(stats.get("window_days"), int):
            return False
        if stats["window_days"] < 7 or digest.get("status") != "completed":
            return False
        if not isinstance(digest.get("segments"), list):
            return False
        datetime.fromisoformat(stats["timestamp"])
        for key in ("total_emails", "total_papers", "processed_papers", "failed_papers"):
            if not isinstance(stats.get(key), int) or isinstance(stats.get(key), bool):
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def weekly_digest_alert(digest_dir, now: datetime) -> Optional[DigestHealthAlert]:
    """检查最近一个已越过周一 10:00 截止点的计划批次。

    周二至周日的 launchd 补跑仍检查刚过去的周一，不会因跨日漏报；同一批次是否
    已经通知由 CLI 的 _digest_health.json 去重。周一 10:00 前看上一周，避免把正在
    运行的本周任务报成故障。手动滚动补跑也算，但历史 date_range 回填绝不算。
    """
    monday = now.date() - timedelta(days=now.weekday())
    if now.weekday() == 0 and (now.hour, now.minute) < (10, 0):
        monday -= timedelta(days=7)
    scheduled_start = datetime.combine(monday, time(9, 0))
    scheduled_end = scheduled_start + timedelta(days=7)
    deadline = datetime.combine(monday, time(10, 0))
    if now < deadline:
        return None
    from .ingest import digest_runs

    digest_dir = Path(digest_dir)
    runs = list(digest_runs(digest_dir))
    # 下周一 09:00 起属于下一批，不能 retroactively 满足上一批；周二补跑仍落在本批窗口。
    candidates = [(ts, p) for ts, p in runs
                  if scheduled_start <= ts < scheduled_end and ts <= now]
    if any(_valid_rolling_weekly_run(digest_dir, p) for _ts, p in candidates):
        return None
    latest = max((ts.date() for ts, p in runs
                  if (digest_dir / "{}_stats.json".format(p.stem)).exists()), default=None)
    detail = ("该时段有 digest 产物，但没有可解析且覆盖滚动近 ≥7 天的完整批次"
              if candidates else "该时段没有 digest run")
    return DigestHealthAlert(monday.isoformat(), (
        "{} 周一批次异常：{}；最近有 stats 的 run：{}。"
        "请查 cron_digest.err.log 并手动补跑，不能按空队列处理"
        "（若 digest 实际仍在运行——本周批次曾跑到 39 分钟——或已在手动补跑，"
        "本提醒可忽略：通知只发一次，明日同一时间会自动复核）。").format(
            monday.isoformat(), detail, latest.isoformat() if latest else "无"))


def digest_alert_already_sent(notes_dir, batch_date: str) -> bool:
    p = C.pending_dir(notes_dir) / "_digest_health.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return batch_date in (data.get("alerts") or {})
    except (OSError, ValueError):
        return False


def record_digest_alert(notes_dir, batch_date: str, now: datetime) -> None:
    """按批次去重通知；健康检查本身仍每天返回非零，直到批次补齐。"""
    p = C.pending_dir(notes_dir) / "_digest_health.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    alerts = data.setdefault("alerts", {})
    alerts[batch_date] = {"notified_at": now.isoformat(timespec="seconds")}
    C._atomic_write(p, json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True))


# ------------------------------------------------------------------ 库内标题去重

def _norm_title(t: Optional[str]) -> str:
    return re.sub(r"[^0-9a-z一-鿿]+", "", (t or "").lower())


def library_titles(index_path) -> Dict[str, str]:
    """规范化标题 → citekey。身份键（DOI/arXiv/title）对不上时的第二道去重：
    同一篇论文先以无 DOI 的 title 键入队、库里是 doi 键（或反之），键不等但标题相同。"""
    p = Path(index_path)
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    out = {}
    for e in data.get("papers", []):
        k = _norm_title(e.get("title"))
        if len(k) >= 20:                      # 太短的标题碰撞概率高，不用
            out[k] = e.get("citekey") or e.get("dedup_key") or ""
    return out


# ------------------------------------------------------------------ 送审与解析

def build_prompt(batch: Sequence[Dict[str, Any]]) -> str:
    rows = []
    for it in batch:
        rows.append({
            "id": it["paper_id"],
            "title": it.get("title"),
            "abstract": (it.get("abstract") or "")[:ABSTRACT_CHARS] or "（无摘要）",
            "filter_note": it.get("one_line") or "",
            "filter_bucket": it.get("bucket") or [],
        })
    return "{}\n\n待研判论文（JSON）：\n{}\n\n{}".format(
        CRITERIA, json.dumps(rows, ensure_ascii=False, indent=1), OUTPUT_SPEC)


def parse_verdicts(raw: str, valid_ids) -> Dict[str, Dict[str, str]]:
    """解析 LLM 输出；id 不在 valid_ids 的丢掉、重复取首条、verdict 非法的丢掉（该条视为漏判，保持 pending）。"""
    s = (raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    if not s.startswith("{"):
        i, j = s.find("{"), s.rfind("}")
        s = s[i:j + 1] if 0 <= i < j else s
    data = json.loads(s)
    out: Dict[str, Dict[str, str]] = {}
    for v in data.get("verdicts", []):
        vid = str(v.get("id", ""))
        if vid not in valid_ids or vid in out:
            continue
        if v.get("verdict") not in ("keep", "drop"):
            continue
        pr = v.get("priority") if v.get("priority") in ("high", "normal", "low") else "normal"
        out[vid] = {"verdict": v["verdict"], "priority": pr, "reason": str(v.get("reason", ""))[:300]}
    return out


@dataclass
class Triage:
    keep: List[Tuple[Path, Dict[str, Any], Dict[str, str]]] = field(default_factory=list)
    drop_llm: List[Tuple[Path, Dict[str, Any], Dict[str, str]]] = field(default_factory=list)
    drop_dup: List[Tuple[Path, Dict[str, Any], str]] = field(default_factory=list)       # 库内已有
    drop_cat: List[Tuple[Path, Dict[str, Any]]] = field(default_factory=list)            # 低置信/抽查排除
    undecided: List[Tuple[Path, Dict[str, Any], str]] = field(default_factory=list)      # LLM 失败/漏判
    model: str = ""


def triage_items(rows: Sequence[Tuple[Path, Dict[str, Any]]], llm, *, model: Optional[str] = None,
                 lib_titles: Optional[Dict[str, str]] = None, batch_size: int = BATCH_SIZE,
                 call: Optional[Callable[..., str]] = None) -> Triage:
    """对待确认条目做决策，不落盘。call 可注入（测试用），缺省走 llm.call(json_mode=True)。"""
    lib_titles = lib_titles or {}
    tr = Triage(model=model or "")
    to_llm: List[Tuple[Path, Dict[str, Any]]] = []
    for qp, it in rows:
        if it.get("category") not in LLM_CATEGORIES:
            tr.drop_cat.append((qp, it))
            continue
        ck = lib_titles.get(_norm_title(it.get("title")))
        if ck:
            tr.drop_dup.append((qp, it, ck))
            continue
        to_llm.append((qp, it))

    def _call(prompt):
        if call:
            return call(prompt)
        return llm.call(prompt, model=model, max_tokens=6000, temperature=0.2, json_mode=True)

    for i in range(0, len(to_llm), batch_size):
        chunk = to_llm[i:i + batch_size]
        by_id = {it["paper_id"]: (qp, it) for qp, it in chunk}
        verdicts: Dict[str, Dict[str, str]] = {}
        err = ""
        for attempt in range(2):                       # 解析失败/漏判重试一次
            try:
                verdicts = parse_verdicts(_call(build_prompt([it for _, it in chunk])), set(by_id))
                if len(verdicts) == len(by_id):
                    break
                err = "漏判 {} 条".format(len(by_id) - len(verdicts))
            except Exception as e:                     # noqa: BLE001 —— 任何失败都只是"没判成"，不是丢弃
                err = "{}: {}".format(type(e).__name__, str(e)[:120])
        for pid, (qp, it) in by_id.items():
            v = verdicts.get(pid)
            if v is None:
                tr.undecided.append((qp, it, err or "无裁决"))
            elif v["verdict"] == "keep":
                tr.keep.append((qp, it, v))
            else:
                tr.drop_llm.append((qp, it, v))
        logger.info("  研判批次 {}/{}：keep {} · drop {} · 未判 {}".format(
            i // batch_size + 1, (len(to_llm) + batch_size - 1) // batch_size,
            sum(1 for p in by_id if verdicts.get(p, {}).get("verdict") == "keep"),
            sum(1 for p in by_id if verdicts.get(p, {}).get("verdict") == "drop"),
            sum(1 for p in by_id if p not in verdicts)))
    return tr


def apply(notes_dir, tr: Triage, *, now: Optional[datetime] = None) -> int:
    """落盘决策：keep/drop 经 record_decisions（先标签后状态），来源统一标 auto_agent。未判的不动。"""
    dec, sources = [], {}
    for qp, it, _ in tr.keep:
        dec.append((qp, it["dedup_key"], "keep"))
    for qp, it, _ in tr.drop_llm:
        dec.append((qp, it["dedup_key"], "drop"))
    for qp, it, _ in tr.drop_dup:
        dec.append((qp, it["dedup_key"], "drop"))
    for qp, it in tr.drop_cat:
        dec.append((qp, it["dedup_key"], "drop"))
    sources = {k: SOURCE_AGENT for _, k, _ in dec}
    return C.record_decisions(notes_dir, dec, now=now, sources=sources)


# ------------------------------------------------------------------ 报告

def _t(it, n=100):
    return (it.get("title") or "").strip().replace("\n", " ")[:n]


def render_report(tr: Triage, *, today: str, ingest_note: str = "") -> str:
    n_total = (len(tr.keep) + len(tr.drop_llm) + len(tr.drop_dup) + len(tr.drop_cat) + len(tr.undecided))
    L = ["# 周 digest 自动筛选报告 · {}".format(today), "",
         "共处理 **{}** 篇待确认：**保留 {}** · 丢弃 {}（LLM 判 {} + 库内已有 {} + 低置信/抽查排除档 {}）· "
         "**未判 {}（保持待确认，明天重试）**".format(
             n_total, len(tr.keep), len(tr.drop_llm) + len(tr.drop_dup) + len(tr.drop_cat),
             len(tr.drop_llm), len(tr.drop_dup), len(tr.drop_cat), len(tr.undecided)),
         "", "研判模型：{} ；标签来源 `auto_agent`（不计入筛选器评测与画像漂移）。".format(tr.model or "（默认）"), ""]
    if ingest_note:
        L += ["入库：{}".format(ingest_note), ""]
    order = {"high": 0, "normal": 1, "low": 2}
    if tr.keep:
        L += ["## 保留（{}）".format(len(tr.keep)), ""]
        for _, it, v in sorted(tr.keep, key=lambda r: order[r[2]["priority"]]):
            L.append("- **[{}]** {} — {}".format(v["priority"], _t(it), v["reason"]))
        L.append("")
    if tr.drop_llm:
        L += ["## 被 LLM 丢弃的纳入/待定条目（{}）— 想捞回的在这里找".format(len(tr.drop_llm)), ""]
        for _, it, v in tr.drop_llm:
            L.append("- {} — {}".format(_t(it), v["reason"]))
        L.append("")
    if tr.drop_dup:
        L += ["## 库内已有，按标题去重（{}）".format(len(tr.drop_dup)), ""]
        for _, it, ck in tr.drop_dup:
            L.append("- {} → `{}`".format(_t(it), ck))
        L.append("")
    if tr.undecided:
        L += ["## 未判（{}）— 保持待确认".format(len(tr.undecided)), ""]
        for _, it, e in tr.undecided:
            L.append("- {} （{}）".format(_t(it), e))
        L.append("")
    L += ["---", "覆盖某天的决定：`python scripts/confirm_ingest.py --reopen YYYY-MM-DD`（已入库的批次不能重开），"
          "然后手动 `python scripts/confirm_ingest.py` 走原弹窗。", ""]
    return "\n".join(L)


def notify_text(tr: Triage) -> str:
    return "保留 {} · 丢弃 {} · 未判 {}".format(
        len(tr.keep), len(tr.drop_llm) + len(tr.drop_dup) + len(tr.drop_cat), len(tr.undecided))
