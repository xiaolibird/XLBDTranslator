# -*- coding: utf-8 -*-
"""知识层 lint：ack 解析/折叠/按 section 结转（自 lint.py 拆出，阶段 4b——
纯字符串处理，依赖面最窄）。外部一律经 `src.scholar.lint` 门面 import。
"""
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from ..utils.logger import get_logger
from .topics import GEN_END
from .vault import split_frontmatter
from .lint_checks import LINT_REPORT_NAME, RetractionScan

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 6a. 「我已经确认过，这条不是问题」（A6）
# ---------------------------------------------------------------------------

# 报告里 ackable ID 的**唯一**渲染形态：`` `#<id>` ``。三节（对撞/陈旧/孤儿）统一用它，
# 于是「这一节里有哪些可 ack 的条目」只需一条正则（`ids_in_text`）就能从渲染好的文本里
# 反解出来——delta 比对（L3）、结转文本的再折叠（L2）、以及"这条 ack 没匹配上任何东西"
# 的反馈（L1）三处都靠这个单一形态，不必让每个 section 各自再回传一份 ID 清单。
ACK_ID_MARK = "`#{}`"

# ID 的边界靠**定界**，不靠枚举允许的字符——这一条是本模块最该留给后人的经验，因为
# 它是同一个 bug 家族的**第三次**，而且第三次是反向的：
#
#   第 1 轮：正则里用 `\w`。Python 3 的 `\w` 按 Unicode 判定，**汉字算单词字符**，
#           于是防幻觉检查整条漏过而 `--verify` 全绿。
#   第 2 轮：换成 `\b` 之后同一个毛病换了个载体——`撤稿` 后面紧跟汉字时两侧都是
#           word-char、边界断言不成立，`撤稿声明` 这种真实的撤稿通知标题全部漏判
#           （见 `_RETRACTED_TITLE_RE` 上方那段）。
#   第 3 轮：为了躲开前两个坑，这里一度改成**逐字符列 ASCII**
#           （`[0-9A-Za-z][0-9A-Za-z_.:+\-]{0,79}`）。矫枉过正：全库 2343 个 citekey
#           里 40 个含非 ASCII 字符，其中 `куксенко2024Аналіз`、`đorđević2025Optimization`、
#           `周立基于深度学习的不完整时序数据补全方法综述` 这 4 个**首字符**就不是 ASCII。
#           后果不只是 ack 失效：`mišić2021Simulationbased` 被截成 `mi`，报告顶部于是
#           弹出「⚠️ 本轮有 1 条 ack 没匹配上：`mi`」——**反过来诬告照抄了正确 ID 的
#           用户**（比第 1 轮的静默无效更糟，静默无效至少不指责人）；`ids_in_text`
#           也认不出它，于是它每个月都被算成"本轮新增"，delta 带着一个永久幽灵。
#
# 正确解法是**别去描述 ID 长什么样**（它可以是任意 Unicode——citekey 就是），而是描述
# 它在文本里被什么框住：报告里 ID 一律渲染成 `` `#<id>` ``，所以**反引号定界**；
# 批注行里 ID 后面跟的是说明，所以**空白（与反引号）定界**。两处都不出现字符类，
# 也就不存在"漏了哪个字符"这回事。
#
# 唯一的代价：`- ack: 我确认过了` 这种没写 ID 的行现在会被解析成 ID=`我确认过了`。
# 这是必须付的——库里真有纯中文 citekey，靠字符集分不开这两者。它会走
# `render_lint_report` 的「N 条 ack 没匹配上」那条反馈路径，不是静默丢弃。
_ACK_ID_IN_TEXT_RE = re.compile(r"`#([^`\s]+)`")

# 用户区里的一行轻量约定：`- ack: ab12cd34 <可选说明>`。
# N3：报告把 ID 打印成 `` `#108b782d` ``，用户在 Obsidian 源码模式 / Vim 里复制这一串时
# **反引号大概率跟着走**，而首版正则不认反引号、也不认有序列表前缀（`1. ack: …`），
# 失败还是静默的——用户只会觉得"这功能不好使"。这三种前缀一律放开。
#
# ID 那一组是 `([^`\s]+)`：**到空白或反引号为止**（见上面那段"定界不枚举"）。
# 排除反引号而不是只排除空白，是因为 `` `#abc` `` 这种写法里收尾的反引号紧贴 ID，
# 用 `(\S+?)(?=\s|$)` 会把它一起吞进 ID。
_ACK_RE = re.compile(
    r"^\s*(?:[-*+]|\d{1,3}[.)])?\s*ack\s*[:：]\s*`?\s*#?\s*([^`\s]+)\s*`?\s*(.*)$",
    re.I)


