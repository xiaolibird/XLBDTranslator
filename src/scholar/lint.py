# -*- coding: utf-8 -*-
"""知识层 lint：札记库作为一个整体，有没有自相矛盾、有没有踩到已撤稿的文献、
有没有已经过时却还挂在概念页上的论断、有没有大片没人回答的缺口。

## 与既有检查的分界

仓库里已经有几层检查，但都不在知识层：

  - `notes_index.py` 的撞键/近重复检测 —— 运维一致性（同一篇论文别进两次）；
  - `build_topics.py --verify` —— 单页可追溯性（这一页的每个引用还找得回去吗）；
  - `repair_references.py` —— 产出层书目完整性。

它们都是**在单个产物内部**问"格式对不对"。本模块问的是另一类问题：**把整个库摆在
一起看，知识本身有没有问题**——A 篇说的和 B 篇说的能同时成立吗？我引的这篇还没被
撤稿吧？这条五年前的结论还算数吗？三年下来我漏掉了哪块？

## 四项检查

1. **跨文献对撞**（`find_contradiction_candidates` + `adjudicate_contradictions`）
   句级证据里 `citable`（可引用证据）与 `refutable`（可反驳观点）两类角色的跨论文
   语义近邻对 —— 程序侧只负责"这两句在讲同一件事"，**是不是真冲突交给 LLM 裁决**。
   这个分工是必须的：2026-08-17 在本库 8747×4266 条句级证据上实测，相似度 ≥0.80 的
   46 对里绝大多数是「两篇论文的〖训练与评估协议〗节都在列 AUROC/AUPRC/F1」这种
   **同节平行描述**，语义极近但毫无冲突；真正有价值的那种（一篇 F1 阈值在验证集上
   调、另一篇固定 0.5 并自陈次优）混在里面，靠相似度分不开。所以相似度只当**候选
   生成器**，判定权交给读得懂内容的一侧。

2. **撤稿检查**（`check_retractions`）走 OpenAlex 的 `is_retracted`（免费、支持
   50 个 DOI 一批过滤）。这是**唯一必须联网**的一项。

3. **陈旧论断**（`find_stale_claims`）纯计算：概念页上的论断，其支撑文献最新的一篇
   也已经是 N 年前 —— 不是判它错，是提醒人工确认有没有被更新的工作推翻。

4. **覆盖缺口**（`coverage_report`）纯计算：已精读但从未被任何概念页引用的高优先级
   论文（该开新概念页了吗），以及证据最薄的几页。

## 防幻觉

对撞裁决沿用概念页那套**编号回译**（见 `topics.py` 模块 docstring）：喂给 LLM 的
候选对只有 `P1..Pn` 与两句原文，**不含 citekey**；模型回来的是编号 + 关系分类，
citekey 由程序侧按编号填回。越界编号剔除，分类不在白名单的整条丢弃，说明文字里
模型自写的引用一律剥掉（复用 `topics._clean_text`）。

## 报告是给人读的，不是给机器读的

产物 `output/scholar_notes/topics/_lint.md` 每一条都必须能点回原文（citekey + 札记
文件行号），且**扫描口径要写在报告里**——"0 篇撤稿"在 373 篇没有 DOI 的前提下不等于
"库是干净的"，不把分母写出来就是在制造虚假的安心。
"""
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..utils.logger import get_logger
from .notes_index import is_retracted
from .topics import (
    DEFAULT_EXCLUDE_SECTIONS, GEN_END, TopicError,
    _CITE_RE, _clean_text, is_topic_page_file, parse_synthesis, ValidationReport,
)
from .vault import ROLE_LABEL, split_frontmatter

logger = get_logger(__name__)

LINT_SCHEMA_VERSION = 1
LINT_REPORT_NAME = "_lint.md"
DEFAULT_PROMPT = "config/prompts/lint_contradiction_prompt.md"
# 写进哨兵注释的「由 X 生成」。topics/ 下不止一种产物，哨兵指错脚本会把人引到一个
# 根本不生成这份文件的命令上（见 topics.assemble 的 generator 参数）。
LINT_GENERATOR = "scripts/lint_notes.py"


# ---------------------------------------------------------------------------
# 1. 跨文献对撞：候选生成（纯计算，不调 LLM）
# ---------------------------------------------------------------------------

# 候选相似度下限。**与概念页召回的 0.55 不是一个口径，别互相照抄**——那是"这句话
# 和这个概念有没有关系"（query↔证据，查询短、证据长，语义密度天然不对等）；这里是
# "这两句话在不在讲同一件具体的事"（证据↔证据，两边同构），要求高得多。
#
# 2026-08-17 在本库 8747 条 citable × 4266 条 refutable（跨论文对，已排除同篇）上
# 实测的分布：
#   ≥0.90        0 对
#   ≥0.85        4 对
#   ≥0.80       46 对
#   ≥0.75      553 对
#   ≥0.70     6198 对
# 抽样看内容：0.80 以上以"两篇论文的同一个精读分节在平行描述各自做法"为主
# （〖训练与评估协议〗节互相之间就贡献了一大批），其中确有真分歧（park2026Missingness
# 的 F1 阈值在验证集上选定 vs poulain2024Graph 固定 0.5 并自陈次优）；0.75~0.78 一带
# 开始混入"两篇都在说自己没做外部验证"这种**观点一致**的对。
#
# 取 0.78：候选量落在几十对量级（一次 LLM 裁决批次能吃下），且几乎每对都确实在讲
# 同一件具体的事——**是不是冲突由 LLM 判，这里只保证"同题"**。调低会线性放大裁决
# 成本（LLM 调用是这条链路唯一花钱的地方），调高会漏掉真分歧。
DEFAULT_PAIR_MIN_SIM = 0.78

# 一次运行最多提交多少对给 LLM 裁决。默认 60 对 ≈ 5 个批次，与概念页单页合成的量级
# 相当。上限存在的理由与 build_topics.py 的 --stale-max-per-run 同源：这套链路的
# LLM 回退链实际有效冗余不到 2 级（见 topics.RETRY_COOLDOWN_HOURS 的依据注释），
# 一次跑太多既慢又容易整批撞限流，不如摊到几次运行。
DEFAULT_MAX_PAIRS = 60

# 每批喂给 LLM 几对。太大容易让模型对靠后的对敷衍（实测综述类任务同样的毛病），
# 太小则批次数上升、更容易在中途撞限流。
DEFAULT_BATCH_SIZE = 12

# 同一对论文（无序）最多保留几个候选对。没有这个配额，两篇结构相似的论文光靠
# 「同名精读分节平行描述」就能刷出十几对候选，把裁决预算吃光而只揭示一件事。
DEFAULT_PER_KEY_PAIR_CAP = 1
# 同一条 highlight 最多参与几个候选对。与上面那条相反，这条**不能设成 1**：一句话
# 同时和三篇论文冲突恰恰是最值得看的信号（实测 poulain2024Graph 那句"F1 阈值固定
# 0.5"同时对上了 3 篇不同论文），砍到 1 会把最强的发现削成最弱的。
DEFAULT_PER_HIGHLIGHT_CAP = 3

DEFAULT_ROLES_A: Tuple[str, ...] = ("citable",)
DEFAULT_ROLES_B: Tuple[str, ...] = ("refutable",)


@dataclass
class PairSide:
    """候选对的一侧：一条句级证据 + 它的出处。"""
    citekey: str
    text: str
    role: Optional[str] = None
    section: Optional[str] = None
    note_file: Optional[str] = None
    note_line: Optional[int] = None
    year: Optional[int] = None
    title: Optional[str] = None


