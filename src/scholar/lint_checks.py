# -*- coding: utf-8 -*-
"""知识层 lint：四项检查与其数据类（自 lint.py 拆出，阶段 4b——叶模块，全链可依赖）。
外部一律经 `src.scholar.lint` 门面 import，不直连本模块。
"""
import json
import math
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib import error, parse, request

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