def ids_in_text(text: Any) -> List[str]:
    """渲染好的报告文本里出现过的全部 ackable ID（小写、保序去重，纯函数）。

    L2/L3/L1 三处共用：结转文本的再折叠要知道"这一节里有哪些 ID"，delta 要拿上一版
    的 ID 集合做差，"这条 ack 没匹配上"的反馈要拿本轮全部 ID 做全集。
    """
    out: List[str] = []
    seen: set = set()
    for m in _ACK_ID_IN_TEXT_RE.finditer(str(text or "")):
        k = m.group(1).lower()
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def parse_acks(user_zone: Any) -> Dict[str, str]:
    """从「我的批注」区解析 `- ack: <id> <说明>`（纯函数，**绝不抛异常**）。

    解析不出就当没 ack——这份报告的生成不能因为用户把批注写歪了就整个失败，更不能
    因为解析崩了而把"我上个月已经确认过"的信息静默丢掉。同一 ID 写两次取**最后一条**
    （人改批注时通常是在下面追加一行新说法）。

    **ID 一律按小写存**，查表侧也一律 `.lower()`：孤儿那一节的 ID 是 citekey
    （`lee2021Multiview`），手抄大小写抄错壳是最常见的失败，而 citekey 在库内本就唯一，
    忽略大小写不会撞键。

    ID 不再校验"必须是 8 位十六进制"：三节的 ID 形态已经不同（pid / claim 哈希 /
    citekey），写死校验会把两节的合法 ack 全判成噪音。代价是打错的 ID 也会被解析出来
    ——这正是要的：`render_lint_report` 会把"本轮 N 条 ack 没匹配上任何条目"列出来，
    比静默丢弃有用得多（L1/N2）。
    """
    out: Dict[str, str] = {}
    try:
        for line in str(user_zone or "").splitlines():
            m = _ACK_RE.match(line)
            if not m:
                continue
            out[m.group(1).lower()] = m.group(2).strip().strip("`").strip()
    except Exception:      # pragma: no cover - 兜底，parse 不该有别的失败模式
        return {}
    return out


def _acks_in_dir(d) -> Dict[str, str]:
    try:
        text = (Path(d) / LINT_REPORT_NAME).read_text(encoding="utf-8")
        _fm, body = split_frontmatter(text)
        from .vault import extract_user_zone
        return parse_acks(extract_user_zone(body or "") or "")
    except Exception:
        return {}


def read_lint_acks(topics_dir, extra_dirs: Optional[Sequence[Any]] = None
                   ) -> Dict[str, str]:
    """读现存 `_lint.md` 的用户区，解析出 ack 表。任何失败一律返回空表。

    L1/N2：**必须能读到 vault 那一份副本**。`sync_topics_to_vault` 会把这份报告同步进
    `~/Documents/ScholarVault/02-主题/_lint.md`，而 `merge_topic_page` 给 vault 副本
    自己独立的一份「我的批注」区——报告在那里逐字教用户写 `- ack: …`，而首版只读
    `notes_dir/topics/`，在那里写什么都没用，且**无提示无警告**。按本项目既定认知
    「人读东西的地方是 Obsidian」，那是主路径不是边角。

    `extra_dirs` 里读不到的目录一律跳过（不存在 / 不是目录 / 没权限都算），**绝不抛
    异常**：ack 是锦上添花，vault 没挂载不该让整份报告生不出来。

    同一 ID 两边都写了时**取 notes_dir 那份的说明**：那是权威产物（`build_vault.py`
    的同步方向是 notes_dir → vault，反过来没有回流通道）。两份都表示"已确认"，差别
    只在说明文字，取哪份都不影响折叠行为。

    B7：**但说明为空时用另一份的**。`- ack: abc`（合法写法，说明可省）出现在 notes
    那份里时，`setdefault` 会让它盖掉 vault 那份的 `- ack: abc 详细理由`，折叠区于是
    显示"（未写说明）"——把用户真写过的字弄丢了。优先方向不变，只是"空"不算一份说明。
    """
    out: Dict[str, str] = dict(_acks_in_dir(topics_dir))
    for d in (extra_dirs or []):
        if d is None:
            continue
        for k, v in _acks_in_dir(d).items():
            if k not in out or (not out[k] and v):
                out[k] = v
    return out