@dataclass
class CandidatePair:
    ref: str                # "P1"（定稿后才赋值，同 topics.Evidence.ref 的约定）
    score: float
    a: PairSide
    b: PairSide

    @property
    def pid(self) -> str:
        """跨运行稳定的候选对 ID（A6）。

        用途是给「我已经确认过，这条不是问题」一个表达通道：生成区每次整块重建、
        不读批注区，所以同一对句子只要相似度还在阈值上，下个月还会被重新拉进候选、
        重新送 LLM 裁决、重新出现在报告里——哪怕上个月我已经在批注里写过"这条我看
        过了"。半年后这份报告会变成每次都在重问我已经回答过的问题。

        两条硬要求：
        - **对"两句顺序对调"稳定**：候选生成时哪句当甲哪句当乙取决于 records 的遍历
          顺序，库一重建就可能反过来，ID 跟着变的话所有 ack 一次全失效；
        - **句子内容一变 ID 就变**：原文改了就是新问题，该重问。所以两侧文本一起进
          哈希，而不是只哈希 citekey 对。

        已知局限（L10，本轮不改）：哈希的是**两侧**文本，所以对面那一篇的句子被改了
        （哪怕本篇一字未动）ID 也会变、ack 随之失效。理论上更精确的做法是只锚定"被
        ack 的那一侧"，但那会引入新的 ack 语义问题（ack 到底是在说"这一对不是问题"
        还是"这一句没问题"），改动的收益远小于风险。
        """
        import hashlib
        sides = sorted(((self.a.citekey or "", _norm_text(self.a.text)),
                        (self.b.citekey or "", _norm_text(self.b.text))))
        raw = "\x1e".join("{}\x1f{}".format(k, re.sub(r"\s+", " ", t)) for k, t in sides)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def _norm_text(s: Any) -> str:
    """句级文本的统一归一化口径：去首尾空白 + 压掉内嵌换行。

    与 `topics.retrieve_evidence` 里那处同源（见其注释 G8）：页面/报告上展示的引文
    一律是单行，若比对基准这边保留原始换行，逐字比对就永远对不上。这里额外还用于
    候选对的文本去重。
    """
    return (s or "").strip().replace("\n", " ")


def find_contradiction_candidates(
        store, *,
        min_sim: float = DEFAULT_PAIR_MIN_SIM,
        roles_a: Sequence[str] = DEFAULT_ROLES_A,
        roles_b: Sequence[str] = DEFAULT_ROLES_B,
        max_pairs: int = DEFAULT_MAX_PAIRS,
        per_key_pair_cap: int = DEFAULT_PER_KEY_PAIR_CAP,
        per_highlight_cap: int = DEFAULT_PER_HIGHLIGHT_CAP,
        exclude_sections: Sequence[str] = DEFAULT_EXCLUDE_SECTIONS,
) -> List[CandidatePair]:
    """全库跨论文的"同题反向角色"句对，按相似度降序，已去重与配额截断。

    实现是分块矩阵乘而不是逐条 `store.search`：本库 8747×4266 的全量得分矩阵是
    3700 万个 float32（150MB），一次算完会有明显的内存尖峰，而按 512 行分块后峰值
    只有 ~9MB，总耗时仍在一秒量级（bge-m3 向量已 L2 归一，点积即余弦）。

    **同篇论文的两条句子永远不算候选**：一篇论文自己在「局限」里说的话与它自己在
    「结论」里说的话之间不存在"文献间冲突"，那是同一作者的自我限定（概念页 prompt
    的第 10 条铁律踩过同一个坑，见 `config/prompts/topic_synthesis_prompt.md`）。

    `exclude_sections` 沿用概念页的默认值：「对我研究的联想」是精读者本人的推测性
    批注而非文献内容，两条主观联想互相"冲突"没有任何知识意义，只会污染裁决预算。
    按**前缀**匹配（库里有三种带括注的变体写法，见 topics.retrieve_evidence 的注释）。
    """
    import numpy as np

    set_a, set_b = set(roles_a), set(roles_b)
    prefixes = tuple(p for p in exclude_sections if p) if exclude_sections else ()

    def _pick(roles: set) -> List[int]:
        out = []
        for i, r in enumerate(store.records):
            if r.get("level") != "highlight" or r.get("role") not in roles:
                continue
            if prefixes and str(r.get("section") or "").startswith(prefixes):
                continue
            if not _norm_text(r.get("text")):
                continue
            out.append(i)
        return out

    idx_a, idx_b = _pick(set_a), _pick(set_b)
    if not idx_a or not idx_b:
        logger.warning("对撞检测没有候选：角色 {} 有 {} 条、角色 {} 有 {} 条",
                       sorted(set_a), len(idx_a), sorted(set_b), len(idx_b))
        return []

    mat_a = store.mat[idx_a]
    mat_b = store.mat[idx_b]
    keys_a = np.array([str(store.records[i].get("citekey") or "") for i in idx_a])
    keys_b = np.array([str(store.records[i].get("citekey") or "") for i in idx_b])

    raw: List[Tuple[float, int, int]] = []
    block = 512
    for start in range(0, len(idx_a), block):
        sub = mat_a[start:start + block]
        scores = sub @ mat_b.T
        # 同篇论文置 -1 而不是事后过滤：np.where 一次搞定，且避免大数组上再来一轮
        # 布尔索引。同时挡住 citekey 为空串的病态记录彼此配对（两边都是 "" 会相等）。
        same = keys_a[start:start + block][:, None] == keys_b[None, :]
        scores = np.where(same, -1.0, scores)
        rows, cols = np.where(scores >= min_sim)
        for r, c in zip(rows, cols):
            raw.append((float(scores[r, c]), idx_a[start + int(r)], idx_b[int(c)]))

    raw.sort(key=lambda t: -t[0])

    seen_pair: Dict[frozenset, int] = {}
    seen_hl: Dict[int, int] = {}
    seen_text: set = set()
    out: List[CandidatePair] = []
    for score, ia, ib in raw:
        ra, rb = store.records[ia], store.records[ib]
        ka, kb = str(ra.get("citekey") or ""), str(rb.get("citekey") or "")
        if not ka or not kb:
            continue
        pair_key = frozenset((ka, kb))
        if seen_pair.get(pair_key, 0) >= per_key_pair_cap:
            continue
        if seen_hl.get(ia, 0) >= per_highlight_cap or seen_hl.get(ib, 0) >= per_highlight_cap:
            continue
        ta, tb = _norm_text(ra.get("text")), _norm_text(rb.get("text"))
        # 同一篇论文的同一句话可能因 role 标注不同而在库里存在多份 chunk
        # （embed_store.chunks_from_index 的 id 里掺了 role，正是为了让它们各自独立），
        # 两句文本完全相同的"冲突"是噪音，不是发现。
        if ta == tb:
            continue
        text_key = (ta, tb)
        if text_key in seen_text:
            continue
        seen_text.add(text_key)
        seen_pair[pair_key] = seen_pair.get(pair_key, 0) + 1
        seen_hl[ia] = seen_hl.get(ia, 0) + 1
        seen_hl[ib] = seen_hl.get(ib, 0) + 1
        out.append(CandidatePair(
            ref="", score=score,
            a=PairSide(citekey=ka, text=ta, role=ra.get("role"), section=ra.get("section"),
                       note_file=ra.get("note_file"), note_line=ra.get("note_line"),
                       year=ra.get("year")),
            b=PairSide(citekey=kb, text=tb, role=rb.get("role"), section=rb.get("section"),
                       note_file=rb.get("note_file"), note_line=rb.get("note_line"),
                       year=rb.get("year")),
        ))
        if max_pairs > 0 and len(out) >= max_pairs:
            break

    for i, p in enumerate(out, 1):
        p.ref = "P{}".format(i)
    return out


