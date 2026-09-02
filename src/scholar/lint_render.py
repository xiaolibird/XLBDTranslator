# -*- coding: utf-8 -*-
"""知识层 lint：整份报告渲染（自 lint.py 拆出，阶段 4b——唯一把四项检查的产出
汇聚到一起的层，依赖方向单一：render → checks/ack/io）。
注意 render_lint_report 签名里的 LintCounts 是字符串注解，grep 不可见——
必须显式 import（PRD 审定点名的坑）。外部一律经 `src.scholar.lint` 门面 import。
"""
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..utils.logger import get_logger
from .topics import is_topic_page_file
from .vault import ROLE_LABEL, split_frontmatter
from .lint_checks import (
    CandidatePair, CoverageReport, PairSide, RetractionScan, StaleClaim, Verdict,
    VerdictReport, DEFAULT_BATCH_SIZE,
    DEFAULT_MAX_PAIRS, DEFAULT_ORPHAN_LIMIT, DEFAULT_PAIR_MIN_SIM,
    DEFAULT_RECENT_MONTHS, DEFAULT_ROLES_A, DEFAULT_ROLES_B,
    DEFAULT_STALE_ANCHOR_LIMIT, DEFAULT_STALE_YEARS,
    RELATION_EMOJI, RELATION_LABEL, REPORTABLE_RELATIONS, year_is_implausible,
)
from .lint_ack import (
    ACK_ID_MARK, DEFAULT_CONTRADICTION_REMINDER_DAYS, LINT_SECTIONS, LINT_SECTION_MARK, SECTION_COUNT_KEY,
    SECTION_COUNT_UNIT, SECTION_HEADING, SECTION_NAME, SECTION_SKIP_FLAG,
    _carry_section, _fold_lead, _fold_note, _fold_open, _must_do_line,
    _squeeze_blanks, _status_line, contradiction_reminder, fold_acked_blocks,
    ids_in_text, split_lint_sections,
)
from .lint_io import LintCounts

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 6. 报告渲染
# ---------------------------------------------------------------------------

def _loc(side: PairSide) -> str:
    if not side.note_file:
        return ""
    return " · {}{}".format(side.note_file, ":{}".format(side.note_line) if side.note_line else "")


def _pair_lines(v: Verdict) -> List[str]:
    p = v.pair
    if v.relation == "self-inconsistency":
        # A4：这一档**不能**用「甲 ↔ 乙」的对撞模板。矛盾是其中一篇自己的（"本文附录 I
        # 与附录 R 对 GPU 型号不一致"），另一篇只是碰巧撞上了它内部矛盾的一半；写成
        # 跨论文对撞会把读者送去核对"这两篇谁的数字对"，而正确动作是回去翻那一篇自己
        # 的原文。标题点名那一篇（模型没点名时如实写"裁决未点名"，不猜）。
        if v.subject in ("a", "b"):
            hit = p.a if v.subject == "a" else p.b
            other = p.b if v.subject == "a" else p.a
            head = "#### {} {}｜`[@{}]` 自己的陈述前后不一致 `#{}`".format(
                RELATION_EMOJI["self-inconsistency"], RELATION_LABEL["self-inconsistency"],
                hit.citekey, p.pid)
            hint = ("→ **正确动作是回去翻 `[@{}]` 自己的原文**（不是跨两篇核对数字）；"
                    "`[@{}]` 只是碰巧撞上了它内部矛盾的其中一半。".format(
                        hit.citekey, other.citekey))
        else:
            head = "#### {} {}｜`[@{}]` 与 `[@{}]` 其中一篇自身（裁决未点名）`#{}`".format(
                RELATION_EMOJI["self-inconsistency"], RELATION_LABEL["self-inconsistency"],
                p.a.citekey, p.b.citekey, p.pid)
            hint = ("→ 裁决说这是**某一篇自己**的内部矛盾但没点名是哪一篇，"
                    "读下面两句原文与裁决说明判断该翻谁的原文。")
        lines = [head, "", hint, ""]
    else:
        lines = ["#### {} {}｜`[@{}]` ↔ `[@{}]`（相似度 {:.2f}）`#{}`".format(
            RELATION_EMOJI.get(v.relation, "•"), RELATION_LABEL.get(v.relation, v.relation),
            p.a.citekey, p.b.citekey, p.score, p.pid), ""]
    if v.note:
        lines += ["{}".format(v.note), ""]
    for side, label in ((p.a, "甲"), (p.b, "乙")):
        head = "- **{}** `[@{}]`".format(label, side.citekey)
        meta = [m for m in (ROLE_LABEL.get(side.role or "", side.role or ""),
                            side.section or "", str(side.year) if side.year else "") if m]
        if meta:
            head += "（{}）".format(" · ".join(meta))
        head += _loc(side)
        lines.append(head)
        if side.title:
            lines.append("  <small>{}</small>".format(side.title))
        lines.append("  > {}".format(side.text))
    lines.append("")
    return lines




# ---------------------------------------------------------------------------
# 6c. 整份报告
# ---------------------------------------------------------------------------

