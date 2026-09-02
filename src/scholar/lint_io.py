# -*- coding: utf-8 -*-
"""知识层 lint：frontmatter/落盘/退出码/summarize（自 lint.py 拆出，阶段 4b——
backfill_notes 只依赖 summarize_lint_run，io 独立使其依赖面最小）。
外部一律经 `src.scholar.lint` 门面 import。
"""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ..utils.logger import get_logger
from .lint_checks import (
    LINT_GENERATOR, LINT_REPORT_NAME, LINT_SCHEMA_VERSION,
)
from .lint_ack import _LINT_SECTION_RE, split_lint_sections

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 7. 落盘
# ---------------------------------------------------------------------------

# 这份表要与 scripts/lint_notes.py 模块 docstring 里的退出码说明保持一致，改一处
# 记得改另一处（同 topics.BUILD_TOPICS_EXIT_CODES 的既有约定）。
#
# **1 优先于 2**（B2）：撤稿命中早在扫描阶段就算出来了、stdout 也已经打过 `🚨` 行，
# 但只要这一轮 `_lint.md` 恰好处于"生成块被手改"的冲突状态（哪怕跟撤稿毫无关系），
# 首版会先因为 conflict 返回 2，下游 `summarize_lint_run` 就只发一条"lint 未跑成"的
# 普通通知——**发现被静默降级**，正是这个模块自己反复强调要避免的事。现在两件事
# 同时发生时退 1（撤稿），落盘冲突的事实同时打进 stdout 与 stderr，
# `summarize_lint_run` 两边都读得到。
LINT_EXIT_CODES = {
    1: "发现已撤稿论文仍在库中（**优先于 2**：两件事同时发生时退 1，"
       "落盘冲突同时写进 stdout/stderr）",
    2: "配置/索引/向量库异常，或报告落盘冲突（且本轮没有撤稿命中）",
}


@dataclass
class LintOutcome:
    """`lint_notes.py` 子进程跑完后的结论，供月度回填这类无人值守调用方解读。

    `ok=False` 只用于「这一轮 lint 自己没跑成」（退出码 2 那类），**不包括**
    「跑成了并且发现了撤稿」——后者是 lint 干活干成了，要走 `alert` 这条独立的路。
    两者混成一个布尔会让调用方只能二选一：要么撤稿不响，要么工具故障被当成撤稿。
    """
    ok: bool
    alert: bool = False          # 有需要人立刻处理的发现（当前只有撤稿）
    detail: str = ""
    # freshness 低音量提醒：派生物陈旧（🧭⚠ 前缀行）不改退出码（"只有撤稿退 1"的
    # 既有约定），但 rc0 时 summarize 原本完全不读 stdout——报警写进一份要靠"死掉的
    # vault job"才能送达 Obsidian 的报告里，等于没报。这条独立字段让月度调用方能发
    # 一条普通通知，且与撤稿的 alert 硬信号互不混淆。
    freshness_alert: str = ""


def summarize_lint_run(stdout: str, stderr: str, returncode: int) -> LintOutcome:
    """把 `lint_notes.py` 子进程的输出压成一条人话结论。

    与 `topics.summarize_build_topics_run` 同一套做法与同一个教训（W3）：stdout 只截
    尾部几行、stderr 完全不读、退出码在通知里被拍扁成一个数字，会让生产事故里"到底
    哪条出了问题"在任何持久化日志中都查不到。诊断信息（退出码语义 + stderr）排在
    发现枚举之前，理由同 Y4：调用方的 notify 弹窗按 300 字符截断，被撤稿论文可能有
    好几篇，排在前面会把 stderr 里的关键提示挤出去。
    """
    lines = [ln.strip() for ln in (stdout or "").strip().splitlines()]
    hits = [ln for ln in lines if ln.startswith("🚨")]
    # freshness 的陈旧行用复合前缀 `🧭⚠`——**只有**这个前缀触发低音量提醒；普通 🧭
    # 行（新鲜/未判定）不触发，否则每次月度全新鲜也弹通知，告警面被训练成噪音。
    # 这里绝不抛异常：backfill 的调用点在 try 块外，抛了会吞掉整段通知。
    try:
        fresh_hits = [ln for ln in lines if ln.startswith("🧭⚠")]
        fresh_alert = "；".join(fresh_hits[:3])
    except Exception:
        fresh_alert = ""
    err = (stderr or "").strip()
    if returncode == 0:
        return LintOutcome(ok=True, freshness_alert=fresh_alert)
    if returncode == 1:
        detail = "发现 {} 篇已撤稿论文仍在库中".format(len(hits) or "若干")
        if err:
            detail += "；stderr：{}".format(err[:200])
        if hits:
            detail += "；{}".format("；".join(hits[:5]))
        return LintOutcome(ok=True, alert=True, detail=detail,
                           freshness_alert=fresh_alert)
    detail = "退出码 {}（{}）".format(returncode, LINT_EXIT_CODES.get(returncode, "未知退出码"))
    if err:
        detail += "；stderr：{}".format(err[:300])
    # B2：退出码 2 也要扫 `🚨` 行。撤稿命中优先退 1，所以走到这里通常意味着"lint 自己
    # 挂了"；但退出码只有一个，而 `ok`/`alert` 本来就是两个独立字段——真出现"既有撤稿
    # 又没跑成"的组合时（例如未来新增的退 2 分支跑在撤稿扫描之后），发现绝不能因为
    # 工具故障而丢失。两条都置上，调用方两条通知都发。
    if hits:
        detail += "；{}".format("；".join(hits[:5]))
    return LintOutcome(ok=False, alert=bool(hits), detail=detail,
                       freshness_alert=fresh_alert)