def attach_pair_titles(pairs: Sequence[CandidatePair], index: dict) -> None:
    """就地补上论文标题（向量库 chunk 不存 title）。同 topics.attach_titles。"""
    titles = {e.get("citekey"): (e.get("title") or "")
              for e in (index.get("papers") or []) if isinstance(e, dict)}
    for p in pairs:
        for side in (p.a, p.b):
            if not side.title:
                side.title = titles.get(side.citekey) or None


# ---------------------------------------------------------------------------
# 2. 跨文献对撞：LLM 裁决
# ---------------------------------------------------------------------------

# 关系分类白名单。**不是二分类**：实测候选池里"同题但不冲突"占绝大多数，只给
# 冲突/不冲突两档会逼模型把"两篇做法不同"硬塞进"冲突"，报告立刻失去信号。
# 分四档后 `none` 是合法且预期中的答案，模型没有凑数压力。
RELATION_LABEL: Dict[str, str] = {
    "conflict": "结论冲突",
    "method-divergence": "方法学分歧",
    "scope-limit": "适用范围限定",
    # A4：张力实际来自**其中一篇自己**（"本文附录 I 与附录 R 对 GPU 型号不一致"），
    # 另一篇只是巧合地撞上了它内部矛盾的其中一半。首版把这种发现套进跨论文对撞的
    # 模板（标题写成「甲 ↔ 乙」），读者第一反应是去核对这两篇谁的数字对，而真正该做
    # 的是去翻那一篇自己的两个附录——报告把发现**路由错了**，浪费用户时间。
    "self-inconsistency": "单篇内部自相矛盾",
    "none": "同题但不构成张力",
}
# 进报告的关系类型（`none` 只计数不列出）。
REPORTABLE_RELATIONS: Tuple[str, ...] = ("conflict", "self-inconsistency",
                                         "method-divergence", "scope-limit")
RELATION_EMOJI = {"conflict": "⚔️", "self-inconsistency": "🔁",
                  "method-divergence": "🔀", "scope-limit": "📐"}

_PAIR_REF_RE = re.compile(r"^[Pp](\d+)$")
# 裁决里"问题出在哪一篇"的回译表。同编号回译的原则：模型只写「甲」/「乙」这种
# 位置代号，程序侧翻成 a/b 再填 citekey——模型永远拿不到 citekey，也就写不出 citekey。
_SUBJECT_MAP = {"甲": "a", "乙": "b", "a": "a", "b": "b", "A": "a", "B": "b"}


@dataclass
class Verdict:
    pair: CandidatePair
    relation: str
    note: str = ""
    # 仅对 `self-inconsistency` 有意义：矛盾出在 "a"（甲）还是 "b"（乙）。
    # 空串 = 模型没点名或点得不合法，渲染时退回"其中一篇（裁决未点名）"的写法。
    subject: str = ""


@dataclass
class VerdictReport:
    """裁决战果。落进报告头部，异常值一眼可见（`invalid_refs` 突然变大 = 模型开始
    乱编编号，`unknown_relations` 变大 = 模型不按白名单答，都该换模型或改 prompt）。"""
    judged: int = 0
    invalid_refs: int = 0
    unknown_relations: int = 0
    duplicate_refs: int = 0
    stripped_cites: int = 0
    batches_failed: int = 0
    errors: List[str] = field(default_factory=list)


def build_contradiction_prompt(pairs: Sequence[CandidatePair], template: str) -> str:
    """填充裁决 prompt。**候选块只给编号与两句原文，不给 citekey**——同概念页的
    编号回译（见模块 docstring），模型看不到 citekey 就写不出 citekey。"""
    def _meta(side: PairSide) -> str:
        # 带上精读分节名：模型据此才知道这句是"作者自述做法"（实验方法）还是
        # "精读者挑的毛病"（局限与可质疑点）——同一句话在这两种语境下该不该算张力
        # 完全不同。年份同理（两篇隔了五年，做法不同更可能是时代差异而非分歧）。
        parts = [ROLE_LABEL.get(side.role or "", side.role or "证据")]
        if side.section:
            parts.append(side.section)
        if side.year:
            parts.append(str(side.year))
        return " · ".join(parts)

    blocks: List[str] = []
    for p in pairs:
        blocks.append("### {}\n- 甲（{}）：{}\n- 乙（{}）：{}".format(
            p.ref, _meta(p.a), p.a.text, _meta(p.b), p.b.text))
    return template.replace("{{PAIR_BLOCK}}", "\n\n".join(blocks))


def load_prompt_template(path) -> str:
    p = Path(path)
    if not p.exists():
        raise TopicError("对撞裁决 prompt 模板不存在：{}".format(p))
    text = p.read_text(encoding="utf-8")
    if "{{PAIR_BLOCK}}" not in text:
        raise TopicError("prompt 模板 {} 缺占位符 {{{{PAIR_BLOCK}}}}".format(p))
    return text


def validate_verdicts(data: dict, pairs: Sequence[CandidatePair]
                      ) -> Tuple[List[Verdict], VerdictReport]:
    """LLM 输出 -> 编号回译后的裁决列表。

    丢弃而非修补，理由同 `topics.validate_synthesis`：编号越界说明模型在这一条上
    要么编造要么张冠李戴，留着比少一条更危险——报告的全部价值就是每条都点得回原文。
    分类不在白名单同理丢弃（而不是兜底成 `none`）：兜底会把"模型答坏了"静默变成
    "确认无冲突"，恰恰是最不该被掩盖的那一种失败。
    """
    report = VerdictReport()
    by_ref = {p.ref: p for p in pairs}
    seen: set = set()
    out: List[Verdict] = []
    # `_clean_text` 的剥离计数写在 topics.ValidationReport 上，这里借一个壳收计数，
    # 再并进 VerdictReport——两边共用同一套剥离规则（正文里模型自写的 citekey 是
    # 唯一能绕开编号校验的编造通道），不在本模块重新实现一份。
    sink = ValidationReport()
    for item in (data.get("verdicts") or []):
        if not isinstance(item, dict):
            continue
        ref = str(item.get("pair") or "").strip()
        m = _PAIR_REF_RE.match(ref)
        if m:
            ref = "P{}".format(int(m.group(1)))     # p3 / P03 -> P3
        pair = by_ref.get(ref)
        if pair is None:
            report.invalid_refs += 1
            continue
        if ref in seen:
            # 同一编号答两次：后一条无从判断该信谁，保留首条（与 _resolve_refs 的
            # 去重口径一致：先到先得），把重复计数亮出来。
            report.duplicate_refs += 1
            continue
        relation = str(item.get("relation") or "").strip().lower()
        if relation not in RELATION_LABEL:
            report.unknown_relations += 1
            continue
        seen.add(ref)
        # subject 只是"问题出在甲还是乙"的位置代号，取值不合法时不丢整条裁决——
        # 与编号/分类不同，它不影响这条发现是否可追溯，只影响标题措辞，退回"未点名"
        # 的写法即可（丢掉整条反而会把一个真发现抹掉）。
        subject = _SUBJECT_MAP.get(str(item.get("subject") or "").strip(), "")
        out.append(Verdict(pair=pair, relation=relation, subject=subject,
                           note=_clean_text(item.get("note"), sink)))
    report.judged = len(out)
    report.stripped_cites = sink.stripped_cites
    return out, report