def render_lint_report(*, verdicts: Sequence[Verdict], candidates: Sequence[CandidatePair],
                       verdict_report: VerdictReport, retraction: RetractionScan,
                       stale_claims: Sequence[StaleClaim], coverage: CoverageReport,
                       cited_by_page: Optional[Dict[str, List[str]]] = None,
                       params: Optional[Dict[str, Any]] = None,
                       contradictions_skipped: bool = False,
                       stale_skipped: bool = False,
                       coverage_skipped: bool = False,
                       stale_years: int = DEFAULT_STALE_YEARS,
                       stale_anchor_limit: int = DEFAULT_STALE_ANCHOR_LIMIT,
                       now: Optional[datetime] = None,
                       previous: Optional[Dict[str, Tuple[str, str]]] = None,
                       acks: Optional[Dict[str, str]] = None,
                       ack_files: Optional[Sequence[Any]] = None,
                       counts: Optional["LintCounts"] = None,
                       previous_counts: Optional[Dict[str, Any]] = None,
                       contradiction_reminder_days: int =
                       DEFAULT_CONTRADICTION_REMINDER_DAYS) -> str:
    """整份报告的正文（哨兵之间的部分）。

    每一节都必须自带**分母与口径**：这份报告最大的失败模式不是漏报，而是让人误以为
    已经查干净了。所以"0 条"从来不单独出现，永远跟着"在 N 篇里查了 M 篇"。

    **被跳过的一项绝不能渲染成"✅ 没发现问题"**——四个 `*_skipped` 标志存在的唯一
    理由就是这个。首版实测踩过：`--skip-stale --skip-coverage` 跑出来的报告里，两节
    都写着一个大绿勾，而它们压根没跑——这比不生成报告危险得多，因为它给出的是
    **主动的**虚假保证。

    A1：跳过的一项**从上一版原样结转**（`previous`，由 `read_previous_lint` 提供），
    并在标题下方插一条时效横幅。首版是每次整块重建，四项互相踩踏：月度回填固定跑
    `--skip-contradictions`，人手动跑的全量对撞结果下个月被无声抹掉。结转不到才退回
    "本轮未执行且没有可结转的历史"的写法——`null`/"未执行"的语义因此收窄成
    **"从来没跑过"**，比"本轮没执行"有用得多。

    ## 第 2 轮：从「全量清单」改成「待办」

    验收判词是「报告是库的持续状态」这件事机制做对了，但「报告是给人看的待办」还没
    开始做——它仍是一份每月逐字重印的全量清单，用户唯一能做的动作（ack）**既在他读
    它的地方无效，也在自动化跑它的节奏下无效**。四个新入参就是为这件事：

    - `acks` 现在作用于**三节**（对撞 / 陈旧 / 孤儿），且对**结转来的文本**也生效
      （`_carry_section` → `fold_acked_blocks`，纯文本折叠，不重跑 LLM）；
    - `ack_files`：报告里的 ack 指引写死绝对路径，把 vault 那份副本一并点名——
      用户读报告的地方是 Obsidian，而那份有它自己独立的批注区（L1/N2）；
    - `counts` / `previous_counts`：顶部状态行一行两用（时效 + 计数），
      并在它之上加一行「本轮必须处理」只放硬信号（L6）。

    N1：状态行的 ✅/⏸ **只认 `rendered` 里的 `is_fresh` 布尔**，绝不从时间戳反推。
    """
    params = params or {}
    cited_by_page = cited_by_page or {}
    now = now or datetime.now()
    now_iso = now.isoformat(timespec="seconds")
    previous = previous or {}
    acks = acks or {}
    previous_counts = previous_counts if isinstance(previous_counts, dict) else {}

    fresh: Dict[str, List[str]] = {
        "retraction": _retraction_section(retraction, cited_by_page),
        "contradictions": _contradiction_section(
            verdicts, candidates, verdict_report, params, acks),
        "stale": [SECTION_HEADING["stale"], ""] + _stale_section(
            stale_claims, params, stale_years, acks,
            (previous.get("stale") or ("", ""))[1], stale_anchor_limit),
        "coverage": [SECTION_HEADING["coverage"], ""] + _coverage_section(
            coverage, acks, (previous.get("coverage") or ("", ""))[1]),
    }
    skipped = {"retraction": retraction.skipped, "contradictions": contradictions_skipped,
               "stale": stale_skipped, "coverage": coverage_skipped}

    rendered: List[Tuple[str, str, str, bool]] = []      # (key, ran_at, 正文, 本轮真跑过)
    for key in LINT_SECTIONS:
        if not skipped[key]:
            # `_squeeze_blanks`：各节拼装时"上一段自带尾空行 + 下一段自带首空行"会留下
            # 连续两个空行（折叠区前面就是这个形状）。以前靠 `fold_acked_blocks` 结转时
            # 顺手压掉，而 A3 的守卫让陈旧/孤儿两节结转时**原样返回**——不在源头压掉，
            # 那两个空行就会一直留在磁盘上。markdown 语义不变，只是别让它长住。
            rendered.append((key, now_iso,
                             "\n".join(_squeeze_blanks(fresh[key])).strip("\n"), True))
            continue
        carried = previous.get(key)
        # N6：`carried[0]`（ran_at）为空 = 上一版那一节本身就是「从未执行且无历史可结转」
        # 的占位文本。把它当成"上一次运行的结果"结转回来，会渲染出一节自己打自己脸的
        # 东西：横幅说"以下是上一次运行的结果"，正文说"没有可结转的历史结果"。
        if carried and carried[1].strip() and carried[0]:
            rendered.append((key, carried[0],
                             _carry_section(key, carried[0], carried[1], now, acks), False))
        else:
            rendered.append((key, "", "\n".join(_never_ran_section(key)).strip("\n"), False))

    # 状态行里的计数：本轮真跑过的用本轮的值，结转的从上一版 frontmatter 拿
    # （与 `carry_forward_counts` 同一口径，但那一步发生在落盘时、拿不到，所以这里
    # 直接读上一版 frontmatter；两边都拿不到就不写计数，而不是写 0）。
    status_counts: Dict[str, Optional[int]] = {}
    for key, _ran, _t, is_fresh in rendered:
        v = None
        if is_fresh and counts is not None:
            v = getattr(counts, {"retraction": "retracted",
                                 "contradictions": "contradictions",
                                 "stale": "stale_claims", "coverage": "orphans"}[key], None)
        elif not is_fresh:
            pv = previous_counts.get(SECTION_COUNT_KEY[key])
            v = pv if isinstance(pv, int) and not isinstance(pv, bool) else None
        status_counts[key] = v

    # B5：顶部计数是"扫描总量"而不是"还剩多少待办"，ack 再多第一屏数字永远不降——
    # 于是"这是一份会随我干活而变短的待办"这个感觉建立不起来（M12 报告顶部写 39 条、
    # 正文写"22 篇老文献是这 36 条论断的唯一地基"，中间没有一句话解释那 3 条差在哪）。
    # 只对**本轮真跑过**的节给待办口径：结转来的那一节我们手上没有条目对象，
    # 编一个数比不写更糟。
    todo_counts: Dict[str, Optional[int]] = {}
    for key, _ran, _t, is_fresh in rendered:
        if not is_fresh or not acks:
            continue
        if key == "stale":
            n = sum(1 for s in stale_claims if s.sid not in acks)
        elif key == "coverage":
            n = sum(1 for e in list(coverage.orphans) + list(coverage.recent_orphans)
                    if str(e.get("citekey") or "").lower() not in acks)
        elif key == "contradictions":
            n = sum(1 for v in verdicts
                    if v.relation in REPORTABLE_RELATIONS and v.pair.pid not in acks)
        else:
            continue
        if status_counts.get(key) is not None and n != status_counts[key]:
            todo_counts[key] = n

    ret_key = [t for t in rendered if t[0] == "retraction"][0]
    L: List[str] = [
        "# 知识层 lint 报告", "",
        "> **本轮必须处理**：{}".format(_must_do_line(
            retraction, ret_key[3], ret_key[1], status_counts.get("retraction"))),
    ]
    L += _status_line([(k, r, f) for k, r, _t, f in rendered], now, status_counts,
                      todo_counts)
    hint = contradiction_reminder({k: r for k, r, _t, _f in rendered}, now,
                                  contradiction_reminder_days)
    if hint:
        L += [hint, ""]
    # L1/N2 第 3 件：ack 打错 ID、或原文变了导致 ID 变了，此前**没有任何反馈**——
    # `acked = [v for v in reportable if v.pair.pid in acks]` 直接丢弃匹配不上的 key，
    # 用户只会看到"我 ack 过的东西又出现了"，无从判断是打错了、内容变了、还是写错文件。
    known: set = set()
    for _k, _r, text, _f in rendered:
        known |= set(ids_in_text(text))
    if known:
        # ack 指引放在**报告顶部而不是某一节里**：三节都能 ack，而每月自动化跑的恰恰
        # 是不含对撞的那两节——指引若只写在对撞那一节，月度报告里它整段不出现。
        L += _ack_hint(ack_files)
    unmatched = [k for k in sorted(acks) if k not in known]
    if unmatched:
        L += ["> ⚠️ 本轮有 **{} 条 ack 没匹配上**任何一条发现：{}。".format(
            len(unmatched), "、".join("`{}`".format(k) for k in unmatched[:20])),
            "> 三种可能：ID 抄错了 / 那一条的原文变了导致 ID 跟着变了（那是设计如此，"
            "内容变了就是新问题）/ ID 属于本轮被跳过且结转不到的那一节。", ""]
    L += ["> 这份报告问的不是「格式对不对」（那是 `build_topics.py --verify`），"
          "而是「把整个库摆在一起看，知识本身有没有问题」。"
          "每一条都点得回札记原文；每一节都写着自己的扫描分母。", ""]
    for key, ran, text, _f in rendered:
        L += [LINT_SECTION_MARK.format(key, ran), text, ""]
    return "\n".join(L).rstrip("\n") + "\n"