# ---------------------------------------------------------------------------
# 6a-2. 对**结转文本**再折叠一次（L2/N4）
# ---------------------------------------------------------------------------

# 折叠块的三种标记行。`fold_acked_blocks` 先把它们整体拆掉再重折，于是这个函数是
# **幂等**的——连续 12 个月结转同一节不会堆出 12 层 <details>。
_FOLD_LEAD_RE = re.compile(r"^>\s*✔️\s*其中\s*\*\*\d+\s*条你此前已确认\*\*")
_FOLD_OPEN_RE = re.compile(r"^<details><summary>你此前已确认的\s*\d+\s*条</summary>$")
_FOLD_NOTE_RE = re.compile(r"^>\s*✔️\s*\*\*你已确认过\*\*：")
_H3_COUNT_RE = re.compile(r"（\d+ 处）")


def _fold_lead(n: int) -> str:
    return ("> ✔️ 其中 **{} 条你此前已确认**，已折叠到本节末尾"
            "（两句原文只要变了 ID 就跟着变，会重新展开）。".format(n))


def _fold_open(n: int) -> str:
    return "<details><summary>你此前已确认的 {} 条</summary>".format(n)


def _fold_note(note: str) -> str:
    # B7：说明里的换行必须压掉。`_FOLD_NOTE_RE` 是逐行匹配的，说明含换行时只有第一行
    # 被认出来、第二行留在原地，下一轮再折又多一行——167→178→189→200…，
    # "字节级幂等"的宣称就此不成立。`parse_acks` 也是逐行匹配、生产上写不出带换行的
    # 说明，但幂等性是这个函数的**契约**，不该靠调用方的巧合成立。
    return "> ✔️ **你已确认过**：{}".format(str(note or "").replace("\n", " ") or "（未写说明）")


def _block_norm(buf: List[str]) -> List[str]:
    """`####` 块的尾部空行归一化成恰好一行——`fold_acked_blocks` 的幂等性靠它
    （否则第二次折叠只是把空行数量改了一下，输出就不再等于第一次的结果）。"""
    out = list(buf)
    while out and not out[-1].strip():
        out.pop()
    return out + [""]


def _unwrap_ack_fold(lines: List[str]) -> List[str]:
    """把上一轮折进去的块摊回原位（只拆我们自己插的三种标记，别人的 <details> 不动）。"""
    out: List[str] = []
    stack: List[bool] = []
    for ln in lines:
        s = ln.strip()
        if _FOLD_OPEN_RE.match(s):
            stack.append(True)
            continue
        if s.startswith("<details"):
            stack.append(False)
            out.append(ln)
            continue
        if s == "</details>":
            if stack and stack.pop():
                continue
            out.append(ln)
            continue
        if _FOLD_LEAD_RE.match(s) or _FOLD_NOTE_RE.match(s):
            continue
        out.append(ln)
    return _squeeze_blanks(out)


def _squeeze_blanks(lines: List[str]) -> List[str]:
    """连续空行压成一个。

    拆掉折叠标记行会留下它们周围的空行，连续 12 轮结转就在节末堆出四十多个空行
    （13 轮端到端实测逮到）。markdown 语义不变，而折叠这才真的收敛（`fold(fold(x))`
    在**字节层面**等于 `fold(x)`，不只是"内容一样"）。
    """
    out: List[str] = []
    for ln in lines:
        if not ln.strip() and out and not out[-1].strip():
            continue
        out.append(ln)
    return out