@dataclass
class LintCounts:
    """报告头部（frontmatter）里的可机读摘要。CLI 的退出码与月度触发器的通知文案
    都从这里取，不从渲染好的 markdown 里反解——那是 W3 踩过的坑（结论只存在于
    人类可读文本里，机器要靠重放才能知道发生了什么）。

    **默认值是 `None`（= 本轮未执行）而不是 0**，与正文里那四个 `*_skipped` 分支
    同一个理由：frontmatter 里写 `n_stale_claims: 0` 等于向任何读它的程序（以及
    日后可能出现的趋势图）断言"查过了，一条都没有"。跳过时写 `null`，语义无歧义。
    """
    retracted: Optional[int] = None
    contradictions: Optional[int] = None   # 判定为张力的句对（不含 none）
    candidates: Optional[int] = None
    stale_claims: Optional[int] = None
    orphans: Optional[int] = None
    batches_failed: Optional[int] = None
    scan_failed_dois: Optional[int] = None


# frontmatter 里的计数键 <- LintCounts 字段。A1 的计数结转与 checks_ran_at 都靠它。
_COUNT_KEYS: Dict[str, str] = {
    "retracted": "n_retracted",
    "contradictions": "n_contradictions",
    "candidates": "n_candidates",
    "stale_claims": "n_stale_claims",
    "orphans": "n_orphans",
    "batches_failed": "n_batches_failed",
    "scan_failed_dois": "n_scan_failed_dois",
}


def carry_forward_counts(counts: LintCounts,
                         prev_fm: Optional[Dict[str, Any]]) -> LintCounts:
    """本轮跑过的用新值，本轮跳过的（`None`）从上一版 frontmatter 结转（纯函数）。

    这样 `null` 的语义从"本轮没执行"收窄成**"从来没执行过"**——后者才是读它的程序
    （以及日后的趋势图）真正需要区分的那件事。上一版解析不出/键不是整数一律当作
    没有历史，绝不抛异常（同 `split_lint_sections` 的降级原则）。
    """
    out = LintCounts(**dict(counts.__dict__))
    if not isinstance(prev_fm, dict):
        return out
    for attr, key in _COUNT_KEYS.items():
        if getattr(out, attr) is not None:
            continue
        v = prev_fm.get(key)
        if isinstance(v, int) and not isinstance(v, bool):
            setattr(out, attr, v)
    return out