def _never_ran_section(key: str) -> List[str]:
    """本轮没跑、上一版也结转不到：这才是真正意义上的"从来没有过结果"。"""
    L = [SECTION_HEADING[key], ""]
    if key == "retraction":
        L += ["本轮**未执行**（`--offline`）。这一项唯一依赖联网，跳过后「库里没有撤稿论文」"
              "这个结论**本轮不成立**。", ""]
    elif key == "contradictions":
        L += ["本轮**未执行**（`--skip-contradictions` 或 `--dry-run`）。这是唯一需要调用 "
              "LLM 的一项。", ""]
    else:
        L += ["本轮**未执行**（{}）。".format(SECTION_SKIP_FLAG[key]), ""]
    L += ["**且没有可结转的历史结果**——这一项要么从来没跑过，要么上一版报告里这一节"
          "解析不出来（首次运行 / 旧格式 / 生成块被改坏）。", ""]
    return L


def _retraction_section(retraction: RetractionScan,
                        cited_by_page: Dict[str, List[str]]) -> List[str]:
    L: List[str] = [SECTION_HEADING["retraction"], ""]
    L += ["扫描口径：keeper {} 篇 → 有 DOI {} 篇 → OpenAlex 解析到 {} 篇"
          "（覆盖率 {:.0%}）。无 DOI {} 篇、查无此条 {} 篇、本轮未查成 {} 篇——"
          "**这三类都不在结论覆盖范围内**。".format(
              retraction.n_papers, retraction.n_with_doi, retraction.n_resolved,
              retraction.coverage, retraction.n_no_doi, retraction.n_unresolved,
              retraction.n_failed), ""]
    if retraction.unhandled:
        L += ["🚨 **{} 篇已撤稿论文还没被标记**。处置（2026-08-17 起的新口径："
              "**札记保留**，只标记 + 踢出向量库）：在该条的「裁决」行加 `⚑ RETRACTED`，"
              "然后跑 `notes_index.py` + `notes_embed.py`。".format(
                  len(retraction.unhandled)), ""]
    for h in retraction.hits:
        pages = [slug for slug, keys in cited_by_page.items() if h.citekey in keys]
        L.append("- {} `[@{}]` **{}**".format(
            "✅ 已标记" if h.acknowledged else "🚨 **未标记**", h.citekey, h.title[:110]))
        L.append("  - 信号：{}{}".format(
            "OpenAlex `is_retracted`" if h.signal == "openalex-flag" else "标题撤稿标记",
            " · <{}>".format(h.openalex_id) if h.openalex_id else ""))
        L.append("  - DOI：`{}`".format(h.doi))
        if h.acknowledged:
            # 已标记的仍然列出来：「库里有这一篇、已处置」是事实，不该从报告里消失。
            # 但它不再触发退出码与通知——处置完还每月喊人的话，几个月后没人信这个警报。
            L.append("  - 札记保留（已打 `⚑ RETRACTED`），已不在向量库，"
                     "概念页/问答/`notes_search` 都召不到它")
        if pages:
            L.append("  - ⚠️ **已被概念页引用**：{}——这几页的论断可能建立在被撤销的"
                     "结论上，标记后必须重跑这几页".format("、".join(sorted(pages))))
        elif not h.acknowledged:
            L.append("  - 未被任何概念页引用")
    if retraction.hits:
        L.append("")
    else:
        L += ["✅ 已解析的 {} 篇里没有撤稿记录。".format(retraction.n_resolved), ""]
    if retraction.errors:
        L += ["<details><summary>扫描异常 {} 条</summary>".format(len(retraction.errors)), ""]
        L += ["- {}".format(e) for e in retraction.errors[:20]]
        L += ["", "</details>", ""]
    return L