def fold_acked_blocks(text: Any, acks: Optional[Dict[str, str]]
                      ) -> Tuple[str, int]:
    """把已 ack 的 `#### …` 块整块搬进节末的 `<details>`（纯函数，**绝不抛异常**）。

    L2/N4 的根因：**结转结转的是渲染好的 markdown 文本**，而 ack 只作用在 fresh 分支。
    于是 A6（ack 折叠）与 A1（跳过即结转）互相抵消——**唯一能被 ack 的那一节，恰好是
    唯一会被结转的那一节**。验收 agent 用真实 topics 目录副本跑了 13 轮模拟（M0 全跑
    + 12 个月 `--skip-contradictions`），那条被 ack 过的张力完整展开了一整年。

    这里刻意做成**纯文本处理**而不是重跑 LLM、也不是把结转改成结转结构化数据：
    - 重跑 LLM 会让月度自动化叠一轮 LLM 调用，正是 `--skip-contradictions` 要避免的；
    - 改成结转结构化数据会动到已端到端验证过的 A1 结转链路（含哨兵/计数/时间戳三处）。

    块边界：`#### ` 开头到下一个 `####`/`###`/`##` 或节末为止。已经在别人的 `<details>`
    里的块（如 L8 折进去的「方法学分歧」）不动——它们本来就不在主视野。折完后 `###`
    分组标题的 `（N 处）` 计数跟着改，一条不剩的分组标题整个删掉。

    ## A3：没有 `#### ` 块的一节**原样返回，连拆都不拆**

    `_unwrap_ack_fold` 原来是**无条件**执行的，而重折的单位是 `#### ` 块。陈旧/孤儿
    两节的条目是 `- ` 列表项（`_coverage_section` 注释里明写"子标题一律用粗体行而不是
    `####`"，理由是别让一条被 ack 的孤儿把整个分档折走），于是**结转这两节 = 只拆不折**：
    折叠区与 ack 说明整个消失，被折进去的条目摊到节末，跟着上面最近的那个粗体分档标题
    ——2010 年的论文因此挂到「最近 3 个月新入库」那一档下面，小标题还写着"列前 1"而下面
    列了 2 条。陈旧节同理：被 ack 的论断摊到别的锚文献标题下，标题写"撑着 1 条"、
    导语写"1 篇老文献是这 1 条论断的唯一地基"，**数字与内容对不上**。

    这不是边角路径：`contradiction_reminder` 每 45 天就在报告与 stdout 里打印
    `--offline --skip-stale --skip-coverage`，`docs/scholar_notes_AGENTS.md` 也把它
    列为标准用法——走的正是这条。

    守卫放在最前面（而不是让 `_carry_section` 按 key 决定）：改动更小、更难写错，
    且顺带堵住 B1 的一半。代价是这两节结转时 ack **删掉了也不会重新展开**，
    但它们每月都是 fresh 跑的（月度自动化只跳过对撞），下一轮就正过来了。
    """
    try:
        acks = acks or {}
        raw = str(text or "")
        if not any(ln.strip().startswith("#### ") for ln in raw.splitlines()):
            return raw.strip("\n"), 0
        lines = _unwrap_ack_fold(raw.splitlines())

        # (kind, lines)；kind ∈ {"text", "h3", "h4"}
        items: List[Tuple[str, List[str]]] = []
        depth = 0
        for ln in lines:
            s = ln.strip()
            opens = s.startswith("<details")
            closes = s == "</details>"
            # `<details` 那一行本身就要**给当前 `####` 块收口**：真实报告里主视野的最后
            # 一个块后面紧跟着 L8 的写作素材折叠区，块边界若不在这里断开，折叠主视野那
            # 一条会把整个写作素材区（连同它里面的 `####` 块）一起搬进"你已确认"里。
            if depth or opens:
                items.append(("text", [ln]))
            elif s.startswith("#### "):
                items.append(("h4", [ln]))
            elif s.startswith("### ") or s.startswith("## "):
                items.append(("h3" if s.startswith("### ") else "text", [ln]))
            elif items and items[-1][0] == "h4":
                items[-1][1].append(ln)
            else:
                items.append(("text", [ln]))
            if opens:
                depth += 1
            elif closes:
                depth = max(0, depth - 1)

        folded: List[Tuple[str, List[str]]] = []      # (说明, 块正文)
        keep: List[Tuple[str, List[str]]] = []
        for kind, buf in items:
            if kind != "h4":
                keep.append((kind, buf))
                continue
            hit = [i for i in ids_in_text("\n".join(buf)) if i in acks]
            if hit:
                folded.append((acks.get(hit[0]) or "", buf))
            else:
                keep.append((kind, buf))

        # `###` 分组标题：重算计数，一条不剩的整个删掉。
        #
        # B1：这一段原来在 `if not folded: return` **之后**，于是两条早退路径
        # （`acks` 为空、以及有 acks 但一条都没命中本节）都跳过了它。审计实测：
        # 用户删掉 ack 或 ack 到了别的分组时，上一轮折进去的块被 `_unwrap_ack_fold`
        # 摊回**节末**，而 `### ⚔️ 结论冲突（1 处）` 底下于是列着 3 条；跨分组更糟
        # ——一条 `🔁 单篇内部自相矛盾` 被摊到 `⚔️ 结论冲突` 标题下面，**关系分类
        # 被错标**，而对撞节在月度节奏下永远是结转的，这个状态会一直挂着。
        #
        # 块的**归位**（摊回原分组而不是节末）成本高，不做；但标题上那个数字必须诚实。
        out_lines: List[str] = []
        for i, (kind, buf) in enumerate(keep):
            if kind == "h3":
                n = 0
                for k2, _b2 in keep[i + 1:]:
                    if k2 == "h3":
                        break
                    if k2 == "h4":
                        n += 1
                if n == 0:
                    continue
                out_lines.append(_H3_COUNT_RE.sub("（{} 处）".format(n), buf[0]))
                continue
            out_lines += _block_norm(buf) if kind == "h4" else buf
        if not folded:
            return "\n".join(_squeeze_blanks(out_lines)).strip("\n"), 0
        tail = ["", _fold_lead(len(folded)), "", _fold_open(len(folded)), ""]
        for note, buf in folded:
            tail += [_fold_note(note), ""] + _block_norm(buf)
        tail += ["</details>", ""]
        return "\n".join(_squeeze_blanks(out_lines + tail)).strip("\n"), len(folded)
    except Exception:      # pragma: no cover - 兜底；结转绝不能因为折叠失败而整节丢失
        return str(text or "").strip("\n"), 0


