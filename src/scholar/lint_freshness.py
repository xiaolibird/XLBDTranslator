# -*- coding: utf-8 -*-
"""派生物新鲜度检查（freshness）：把「忘了同步派生物」从靠记忆变成会报警。

纯计算、不联网、不花钱。四个子项，各自比对「派生物记录的源版本戳」与「源的当前
版本戳」：

  embed  向量库 embeddings.sqlite3 的 meta.source_generated_at vs 索引 generated_at
  vault  Obsidian vault 的 _meta.json 双戳（索引戳 + topics mtime，**逐分量**判）
  xlsx   桌面时间线 xlsx 的 sidecar meta（source_index_generated_at）+ 本体存在性
  refs   all_references.json 存在且可解析（pandoc 硬依赖，坏文件会毒害引用渲染）

三态判定（每个落后分量独立走一遍）：
  新鲜    派生物戳 == 源戳（字符串/浮点原样比较；三处派生物存的都是源戳的原样拷贝）
  ⌛未判定 落后，但落后分量自己的源戳距今 < 该子项 grace ——「源侧刚变，job 还没轮到」。
          月度链路（backfill 刷完 topics 立即跑 lint）永远落在这个窗口里，判陈旧
          就是每月狼来了、显示新鲜就是说谎，所以必须有第三态。
  陈旧    落后且超 grace。消息必须带责任 launchd job 与 err.log 路径（报出问题的
          同时指向行动）。

grace 按子项定（统一一个值曾被对抗审核证伪两次——太小假阳性、太大假阴性）：
  vault/xlsx 600s（WatchPaths ThrottleInterval 120s + 重建实测 ~15s）
  embed 1800s（Throttle 600s + 嵌入分钟级）

死 job 诊断（只在「落后 ∧ 派生物自身构建戳 > DEAD_JOB_DAYS」时启动）：
  - embed 例外：built_at 每次 sync 无条件刷新（含空 diff）+ RunAtLoad，是可信心跳
    ——落后 ∧ built_at 老于阈值 → 直接判陈旧，无需 launchctl。
  - vault/xlsx 的 self 戳只在真跑时更新，空窗数周后老戳是健康常态，不能独立升格；
    改为查一次 `launchctl list <label>`：明确 Could not find service → 陈旧（job 已
    卸载）；在位 → 维持原判、附注上次真跑时间与退出码；命令不可用/查询失败（Linux
    CI、SSH 非 gui 会话）→ 维持原判、附注诊断不可用，**不升格不崩**。
    测试经 job_probe 参数依赖注入，不 shell 真命令。

健壮性铁律：本模块**绝不抛异常**把 lint 搞成退 2。vault `_meta.json` 与 xlsx sidecar
是裸 write_text 非原子写（vault.py / export_timeline_xlsx.py），读到半截 JSON、
键缺失、类型不符（source_topics_mtime 是字符串）一律降级为「未判定：元数据不可读，
可能正在重写」。sqlite 撞上写窗（database is locked）同样未判定。

输出纪律（违者会污染既有告警链，见 test_lint_freshness 的回归用例）：
  - 全部输出走 stdout（stderr 头 300 字符会被 summarize_lint_run 拼进撤稿通知）；
  - 禁用 `🚨` 前缀（撤稿通知专属）、`⏳`（stale 节标题专属；本模块的沙漏是 ⌛，
    字形近似但码位不同）、反引号#ID 形态（会被 _ACK_ID_IN_TEXT_RE 当 ack ID）；
  - 普通行前缀 `🧭`，陈旧行前缀 `🧭⚠`——只有后者会被 summarize_lint_run 升格成
    低音量通知（全新鲜也弹通知会把告警面训练成噪音）。

本模块**不得被 lint 侧任何模块 import**（lint/lint_notes 经参数接收渲染好的文本块
与计数，方向恒为 CLI → 本模块，防 import 环——阶段 4 拆分 lint.py 时的硬约束）。
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# 各子项的宽限窗（秒）。键名同时是 grace_overrides 的键空间与 CLI --grace-seconds
# 标量展开的目标；refs 是存在性检查，没有 grace 概念，不在此表。
GRACE_DEFAULTS: Dict[str, int] = {"embed": 1800, "vault": 600, "xlsx": 600}

# 派生物自身构建戳老于这么多天且仍落后时，启动死 job 诊断（embed 直接判陈旧）。
DEAD_JOB_DAYS = 7

# launchd job 的 Label（以 plist 内 <key>Label</key> 的值为准，不是文件名——
# monthly 的 Label 就与文件名不同，这个坑在 PRD 审核里被专门点名过）。
JOB_LABELS: Dict[str, str] = {
    "embed": "com.xlbd.scholar-embed",
    "vault": "com.xlbd.scholar-vault",
    # xlsx 搭 vault job 的车（sync_vault.py 顺带调 export_timeline），责任 job 相同
    "xlsx": "com.xlbd.scholar-vault",
}
_LOG_DIR = "~/Library/Logs/xlbd-scholar-digest"
JOB_LOGS: Dict[str, str] = {
    "embed": _LOG_DIR + "/cron_embed.err.log",
    "vault": _LOG_DIR + "/cron_vault.err.log",
    "xlsx": _LOG_DIR + "/cron_vault.err.log",
}

_ITEM_LABELS = {"embed": "向量库", "vault": "vault", "xlsx": "时间线xlsx", "refs": "全局书目"}

FRESH, PENDING, STALE = "fresh", "pending", "stale"
_STATE_ICON = {FRESH: "✅", PENDING: "⌛", STALE: "⚠"}
_STATE_WORD = {FRESH: "新鲜", PENDING: "未判定", STALE: "陈旧"}


def _oneline(text: str) -> str:
    """detail 单行化 + HTML 注释定界符中和。

    两类注入都实证过：① POSIX 文件名可含换行——`b\\n<!-- LINT-SECTION … -->\\nc.md`
    能向报告正文注入伪节标记（穿透 checks_ran_at）、向 stdout 注入伪 `🚨` 行；
    ② **行内**伪 `<!-- END GENERATED … -->` 不需要换行——vault.extract_user_zone 用
    substring find(GEN_END)，下一轮 merge 会在伪哨兵处截断、把生成块尾巴复制进用户区
    （该 substring-find 是预存在缺陷，论文标题同样能载入；这里先把 freshness 新增的
    载体堵死）。所有出口统一在这里洗换行 + 中和注释定界符。"""
    flat = " ".join(str(text).split())
    # C0 控制符（\x00、\x1b——ANSI 序列可在终端日志里做视觉欺骗）与双向覆盖符一并剥掉
    flat = "".join(ch for ch in flat if ord(ch) >= 0x20 and ch not in "‪‫‬‭‮⁦⁧⁨⁩")
    return flat.replace("<!--", "<¡--").replace("-->", "--›")


@dataclass
class FreshnessItem:
    key: str
    state: str
    detail: str = ""

    @property
    def label(self) -> str:
        return _ITEM_LABELS.get(self.key, self.key)


@dataclass
class FreshnessReport:
    items: List[FreshnessItem] = field(default_factory=list)

    @property
    def n_stale(self) -> int:
        return sum(1 for it in self.items if it.state == STALE)

    def stdout_lines(self) -> List[str]:
        """陈旧行 `🧭⚠` 前缀（会被 summarize_lint_run 升格成通知），其余 `🧭`。"""
        out = []
        for it in self.items:
            prefix = "🧭⚠" if it.state == STALE else "🧭"
            out.append("{} {}：{}{}".format(
                prefix, it.label, _STATE_WORD.get(it.state, it.state),
                "——" + _oneline(it.detail) if it.detail else ""))
        return out

    def render_block(self, now: Optional[datetime] = None) -> str:
        """插进 _lint.md 哨兵内、首个 LINT-SECTION 标记之前的文本块。

        不带 LINT-SECTION 标记：split_lint_sections 丢弃首标记前文本，因此这一块
        永不被结转、不进 checks_ran_at——freshness 每轮都真跑（或被 --skip），
        不需要也不允许结转（结转一份过期的新鲜度结论比没有更糟）。
        """
        now = now or datetime.now()
        L = ["## 🧭 派生物新鲜度", ""]
        L.append("> 检查于 {}（本节不结转：每轮重算，跳过即缺席）".format(
            now.isoformat(timespec="seconds")))
        L.append("")
        for it in self.items:
            L.append("- {} **{}**：{}{}".format(
                _STATE_ICON.get(it.state, "?"), it.label,
                _STATE_WORD.get(it.state, it.state),
                "——" + _oneline(it.detail) if it.detail else ""))
        L.append("")
        return "\n".join(L)


# ---------------------------------------------------------------------------
# 时间口径（两对，不得交叉——索引侧是无时区秒级 ISO 字符串，topics 侧是 epoch float）
# ---------------------------------------------------------------------------

def _iso_age_seconds(iso: Any, now: datetime) -> Optional[float]:
    """无时区 ISO 字符串距今秒数；解析不了返回 None（同 lint._days_since 的 naive 口径）。"""
    if not isinstance(iso, str) or not iso:
        return None
    try:
        return (now - datetime.fromisoformat(iso)).total_seconds()
    except Exception:
        return None


def _epoch_age_seconds(mtime: Any, now_ts: float) -> Optional[float]:
    if isinstance(mtime, bool) or not isinstance(mtime, (int, float)):
        return None
    return now_ts - float(mtime)


def _fmt_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "?"
    if seconds < 0:
        return "未来{}秒".format(int(-seconds))
    if seconds < 3600:
        return "{}分钟前".format(max(0, int(seconds // 60)))
    if seconds < 86400:
        return "{:.1f}小时前".format(seconds / 3600)
    return "{:.1f}天前".format(seconds / 86400)


# ---------------------------------------------------------------------------
# 数据源读取（全部防御式：任何异常 → None + 原因标签，绝不上抛）
# ---------------------------------------------------------------------------

def _read_json(path: Optional[Path]) -> Tuple[Optional[dict], str]:
    """(dict, "") 或 (None, 原因)。裸 write_text 的半写/坏文件走"不可读"降级。"""
    if path is None:
        return None, "路径未定位"
    try:
        p = Path(path)
        if not p.exists():
            return None, "文件不存在"
        obj = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(obj, dict):
            return None, "顶层不是对象"
        return obj, ""
    except Exception as exc:
        return None, "元数据不可读，可能正在重写（{}）".format(type(exc).__name__)


def read_store_meta(db_path: Path) -> Tuple[Optional[Dict[str, str]], str]:
    """只读打开 sqlite 只查 meta 表——现有读法（VectorStore.load）要全量加载 145M
    向量 blob，为读两个时间戳不可接受。`mode=ro` URI 打开，WAL 库读不阻塞写；
    真撞上 `database is locked`（embed 写窗）→ (None, "locked")，未判定不报错。
    """
    p = Path(db_path)
    if not p.exists():
        return None, "missing"
    try:
        con = sqlite3.connect("file:{}?mode=ro".format(p), uri=True, timeout=2)
        try:
            rows = con.execute(
                "SELECT key, value FROM meta WHERE key IN "
                "('source_generated_at','built_at')").fetchall()
        finally:
            con.close()
        return {k: v for k, v in rows}, ""
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            return None, "locked"
        return None, "opfail:{}".format(exc)
    except Exception as exc:
        return None, "opfail:{}".format(type(exc).__name__)


def topics_mtime(notes_dir: Path) -> Optional[float]:
    """notes_dir/topics/ 下全部 *.md（rglob，含 qa/ 与 _lint.md）的最大 mtime。

    口径必须与 scripts/sync_vault.topics_mtime 及 src/scholar/vault.write_vault
    写进 `_meta.json` 的 source_topics_mtime **逐字相同**（rglob 口径），否则
    freshness 的 vault 双戳比对恒假阳/假阴——改任何一处必须同步改这三处。
    """
    try:
        tdir = Path(notes_dir) / "topics"
        if not tdir.is_dir():
            return None
        times = [p.stat().st_mtime for p in tdir.rglob("*.md")]
        return max(times) if times else None
    except Exception:
        return None


def _default_job_probe(label: str) -> Tuple[str, str]:
    """`launchctl list <label>` → ("loaded"|"missing"|"unavailable", 附注)。

    仅在死 job 诊断分支被调用（落后 ∧ self 戳超龄），零成本本地命令。
    Linux CI / SSH 非 gui 会话 / launchctl 挂起一律 "unavailable"（维持原判）。
    """
    try:
        p = subprocess.run(["launchctl", "list", label],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return "unavailable", ""
    if p.returncode == 0:
        m = re.search(r'"LastExitStatus"\s*=\s*(-?\d+)', p.stdout or "")
        code = m.group(1) if m else ""
        return "loaded", ("上次退出码 {}".format(code) if code and code != "0" else "")
    blob = (p.stderr or "") + (p.stdout or "")
    if "Could not find service" in blob:
        return "missing", ""
    return "unavailable", ""


# ---------------------------------------------------------------------------
# 三态判定核心
# ---------------------------------------------------------------------------

def _stale_hint(key: str) -> str:
    return "责任 job：{}，日志：{}".format(JOB_LABELS.get(key, "?"), JOB_LOGS.get(key, "?"))


def _judge_component(behind: bool, src_age: Optional[float], grace: float) -> str:
    """单个落后分量的三态：新鲜 / 未判定（源侧刚变）/ 陈旧。"""
    if not behind:
        return FRESH
    if src_age is None:
        return PENDING  # 源戳都解析不了，没资格断言陈旧
    if src_age < grace:
        return PENDING
    return STALE


def _diagnose_dead_job(key: str, self_age: Optional[float],
                       probe: Callable[[str], Tuple[str, str]]) -> Tuple[Optional[str], str]:
    """(强制状态 or None, 附注)。只对 vault/xlsx 调 launchctl；embed 走 built_at 心跳。"""
    if self_age is None or self_age < DEAD_JOB_DAYS * 86400:
        return None, ""
    status, extra = probe(JOB_LABELS[key])
    if status == "missing":
        return STALE, "job 已卸载（launchctl 查无 {}）".format(JOB_LABELS[key])
    if status == "loaded":
        note = "job 在位，上次真跑 {}".format(_fmt_age(self_age))
        if extra:
            note += "，" + extra
        return None, note
    return None, "job 诊断不可用"


def check_freshness(index_path: Path, notes_dir: Path,
                    vault_dir: Optional[Path],
                    timeline_xlsx_path: Optional[Path],
                    timeline_meta_path: Optional[Path], *,
                    grace_overrides: Optional[Dict[str, float]] = None,
                    now: Optional[datetime] = None,
                    job_probe: Optional[Callable[[str], Tuple[str, str]]] = None,
                    ) -> FreshnessReport:
    """四个子项的新鲜度报告（纯函数式：路径全显式传入，绝不自行探测生产路径——
    生产守卫在 CLI 层，见 scripts/lint_notes.py；A4「单测打到生产」事故的设防）。

    vault_dir / timeline_* 传 None 表示该派生物未定位 → 对应子项未判定。
    grace_overrides 按 GRACE_DEFAULTS 的键覆写（CLI --grace-seconds 标量展开成全键）。
    job_probe 供测试依赖注入，默认 shell `launchctl list`。
    """
    now = now or datetime.now()
    # 单钟：epoch 侧年龄从同一个 now 推（naive datetime 按本地时区解释，与本机文件
    # mtime 同一坐标系）。此前 epoch 侧独立取 time.time()，测试注入历史 now 时两钟
    # 分叉，是压测点名的陷阱。
    now_ts = now.timestamp()
    grace = dict(GRACE_DEFAULTS)
    for k, v in (grace_overrides or {}).items():
        # 值校验：None/bool/NaN 一律忽略保默认——本函数的铁律是绝不抛异常，
        # `age < None` 的 TypeError 会把整轮 lint 搞成退 2（压测实证）。
        if (k in grace and isinstance(v, (int, float))
                and not isinstance(v, bool) and v == v):
            grace[k] = v
    probe = job_probe or _default_job_probe
    rep = FreshnessReport()

    # 源侧当前戳（延迟 import：embed_store 顶层拉 numpy，只为一个 8KB 头部正则。
    # import 失败也不许抛——numpy 缺失的环境里全部子项降级为未判定）
    try:
        from .embed_store import DB_NAME, read_index_generated_at
    except Exception as exc:
        for key in ("embed", "vault", "xlsx", "refs"):
            rep.items.append(FreshnessItem(
                key, PENDING, "内部依赖不可用（{}）".format(type(exc).__name__)))
        return rep
    try:
        index_stamp = read_index_generated_at(Path(index_path)) or ""
    except Exception:
        index_stamp = ""
    index_age = _iso_age_seconds(index_stamp, now)

    # ---- embed：向量库 vs 索引 ----
    meta, err = read_store_meta(Path(notes_dir) / DB_NAME)
    if not index_stamp:
        rep.items.append(FreshnessItem("embed", PENDING, "索引 generated_at 不可读"))
    elif meta is None:
        if err == "missing":
            rep.items.append(FreshnessItem("embed", PENDING, "从未建库（embeddings.sqlite3 不存在）"))
        elif err == "locked":
            rep.items.append(FreshnessItem("embed", PENDING, "库正被写入（database is locked）"))
        else:
            rep.items.append(FreshnessItem("embed", PENDING, "库 meta 不可读（{}）".format(err)))
    else:
        src = str(meta.get("source_generated_at") or "")
        behind = src != index_stamp
        state = _judge_component(behind, index_age, grace["embed"])
        detail = ""
        built_age = _iso_age_seconds(meta.get("built_at"), now)
        if behind:
            detail = "库基于 {}，索引已是 {}（{}）".format(
                src or "（无戳）", index_stamp, _fmt_age(index_age))
            # embed 例外：built_at 是可信心跳，落后 ∧ 心跳超龄 → 直接陈旧
            if built_age is not None and built_age > DEAD_JOB_DAYS * 86400:
                state = STALE
                detail += "；job 可能已停跑（built_at {}）".format(_fmt_age(built_age))
        if state == STALE:
            detail += "。" + _stale_hint("embed")
        rep.items.append(FreshnessItem("embed", state, detail))

    # ---- vault：双戳逐分量 ----
    vmeta, verr = _read_json(Path(vault_dir) / "_meta.json" if vault_dir else None)
    if vmeta is None:
        rep.items.append(FreshnessItem(
            "vault", PENDING,
            "vault 未定位" if vault_dir is None else "_meta.json：{}".format(verr)))
    else:
        comp_states: List[str] = []
        details: List[str] = []
        # 分量 1：索引戳。grace 用索引戳自己的年龄。
        src_idx = vmeta.get("source_index_generated_at")
        if not index_stamp:
            comp_states.append(PENDING)
            details.append("索引 generated_at 不可读")
        elif not isinstance(src_idx, str):
            comp_states.append(PENDING)
            details.append("source_index_generated_at 类型异常")
        else:
            behind = src_idx != index_stamp
            comp_states.append(_judge_component(behind, index_age, grace["vault"]))
            if behind:
                details.append("索引分量：vault 基于 {}，当前 {}（{}）".format(
                    src_idx, index_stamp, _fmt_age(index_age)))
        # 分量 2：topics mtime。grace 用 topics 当前 mtime 自己的年龄——月度链路里
        # topics 尾巴刚写而索引早已同步，混用索引戳年龄会把健康 vault 判陈旧。
        cur_tm = topics_mtime(Path(notes_dir))
        src_tm = vmeta.get("source_topics_mtime")
        if cur_tm is not None:
            if isinstance(src_tm, bool) or not isinstance(src_tm, (int, float, type(None))):
                comp_states.append(PENDING)
                details.append("source_topics_mtime 类型异常")
            else:
                behind = (src_tm is None) or (float(src_tm) != cur_tm)
                tm_age = _epoch_age_seconds(cur_tm, now_ts)
                comp_states.append(_judge_component(behind, tm_age, grace["vault"]))
                if behind:
                    details.append("topics 分量：vault 快照 {}，notes 侧最新改动 {}".format(
                        src_tm, _fmt_age(tm_age)))
        # 汇总：stale > pending > fresh
        state = STALE if STALE in comp_states else (
            PENDING if PENDING in comp_states else FRESH)
        # 死 job 诊断（self 戳 = vault 上次真跑时间）
        forced, note = (None, "")
        if any(s != FRESH for s in comp_states):
            forced, note = _diagnose_dead_job(
                "vault", _iso_age_seconds(vmeta.get("generated_at"), now), probe)
        if forced:
            state = forced
        if note:
            details.append(note)
        detail = "；".join(details)
        if state == STALE:
            # 页级详情：只在判陈旧时补算，帮人定位是哪几页没同步过去
            lag_pages = _lagging_topic_pages(Path(notes_dir), src_tm)
            if lag_pages:
                detail += "；落后页：" + "、".join(lag_pages)
            detail += "。" + _stale_hint("vault")
        rep.items.append(FreshnessItem("vault", state, detail))

    # ---- xlsx：sidecar 戳 + 本体存在 ----
    tmeta, terr = _read_json(Path(timeline_meta_path) if timeline_meta_path else None)
    xlsx_exists = bool(timeline_xlsx_path) and Path(timeline_xlsx_path).exists()
    if tmeta is None:
        if timeline_meta_path is None:
            rep.items.append(FreshnessItem("xlsx", PENDING, "时间线路径未定位"))
        elif terr == "文件不存在" and not xlsx_exists:
            rep.items.append(FreshnessItem("xlsx", PENDING, "从未导出（xlsx 与 sidecar 均不存在）"))
        else:
            rep.items.append(FreshnessItem("xlsx", PENDING, "sidecar：{}".format(terr)))
    elif not xlsx_exists:
        rep.items.append(FreshnessItem(
            "xlsx", STALE,
            "sidecar 在而 xlsx 本体不存在（可能被手删）。" + _stale_hint("xlsx")))
    else:
        src = tmeta.get("source_index_generated_at")
        if not index_stamp:
            rep.items.append(FreshnessItem("xlsx", PENDING, "索引 generated_at 不可读"))
        elif not isinstance(src, str):
            rep.items.append(FreshnessItem("xlsx", PENDING, "source_index_generated_at 类型异常"))
        else:
            behind = src != index_stamp
            state = _judge_component(behind, index_age, grace["xlsx"])
            detail = ""
            if behind:
                detail = "xlsx 基于 {}，索引已是 {}（{}）".format(
                    src, index_stamp, _fmt_age(index_age))
                forced, note = (None, "")
                forced, note = _diagnose_dead_job(
                    "xlsx", _iso_age_seconds(tmeta.get("written_at"), now), probe)
                if forced:
                    state = forced
                if note:
                    detail += "；" + note
            if state == STALE:
                detail += "。" + _stale_hint("xlsx")
            rep.items.append(FreshnessItem("xlsx", state, detail))

    # ---- refs：存在 + 可解析 ----
    refs_path = Path(notes_dir) / "all_references.json"
    if not refs_path.exists():
        rep.items.append(FreshnessItem(
            "refs", STALE, "all_references.json 不存在（pandoc 书目硬依赖）"))
    else:
        try:
            json.loads(refs_path.read_text(encoding="utf-8"))
            rep.items.append(FreshnessItem("refs", FRESH, ""))
        except Exception as exc:
            rep.items.append(FreshnessItem(
                "refs", STALE,
                "all_references.json 损坏（{}），会毒害 pandoc 引用渲染".format(
                    type(exc).__name__)))

    return rep


def _lagging_topic_pages(notes_dir: Path, src_tm: Any, limit: int = 5) -> List[str]:
    """notes 侧 mtime 晚于 vault 快照的 topics 页名（最多 limit 个，供陈旧详情）。"""
    try:
        if isinstance(src_tm, bool) or not isinstance(src_tm, (int, float)):
            return []
        tdir = Path(notes_dir) / "topics"
        if not tdir.is_dir():
            return []
        lag = [(p.stat().st_mtime, p.name) for p in tdir.rglob("*.md")
               if p.stat().st_mtime > float(src_tm)]
        lag.sort(reverse=True)
        names = [n for _, n in lag[:limit]]
        if len(lag) > limit:
            names.append("等{}页".format(len(lag)))
        return names
    except Exception:
        return []