def _ack_hint(ack_files: Optional[Sequence[Any]]) -> List[str]:
    """「怎么表达『我确认过了』」那段指引（L1/N2 第 2 件）。

    **写死绝对路径**：首版只说"在下方「我的批注」里写一行"，而用户读报告的地方是
    Obsidian 里那份 vault 副本——那份有它自己独立的批注区，在那里写什么都没用（首版
    静默无效）。现在 `read_lint_acks` 会同时读两份，指引也就必须把两份都点名，否则用户
    仍然不知道自己在哪一份文件里写才算数。
    """
    files = [str(p) for p in (ack_files or []) if p]
    L = ["> **「这条我看过了，不是问题」怎么表达**：在「我的批注」区写一行 "
         "`- ack: <id> <说明>`。`<id>` 就是条目后面那个 `` `#xxxxxxxx` ``"
         "（连反引号一起复制也认；孤儿那一节的 ID 直接就是 citekey）。"]
    if files:
        L.append("> 写在**下面任意一份**文件里都算（两份都读，取并集）：")
        for i, p in enumerate(files):
            L.append("> - `{}`{}".format(
                p, "（札记库权威产物）" if i == 0 else "（Obsidian 里你实际在读的那一份）"))
    else:
        L.append("> 写在本文件下方的「我的批注」区。")
    L += ["> 下轮它就会被折叠到本节末尾的折叠区——**不是不再显示**："
          "内容只要变了 ID 就跟着变，会重新展开，因为内容变了就是新问题，该重问。", ""]
    return L


def _fmt_delta(cur_ids: Sequence[str], prev_text: Any, unit: str,
               n_acked: int = 0) -> List[str]:
    """一行 delta：本轮新增 / 与上轮相同 / 已消失（L3）。

    "上轮"从结转机制已有的上一版正文里解析 ID（`ids_in_text`），纯文本比对，不需要额外
    状态文件。上一版那一节没有任何 ID（首次运行 / 旧格式）时**明说不做比对**，而不是把
    全部条目报成"新增"——那是格式迁移，不是发现。

    B5：`n_acked > 0` 时补一句**待办口径**。delta 的分母含已 ack 的条目（那是对的：
    折叠不是删除），但用户想知道的是"这数字会不会随我干活而变小"，两个口径都写出来
    比让他自己去第 403 行找那句说明强。
    """
    todo = ["（其中 {} {}是你此前确认过的，**待办 {}**——折叠不是删除，"
            "所以上面的数字含它们。）".format(n_acked, unit, len(set(
                i.lower() for i in cur_ids)) - n_acked), ""] if n_acked > 0 else []
    prev = set(ids_in_text(prev_text))
    if not prev:
        return ["（上一版报告里这一节没有可比对的 ID，本轮不做增量比对——下一轮起就有了。）",
                ""] + todo
    cur = set(i.lower() for i in cur_ids)
    gone = len(prev - cur)
    return ["本轮新增 **{}** {} · 与上轮相同 {} · 已消失 {}。"
            "「已消失」通常是好消息（那一页重合成后换了更新的证据），"
            "但也可能是那一页出了问题——数字异常大时值得扫一眼。".format(
                len(cur - prev), unit, len(cur & prev), gone), ""] + todo


# L8：`method-divergence` 与 `scope-limit` 不是待办。真实报告 13 条可报张力里 11 条是
# 「方法学分歧」，内容清一色"两篇的 F1 阈值 / 插补策略 / 多重比较校正不同，指标不可直接
# 比较"——这类**不是可修的缺陷，是文献的永久属性**，没有任何操作能让它下一轮消失。
# 它们挤占 ⚔️ 节的全部主视野，把真正要动手的「结论冲突」与 🔁 埋在下面。
MAIN_RELATIONS: Tuple[str, ...] = ("conflict", "self-inconsistency")
BACKGROUND_RELATIONS: Tuple[str, ...] = ("method-divergence", "scope-limit")