# ---------------------------------------------------------------------------
# 6b. 按 section 结转（A1）
# ---------------------------------------------------------------------------

# 四个稳定 key。报告是「库的当前状态」而不是「最后一次调用的快照」，全靠它们：
# 月度回填固定跑 `--skip-contradictions`，人手动跑一次全量对撞的结果下个月会被自动化
# 无声抹掉；人手动只看对撞（`--offline --skip-stale --skip-coverage`），撤稿那一节被
# 清空。而 `output/` 在 .gitignore 里，抹掉就是彻底不见——这比没有这项检查更危险，
# 它给人「有个月度机制在盯着」的错觉，盯没盯全看运气。
LINT_SECTIONS: Tuple[str, ...] = ("retraction", "contradictions", "stale", "coverage")
SECTION_HEADING: Dict[str, str] = {
    "retraction": "## ☣️ 撤稿检查",
    "contradictions": "## ⚔️ 跨文献对撞",
    "stale": "## ⏳ 证据基础可能过时的论断",
    "coverage": "## 🕳 覆盖缺口",
}
SECTION_SKIP_FLAG: Dict[str, str] = {
    "retraction": "`--offline`",
    "contradictions": "`--skip-contradictions` 或 `--dry-run`",
    "stale": "`--skip-stale`",
    "coverage": "`--skip-coverage`",
}
SECTION_NAME: Dict[str, str] = {
    "retraction": "撤稿", "contradictions": "对撞", "stale": "陈旧", "coverage": "缺口",
}

# 标记必须是 HTML 注释（Obsidian 与 pandoc 都不渲染），且落在 `<!-- BEGIN GENERATED -->`
# 与 `<!-- END GENERATED -->` 之间——哨兵哈希覆盖的是整个生成块正文，section 标记在其
# 内部，不影响哈希机制（见 test_lint.py 对此的回归）。
LINT_SECTION_MARK = "<!-- LINT-SECTION {} ran_at={} -->"
_LINT_SECTION_RE = re.compile(
    r"^<!--\s*LINT-SECTION\s+([A-Za-z0-9_-]+)(?:\s+ran_at=(\S*))?\s*-->$")

# 距上次跨文献对撞超过这么多天就在报告与 stdout 里提醒补跑（A5）。对撞是四项里质量
# 最高也最贵的一项，月度自动化明确只跑 `--skip-contradictions`（成本理由正当），
# 于是它完全依赖人自己想起来手动跑——典型的"我记得有这功能，但从没主动跑过"。
DEFAULT_CONTRADICTION_REMINDER_DAYS = 45