def adjudicate_contradictions(llm, pairs: Sequence[CandidatePair], template: str, *,
                              batch_size: int = DEFAULT_BATCH_SIZE,
                              model: Optional[str] = None,
                              max_tokens: int = 4000
                              ) -> Tuple[List[Verdict], VerdictReport]:
    """分批调 LLM 裁决。

    **单批失败不中止整轮**：候选对之间彼此独立，第 3 批撞上限流不该让前两批已经
    拿到的裁决一起作废（概念页那边单页失败不带走整批是同一条原则，见
    `build_topics.py` 主循环）。失败批次计入 `batches_failed` 并把错误摘要留在
    `errors` 里，报告头部会如实写出"本轮有 N 批未裁决"——**不能让缺席的批次看起来
    像是"裁决过且无冲突"**。

    连续失败熔断同 `build_topics.py`：回退链一旦整条耗尽，后面每批都会以同一个错误
    再失败一次，连挂 2 批就停。
    """
    verdicts: List[Verdict] = []
    total = VerdictReport()
    streak = 0
    n_batches = (len(pairs) + batch_size - 1) // max(1, batch_size)
    for bi in range(n_batches):
        batch = list(pairs)[bi * batch_size:(bi + 1) * batch_size]
        if not batch:
            continue
        if streak >= 2:
            total.batches_failed += n_batches - bi
            total.errors.append("连续 2 批失败（多半是 LLM 回退链已整条耗尽），"
                                "中止剩余 {} 批".format(n_batches - bi))
            break
        prompt = build_contradiction_prompt(batch, template)
        try:
            raw = llm.call(prompt, model=model, max_tokens=max_tokens,
                           temperature=0.1, json_mode=True)
            data = parse_synthesis(raw)
        except Exception as e:
            streak += 1
            total.batches_failed += 1
            total.errors.append("第 {} 批（{} 对）裁决失败：{}: {}".format(
                bi + 1, len(batch), type(e).__name__, str(e)[:160]))
            logger.warning("对撞裁决第 {} 批失败：{}", bi + 1, e)
            continue
        streak = 0
        vs, rep = validate_verdicts(data, batch)
        verdicts += vs
        total.judged += rep.judged
        total.invalid_refs += rep.invalid_refs
        total.unknown_relations += rep.unknown_relations
        total.duplicate_refs += rep.duplicate_refs
        total.stripped_cites += rep.stripped_cites
    return verdicts, total


# ---------------------------------------------------------------------------
# 3. 撤稿检查（OpenAlex）
# ---------------------------------------------------------------------------

OPENALEX_API = "https://api.openalex.org/works"
# 一次 filter 里塞多少个 DOI。OpenAlex 官方文档给的 OR 上限是 50。
OPENALEX_BATCH = 50
# 出版社在标题里打的撤稿标记。OpenAlex 的 `is_retracted` 有已知覆盖缺口（它依赖
# Crossref 的撤稿关系记录，部分期刊只改标题不发结构化撤稿记录），标题前缀是**独立
# 的第二信号**——两个信号任一命中都要报，宁可让人多核对一次。
#
# **不许用 `\b`**（B1）：Python 3 的 `\b` 与 `\w` 一样按 Unicode 判定单词字符，汉字
# 算单词字符，所以「撤稿」后面紧跟另一个汉字时两侧都是 word-char，边界断言不成立、
# 整条匹配失败——`撤稿：某某` 能命中纯属冒号是非 word 字符的巧合，而真实的撤稿通知
# 标题恰恰是 `撤稿声明` / `已撤回论文` 这种形状，全部漏判。
#
# 分两支写：
#   - ASCII 标记（RETRACTED/WITHDRAWN）后面**显式列出**"不是 ASCII 字母或数字"，
#     免得 `Retractedness of ...` 这类以标记为词根的正常标题被误报；
#   - CJK 标记（撤稿/已撤回）**不设后置边界**——中文没有词边界这回事，标题以它开头
#     就是撤稿通知，后面跟什么都算。
_RETRACTED_TITLE_RE = re.compile(
    r"^\s*(?:(?:RETRACTED|WITHDRAWN)(?![A-Za-z0-9])|撤稿|已撤回)", re.I)


def normalize_doi(doi: Any) -> str:
    """DOI 归一化：剥掉 scheme/前缀、去尾斜杠、转小写。

    OpenAlex 回传的 DOI 一律是 `https://doi.org/10.x/y` 且**小写**，而索引里存的
    形态五花八门（`10.48550/arXiv.2101.09986` 这种带大小写混排的 arXiv DOI 很常见）。
    两边不走同一个归一化就会对不上号，表现为"全库 DOI 都查不到"这种一眼可见的故障，
    但更坏的情况是只有一部分对不上——那会静默缩小实际扫描范围。
    """
    s = str(doi or "").strip()
    if not s:
        return ""
    s = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", s, flags=re.I)
    s = re.sub(r"^doi:", "", s, flags=re.I)
    return s.strip().strip("/").lower()


@dataclass
class RetractionHit:
    citekey: str
    doi: str
    title: str
    signal: str          # openalex-flag / title-marker
    openalex_id: str = ""
    # 札记里已经打了 `⚑ RETRACTED` 吗（见 notes_index.RETRACTED_FLAG）。
    # **这一位决定要不要喊人**：撤稿论文按新口径是"保留札记 + 标记 + 踢出向量库"，
    # 处置完之后它**永远还在库里**——不分已标记/未标记的话，
    # 每月的自动 lint 会对着同两篇一直退 1、一直弹通知，几个月后这个警报就没人信了。
    # 已标记的仍然列在报告里（是"库里有这些、已处置"的事实），但不再是硬信号。
    acknowledged: bool = False


@dataclass
class RetractionScan:
    """扫描结果 **与扫描口径**。分母必须跟着结论一起走：`hits` 为空在
    `n_no_doi=373` 的前提下只意味着"有 DOI 的那部分是干净的"，报告里不写分母
    就是在制造虚假的安心。"""
    hits: List[RetractionHit] = field(default_factory=list)
    n_papers: int = 0            # 参与扫描的 keeper 论文数
    n_with_doi: int = 0
    n_resolved: int = 0          # OpenAlex 真的返回了记录的
    n_no_doi: int = 0
    n_unresolved: int = 0        # 有 DOI 但 OpenAlex 查无此条
    n_failed: int = 0            # 落在失败批次里、本轮压根没查成的 DOI 数
    errors: List[str] = field(default_factory=list)
    skipped: bool = False        # --offline：整项没跑（区别于"跑了但一无所获"）

    @property
    def coverage(self) -> float:
        return (self.n_resolved / self.n_papers) if self.n_papers else 0.0

    @property
    def unhandled(self) -> List[RetractionHit]:
        """**还没在札记里打 `⚑ RETRACTED` 的**那些——只有这些才是要人动手的。

        退出码、`🚨` 行、月度 notify 全都只看这一个列表。已标记的仍然出现在报告里
        （"库里有这两篇、已处置"是事实，不该消失），但不再触发警报——
        否则处置完之后每月照样退 1，几个月后这个信号就没人信了。
        """
        return [h for h in self.hits if not h.acknowledged]