def _contradiction_section(verdicts: Sequence[Verdict],
                           candidates: Sequence[CandidatePair],
                           verdict_report: VerdictReport,
                           params: Dict[str, Any],
                           acks: Dict[str, str]) -> List[str]:
    L: List[str] = [SECTION_HEADING["contradictions"], ""]
    reportable = [v for v in verdicts if v.relation in REPORTABLE_RELATIONS]
    none_n = sum(1 for v in verdicts if v.relation == "none")
    L += ["扫描口径：相似度 ≥{} 的跨论文「{} ↔ {}」句对，取前 {} 对送 LLM 裁决；"
          "本轮候选 {} 对、裁决成功 {} 对、其中 {} 对被判定为同题但不构成张力。".format(
              params.get("pair_min_sim", DEFAULT_PAIR_MIN_SIM),
              "/".join(ROLE_LABEL.get(r, r) for r in params.get("roles_a", DEFAULT_ROLES_A)),
              "/".join(ROLE_LABEL.get(r, r) for r in params.get("roles_b", DEFAULT_ROLES_B)),
              params.get("max_pairs", DEFAULT_MAX_PAIRS),
              len(candidates), verdict_report.judged, none_n), ""]
    L += ["相似度只是**候选生成器**——它找的是「在讲同一件事」，不是「互相矛盾」。"
          "判定权在 LLM，所以下面每一条都要自己看两句原文再下结论。", ""]
    if verdict_report.batches_failed:
        L += ["⚠️ **{} 批未裁决**（约 {} 对候选本轮没看过，不等于它们没问题）：".format(
            verdict_report.batches_failed,
            verdict_report.batches_failed * params.get("batch_size", DEFAULT_BATCH_SIZE)), ""]
        L += ["- {}".format(e) for e in verdict_report.errors[:6]]
        L.append("")
    flags = []
    if verdict_report.invalid_refs:
        flags.append("非法编号 {}".format(verdict_report.invalid_refs))
    if verdict_report.unknown_relations:
        flags.append("非白名单分类 {}".format(verdict_report.unknown_relations))
    if verdict_report.duplicate_refs:
        flags.append("重复编号 {}".format(verdict_report.duplicate_refs))
    if verdict_report.stripped_cites:
        flags.append("剥离裸引用 {}".format(verdict_report.stripped_cites))
    if flags:
        L += ["⚑ 裁决异常计数：{}（数字持续变大说明该换模型或改 prompt）。".format(
            "，".join(flags)), ""]
    if not reportable:
        L += ["本轮没有判定为张力的句对。", ""]
        return L

    acked = [v for v in reportable if v.pair.pid in acks]
    fresh = [v for v in reportable if v.pair.pid not in acks]
    L += ["每条张力后面的 `` `#xxxxxxxx` `` 是这一对的稳定 ID（怎么用见报告顶部）。", ""]
    main = [v for v in fresh if v.relation in MAIN_RELATIONS]
    background = [v for v in fresh if v.relation in BACKGROUND_RELATIONS]
    for rel in MAIN_RELATIONS:
        group = [v for v in main if v.relation == rel]
        if not group:
            continue
        L += ["### {} {}（{} 处）".format(
            RELATION_EMOJI.get(rel, "•"), RELATION_LABEL[rel], len(group)), ""]
        for v in sorted(group, key=lambda x: -x.pair.score):
            L += _pair_lines(v)
    if not main and (background or acked):
        L += ["本轮**主视野没有待办**：没有「结论冲突」也没有「单篇内部自相矛盾」。", ""]
    if background:
        # L8：这一档默认折起来，**摘要行本身就写明它不是待办**（不展开也读得到）。
        # 展开的成本是一次点击，而让 11 条不可行动项包住 2 条可行动项的成本是整节被
        # 跳过不看。
        #
        # 导语**必须写在 `<summary>` 里或 `<details>` 内部**，不能作为 `####` 块之间的
        # 一行散文：`fold_acked_blocks` 的块边界规则是"到下一个 `####`/`###`/`##` 或
        # `<details>` 为止"，块后面的散文会被当成该块的一部分，ack 折叠时被一起搬走
        # （13 轮端到端实测逮到）。生成侧不要在 `####` 块之间插节级散文。
        L += ["<details><summary>📎 另有 <b>{} 处方法学分歧 / 适用范围限定</b>"
              "——<b>不是待办</b>，是写作素材，展开看</summary>".format(len(background)), "",
              "> 这一档是 discussion / related work 的**写作素材**："
              "两篇的 F1 阈值、插补策略、多重比较校正不同，是文献的**永久属性**，"
              "没有任何操作能让它下一轮消失，所以它不该占 ⚔️ 节的主视野。", ""]
        for rel in BACKGROUND_RELATIONS:
            group = [v for v in background if v.relation == rel]
            if not group:
                continue
            L += ["### {} {}（{} 处）".format(
                RELATION_EMOJI.get(rel, "•"), RELATION_LABEL[rel], len(group)), ""]
            for v in sorted(group, key=lambda x: -x.pair.score):
                L += _pair_lines(v)
        L += ["</details>", ""]
    if not fresh:
        L += ["本轮没有**新的**张力：所有 {} 条都是你此前确认过的。".format(len(acked)), ""]
    if acked:
        # 这里的标记行必须与 `fold_acked_blocks` 认的三种一模一样——结转那一路会把它们
        # 拆开再重折，对不上就会在下一轮堆出第二层 <details>。
        L += ["", _fold_lead(len(acked)), "", _fold_open(len(acked)), ""]
        for v in sorted(acked, key=lambda x: -x.pair.score):
            L += [_fold_note(acks.get(v.pair.pid) or ""), ""]
            L += _pair_lines(v)
        L += ["</details>", ""]
    return L


def _stale_claim_lines(s: StaleClaim) -> List[str]:
    return ["- **{}** · `{}.md:{}` {}".format(
                s.claim.heading, s.claim.slug, s.claim.line, ACK_ID_MARK.format(s.sid)),
            "  > {}".format(s.claim.text[:220]),
            "  证据：{}".format(" ".join(
                "`[@{}]`({})".format(k, y if y else "?") for k, y in s.years.items()))]