def split_lint_sections(body: Any) -> Dict[str, Tuple[str, str]]:
    """上一版报告正文 -> `{section key: (ran_at, 该节正文)}`（纯函数）。

    **解析失败一律降级成"没有可结转的历史"，绝不抛异常**：这份报告的生成不能因为
    上一版被人手改坏了就整个失败——那会把 A1 想解决的问题（结论静默消失）换一种
    更糟的形式重现（报告压根生不出来）。

    只扫 `GEN_END` 之前：用户在「我的批注」里粘一段旧报告（很自然的动作，比如"这是
    上个月的结论，留个底"）不该被当成可结转的历史结果结转回生成区。
    """
    out: Dict[str, Tuple[str, str]] = {}
    try:
        text = str(body or "")
        end = text.find(GEN_END)
        if end >= 0:
            text = text[:end]
        cur: Optional[str] = None
        ran = ""
        buf: List[str] = []
        for line in text.splitlines():
            m = _LINT_SECTION_RE.match(line.strip())
            if m:
                # N7：同一个 key 出现两次时**先到先得**，与 `validate_verdicts` 对重复
                # 编号的口径一致。首版"后者胜"会静默吃掉前一份的发现——这只可能由手改
                # 产生（而手改会先触发哨兵哈希 conflict），但"重复时丢掉哪一份"没有理由
                # 选丢掉先出现的那份。
                if cur and cur not in out:
                    out[cur] = (ran, "\n".join(buf).strip("\n"))
                cur, ran, buf = m.group(1), (m.group(2) or "").strip(), []
                continue
            if cur is not None:
                buf.append(line)
        if cur and cur not in out:
            out[cur] = (ran, "\n".join(buf).strip("\n"))
    except Exception:
        return {}
    return {k: v for k, v in out.items() if v[1].strip()}


def read_previous_lint(topics_dir) -> Dict[str, Tuple[str, str]]:
    """现存 `_lint.md` 的各 section（供本轮结转）。任何失败一律返回空表。"""
    try:
        text = (Path(topics_dir) / LINT_REPORT_NAME).read_text(encoding="utf-8")
        _fm, body = split_frontmatter(text)
        return split_lint_sections(body if isinstance(body, str) else text)
    except Exception:
        return {}


def read_previous_lint_counts(topics_dir) -> Dict[str, Any]:
    """现存 `_lint.md` 的 frontmatter（状态行里结转那半的计数从这里取，L6）。

    与 `carry_forward_counts` 是同一份数据、不同时机：那一步发生在落盘时（拿得到
    `read_existing` 的结果），而状态行在渲染时就要写出来，所以这里单独读一次。
    任何失败一律返回空表。
    """
    try:
        text = (Path(topics_dir) / LINT_REPORT_NAME).read_text(encoding="utf-8")
        fm, _body = split_frontmatter(text)
        return fm if isinstance(fm, dict) else {}
    except Exception:
        return {}


def _days_since(ran_at: str, now: datetime) -> Optional[int]:
    """`now - ran_at` 的天数。**有符号**：负数 = 时间戳在未来。

    N1 附带：首版是 `max(0, ...)`，未来时间戳（机器时钟跳变 / 手改 / 时区混用）被压成
    0 天，而 0 天在首版状态行里就是「✅ 本轮刚跑」——一个明显异常的输入被渲染成最强的
    正面保证。现在原样返回，由渲染侧分别措辞。
    """
    try:
        return (now - datetime.fromisoformat(str(ran_at))).days
    except Exception:
        return None


def _date_of(ran_at: Any) -> str:
    """ISO 时间戳 -> `YYYY-MM-DD`（解析不出就原样回显，绝不抛异常）。"""
    s = str(ran_at or "").strip()
    try:
        return datetime.fromisoformat(s).date().isoformat()
    except Exception:
        return s or "时间戳缺失"


def _age_phrase(days: Optional[int]) -> str:
    """把天数说成人话。0 天与未来时间戳各有各的写法，绝不含糊成"刚跑"。"""
    if days is None:
        return "**上一次运行**"
    if days < 0:
        return "**一个未来时间戳**（比现在晚 {} 天，多半是机器时钟或手改所致）".format(-days)
    if days == 0:
        return "**今天早些时候**"
    return "**{} 天前**".format(days)