def _scan_targets(index: dict) -> List[dict]:
    """参与撤稿扫描的条目：keeper（非跨月重复）且有 citekey。

    与 `embed_store.chunks_from_index` / `notes_query.py` 的入选口径一致——落选与
    重复条目不在库里承担证据责任，替它们查撤稿是白花网络请求。
    """
    out = []
    for e in (index.get("papers") or []):
        if not isinstance(e, dict):
            continue
        if e.get("duplicate_of") or not e.get("citekey"):
            continue
        out.append(e)
    return out


def check_retractions(index: dict, *, client=None, mailto: str = "",
                      batch_size: int = OPENALEX_BATCH, timeout: float = 25.0,
                      limit: int = 0) -> RetractionScan:
    """全库 DOI 过一遍 OpenAlex 的 `is_retracted`。

    网络失败**不抛出**：这是月度无人值守链路的一环，一次 DNS 抖动不该让整份 lint
    报告生成不出来。失败批次里的 DOI 数计入 `n_failed`，报告里如实写"本轮 N 篇未
    查成"——与"查了、没问题"严格区分开。

    含 `,` 或 `|` 的 DOI 直接计入未解析：这两个字符是 OpenAlex filter 语法里的
    AND/OR 分隔符，塞进去会静默改变查询语义（不是报错，是查了别的东西）。DOI 规范
    允许这些字符，实际极罕见，宁可漏查也不要查错。
    """
    scan = RetractionScan()
    targets = _scan_targets(index)
    if limit > 0:
        targets = targets[:limit]
    scan.n_papers = len(targets)

    by_doi: Dict[str, List[dict]] = {}
    for e in targets:
        doi = normalize_doi(e.get("doi"))
        if not doi:
            scan.n_no_doi += 1
            continue
        if "," in doi or "|" in doi:
            scan.n_no_doi += 1
            scan.errors.append("citekey {} 的 DOI 含 filter 语法保留字符，跳过：{}".format(
                e.get("citekey"), doi))
            continue
        by_doi.setdefault(doi, []).append(e)
    scan.n_with_doi = sum(len(v) for v in by_doi.values())
    if not by_doi:
        return scan

    from .fulltext import ipv4_client
    own = client is None
    c = client or ipv4_client(timeout=timeout)
    dois = sorted(by_doi)
    try:
        for start in range(0, len(dois), max(1, batch_size)):
            chunk = dois[start:start + max(1, batch_size)]
            params = {
                "filter": "doi:" + "|".join(chunk),
                "select": "id,doi,is_retracted,display_name",
                # per-page 必须 > batch_size：同一个 DOI 在 OpenAlex 里可能对应多条
                # work（实测 arXiv DOI 会同时匹配 preprint 与正式版两条），按 50 取
                # 会在边界上静默截断，把没返回到的条目误算成"查无此条"。
                "per-page": 200,
            }
            if mailto:
                params["mailto"] = mailto
            try:
                resp = c.get(OPENALEX_API, params=params)
                resp.raise_for_status()
                results = (resp.json() or {}).get("results") or []
            except Exception as exc:
                scan.n_failed += sum(len(by_doi[d]) for d in chunk)
                scan.errors.append("OpenAlex 批次失败（{} 个 DOI）：{}: {}".format(
                    len(chunk), type(exc).__name__, str(exc)[:160]))
                logger.warning("撤稿检查批次失败（{} 个 DOI）：{}", len(chunk), exc)
                continue
            got: set = set()
            for w in results:
                if not isinstance(w, dict):
                    continue
                doi = normalize_doi(w.get("doi"))
                entries = by_doi.get(doi)
                if not entries:
                    continue
                got.add(doi)
                title = str(w.get("display_name") or "")
                flagged = bool(w.get("is_retracted"))
                marked = bool(_RETRACTED_TITLE_RE.match(title))
                if not (flagged or marked):
                    continue
                for e in entries:
                    scan.hits.append(RetractionHit(
                        acknowledged=is_retracted(e),
                        citekey=str(e.get("citekey")), doi=doi,
                        title=str(e.get("title") or title),
                        signal="openalex-flag" if flagged else "title-marker",
                        openalex_id=str(w.get("id") or "")))
            scan.n_resolved += sum(len(by_doi[d]) for d in got)
            scan.n_unresolved += sum(len(by_doi[d]) for d in chunk if d not in got)
    finally:
        if own:
            try:
                c.close()
            except Exception:
                pass
    # 同一 citekey 可能被两条 OpenAlex work 各命中一次（同 DOI 多 work），去重保序
    seen: set = set()
    deduped: List[RetractionHit] = []
    for h in scan.hits:
        if h.citekey in seen:
            continue
        seen.add(h.citekey)
        deduped.append(h)
    scan.hits = deduped
    return scan


# ---------------------------------------------------------------------------
# 4. 陈旧论断（纯计算）
# ---------------------------------------------------------------------------

# 概念页生成块里的论断行：`- 正文 [@k1] [@k2]`，以及分歧区的
# `- **一方**：正文 [@k]`。要排除的是证据表那种 `- ● **E1** \`[@key]\` …` 行。
_EVIDENCE_LINE_RE = re.compile(r"^- [●○] \*\*E\d+\*\*")
_HEADING_RE = re.compile(r"^#{2,3} (.+)$")
# 证据表小节的标题（`## 本页证据（60 条 · 21 篇）`）——从这里往下不再有论断。
_EVIDENCE_SECTION_RE = re.compile(r"^## 本页证据")

DEFAULT_STALE_YEARS = 5
# 陈旧那一节最多逐条列几篇锚文献（<=0 = 不限）。B3：这一节的量是**日历驱动**的——
# 判据是 `now_year - 5`，所以 2027-01-01 那天没有任何人做错任何事，它自己就从
# 39 条/24 篇长到 62 条/38 篇（实测把年份往前推：2028 年 116 条/61 篇、2031 年
# 353 条/202 篇，该节渲染行数 195 → 306 → 537 → 1671）。孤儿那节一开始就有
# `--orphan-limit`，这节没有，于是它是三节里唯一会无声无息长到上千行的。
# 取 20：真实分布下前 9 篇就覆盖 62% 的论断，20 已经远超"一个下午能做完"的量。
DEFAULT_STALE_ANCHOR_LIMIT = 20


@dataclass
class PageClaim:
    slug: str
    heading: str
    text: str
    citekeys: List[str]
    line: int          # 文件内 1-based 行号，方便直接跳过去看


@dataclass
class StaleClaim:
    claim: PageClaim
    newest_year: int
    years: Dict[str, Optional[int]] = field(default_factory=dict)

    @property
    def sid(self) -> str:
        """跨运行稳定的论断 ID（L3），语义与 `CandidatePair.pid` 一致。

        `sha1(slug + 归一化论断文本)[:8]`。三点故意的取舍：
        - **带 slug**：同一句话出现在两页上是两条独立的待办（"这一页要不要换证据"
          是按页问的），换页要重问；
        - **不带行号**：上下几行插进一条新论断会把行号全推走，ack 不该因此全失效；
        - **文本一变就变**：论断改了就是新论断，该重问（同 pid）。
        """
        import hashlib
        raw = "{}\x1f{}".format(self.claim.slug,
                               re.sub(r"\s+", " ", _norm_text(self.claim.text)))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]

    @property
    def anchor(self) -> str:
        """撑着这条论断、且**最新**的那一篇 citekey（L5 的聚合轴）。

        判据本身就是"最新的一篇也已经 N 年前"，所以要去补新文献的正是这一篇；挂到它
        同时引的 1990 年经典方法论文下会把可执行单位指错人。同年多篇时取 citekey 排序
        的第一个（只求确定性，不求"更对"）。
        """
        same = sorted(k for k, y in self.years.items() if y == self.newest_year)
        if same:
            return same[0]
        return self.claim.citekeys[0] if self.claim.citekeys else ""