def build_lint_frontmatter(counts: LintCounts, generated_at: str,
                           preserved: Optional[Dict[str, Any]] = None,
                           checks_ran_at: Optional[Dict[str, str]] = None,
                           freshness_stale: Optional[int] = None) -> str:
    """受管键覆盖、用户自加键保留，同 topics.build_frontmatter 的约定。

    `checks_ran_at`（A1）是 `{section key: ISO 时间}`，本轮跑过的 key 用新时间、
    跳过的从上一版结转（值由正文里的 `<!-- LINT-SECTION ... ran_at=... -->` 标记
    反解，见 `write_lint_report`——单一事实来源，不在两处各记一份）。

    **不写 `topic` 键**：那个键是"我是一页概念页"的唯一判据（`render_topics_index` /
    `audit_topic_pages` / `stale_topic_slugs` / `sync_topics_to_vault` 四处都认它）。
    报告不是概念页，误认会让它出现在概念页索引里、被日历兜底强制"重合成"、被
    `--verify` 当页面审计——文件名的 `_` 前缀（`is_topic_page_file`）是第一道防线，
    这里不写 `topic` 是第二道。
    """
    from .topics import _render_frontmatter
    managed: Dict[str, Any] = {
        "title": "知识层 lint 报告",
        "type": "lint",
        "lint_schema": LINT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "checks_ran_at": dict(checks_ran_at or {}),
        "n_retracted": counts.retracted,
        "n_contradictions": counts.contradictions,
        "n_candidates": counts.candidates,
        "n_stale_claims": counts.stale_claims,
        "n_orphans": counts.orphans,
        "n_batches_failed": counts.batches_failed,
        "n_scan_failed_dois": counts.scan_failed_dois,
        "tags": ["札记/知识层lint"],
    }
    # freshness 的计数与四节的 null 语义体系刻意解耦：它不进 LintCounts/_COUNT_KEYS/
    # carry_forward（每轮真跑重算、不结转），跳过时**整键缺席**而不是写 null——
    # "null=从来没执行过"的语义已被 carry_forward_counts 占用，freshness 写 null
    # 就是在那套体系里说谎。缺席还必须防 preserved 穿透：preserved 是上一版完整
    # frontmatter，键掉出 managed 后旧值会被当"用户自加键"原样保留（下面那个循环），
    # 于是 skip 轮会显示上一轮的旧计数且无任何时效标注——这里显式剔除。
    if freshness_stale is not None:
        managed["n_freshness_stale"] = freshness_stale
    items = dict(managed)
    for k, v in (preserved or {}).items():
        if k not in managed and k != "n_freshness_stale":
            items[k] = v
    return _render_frontmatter(items)


def _insert_freshness_block(body: str, block: str) -> str:
    """把 freshness 文本块插进 body 的**第一个 LINT-SECTION 标记之前**。

    这是唯一安全的位置（PRD 三轮对抗审核独立收敛的结论）：split_lint_sections 丢弃
    首标记前文本 → 这一块永不被结转、不进 checks_ran_at；拼在 coverage 之后会被吸进
    该节 buffer、随 --skip-coverage 结转成永久化石；自带标记会污染 frontmatter 的
    checks_ran_at。找不到任何标记时（理论不可达：render 恒产出四节）追加尾部——
    此时 body 无节可结转，追加同样安全。
    """
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if _LINT_SECTION_RE.match(line.strip()):
            return "\n".join(lines[:i] + [block.rstrip("\n"), ""] + lines[i:])
    return body.rstrip("\n") + "\n\n" + block.rstrip("\n") + "\n"


def write_lint_report(topics_dir, body: str, counts: LintCounts, *,
                      dry_run: bool = False,
                      now: Optional[datetime] = None,
                      freshness_block: Optional[str] = None,
                      freshness_stale: Optional[int] = None) -> Tuple[Optional[Path], str]:
    """落盘 `topics/_lint.md`，走与概念页同一套哨兵合并。返回 `(路径, 状态)`。

    状态取值同 `topics.merge_topic_page`：`new`/`merged`/`conflict`/`unchanged`。
    走哨兵而不是直接覆盖，是因为这份报告天然会被人在旁边写批注（"这条我核对过，
    不是真冲突"）——那正是它的用法，覆盖掉等于让人白干。生成块被手改时报 conflict
    而不是强行覆盖，同 vault 约定。

    A1 的两件事在这里落地：
    - `checks_ran_at` 从 `body` 里的 section 标记**反解**出来，而不是让调用方再传一份
      ——正文与 frontmatter 只能有一个事实来源，两处各记一份迟早会对不上；
    - 计数从上一版 frontmatter 结转（`carry_forward_counts`），本轮跳过的项不再被
      写成 `null` 把上一次的结论抹掉。
    """
    from .notes_index import write_if_changed
    from .topics import merge_topic_page, read_existing
    now = now or datetime.now()
    path = Path(topics_dir) / LINT_REPORT_NAME
    existing, fm, _prev = read_existing(path)
    if freshness_block:
        body = _insert_freshness_block(body, freshness_block)
    ran_at = {k: v[0] for k, v in split_lint_sections(body).items() if v[0]}
    content, status = merge_topic_page(
        build_lint_frontmatter(carry_forward_counts(counts, fm),
                               now.isoformat(timespec="seconds"),
                               preserved=fm, checks_ran_at=ran_at,
                               freshness_stale=freshness_stale),
        body, existing, generator=LINT_GENERATOR)
    if content is None:
        return path, "conflict"
    if dry_run:
        return path, status
    changed = write_if_changed(path, content)
    return path, status if changed else "unchanged"