def _strip_carry_banner(lines: List[str]) -> List[str]:
    """去掉上一轮结转时插进去的时效横幅。

    没有这一步，连续跳过 N 轮会在同一节堆叠 N 条横幅，而且每条写的天数都不一样，
    读起来像是这一节被跑过 N 次。
    """
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and lines[i].lstrip().startswith(">") and "⏸" in lines[i]:
        while i < len(lines) and lines[i].lstrip().startswith(">"):
            i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    return lines[i:]


def _carry_section(key: str, ran_at: str, text: str, now: datetime,
                   acks: Optional[Dict[str, str]] = None) -> str:
    """把上一版某节结转，在标题下方插一行醒目的时效说明，并对已 ack 的条目再折叠一次。

    L2：折叠这一步是纯文本处理（`fold_acked_blocks`），不重跑 LLM。没有它，月度自动化
    固定跑 `--skip-contradictions` 的节奏下 ack 永远不生效——对撞那一节年年原样展开。
    """
    lines = text.splitlines()
    head = SECTION_HEADING.get(key, "")
    if lines and lines[0].strip().startswith("## "):
        head, lines = lines[0], lines[1:]
    lines = _strip_carry_banner(lines)
    body, n_folded = fold_acked_blocks("\n".join(lines), acks)
    days = _days_since(ran_at, now)
    when = ran_at or "时间戳缺失"
    banner = [
        "> ⏸ **本轮未执行**（{}）。以下是 {}（{}）那次运行的结果，".format(
            SECTION_SKIP_FLAG.get(key, ""), _age_phrase(days), when),
        "> **不代表当前状态**——库在这期间新增了论文，这一节的结论可能已经变了。",
    ]
    if key == "retraction":
        # N8：结转来的「已被概念页引用：X、Y」是**上一轮的页面状态**，而这恰恰是最容易
        # 被当成当前事实去行动的一句——用户会照着那份名单去重跑那几页。
        banner.append("> 每条撤稿下面那句「已被概念页引用：…」同样是**那一轮的页面快照**，"
                      "概念页在这期间可能已经重合成过，动手前先核实。")
    if n_folded:
        banner.append("> （其中 {} 条你此前在批注里确认过，已折叠到本节末尾——"
                      "折叠是本轮做的，不是那一轮的结果。）".format(n_folded))
    return "\n".join(([head] if head else []) + [""] + banner + [""]
                     + body.splitlines()).strip("\n")


def contradiction_reminder(checks_ran_at: Dict[str, str], now: datetime,
                           threshold_days: int = DEFAULT_CONTRADICTION_REMINDER_DAYS) -> str:
    """距上次对撞检查太久时的一句提示（纯函数；不超阈值返回空串）。

    刻意做成**提示**而不是强制触发：月度自动化不跑对撞的成本理由是正当的，把它改成
    自动跑会让每月回填多叠一轮 LLM（W7 的历史事故就是这个形状）。
    """
    ran = str((checks_ran_at or {}).get("contradictions") or "").strip()
    cmd = ("PYTHONPATH=. python scripts/lint_notes.py "
           "--offline --skip-stale --skip-coverage")
    if not ran:
        return ("⏰ **跨文献对撞从未执行过**（四项里质量最高的一项，月度自动化不跑它）。"
                "补跑：`{}`".format(cmd))
    days = _days_since(ran, now)
    if days is None or days < max(0, threshold_days):
        return ""
    return ("⏰ **距上次跨文献对撞已 {} 天**（阈值 {} 天，`--contradiction-reminder-days` 可调）。"
            "这是四项里质量最高也最贵的一项，月度自动化只跑 `--skip-contradictions`，"
            "它完全依赖人自己想起来。补跑：`{}`".format(days, threshold_days, cmd))


# 各节计数在状态行里的量词（L6：一行两用，既说时效也说本轮发现了多少）。
SECTION_COUNT_UNIT: Dict[str, str] = {
    "retraction": "篇", "contradictions": "处", "stale": "条", "coverage": "篇",
}
# 状态行里各节计数取 frontmatter 的哪个键（结转那半靠它从上一版 frontmatter 拿数）。
SECTION_COUNT_KEY: Dict[str, str] = {
    "retraction": "n_retracted", "contradictions": "n_contradictions",
    "stale": "n_stale_claims", "coverage": "n_orphans",
}