def _stale_section(stale_claims: Sequence[StaleClaim], params: Dict[str, Any],
                   stale_years: int, acks: Optional[Dict[str, str]] = None,
                   prev_text: Any = "",
                   anchor_limit: int = DEFAULT_STALE_ANCHOR_LIMIT) -> List[str]:
    """陈旧论断这一节。

    L5：展示轴从「按概念页平铺 39 条」改成**按撑着它的那篇老文献聚合**。真实数据里
    39 条背后只有 24 篇老论文（`lee2021Multiview` 一篇撑 4 条、`li2021Imputation` 4 条），
    而**可执行单位就是那 24 篇**——"这 24 篇老文献是 N 条论断的唯一地基，去补一轮新
    文献"是一个下午能做完的事，"39 条陈旧论断"不是。每条论断挂到它**最新**的那篇支撑
    文献下（`StaleClaim.anchor`），因为判据本身就是"最新的一篇也已 N 年前"。

    L3：每条论断带稳定 ID（`sid`）可以 ack，节首带一行 delta。此前这一节**每月自动跑、
    每月逐字重印**，用户既不能消掉、也看不出哪条是新的。

    B3：这一节的量是**日历驱动**的（阈值是 `now_year - stale_years`），所以两件事：
    锚文献数有 cap（`anchor_limit`，孤儿那节早就有 `--orphan-limit`，这节没有），
    以及 delta 那行要区分"因阈值前移而新增"与"因页面重合成而新增"。
    """
    acks = acks or {}
    threshold_year = (params.get("now_year") or datetime.now().year) - stale_years
    L: List[str] = []
    L += ["口径：概念页论断中，**支撑文献最新的一篇**也已是 {} 年前（{} 年及更早）。"
          "这不是判它错——方法学论断本来就可能十年不变。它只回答一个问题："
          "「这条结论后来有没有被更新的工作推翻，我确认过吗」。"
          "任一支撑文献年份未知的论断不参与判定。".format(
              stale_years, threshold_year), ""]

    acked = [s for s in stale_claims if s.sid in acks]
    live = [s for s in stale_claims if s.sid not in acks]
    by_key: Dict[str, List[StaleClaim]] = {}
    for s in live:
        by_key.setdefault(s.anchor, []).append(s)
    pages = {s.claim.slug for s in live}
    order = sorted(by_key, key=lambda k: (-len(by_key[k]), k))
    shown = order[:anchor_limit] if anchor_limit > 0 else order
    # delta 与下面那句阈值说明都只能拿**本轮真渲染出来的** sid 去比：上一版正文里能
    # 解析出来的也只有它列出来的那些。拿全量比会让被 `anchor_limit` 收起来的那批
    # 每一轮都被报成"本轮新增"——正是 A1 那个"永久幽灵"的形状，只是换了个成因。
    rendered = [s for k in shown for s in by_key[k]] + list(acked)
    L += _fmt_delta([s.sid for s in rendered], prev_text, "条", len(acked))
    # B3 之二：阈值每年 1 月 1 日自己往前走一格，那天没有任何人做错任何事，这一节
    # 就从 195 行长到 306 行、delta 写「本轮新增 23 条」。11 个月是 0、然后每年放一次
    # 烟花——而那次"新增"不是发现，是时钟走了一格。报告必须自己说明这一点，否则
    # 用户只能自己去猜为什么突然多了一批。判据：新增的那批里最新支撑文献年份**正好
    # 等于**本轮的阈值年，那就是刚被阈值扫进来的。
    prev_ids = set(ids_in_text(prev_text))
    if prev_ids:
        newly = [s for s in rendered
                 if s.sid.lower() not in prev_ids and s.newest_year == threshold_year]
        if newly:
            L += ["其中 **{} 条**是因为陈旧阈值前移到 {} 年才掉进来的，**不是内容变化**"
                  "——这一节的量是日历驱动的（阈值 = 当前年 − {}），"
                  "每年 1 月会有一批整批掉进来。".format(
                      len(newly), threshold_year, stale_years), ""]
    if not stale_claims:
        L += ["✅ 没有论断的证据基础全部老于该阈值。", ""]
        return L

    if live:
        L += ["**{} 篇老文献是这 {} 条论断的唯一地基**（跨 {} 页）。"
              "可执行单位是**这几篇**——去给撑得最多的那几篇补一轮新文献，"
              "比逐条核对 {} 条论断快得多。".format(
                  len(by_key), len(live), len(pages), len(live)), ""]
        # B6：说了可执行单位却没说用什么工具执行。孤儿那节给了 `notes_search.py`
        # （查库内），这里要的是**查库外的新文献**，仓库里现成有 search_pubs.py
        # （skill `scholar-search`）。
        L += ["补文献用：`PYTHONPATH=. python scripts/search_pubs.py \"<该篇的主题词>\" "
              "--days 1825`（arXiv/PubMed 临时检索，不入库；命中会标注是否已在札记库）。"
              "确认某条仍然算数就写一行 ack，见报告顶部。", ""]
        # B6 之二：真实分布是长尾（4/4/3/3/2/2/2/2/2 + 15 个 singleton），**前几篇就能
        # 清掉一多半**。这句话读者现在得自己数，直接写出来才叫"可执行"。
        hot = [k for k in order if len(by_key[k]) >= 2]
        if hot:
            covered = sum(len(by_key[k]) for k in hot)
            L += ["其中 **{} 篇各撑着 ≥2 条**，合计 {} 条（{:.0%}）——先补这几篇，"
                  "剩下 {} 篇各只撑 1 条。".format(
                      len(hot), covered, covered / len(live), len(by_key) - len(hot)), ""]
        for ck in shown:
            group = sorted(by_key[ck], key=lambda s: (s.claim.slug, s.claim.line))
            L += ["### `[@{}]`（{} 年 · 撑着 {} 条论断）".format(
                ck, group[0].newest_year, len(group)), ""]
            for s in group:
                L += _stale_claim_lines(s)
            L.append("")
        rest = order[len(shown):]
        if rest:
            L += ["另有 **{} 篇**（合计 {} 条论断）未逐条列出（`--stale-anchor-limit {}`，"
                  "`--stale-anchor-limit 0` 全列）：{}。".format(
                      len(rest), sum(len(by_key[k]) for k in rest), anchor_limit,
                      "、".join("`[@{}]`".format(k) for k in rest[:40])), ""]
    else:
        L += ["本轮没有**新的**陈旧论断：所有 {} 条都是你此前确认过的。".format(len(acked)), ""]
    if acked:
        L += ["", _fold_lead(len(acked)), "", _fold_open(len(acked)), ""]
        for s in sorted(acked, key=lambda s: (s.claim.slug, s.claim.line)):
            L += [_fold_note(acks.get(s.sid) or ""), ""] + _stale_claim_lines(s) + [""]
        L += ["</details>", ""]
    return L