def parse_page_claims(text: str, slug: str) -> List[PageClaim]:
    """从一页概念页的**生成块**里抽出全部论断行（纯函数）。

    只扫生成块（`GEN_END` 之前）：「我的批注」区里用户自己写的 `- 某某说法 [@key]`
    是私人笔记，被判成"陈旧论断"是纯噪音——同 `audit_topic_pages` 对裸引用只扫生成块
    的理由（`--verify` 的价值全在于"报警了就一定有事"）。

    到「本页证据」小节为止：证据表每行也带 `[@key]`，但那是原始证据清单不是论断，
    混进来会让每页凭空多出几十条"论断"，陈旧判定随之失去意义。
    """
    end = text.find(GEN_END)
    body = text[:end] if end >= 0 else text
    out: List[PageClaim] = []
    heading = ""
    for i, line in enumerate(body.splitlines(), 1):
        if _EVIDENCE_SECTION_RE.match(line):
            break
        m = _HEADING_RE.match(line)
        if m:
            heading = m.group(1).strip()
            continue
        if not line.startswith("- ") or _EVIDENCE_LINE_RE.match(line):
            continue
        keys = _CITE_RE.findall(line)
        if not keys:
            continue
        claim = _CITE_RE.sub("", line[2:]).strip()
        claim = re.sub(r"\s{2,}", " ", claim).strip()
        if not claim:
            continue
        out.append(PageClaim(slug=slug, heading=heading or "（无小节标题）",
                             text=claim, citekeys=list(dict.fromkeys(keys)), line=i))
    return out


def _year_map(index: dict) -> Dict[str, Optional[int]]:
    out: Dict[str, Optional[int]] = {}
    for e in (index.get("papers") or []):
        if isinstance(e, dict) and e.get("citekey"):
            y = e.get("year")
            out[e["citekey"]] = int(y) if isinstance(y, int) else None
    return out


def find_stale_claims(topics_dir, index: dict, *,
                      max_age_years: int = DEFAULT_STALE_YEARS,
                      now_year: Optional[int] = None) -> List[StaleClaim]:
    """支撑文献**最新的一篇**也已经是 `max_age_years` 年前的论断。

    判据故意取"最新的一篇"而不是"平均"或"最老的一篇"：一条论断只要有任何一条近期
    证据撑着就不算陈旧，哪怕它同时引了 1990 年的原始方法论文——引经典文献是好事，
    不该被报成问题。

    这不是"判它错"，是"提醒人工确认有没有被更新的工作推翻"。报告里必须这么措辞：
    方法学论断（如 Little's test 的前提假设）本来就可能十年不变，把它当缺陷报会让
    整份报告的信噪比崩掉，读者随后会连真信号一起忽略。

    年份查不到的 citekey（索引里没这条 / `year` 为空）**不参与**判定，且只要一条
    论断里有任何一个 citekey 年份未知，整条就不判陈旧——未知不能当"很老"处理，
    否则元数据缺失会被放大成一堆假阳性。

    ## 已知局限（L5，本轮不修）

    判据量的是**论断的形状**（支撑文献有多老），不是**风险**。真正的判据应该是
    「这条论断所在的概念，库里 2022 年后有没有更新的候选证据」——有，才叫"你可能漏了
    新工作"；没有，那是诚实的领域现状，根本不该报。实测 39 条里 25 条纯粹卡在
    `2026-5=2021` 这个阈值上，而它们所在的概念库里未必有更新的东西。
    换用那个判据要给本函数引入**向量库依赖**（现在它是纯计算、无依赖，月度链路里
    最稳的一环），本轮不做。展示侧已按 L5 改成按 citekey 聚合，让可执行单位从
    "39 条论断"变成"24 篇该补的老文献"。
    """
    now_year = now_year or datetime.now().year
    years = _year_map(index)
    out: List[StaleClaim] = []
    # 非递归是**有意的**：`topics/qa/` 里的归档问答不是概念页，不参与陈旧论断分析
    # （它只在人再问一次时才更新，"这条论断的支撑文献有多老"对它没有可执行含义）。
    # 覆盖率那一处是唯一放宽的，见 `_coverage_scan_files`。
    for path in sorted(Path(topics_dir).glob("*.md")):
        if not is_topic_page_file(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # UnicodeDecodeError 是 ValueError 的子类，只捕 OSError 挡不住它。
            # 目录里内容是中文，有人用 GBK 存回来一个文件，就能让 --list /
            # --verify / 查重 / 身份扫描 / INDEX 重建 / lint 覆盖率一起挂——
            # 而这几处的 docstring 都写着「绝不抛异常」。
            continue
        fm, _ = split_frontmatter(text)
        if not isinstance(fm, dict) or not fm.get("topic"):
            continue
        for claim in parse_page_claims(text, str(fm["topic"])):
            ys = {k: years.get(k) for k in claim.citekeys}
            if any(v is None for v in ys.values()):
                continue
            newest = max(v for v in ys.values() if v is not None)
            if now_year - newest >= max_age_years:
                out.append(StaleClaim(claim=claim, newest_year=newest, years=ys))
    out.sort(key=lambda s: (s.newest_year, s.claim.slug, s.claim.line))
    return out


# ---------------------------------------------------------------------------
# 5. 覆盖缺口（纯计算）
# ---------------------------------------------------------------------------

DEFAULT_ORPHAN_LIMIT = 25
# 「最近入库」的窗口（月）。A3：孤儿名单里排前面的清一色是最近两年的论文，那不是
# "概念页漏了一个问题域"的信号，只是它们还没排进任何一页的 top-max_evidence。
DEFAULT_RECENT_MONTHS = 3

# L4：**按前缀匹配，不加 `$`**。全库 199/2343 条的 `month` 是手动/批次桶格式
# （`2026-08-10`、`2026-08-np`、`2026-07-28-TFM`、`2026-08-npjDM-supplement`），
# 首版的 `^(\d{4})-(\d{2})$` 一律解析不出 → None → 全落进 settled 档，于是 8 篇**本月
# 刚入库**的论文被展示成"值得考虑要不要开新页"，A3 想挡的假信号原样保留。
# `(?!\d)` 是必须的：`2026-081` 不是 8 月，是解析不出来的东西，不能当成 8 月。
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})(?!\d)")


def _month_ord(m: Any) -> Optional[int]:
    """`YYYY-MM`（**或以它开头**的批次桶）-> 单调整数（年*12+月）。
    解析不出返回 None（一律当"不是新入库"）。"""
    mm = _MONTH_RE.match(str(m or "").strip())
    if not mm:
        return None
    year, mon = int(mm.group(1)), int(mm.group(2))
    if not (1 <= mon <= 12):
        return None
    return year * 12 + mon