def _status_line(sections: List[Tuple[str, str, bool]], now: datetime,
                 counts: Optional[Dict[str, Optional[int]]] = None,
                 todo: Optional[Dict[str, Optional[int]]] = None) -> List[str]:
    """报告顶部那行「本轮状态」：四项各自是本轮真跑的还是结转的、结转的有多旧、各多少条。

    N1（本轮最危险的一条）：**绝不从时间戳反推是否本轮执行**。首版用 `days == 0` 判
    「本轮刚跑」，于是"全跑发现 0 篇撤稿 → 同日 `--offline` 窄跑"这条真实路径下，状态
    行写着「撤稿 ✅ 本轮刚跑」，而往下翻 12 行同一份文件写着「⏸ 本轮未执行
    （`--offline`）」——同一份报告两处互相打脸，**而排在最前面、最可能被扫一眼就走的
    那处是错的**。`render_lint_report` 里的 `rendered` 本来就明确知道每节 fresh 还是
    carried，把这个布尔传下来，`✅` 只认它。

    L7：**每一项都带绝对日期**。报告是磁盘上的静态文件，用户可能 60 天后才打开；更糟
    的是月度 launchd 若挂了（本仓库有前科：`com.xlbd.scholar-embed` 退出码 2），文件根本
    不会更新，那行"✅ 本轮刚跑"会一直挂着，而"刚"是相对谁说的没人知道。
    """
    counts = counts or {}
    todo = todo or {}
    parts: List[str] = []
    for key, ran, is_fresh in sections:
        name = SECTION_NAME.get(key, key)
        n = counts.get(key)
        tail = "" if n is None else " · {} {}".format(n, SECTION_COUNT_UNIT.get(key, "条"))
        # B5：ack 掉一批之后第一屏的数字必须跟着降，否则"这份待办会随我干活变短"
        # 这个感觉建立不起来（ack 掉 5 篇孤儿之后顶部依然写着「缺口 · 208 篇」）。
        # 两个口径都写：总量是扫描分母，待办才是"我还要动手的"。
        t = todo.get(key)
        if n is not None and isinstance(t, int):
            tail += " · 待办 {}".format(t)
        if is_fresh:
            parts.append("{} ✅ 本轮刚跑（{}{}）".format(name, _date_of(ran), tail))
            continue
        if not ran:
            parts.append("{} ❓ 从未执行".format(name))
            continue
        days = _days_since(ran, now)
        if days is None:
            parts.append("{} ⏸ 结转自上一次（时间戳无法解析：{}{}）".format(name, ran, tail))
        elif days < 0:
            parts.append("{} ⏸ 结转自一个**未来**时间戳（{}，比现在晚 {} 天{}）".format(
                name, _date_of(ran), -days, tail))
        elif days == 0:
            parts.append("{} ⏸ 结转自今天早些时候（{}{}）".format(name, _date_of(ran), tail))
        else:
            parts.append("{} ⏸ 结转自 {} 天前（{}{}）".format(name, days, _date_of(ran), tail))
    return ["> **本轮状态**：{}。".format(" · ".join(parts)),
            "> 标 ⏸ 的那几节是**上一次运行的结果原样结转**，不代表当前状态；"
            "标 ✅ 的才是本轮真跑出来的（括号里是那一节结果的绝对日期）。", ""]


def _must_do_line(retraction: RetractionScan, rendered_fresh: bool,
                  carried_ran: str, carried_n: Optional[int]) -> str:
    """顶部那行「本轮必须处理」（L6）：**只放硬信号**。

    验收实测：生成块开头依次是 导语 blockquote → 本轮状态 2 行 → ⏰ 提醒 1 段 →
    撤稿口径分母 1 段，**第一处真发现（🚨）在生成块第 16 行**。30 秒内用户知道的是
    "哪几节是结转的"，不是"我现在要做什么"。

    当前唯一的硬信号是撤稿（也是唯一让退出码非零的一项）。陈旧/缺口/对撞都是"值得看
    一眼"，放进来会立刻让这一行退化成第二份全量清单。结转来的撤稿命中**照样要报**：
    本轮没复查不等于那篇论文已经被移出库了。
    """
    if rendered_fresh:
        n = len(retraction.hits)
        return "撤稿 {} 篇".format(n) if n else "无"
    if isinstance(carried_n, int) and carried_n > 0:
        return "撤稿 {} 篇（结转自 {}，**本轮未复查**，未确认已处置）".format(
            carried_n, _date_of(carried_ran))
    return "无"