def _orphan_lines(entries: Sequence[dict], now_year: int = 0) -> List[str]:
    """孤儿名单的条目行。ID 就是 **citekey 本身**（L3）——它已经是跨运行稳定的身份，
    再哈希一层只会让人没法从报告一眼认出这是哪篇。"""
    out: List[str] = []
    for e in entries:
        ck = str(e.get("citekey") or "")
        # L4：`year` 晚于明年只能是元数据错误（实测 kishore2045Quantifying 的 2045），
        # 让它顶在"该开新页"名单第一行是在浪费用户时间。只标不改，索引数据不动。
        suspicious = now_year and year_is_implausible(e.get("year"), now_year)
        out.append("- `[@{}]`（{}{}）{} {}{}".format(
            ck, e.get("year") or "?",
            " · 入库 {}".format(e["month"]) if e.get("month") else "",
            str(e.get("title") or "")[:110],
            ACK_ID_MARK.format(ck),
            " ⚠️ **元数据可疑**（`year` 晚于 {} 或缺失，多半是索引里的年份写错了，"
            "本节排序已把它沉底）".format(now_year + 1) if suspicious else ""))
        if e.get("one_line"):
            out.append("  <small>{}</small>".format(e["one_line"]))
    out.append("")
    return out


def _split_acked(entries: Sequence[dict], acks: Dict[str, str]
                 ) -> Tuple[List[dict], List[dict]]:
    live = [e for e in entries if str(e.get("citekey") or "").lower() not in acks]
    done = [e for e in entries if str(e.get("citekey") or "").lower() in acks]
    return live, done


def _orphan_bucket_head(title: str, total: int, live: Sequence[dict],
                        acked: Sequence[dict], shown: Sequence[dict]) -> str:
    """一档孤儿的粗体小标题（A2）。

    首版写的是「（{总数} 篇，列前 {列出数}）」，两个数都不回答用户真正在问的那件事：
    **我处理掉一批之后，队列里还剩多少**。ack 掉列出的 25 篇之后，这一行原来会连同
    整个分档一起消失——一个认真干完 25 篇的用户下个月看到这一档空了，会以为干完了，
    而后面还有 143 篇。所以三个数一起写：已确认 / 待办 / 本节列出。
    """
    rest = len(live) - len(shown)
    parts = ["共 {} 篇".format(total)]
    if acked:
        parts.append("已确认 {}".format(len(acked)))
    parts.append("待办 {}".format(len(live)))
    if rest > 0:
        parts.append("本节列出前 {} · 队列里还有 {} 篇没列，`--orphan-limit 0` 全列"
                     .format(len(shown), rest))
    else:
        parts.append("已全部列出")
    return "**{}（{}）**".format(title, " · ".join(parts))