def year_is_implausible(year: Any, now_year: int) -> bool:
    """索引里的 `year` 离谱到不该拿它排序（L4）。**只标不改**——不动索引数据。

    判据是"晚于明年"：预印本/在线优先出版让 `now_year + 1` 完全正常（12 月拿到的
    next-issue 论文标次年），再往后就只能是元数据错误（实测 `kishore2045Quantifying`
    的 2045）。`year` 缺失也归到这一档：它同样不该参与年份排序，且"年份未知"本身
    就是值得顺手补的元数据缺口。
    """
    if not isinstance(year, int) or isinstance(year, bool):
        return True
    return year > (now_year + 1)


def _settled_orphan_key(e: dict, now_year: int) -> Tuple[int, int, str]:
    """settled 孤儿的排序键：可信年份优先且**最老在前**，年份离谱/未知的一律沉底。"""
    y = e.get("year")
    bad = year_is_implausible(y, now_year)
    return (1 if bad else 0, 9999 if bad else int(y), str(e.get("citekey") or ""))


@dataclass
class CoverageReport:
    n_keeper: int = 0
    n_deep_read: int = 0          # 已精读（has_full_text_reading）
    n_cited: int = 0              # 被任意概念页引用过的
    n_deep_read_cited: int = 0
    # 「值得考虑要不要开新页」那一档：入库已经有一段时间、仍然一页都没进过的
    # **A2：这里是完整名单，不截断**。截断交给 `_coverage_section`——它才知道哪些
    # 条目已被 ack，才能做到"ack 掉一批、队列往前走一批"。
    orphans: List[dict] = field(default_factory=list)
    # 「最近 N 个月才入库」那一档：多半只是还没排进任何一页的前 max_evidence 名
    recent_orphans: List[dict] = field(default_factory=list)
    # 每档最多列几篇（<=0 = 不限）。渲染侧在**分完 ack 之后**用它取前 N。
    orphan_limit: int = DEFAULT_ORPHAN_LIMIT
    n_orphans_total: int = 0
    n_orphans_recent: int = 0
    n_orphans_settled: int = 0
    # (slug, 证据条数, 该页配置的 max_evidence)。第三个元素为 None = **没有已知上限**，
    # 有两种截然不同的成因，靠 `retired_pages` 区分（N5）：
    #   - 调用方没给 specs（`specs=None`）——所有页的 cap 都是 None；
    #   - 给了 specs 但这一页已从 `config/topics.yaml` 下线（退役页，`sync_topics_to_vault`
    #     有专门的 `retired: true` 分支，所以它留在 topics/ 是既定行为而非异常）。
    thin_pages: List[Tuple[str, int, Optional[int]]] = field(default_factory=list)
    # 给了 specs、但不在 specs 里的 slug（退役页）。N5：此前它们被写成"未知（调用方没给
    # specs）"——而 specs 明明给了——同时还被算进"已核实全部饱和"的分母。
    retired_pages: List[str] = field(default_factory=list)
    n_pages: int = 0
    recent_months: int = DEFAULT_RECENT_MONTHS
    # L4：判定 `year` 是否离谱（> now_year + 1）用的基准年。渲染侧据此标「元数据可疑」。
    now_year: int = 0