def _coverage_section(coverage: CoverageReport, acks: Optional[Dict[str, str]] = None,
                      prev_text: Any = "") -> List[str]:
    acks = acks or {}
    L: List[str] = []
    L += ["概念页 {} 页覆盖了 keeper {} 篇里的 {} 篇（{:.0%}）；已精读 {} 篇里覆盖 {} 篇"
          "（{:.0%}）。「覆盖」= 至少进过某一页的证据池，不要求被论断引用。".format(
              coverage.n_pages, coverage.n_keeper, coverage.n_cited,
              (coverage.n_cited / coverage.n_keeper) if coverage.n_keeper else 0.0,
              coverage.n_deep_read, coverage.n_deep_read_cited,
              (coverage.n_deep_read_cited / coverage.n_deep_read) if coverage.n_deep_read else 0.0),
          ""]
    known = [t for t in coverage.thin_pages if t[2] is not None]
    saturated = [t for t in known if t[1] >= t[2]]
    genuinely_thin = [t for t in known if t[1] < t[2]]
    all_saturated = bool(known) and not genuinely_thin

    # A2：截断在这里做，**在分完 ack 之后**。`coverage_report` 交下来的是完整名单。
    # 子标题一律用粗体行而不是 `####`：`fold_acked_blocks` 的折叠单位就是 `####` 块，
    # 这一节若用 `####` 做分档标题，结转时一条被 ack 的孤儿会把**整个分档**折走。
    limit = coverage.orphan_limit
    live_settled, acked_settled = _split_acked(coverage.orphans, acks)
    live_recent, acked_recent = _split_acked(coverage.recent_orphans, acks)
    show_settled = live_settled[:limit] if limit > 0 else live_settled
    show_recent = live_recent[:limit] if limit > 0 else live_recent
    acked_orphans = acked_settled + acked_recent

    # delta 拿的是**本轮真列出来的**那些 ID（含折叠区里的），不是完整名单：上一版正文
    # 里能解析出来的也只有它列出来的那些，拿全量去比会在改口径的那一轮报出
    # "本轮新增 183 篇"——那是截断口径变了，不是发现。
    L += _fmt_delta([str(e.get("citekey") or "")
                     for e in list(show_settled) + list(show_recent) + acked_orphans],
                    prev_text, "篇", len(acked_orphans))
    if coverage.orphans or coverage.recent_orphans:
        # A3：「孤儿」的准确含义是**没挤进任何一页的 top-max_evidence**，不等于"跟这
        # 几个概念无关"，更不等于"queries 漏了问题域"。首版把它写成"该不该开新概念页
        # 的直接依据"，而实测 8 页全部触及各自 max_evidence 上限——在那个前提下这个
        # 推论根本不成立。
        if all_saturated:
            cap_state = "{} 页当前**全部饱和**（各自都触及 `max_evidence` 上限）".format(
                len(known))
        elif saturated:
            cap_state = "已知上限的 {} 页里有 {} 页触及 `max_evidence` 上限".format(
                len(known), len(saturated))
        elif known:
            cap_state = "各页均未触及 `max_evidence` 上限"
        elif coverage.retired_pages:
            # N5 的同一类错误：`known` 为空**不等于**"调用方没给 specs"，也可能是
            # 剩下的页全部已从 topics.yaml 下线（CLI 真跑一次就撞上了）。
            cap_state = "本轮列出的概念页**全部已从 `config/topics.yaml` 下线**，" \
                        "没有一页还有配置上限可比，无从判断是否触顶"
        else:
            cap_state = "本轮拿不到各页的 `max_evidence`，无从判断是否触顶"
        L += ["### 概念页碰不到的高优先级精读论文（共 {} 篇）".format(
            coverage.n_orphans_total), ""]
        L += ["它们连证据池都没进过。但**这不等于它们与这些概念无关**——{}，"
              "所以「碰不到」的准确含义是**没挤进任何一页的 top-`max_evidence`**，"
              "而不是「`queries` 漏了这个问题域」。"
              "判断某一篇是不是真缺口：对它手动跑一次 "
              "`PYTHONPATH=. python scripts/notes_search.py <该篇主题>`，"
              "看它在全库语义排名里的位置。".format(cap_state), ""]
    if show_settled:
        L += [_orphan_bucket_head("值得考虑要不要开新页", coverage.n_orphans_settled,
                                  live_settled, acked_settled, show_settled), "",
              "入库已经有一段时间、经历过至少一轮概念页重合成，仍然一页都没进过。"
              "**按年份正序（最老优先）**——年份倒序会让库里最久没人碰的老文献永远排不进"
              "这个名单，而那批恰恰更可能是真缺口。", ""]
        L += _orphan_lines(show_settled, coverage.now_year)
    if show_recent:
        L += [_orphan_bucket_head("最近 {} 个月新入库".format(coverage.recent_months),
                                  coverage.n_orphans_recent,
                                  live_recent, acked_recent, show_recent), "",
              "**多半只是还没排进任何一页的前 `max_evidence` 名**，不是缺口信号——"
              "这一档按年份倒序排（最新在前），那是这一档的语义；"
              "`year` 晚于明年或缺失的一样沉底，那是元数据错误不是新论文。", ""]
        L += _orphan_lines(show_recent, coverage.now_year)
    if not (coverage.orphans or coverage.recent_orphans):
        L += ["✅ 没有高优先级精读论文被概念页整体漏掉。", ""]
    elif not (live_settled or live_recent):
        L += ["本轮没有**新的**孤儿：列出的都是你此前确认过的。", ""]
    if acked_orphans:
        L += ["", _fold_lead(len(acked_orphans)), "", _fold_open(len(acked_orphans)), ""]
        for e in acked_orphans:
            L += [_fold_note(acks.get(str(e.get("citekey") or "").lower()) or ""), ""]
            L += _orphan_lines([e], coverage.now_year)
        L += ["</details>", ""]

    if coverage.thin_pages:
        if all_saturated:
            # A2 + L9：实测 8 页的证据数与 config/topics.yaml 里各页的 max_evidence 一模
            # 一样——这项检查在当前配置下退化成"复述配置文件里已经写好的数字"。诚实地
            # 说清它给不出信号是对的，但**不必为此每月重印一张 8 行全标"饱和"的表**：
            # 那 12 行是纯噪音。收成一句话，表留给"至少有一页真的薄"的那天。
            L += ["### 各页证据厚度", "",
                  "本轮 {} 页**全部触及各自 `max_evidence` 上限**，这项检查暂时给不出信号"
                  "（它只是在复述 `config/topics.yaml` 里已经写好的数字），故本节不出表。"
                  "想让它有信号：要么调高 `max_evidence` 看候选池够不够，"
                  "要么等库里出现真正冷门的概念页。".format(len(known)), ""]
            if coverage.retired_pages:
                L += ["（另有 {} 页已从 `config/topics.yaml` 下线（退役页），"
                      "不在上面那个分母里：{}。）".format(
                          len(coverage.retired_pages),
                          "、".join("[[{}]]".format(s) for s in coverage.retired_pages)), ""]
            return L
        L += ["### 各页证据厚度", "", "| 概念页 | 证据条数 | 配置上限 | 判定 |", "|---|---|---|---|"]
        for slug, n, cap in coverage.thin_pages:
            if cap is None and slug in coverage.retired_pages:
                # N5：给了 specs 但这一页不在里面 = 已从 topics.yaml 下线的退役页
                # （留在 topics/ 是既定行为）。首版把它写成"调用方没给 specs"——
                # 对着一个明明传了 specs 的运行说"你没给 specs"。
                L.append("| [[{}]] | {} | — | 已从 `config/topics.yaml` 下线（退役页）|"
                         .format(slug, n))
            elif cap is None:
                L.append("| [[{}]] | {} | ? | 未知（调用方没给 `specs`） |".format(slug, n))
            elif n >= cap:
                L.append("| [[{}]] | {} | {} | 饱和（触及配置上限 {}） |".format(slug, n, cap, cap))
            else:
                L.append("| [[{}]] | {} | {} | **薄** |".format(slug, n, cap))
        L.append("")
        if genuinely_thin:
            L += ["标「薄」的 {} 页**没被配置上限截断**，那才是真的「库里就这么多证据」。"
                  "要么库里确实没这方面的文献（诚实的缺口，该去补），要么 queries 写得不对"
                  "（该改 `config/topics.yaml`）。区分方法：手动 "
                  "`PYTHONPATH=. python scripts/notes_search.py <同一个概念>` 搜一次，"
                  "看是不是真的搜不到。".format(len(genuinely_thin)), ""]
        elif coverage.retired_pages and not known:
            L += ["（上面 {} 页**全部已从 `config/topics.yaml` 下线**（退役页），"
                  "没有一页还有配置上限可比，这项检查本轮无从判断——"
                  "配置本身是读到了的。）".format(len(coverage.retired_pages)), ""]
        else:
            # specs 没传进来：不知道每页的上限，就**不要**给那句诱导性建议——
            # 让人去查一个查不出结果的东西比不给建议更糟。
            L += ["（本轮没拿到 `config/topics.yaml` 的 `max_evidence`，无从判断这些数字是"
                  "「库里真的只有这么多」还是「卡在配置上限」，故不给建议。）", ""]
    return L