def cited_by_page(topics_dir) -> Dict[str, List[str]]:
    """每页概念页出现过的 citekey：`{slug: [citekey, ...]}`。

    撤稿检查靠它回答那个真正要紧的问题——**这篇被撤的论文正在给哪几页的论断当地基**。
    只报"库里有一篇撤稿"而不说它渗进了哪里，人还是得自己全库 grep 一遍。
    """
    out: Dict[str, List[str]] = {}
    # 非递归是**有意的**（同 `stale_claims`）：返回值的键是概念页 slug，
    # 掺进问答页会让"这篇撤稿正在给哪几页当地基"这句话的主语变成两种东西。
    for path in sorted(Path(topics_dir).glob("*.md")):
        if not is_topic_page_file(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # UnicodeDecodeError 是 ValueError 的子类，只捕 OSError 挡不住它。
            # 目录里内容是中文，有人用 GBK 存回来一个文件，就能让 --list /
            # --verify / 查重 / 身份扫描 / INDEX 重建 / lint 覆盖率一起挂——
            # 而这几处的 docstring 都写着「绝不抛异常」。
            continue
        fm, _ = split_frontmatter(text)
        if not isinstance(fm, dict) or not fm.get("topic"):
            continue
        out[str(fm["topic"])] = sorted(set(_CITE_RE.findall(text)))
    return out


def _coverage_scan_files(topics_dir) -> List[Path]:
    """覆盖率统计要扫的文件：概念页 + **归档问答**（`topics/qa/*.md`）。

    B2：这是四处 `glob("*.md")` 里**唯一**该放宽到 qa 子目录的一处。其余三处
    （`stale_claims` / `cited_by_page` / `coverage_report` 的页面统计）**不放宽**——
    qa 页不是概念页，不进概念页索引、不参与日历兜底、不被概念页审计。
    `is_topic_page_file` 是按**文件名**判的，而 `qa-xxx.md` 不以 `_` 开头，
    一旦那三处也改成 rglob，问答页会被当成概念页参与全部三项。

    这里写成"两次显式 glob"而不是一句 `rglob`：rglob 会顺带扫进 `topics/.obsidian/`
    之类 Obsidian 自己生成的目录，而那不是任何人的产物。

    为什么覆盖率这一处**要**算上：孤儿名单回答的是"哪些论文连概念层的证据池都进不去"。
    一篇被专门问过一次、还进了某页问答证据表的论文显然已经被看见过了，把它列进
    "该考虑开新页"的名单是假信号——而问答越多，假信号越多。
    """
    root = Path(topics_dir)
    return sorted(root.glob("*.md")) + sorted((root / "qa").glob("*.md"))


def cited_citekeys(topics_dir) -> set:
    """全部概念页与归档问答（含证据表）里出现过的 citekey。

    **包含证据表**：一篇论文被召回进了某页的证据池，就说明它已经在概念层被"看见"
    过了，只是这一轮没被论断引用——它不是缺口，是候补。缺口分析要找的是**连证据池
    都进不去**的那批论文（说明现有 8 页的 queries 覆盖不到它们所在的问题域，该考虑
    开新页了），把候补混进去会把真信号淹掉。

    `is_topic_page_file` 那道文件名防线在这里仍是**唯一**防线（本函数纯文本扫
    `[@key]`，不看 frontmatter）：`_lint.md` 自己列出的撤稿/对撞 citekey 不能被算成
    覆盖，`topics/qa/INDEX.md` 同理。
    """
    keys: set = set()
    for path in _coverage_scan_files(topics_dir):
        if not is_topic_page_file(path):
            continue
        try:
            keys.update(_CITE_RE.findall(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            # UnicodeDecodeError 是 ValueError 的子类，只捕 OSError 挡不住它。
            # 目录里内容是中文，有人用 GBK 存回来一个文件，就能让 --list /
            # --verify / 查重 / 身份扫描 / INDEX 重建 / lint 覆盖率一起挂——
            # 而这几处的 docstring 都写着「绝不抛异常」。
            continue
    return keys


def coverage_report(topics_dir, index: dict, *,
                    orphan_limit: int = DEFAULT_ORPHAN_LIMIT,
                    tiers: Sequence[str] = ("high",),
                    specs: Optional[Sequence[Any]] = None,
                    recent_months: int = DEFAULT_RECENT_MONTHS,
                    now_month: Optional[str] = None) -> CoverageReport:
    """概念页层对札记库的覆盖情况，以及最值得补的缺口。

    孤儿只列**高优先级且已精读**的：全库 2000+ 篇里没被概念页碰到的有一千多篇，
    整列出来等于没列。限定到"我自己标了高优先级、还花时间精读过、却没有任何一页
    容得下它"这一小撮，才是真正在说"你的概念页配置漏了一个问题域"。

    A3：孤儿再按**入库月**分两档。实测 208 个孤儿里排前 15 全是 2025/2026 年的论文，
    而排序本身就是按年份倒序——这是分布使然，不是发现。最近 `recent_months` 个月才
    入库的单列一档（"多半只是还没排进任何一页的前 max_evidence 名"），其余那档才是
    "值得考虑要不要开新页"。`month` 解析不出的一律进后一档（"新入库"是个需要证据的
    正面断言，缺证据时不该白送）。

    A2：`specs`（`List[TopicSpec]`，调用方从 `config/topics.yaml` 加载）给了才知道
    每页的 `max_evidence`。不给时 `thin_pages` 第三个元素是 None，报告那边据此**不
    输出**"垫底的几页要么没证据要么 queries 写得不对"那句建议——8 页证据数与配置上限
    一模一样时，那句话是在给一个永远不会出现的场景准备建议。

    ## 已知局限（L9，本轮不修）

    `thin_pages` 真正能给出信号的量是**截断前的候选池大小**，而不是截断后的
    `n_evidence`：「一页有 63 个候选卡在 60」和「有 800 个候选卡在 60」是完全不同的
    两件事，而现在它们在 frontmatter 里长得一模一样（都是 `n_evidence: 60`）。
    补上它要改 `scripts/build_topics.py`，把 `select_evidence` 截断前的池大小写进各页
    frontmatter——**不在本轮白名单里，只记录不实现**。
    """
    rep = CoverageReport(recent_months=recent_months)
    cited = cited_citekeys(topics_dir)
    max_ev = {str(getattr(s, "slug", "")): getattr(s, "max_evidence", None)
              for s in (specs or [])} if specs is not None else None
    want_tiers = set(tiers)
    now_ord = _month_ord(now_month) if now_month else None
    if now_ord is None:
        _n = datetime.now()
        now_ord = _n.year * 12 + _n.month
    cutoff = now_ord - max(0, recent_months) + 1

    orphans: List[dict] = []
    recent: List[dict] = []
    for e in _scan_targets(index):
        rep.n_keeper += 1
        ck = e["citekey"]
        deep = bool(e.get("has_full_text_reading"))
        if deep:
            rep.n_deep_read += 1
        if ck in cited:
            rep.n_cited += 1
            if deep:
                rep.n_deep_read_cited += 1
            continue
        if deep and (not want_tiers or e.get("priority_tier") in want_tiers):
            mo = _month_ord(e.get("month"))
            (recent if (mo is not None and mo >= cutoff) else orphans).append(e)
    rep.n_orphans_total = len(orphans) + len(recent)
    rep.n_orphans_recent = len(recent)
    rep.n_orphans_settled = len(orphans)
    # now_ord = 年*12+月（月份 1..12），所以 12 月那一档要减一才落回本年。
    rep.now_year = (now_ord - 1) // 12
    # L4：settled 档换成**年份正序（最老优先）**。年份倒序在真实分布下保证了最老的那批
    # （`walker2009Evaluation`、`hu2014Dynamic`…）永远排不进列出的前 25——而 184 篇
    # settled 在 2021–2026 上分布相当均匀，"库里最久没人碰的老文献"恰恰更可能是真缺口，
    # 最新的那批更可能只是还没轮到。没有选 `priority_rank`：它是**入库当月**的批内排名，
    # 跨月不可比（同一个 rank=1 在 3 篇的月份和 40 篇的月份完全不是一回事）。
    # 年份离谱/未知的沉到最后：`kishore2045Quantifying`（year=2045）此前稳居榜首。
    orphans.sort(key=lambda e: _settled_orphan_key(e, rep.now_year))
    # recent 档保持年份倒序：这一档的语义就是"最近入库的"，最新在前是对的。
    # B4：但**年份离谱/未知的一样要沉底**。这一档按年份倒序，而 `_orphan_lines` 是
    # 不分档地给可疑条目追加"本节排序已把它沉底"那句话的——首版只给 settled 档加了
    # 判据，于是真实报告里 `kishore2045Quantifying`（year=2045）成了「最近 3 个月新
    # 入库」那一档的**第 1 行**，紧跟着一句"已把它沉底"。报告两处互相矛盾、而排在最
    # 前面那处是错的，正是 N1 花大力气修掉的那一类。
    recent.sort(key=lambda e: (1 if year_is_implausible(e.get("year"), rep.now_year) else 0,
                               -(e.get("year") or 0), str(e.get("citekey"))))
    # A2：**完整名单交给渲染侧**，`orphan_limit` 不在这里生效。首版在这里先截断、
    # `_split_acked` 在渲染侧才分 ack ——截断在前、分 ack 在后，于是 208 篇孤儿里
    # 你永远只能看见同样那 25 篇：认真处理完列出的 25 篇再渲染，那个小标题**整个消失**，
    # 后面 143 篇一篇没顶上来，也没有任何一行告诉用户队列里还有多少。
    # ack 让条目"不占版面"做到了，让待办"往前走"没做到——这一半在这里补上。
    rep.orphans = orphans
    rep.recent_orphans = recent
    rep.orphan_limit = orphan_limit

    # B5：`n_pages` 与 `thin_pages`/`cited_by_page` 用同一套判据（frontmatter 有
    # `topic` 键）。此前 n_pages 只按文件名计数，往 topics_dir 丢一个没有 frontmatter
    # 的杂散 .md 就会让报告写"概念页 2 页覆盖了……"而下面表格只有 1 行。
    thin: List[Tuple[str, int, Optional[int]]] = []
    retired: List[str] = []
    # 非递归是**有意的**（同上两处）：`n_pages` 与 `thin_pages` 说的是"概念页有几页、
    # 哪几页太薄"。问答页混进来会让页数虚涨，还会因为不在 topics.yaml 里而被
    # 一律报成"已退役的概念页"。
    for path in sorted(Path(topics_dir).glob("*.md")):
        if not is_topic_page_file(path):
            continue
        try:
            fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            # UnicodeDecodeError 是 ValueError 的子类，只捕 OSError 挡不住它。
            # 目录里内容是中文，有人用 GBK 存回来一个文件，就能让 --list /
            # --verify / 查重 / 身份扫描 / INDEX 重建 / lint 覆盖率一起挂——
            # 而这几处的 docstring 都写着「绝不抛异常」。
            continue
        if not isinstance(fm, dict) or not fm.get("topic"):
            continue
        slug = str(fm["topic"])
        n = fm.get("n_evidence")
        # N5：用 `in` 而不是 `.get()`。给了 specs 但这一页不在里面 = **已从 topics.yaml
        # 下线的退役页**（留在 topics/ 是既定行为），不是"调用方没给 specs"。首版把这两
        # 件事都写成后者，报告因此对着一个明明传了 specs 的运行说"你没给 specs"。
        cap = None
        if max_ev is not None:
            if slug in max_ev:
                cap = max_ev[slug]
            else:
                retired.append(slug)
        thin.append((slug, int(n) if isinstance(n, int) else 0,
                     int(cap) if isinstance(cap, int) else None))
    thin.sort(key=lambda t: (t[1], t[0]))
    rep.thin_pages = thin
    rep.retired_pages = sorted(retired)
    rep.n_pages = len(thin)
    return rep


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
