# -*- coding: utf-8 -*-
"""概念页层：把 paper-centric 的札记库编译出一个 concept-centric 的轴。

札记库原本只回答「每篇论文说了什么」，一篇写成之后不再改。这个模块回答另一个
问题：**关于某个概念，全库现在的共识、分歧与缺口是什么**——并且随新论文入库而
重写。产物是 output/scholar_notes/topics/<slug>.md，每页一个概念。

## 防幻觉：证据编号回译

综述是最容易滋生编造的体裁（"多项研究表明……"后面挂一个不存在的 citekey，读起来
毫无破绽）。这里的对策不是事后检测，而是让编造在**物理上不可能**：

  1. 召回阶段给每条句级证据编号 E1..En，连同 citekey 一起留在程序侧；
  2. 喂给 LLM 的证据块只带编号，prompt 明令「引用只写编号，不许写 citekey」；
  3. LLM 回来的 evidence 是编号列表，由 `validate_synthesis` 回译成 citekey——
     越界编号（E99 而召回只有 70 条）直接剔除，剔空的整条论断丢弃；
  4. 论断正文里若仍出现模型自己写的引用标记，一律剥掉并计数——这是唯一可能
     编造 citekey 的通道，因为它没经过编号校验。

     2026-08-17 前，第 4 步只挡了带方括号的 `[@xxx]` 一种写法。而 pandoc-citeproc
     同样认不带方括号的裸 `@xxx`（作者年份型行文引用，如"如 @smith2020 所述"，是
     合法引用语法），实测模型确实会用这种写法漏网：逃过剥离、逃过 `audit_topic_pages`
     的死键检测、也逃过 `to_wiki_links`，`--verify` 照样全绿。现在的做法不是逐一模拟
     pandoc 语法，而是结构性收紧：中文综述正文本来就不需要出现 `@` 字符，所以校验后
     的文本里不许再有任何 `@`（唯一例外是邮箱形式 `a@b.com`，靠"@ 前是否紧邻字母数字/
     点/连字符"判断，见 `_BARE_CITE_ONLY_RE`）。`audit_topic_pages` 也会扫描已生成页面
     里残留的裸引用（`bare_cites`），兜住这道防线补上之前生成的旧页。

于是页面上的每个 citekey 都来自召回集合，必然对应库里真实存在的那一句。

## 分层

纯函数（不碰网络/磁盘，可直接单测）：
  load_topic_specs / select_evidence / build_prompt / parse_synthesis /
  validate_synthesis / render_topic_md / merge_topic_page
有 IO：
  retrieve_evidence（向量库检索）/ synthesize_topic（LLM）/ build_topic
"""
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..utils.logger import get_logger
from .notes_index import write_if_changed
from .vault import (
    ROLE_LABEL, extract_user_zone, generated_block_tampered, split_frontmatter,
    _gen_hash, GEN_END, _SAFE_KEY, _YAML_WORDS, _y,
)

logger = get_logger(__name__)

TOPIC_SCHEMA_VERSION = 1

TOPICS_DIRNAME = "topics"           # notes_dir 下的概念页目录
DEFAULT_CONFIG = "config/topics.yaml"
DEFAULT_PROMPT = "config/prompts/topic_synthesis_prompt.md"

DEFAULT_MAX_EVIDENCE = 60
# 单篇论文最多贡献几条证据。没有这个配额，一篇 highlight 特别多的综述型文献能占满
# 整个召回集，页面读起来像它的读书笔记——概念页的价值恰恰在于横跨多篇。
DEFAULT_PER_PAPER_CAP = 4
# 召回相似度下限。**不要照抄 notes_search 的 0.65**——那是 paper 级检索（标题+一句话，
# 语义密度高）的口径，套到句级证据上会把真命中滤掉：实测 query "shadow variable MNAR
# identification" 的最佳命中（chen2026Partial「影子变量区间中点平均绝对误差 0.06」）
# 只有 0.593 分，0.65 下整页召回 0 条。
#
# 2026-08-16 在本库 17775 条 highlight 上分段抽查的结果：
#   ≥0.55       全部题内，可直接当证据
#   0.50~0.55   一半题内一半漂移（"结局识别算法未做诊断性能评估"这类挂到因果泛化查询上）
#   <0.50       基本是词面巧合
# 故取 0.55。冷门概念（如影子变量）只召回十来条是**诚实的信号**而非故障——页面会把
# 证据数写在标题栏，缺口一节也会点明证据不足，这正是概念页该暴露的库覆盖缺口。
DEFAULT_MIN_SIM = 0.55

# bucket 命中给 select_evidence 排序加的分（只影响排序，不影响 min_sim 判定，见
# retrieve_evidence）。2026-08-16 之前 bucket 是硬过滤，审计发现全库 2216 篇 keeper 里
# 816 篇（37%）bucket 为空——其中 manual 系列 220 篇 100% 全空（pdf_ingest.py 从没写过
# 这个字段），恰恰是标注最细、带页码、经交叉核验的最高质量证据，硬过滤把它们从 7/8 个
# 概念页里整体排除（icu-benchmarks.md 因此写出"AmsterdamUMCdb/HiRID 全库未出现"的假结论，
# 实际库里有 15 篇提及、只是 bucket 全空）。改软加权后取 +0.03：小于 DEFAULT_MIN_SIM 自己
# 标出的档位间隔（0.50~0.55 半题内半漂移算一档，档宽 0.05），够让同档里标了偏好维度的
# 证据排到前面，但不足以让加成后的分数跨档反超一条明显更相关的证据。
DEFAULT_BUCKET_BONUS = 0.03

# per-query 相对判据系数：eff_min = max(min_sim, alpha × 本次 query 的 top1 分)。
# 全局绝对阈值与查询强度耦合（2026-08-21 标定，config/topics.yaml 全部 36 条真实 query
# 实测，表存 docs/decisions/topics_threshold_calibration_2026-08.md）：强 query
# （top1≥0.70，如 "informative missingness"）在 0.55 下 top-200 全数过线，边缘尾巴
# 靠 per_query_k 截断而不是靠阈值；冷门 query（top1<0.62，如 "shadow variable MNAR"）
# 只召回个位数是诚实的库覆盖信号，不该再收紧。α=0.85 恰好只对 top1≥0.647 的强 query
# 生效（0.85×0.647≈0.55），对冷门 query 完全退回 floor：
#   α=0.80 几乎不生效（36 条里仅 4 条门槛动了）；α=0.90 把强 query 砍到只剩 3-7 条，
#   0.60~0.65 段的真命中被误杀。传 0 可彻底关闭相对判据（退回纯绝对阈值）。
DEFAULT_RELATIVE_ALPHA = 0.85

# 默认排除的精读分节：这不是文献内容，是精读者自己的推测性批注，混进证据池会被综述
# 当成"已验证结论"引用（2026-08-16 审计实测：missingness-causal.md 把「对我研究的联想」
# 里"可为跨中心MNAR缺失图学习提供隐私保障"这句主观联想写成了论文证明过的能力）。
# 可按页在 topics.yaml 显式覆盖（填 [] 即不排除任何 section）。
DEFAULT_EXCLUDE_SECTIONS = ["对我研究的联想"]

GEN_BEGIN = ("<!-- BEGIN GENERATED v{} h={} · 由 {} 生成 · "
             "此块会被重建覆盖，请写在下面的「我的批注」里 -->")
# GEN_END 直接从 vault 导入（不在这里重新声明字面量）：此前两边各自维护一份逐字符相同
# 的字符串，纯靠人工保持一致、没有测试强制——任何一边单改措辞，extract_user_zone/
# generated_block_tampered（本模块从 vault 导入的那两个函数，内部认的是 vault 自己那份
# 常量）就会在另一边生成的页面上找不到哨兵，返回 None，导致每次重建都误判成 conflict
# （G6）。GEN_BEGIN 因文案里写了各自的生成脚本名，继续独立声明，但格式必须仍能被
# vault.GEN_BEGIN_RE 认出，见 test_topics.py 对应回归测试。
USER_HEADING = "## 我的批注"
DEFAULT_USER_ZONE = "\n{}\n\n".format(USER_HEADING)

ROLE_EMOJI = {"citable": "🟪", "refutable": "🟦", "method": "🟩"}

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_REF_RE = re.compile(r"^[Ee](\d+)$")

# 论断正文里模型自己写的引用标记：pandoc-citeproc 会把它当真引用去挂书目，是唯一能
# 绕开编号校验的编造通道。两种合法 pandoc 引用语法都要挡：
#   - 带方括号 `[@key]`（含 `[@k1; @k2]` 紧凑多键写法，`[^\]]+` 本就会整段吃掉）；
#   - 不带方括号的裸 `@key`（作者年份型行文引用，如"如 @smith2020 所述"）——G1 实测：
#     此前只挡了方括号形式，模型确实会用裸写法漏网，逃过剥离/死键检测/wiki 链接转换
#     三道防线，`--verify` 照样全绿。
# _BRACKET_CITE_RE 单独留一份（而不是只留组合后的 _BARE_CITE_RE）：audit_topic_pages
# 扫描已生成页面里的"残留裸引用"时，要先挖掉合法的方括号引用，剩下的才是真正裸引用——
# 否则 `[@k1; @k2]` 这种紧凑写法里的第二个 `@k2` 会被误判成一条裸引用（它明明在合法
# 方括号内部）。
_BRACKET_CITE_RE = re.compile(r"\[@[^\]]+\]")
# 裸 @key。两侧都必须按 **ASCII** 界定，两次踩坑都出在这里（2026-08-17 实测）：
#
# 后顾断言排除邮箱形式 a@b.com（@ 前紧邻 ASCII 字母/数字/下划线/./- 判为邮箱局部，
# 不剥离——没理由误伤用户批注区里真写的联系方式）。**不能写成 `(?<![\w.\-])`**：
# Python 3 的 `\w` 按 Unicode 匹配、汉字也算单词字符（`re.match(r'\w','究')` 为真），
# 于是「研究表明@sneaky2020Ghost能降低误差」这种中文行文里最自然的紧贴写法会被判成
# 邮箱局部整条放行，audit 的 bare_cites 一并看不见、`--verify` 全绿——比没有防线更
# 危险，它制造错误的安全感。
#
# 匹配体同理限定在 citekey 的合法字符集（ASCII 字母数字与 `_.-:`）内，**不能写成
# "排除空白与中文标点"的宽字符类**：citekey 不可能包含汉字，宽字符类会一路吃到句末
# 标点，把 `@key` 后面的正文（"能降低误差"）连带剥掉，页面上留下半句话。
_BARE_CITE_ONLY_RE = re.compile(r"(?<![A-Za-z0-9_.\-])@[A-Za-z0-9][A-Za-z0-9_.\-:]*")
_BARE_CITE_RE = re.compile(_BRACKET_CITE_RE.pattern + "|" + _BARE_CITE_ONLY_RE.pattern)


class TopicError(RuntimeError):
    """配置非法 / prompt 缺失 / LLM 输出无法解析。CLI 据此退出码 2。"""


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class TopicSpec:
    slug: str
    title: str
    question: str
    queries: List[str]
    # 偏好维度，非硬过滤（2026-08-16 起，见 DEFAULT_BUCKET_BONUS 的依据注释）：命中的证据
    # 排序时加分靠前，bucket 为空或不匹配都不惩罚——退化为纯语义排序，不会把证据挡在门外。
    buckets: List[str] = field(default_factory=list)
    roles: List[str] = field(default_factory=list)
    max_evidence: int = DEFAULT_MAX_EVIDENCE
    # 硬过滤，默认值见 DEFAULT_EXCLUDE_SECTIONS（排除精读者的主观批注类分节）。
    # 这与 buckets 不同：那是"值不值得优先"，这是"算不算数"，所以仍然是硬过滤而非加权。
    exclude_sections: List[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_SECTIONS))


def load_topic_specs(config_path) -> List[TopicSpec]:
    """config/topics.yaml -> TopicSpec 列表。

    配置错误一律抛 TopicError 而不是跳过：概念页是人工策展的产物，静默少生成一页
    比报错更糟——用户会以为那个概念库里真的没证据。
    """
    import yaml
    p = Path(config_path)
    if not p.exists():
        raise TopicError("概念页配置不存在：{}".format(p))
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:
        raise TopicError("概念页配置解析失败（{}）：{}".format(p, e))
    raw = data.get("topics")
    if not isinstance(raw, list) or not raw:
        raise TopicError("概念页配置里没有 topics 列表：{}".format(p))

    specs: List[TopicSpec] = []
    seen: Dict[str, int] = {}
    for i, item in enumerate(raw):
        where = "topics[{}]".format(i)
        if not isinstance(item, dict):
            raise TopicError("{} 不是映射".format(where))
        slug = str(item.get("slug") or "").strip()
        if not _SLUG_RE.match(slug):
            # slug 直接做文件名与 wiki 链接，非 ASCII/带空格会在 Obsidian 与 shell 两侧各炸一次
            raise TopicError("{} 的 slug '{}' 非法（须 kebab-case 小写 ASCII）".format(where, slug))
        if slug in seen:
            raise TopicError("slug '{}' 重复（{} 与 topics[{}]）——后者会覆盖前者的页面".format(
                slug, where, seen[slug]))
        seen[slug] = i
        title = str(item.get("title") or "").strip()
        question = str(item.get("question") or "").strip()
        if not title or not question:
            raise TopicError("{}（{}）缺 title 或 question".format(where, slug))
        queries = [str(q).strip() for q in (item.get("queries") or []) if str(q).strip()]
        if not queries:
            raise TopicError("{}（{}）没有 queries，无法召回证据".format(where, slug))
        roles = [str(r).strip() for r in (item.get("roles") or []) if str(r).strip()]
        bad_roles = [r for r in roles if r not in ROLE_LABEL]
        if bad_roles:
            raise TopicError("{}（{}）的 roles 含非法值 {}（合法：{}）".format(
                where, slug, bad_roles, sorted(ROLE_LABEL)))
        try:
            max_ev = int(item.get("max_evidence") or DEFAULT_MAX_EVIDENCE)
        except (TypeError, ValueError):
            raise TopicError("{}（{}）的 max_evidence 不是整数".format(where, slug))
        if max_ev <= 0:
            raise TopicError("{}（{}）的 max_evidence 必须为正".format(where, slug))
        # 缺省 = DEFAULT_EXCLUDE_SECTIONS；显式写了这个键（哪怕是空列表）就按用户写的来——
        # 允许个别页主动要「对我研究的联想」这类联想式内容，而不是全局一刀切。
        if "exclude_sections" in item:
            exclude_sections = [str(s).strip() for s in (item.get("exclude_sections") or [])
                                if str(s).strip()]
        else:
            exclude_sections = list(DEFAULT_EXCLUDE_SECTIONS)
        specs.append(TopicSpec(
            slug=slug, title=title, question=question, queries=queries,
            buckets=[str(b).strip() for b in (item.get("buckets") or []) if str(b).strip()],
            roles=roles, max_evidence=max_ev, exclude_sections=exclude_sections,
        ))
    return specs


# ---------------------------------------------------------------------------
# 证据召回
# ---------------------------------------------------------------------------

@dataclass
class Evidence:
    """一条候选证据：向量库里的一个 highlight chunk + 它的出处。"""
    ref: str                      # "E1"（select_evidence 定稿后才赋值）
    citekey: str
    text: str
    role: Optional[str]
    section: Optional[str]
    note_file: Optional[str]
    note_line: Optional[int]
    score: float
    year: Optional[int] = None
    tier: Optional[str] = None
    title: Optional[str] = None
    # bucket 命中的加分（DEFAULT_BUCKET_BONUS），只用于 select_evidence 内部排序。
    # score 字段本身永远是原始余弦相似度——展示、以及任何要跟 min_sim 比较的地方，
    # 都不该被这个加成污染。
    bucket_boost: float = 0.0


def select_evidence(candidates: Sequence[Evidence], max_evidence: int,
                    per_paper_cap: int = DEFAULT_PER_PAPER_CAP) -> List[Evidence]:
    """按分数降序取证，同篇论文超过 per_paper_cap 条的丢弃，最后重编 E1..En。

    排序键是 score + bucket_boost（软加权，见 DEFAULT_BUCKET_BONUS）；重编号后的
    Evidence.score 仍是原始余弦，加成只体现在名次上，不回写进 score——否则展示的
    "相似度"会失真，日后想核对 min_sim 判定是否合理也没了可比的原始值。

    配额是"轮次制"而不是"先到先得"：先给每篇取第 1 条，再给每篇取第 2 条……
    这样即使某篇论文霸占了分数榜前 20 名，其它论文仍能进入前排。直接按分数截断
    会让一篇 highlight 密集的综述吃掉整页。
    """
    if max_evidence <= 0:
        return []

    def _sort_key(e: Evidence) -> float:
        return e.score + e.bucket_boost

    by_paper: Dict[str, List[Evidence]] = {}
    for c in sorted(candidates, key=lambda e: -_sort_key(e)):
        by_paper.setdefault(c.citekey, []).append(c)

    # 论文之间按其最高分排序，保证轮次内也是强证据优先
    papers = sorted(by_paper, key=lambda k: -_sort_key(by_paper[k][0]))
    picked: List[Evidence] = []
    for rank in range(max(1, per_paper_cap)):
        for ck in papers:
            group = by_paper[ck]
            if rank < len(group):
                picked.append(group[rank])
        if len(picked) >= max_evidence:
            break
    picked.sort(key=lambda e: -_sort_key(e))
    picked = picked[:max_evidence]
    out: List[Evidence] = []
    for i, e in enumerate(picked, 1):
        out.append(Evidence(
            ref="E{}".format(i), citekey=e.citekey, text=e.text, role=e.role,
            section=e.section, note_file=e.note_file, note_line=e.note_line,
            score=e.score, year=e.year, tier=e.tier, title=e.title,
            bucket_boost=e.bucket_boost,
        ))
    return out


def retrieve_evidence(spec: TopicSpec, store, embed_client, *,
                      min_sim: float = DEFAULT_MIN_SIM,
                      per_query_k: int = 80,
                      per_paper_cap: int = DEFAULT_PER_PAPER_CAP,
                      bucket_bonus: float = DEFAULT_BUCKET_BONUS,
                      relative_alpha: float = DEFAULT_RELATIVE_ALPHA) -> List[Evidence]:
    """多 query 向量检索 -> 合并去重 -> select_evidence 定稿。

    每个 query 各检索一次而不是把 queries 拼成一句：拼接会把几个概念平均成一个
    语义中点，谁也召不准（"MNAR 诊断" + "Little's test" 拼起来两头不靠）。
    同一 chunk 被多个 query 命中时取最高分。

    mask（硬过滤）只按 level / roles / exclude_sections 这几个语义明确的条件筛：
    roles 是证据本身的性质分类，exclude_sections 排除的是不算数的主观批注，两者都是
    "算不算数"的问题。buckets 不是——它是"值不值得优先"，2026-08-16 前曾经也硬过滤，
    审计发现这会把大量未打 bucket 标签的高质量证据（尤其 manual 系列）整批排除在外，
    于是改成 select_evidence 排序阶段的软加权（见 DEFAULT_BUCKET_BONUS）：命中的证据
    排序靠前，没打标签或标签不匹配的都不受罚，min_sim 门槛判定用的仍是加成前的原始分。
    """
    import numpy as np

    # highlight 级才有句子可引；paper 级 chunk 是标题+一句话，不能当证据
    role_filter = set(spec.roles) if spec.roles else None
    bucket_pref = set(spec.buckets) if spec.buckets else None
    # 前缀匹配而非精确相等：同一类分节在库里存在带括注的变体，实测有三种写法——
    # 「对我研究的联想」2179 条、「对我研究的联想（研究者自身引申，非论文原创结论）」3 条、
    # 「对我研究的联想(MNAR / MA-GCT / 缺失机制 / 生存分析)」1 条。精确匹配只挡住第一种，
    # 漏进来的变体照样是主观批注（2026-08-17 实测：mnar-diagnosis 页的 E53 就是这样混进去
    # 并被引用的），而它们恰恰因为写得更详细而更像结论。
    exclude_prefixes = tuple(spec.exclude_sections) if spec.exclude_sections else ()
    mask = np.zeros(len(store.records), dtype=bool)
    for i, r in enumerate(store.records):
        if r.get("level") != "highlight":
            continue
        if role_filter and r.get("role") not in role_filter:
            continue
        if exclude_prefixes and str(r.get("section") or "").startswith(exclude_prefixes):
            continue
        mask[i] = True
    if not mask.any():
        logger.warning("topic '{}' 的过滤条件（roles={} exclude_sections={}）没有匹配到任何句级证据",
                       spec.slug, spec.roles or "全部", spec.exclude_sections or "无")
        return []

    best: Dict[str, Evidence] = {}
    for q in spec.queries:
        vec = embed_client.embed([q])[0]
        hits = store.search(vec, mask=mask, top_k=per_query_k)
        # per-query 相对判据：min_sim 是 floor（语义不变），强 query 按自身 top1 抬门槛，
        # 冷门 query 退回 floor（见 DEFAULT_RELATIVE_ALPHA 注释与标定表）。
        top1 = hits[0][1] if hits else 0.0
        eff_min = max(min_sim, relative_alpha * top1)
        for idx, score in hits:
            if score < eff_min:
                continue
            r = store.records[idx]
            cid = r["id"]
            prev = best.get(cid)
            if prev is not None and prev.score >= score:
                continue
            boost = bucket_bonus if (bucket_pref and (set(r.get("bucket") or []) & bucket_pref)) else 0.0
            # 换行在这里（Evidence 的唯一构造点）统一归一化，而不是留给各个渲染/比对处
            # 各自处理：render_generated_block 渲染证据表时已经做过一次 .replace("\n"," ")，
            # 但 render_evidence_block（喂给 LLM 的证据块）与 audit_topic_pages 的 hl_by_key
            # （失锚比对基准）此前都没做——一旦某条 highlight 真带了内嵌换行（notes_index.py
            # 记录过"多行标题"这类邮件解析产物历史上真实出现过），页面上已折叠成单行的引文
            # 与这里原始带换行的字符串就永远逐字对不上，会把完全正常的证据误报成"失锚"（G8）。
            text = (r["text"] or "").strip().replace("\n", " ")
            best[cid] = Evidence(
                ref="", citekey=r["citekey"], text=text, role=r.get("role"),
                section=r.get("section"), note_file=r.get("note_file"),
                note_line=r.get("note_line"), score=float(score), bucket_boost=boost,
                year=r.get("year"), tier=r.get("tier"),
            )
    return select_evidence(list(best.values()), spec.max_evidence, per_paper_cap)


def new_citekeys_from_note(index: dict, note_file: str) -> set:
    """某份札记文件带进来的 citekey 集合（= 这一批新入库的论文）。

    入库流水线不需要为此改造：`run_ingest` 返回新写的 md 路径，索引里 `note_file`
    等于它的 keeper 条目就是本批新增。跨月去重后的重复条目（duplicate_of 非空）
    不算——它们的证据在别的月份已经进过概念页了。
    """
    name = Path(note_file).name
    return {p["citekey"] for p in (index.get("papers") or [])
            if isinstance(p, dict) and p.get("citekey")
            and not p.get("duplicate_of") and p.get("note_file") == name}


def new_citekeys_from_notes(index: dict, note_files: Sequence[str]) -> set:
    """`new_citekeys_from_note` 的多文件版本：并集，不是列表拼接（用得着去重）。

    W7：月度回填一次跑多个月份时，此前收尾对每个月份的札记各起一次
    `build_topics.py --affected-by-note` 子进程——连续几个月命中同一页会把它反复
    合成 N 次（只有最后一次有效），`--since 2023-01 --until 2026-05` 的 41 个月
    实测因此撞了 Claude 订阅限流。改法是先把全部月份的新 citekey 收集成一个并集，
    整次回填只路由 + 合成一次；这个函数就是"收集"那一步。
    """
    out: set = set()
    for note_file in note_files:
        out |= new_citekeys_from_note(index, note_file)
    return out


def affected_topics(specs: Sequence[TopicSpec], store, embed_client,
                    new_citekeys: Sequence[str], *,
                    min_sim: float = DEFAULT_MIN_SIM,
                    per_paper_cap: int = DEFAULT_PER_PAPER_CAP,
                    bucket_bonus: float = DEFAULT_BUCKET_BONUS
                    ) -> List[Tuple[str, List[str]]]:
    """新入库的论文影响了哪些概念页。返回 [(slug, 命中的新 citekey 列表)]。

    判定方式是**直接重跑召回，看新论文有没有真的挤进证据集**，而不是拿新论文的
    highlight 与 topic 的 queries 单独算个相似度。理由：一篇论文与某概念"有点相关"
    不等于它能进入那一页——`max_evidence` 与 `per_paper_cap` 截断之后，排在 70 名
    开外的证据对页面内容毫无影响，为它重跑一次 LLM 合成纯属浪费。召回本身不花钱
    （只查向量库），拿它当判据最准。

    这是 P2「living document」的路由器：周度/月度入库后据此只重合成真受影响的页，
    而不是每次把 8 页全部重跑一遍（一轮全量要 20+ 分钟且会撞订阅限流）。
    """
    want = {k for k in new_citekeys if k}
    if not want:
        return []
    out: List[Tuple[str, List[str]]] = []
    for spec in specs:
        try:
            evs = retrieve_evidence(spec, store, embed_client, min_sim=min_sim,
                                    per_paper_cap=per_paper_cap, bucket_bonus=bucket_bonus)
        except Exception as e:
            # 单页召回失败不该让整个路由判断失败：把它当"受影响"处理，交给后续重合成
            # 时再报错——漏判（该更新的没更新）比误判（多跑一页）更难被发现。
            logger.warning("topic '{}' 召回失败，保守视为受影响：{}", spec.slug, e)
            out.append((spec.slug, []))
            continue
        hit = sorted({e.citekey for e in evs} & want)
        if hit:
            out.append((spec.slug, hit))
    return out


# 阈值语境不是"向量库多旧还能将就用"（那是 embed_store.STALE_DEGRADE_SECONDS=24h，
# 检索类只读场景的容忍度），而是"这批新论文的 embedding 有没有来得及写进库"——P2 路由
# 紧跟在 ingest/backfill 自己的向量同步之后跑，正常情况下两边读到的是同一次 index
# 写盘产生的 generated_at，lag 应为 0；一旦出现正数 lag，几乎总意味着 W2 实测过的
# 情形：`sync_store` 在 ingest/backfill 里失败被吞掉（best-effort 设计使然），或独立的
# WatchPaths embed job 抢跑——路由这一刻看到的向量库压根不含本批论文的 embedding。
ROUTING_STALE_SECONDS = 300


def routing_is_stale(store, index_generated_at: Optional[str]) -> bool:
    """P2 路由（`affected_topics`）能不能信"召回不到 = 不影响任何页"？

    两步判断的口径与 workflow.py 对同一类"向量库落后要不要直接降级"的既有处理一致：
    先用 `freshness_warning` 判定"旧不旧"（`source_generated_at` 存在且早于
    `index_generated_at`），再用 `freshness_lag_seconds` 量出"旧多少"。lag 解析不出
    （时间戳格式异常）时从严当超阈值处理——"已经判定旧但量不出差多少"不能被当成
    "那就还是信它吧"。

    G2：`sync_store` 在 ingest 里失败被吞掉后，向量库永远不含新批论文的 embedding，
    `affected_topics` 召回不到 -> 路由判定"无需重新合成" -> 退出码 0 -> 新论文的证据
    永久错过任何概念页，且没有任何机制回头重跑漏判的批次。调用方须在 hits 为空时，
    先确认本函数返回 False，才能相信"确实不影响任何页"而不是"看不见"。
    """
    warn = store.freshness_warning(index_generated_at)
    if not warn:
        return False
    lag = store.freshness_lag_seconds(index_generated_at)
    return lag is None or lag > ROUTING_STALE_SECONDS


def is_topic_page_file(path) -> bool:
    """topics/ 下这个文件是不是一页概念页（**按文件名判，不读内容**）。

    这个目录下并不是只有概念页：
      - `INDEX.md` 是目录页（自己就是从各页 frontmatter 汇总出来的）；
      - `_lint.md` 是知识层 lint 报告（`lint.py` 的产物）；
      - `.retry_state.json` 是跨触发失败冷却状态（不是 `.md`，天然不进 `glob("*.md")`）。

    约定 `_` 前缀留给「放在 topics/ 里、但不是一页概念页」的派生产物。四处扫描
    （`stale_topic_slugs`/`audit_topic_pages`/`render_topics_index` 与 lint 侧的
    `cited_citekeys`）都过这一关。

    这层是**冗余防线**而非唯一防线：前三处本来就还要求 frontmatter 里有 `topic` 键，
    报告与目录页都没有这个键，本就进不去。但 lint 侧的 `cited_citekeys` 是纯文本
    扫 `[@key]`、根本不看 frontmatter——若不按文件名先挡一道，`_lint.md` 里逐条列出的
    撤稿/对撞 citekey 会被算成"已被概念页覆盖"，把覆盖率虚报上去，而且报告越长虚报
    越多（自己引用自己）。所以这道防线在那一处是**唯一**防线。
    """
    name = Path(path).name
    return name.endswith(".md") and name != "INDEX.md" and not name.startswith("_")


def stale_topic_slugs(topics_dir: Path, specs: Sequence[TopicSpec], *,
                      max_age_days: float = 30.0,
                      now: Optional[datetime] = None,
                      max_count: Optional[int] = None) -> List[str]:
    """`generated_at` 落后"现在"超过 `max_age_days` 天的概念页 slug 列表，按
    `generated_at` 从旧到新排序（最久没更新的排最前）。

    W8：`select_evidence` 是"按最高分取前 N"，历史强证据不会过期、不会被淘汰——一页
    若因某次合成失败（LLM 抽风/订阅限流/Ollama 未起）而错过一轮更新，此后只能指望
    "未来新论文恰好挤进它的证据集"才会被 `affected_topics` 自然重试，这个概率随语料
    增长递减，一次性故障可能变成事实上永久搁置。这里给一条**不依赖路由命中**的兜底：
    纯按日历时间判断，与本轮有没有新论文、新论文挤不挤得进证据集都无关。

    只看仍在 `specs`（当前 topics.yaml）里的 slug——已退役的页不该被强制拉回来重跑，
    它本就该定格在最后一次生成的内容上（同 `render_topics_index` 对退役页的处理）。
    `generated_at` 解析不出的页面（字段缺失/格式异常）不强制——那类问题该由 `--verify`
    的死键/失锚检测去发现，不该让格式异常本身触发一次没有意义的重跑。

    X3：W8 兜底本身是"无条件拉回全部过期页"——若某次上线是批量生成（同一批 slug 的
    `generated_at` 挤在同一个时间点），会在同一个日历阈值上同时过期（复审 agent 用
    仓库真实 config/topics.yaml 复现：8 页全部命中）。调用方（build_topics.py）
    此前对 `stale` 列表是无条件并集，单次运行会连续跑 N 页 3-4 分钟的 LLM 合成，
    8 页量级是 25-30 分钟连续调用——这正是 P2 路由机制本来要避免的成本形状，也更
    容易撞订阅限流。`max_count` 给"每次最多拉回几个"开一个口子，多出来的页留给
    下次触发，自然摊开而不是一次性引爆；必须先按 `generated_at` 排序再截断，否则
    "最多 N 个"会截到不一定最旧的 N 个，退化成没有优先级的随机限流。
    """
    now = now or datetime.now()
    by_slug = {s.slug for s in specs}
    out: List[Tuple[datetime, str]] = []
    for path in sorted(Path(topics_dir).glob("*.md")):
        if not is_topic_page_file(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, _ = split_frontmatter(text)
        if not isinstance(fm, dict) or not fm.get("topic"):
            continue
        slug = str(fm["topic"])
        if slug not in by_slug:
            continue
        try:
            gen_at = datetime.fromisoformat(str(fm.get("generated_at") or ""))
        except ValueError:
            continue
        if (now - gen_at).total_seconds() > max_age_days * 86400:
            out.append((gen_at, slug))
    out.sort(key=lambda pair: pair[0])
    slugs = [slug for _, slug in out]
    if max_count is not None and max_count >= 0:
        slugs = slugs[:max_count]
    return slugs


# ---------------------------------------------------------------------------
# P2 路由编排：合并路由命中 + 日历兜底 + 跨触发失败冷却
# （build_topics.py 的 --affected-by/--affected-by-note 路由分支专用；
#  --topic 显式指定 / 全量构建不经过这里，见 build_topics.py::main）
# ---------------------------------------------------------------------------

@dataclass
class RoutingPlan:
    """P2 路由本轮真正要重新合成的 slug 集合，及用于展示的中间结果。"""
    want_slugs: List[str]        # 最终要合成的 slug：路由命中在前、stale 补课在后，去重保序
    stale_selected: List[str]    # 本轮实际纳入的 stale 补课（已按 generated_at 从旧到新截断）
    stale_held_back: List[str]   # 超过 --stale-max-per-run 的部分，留给下次触发


def plan_synthesis_targets(hits: Sequence[Tuple[str, List[str]]],
                           stale_all: Sequence[str], *,
                           routing_ok: bool,
                           stale_max_per_run: Optional[int] = None) -> RoutingPlan:
    """合并 P2 路由命中（`affected_topics` 的结果）与 W8 日历兜底
    （`stale_topic_slugs` 的结果），得到本轮真正要重新合成的 slug 集合。

    纯函数：调用方负责先做好"向量库是否陈旧"的判断与冷却过滤，这里只管合并与
    按名额截断，不碰向量库/磁盘。

    Y1（运行时复审实测）：`routing_ok=False`（向量库落后当前索引，见
    `routing_is_stale`）时，调用方应传入空的 `hits`（或本函数据 `routing_ok`
    直接忽略 `hits`，双保险）——但 `stale_all` 只读页面 frontmatter 的
    `generated_at`，不碰向量库，不该被向量库的健康度连带拖累。W8 兜底存在的
    全部理由就是给"因各种故障错过更新的页"一条不依赖路由的补课通道；此前
    build_topics.py 判定向量库陈旧时直接整体 return 2，连这条兜底也一并挡住了
    ——W2（路由不可信要拒绝）与 W8（日历兜底不依赖路由）两条修复单看都对，
    叠加后互相拖累，这是本函数存在的直接原因：把"合并"这一步收敛成一处，
    调用方不必再自己记住这层交互关系。

    `stale_all` 须已按 `generated_at` 从旧到新排序（`stale_topic_slugs` 的既有
    约定），这里只做"排除已被路由命中的 slug"与"按 `stale_max_per_run` 截断"
    两件事，不重新排序（截断前必须已经是按陈旧程度排好的，同 X3 的既有处理，
    否则"最多 N 个"会截到不一定最旧的 N 个）。
    """
    want = list(dict.fromkeys(s for s, _ in hits)) if routing_ok else []
    want_set = set(want)
    remaining = [s for s in stale_all if s not in want_set]
    if stale_max_per_run is not None and stale_max_per_run > 0:
        selected = remaining[:stale_max_per_run]
    else:
        selected = remaining
    held_back = remaining[len(selected):]
    want += selected
    return RoutingPlan(want_slugs=want, stale_selected=selected, stale_held_back=held_back)


# 纯运行状态、不是知识产物：命名带点前缀 + .json 后缀，双重避开
# render_topics_index/audit_topic_pages/sync_topics_to_vault 对 topics_dir 的
# glob("*.md") 扫描——不能让这份状态文件被当成概念页处理。
RETRY_STATE_FILENAME = ".retry_state.json"

# 第 N 次连续失败后的冷却时长（小时），下标 = consecutive_failures - 1，超出表长按
# 最后一档（24h）封顶。Y2 派活单实测依据：生产回退链 claude-agent(sonnet) ->
# claude-agent(opus) -> gemini 共 3 级，llm.call 默认 max_retries=4，一页合成失败要
# 吃满 12 次底层调用、纯退避 ≥42 秒；claude-agent 两档共享同一订阅配额、gemini 在
# 当前地区不可用，真实有效冗余不到 2 级。没有跨触发记忆时，活跃期（周度/月度/
# 手动精读批量协议）每次触发都会对着同一批已知失败的页重放这套调用，订阅一旦
# 打满就是持续空转。指数退避而非固定间隔：偶发抖动一小时后就能恢复，真正的长期
# 故障（如订阅欠费）不会被摊到每小时都来敲一次注定失败的 12 次调用。
RETRY_COOLDOWN_HOURS: Tuple[float, ...] = (1.0, 4.0, 12.0, 24.0)


def _retry_state_path(topics_dir) -> Path:
    return Path(topics_dir) / RETRY_STATE_FILENAME


def _load_retry_state(topics_dir) -> Dict[str, Dict[str, Any]]:
    """读取冷却状态。**必须永不抛出**：这是纯运行状态而非知识产物，文件不存在
    （从未失败过/被人工清理）、损坏（并发写入/磁盘抖动/人工误改内容，含非法 JSON
    与非法 UTF-8 两种"坏法"）都只应退化成"没有冷却记忆"，绝不能让这个辅助机制
    自己出故障就连带阻断正常合成——宁可多重试几次已知失败的页，也不能因为一个
    状态文件损坏就让全部概念页永久卡住。
    """
    path = _retry_state_path(topics_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        # ValueError 覆盖 json.JSONDecodeError 与 UnicodeDecodeError（两者都是其子类）
        # ——文件内容不是合法 JSON、或干脆不是合法 UTF-8（如写入中途被杀留下的半截
        # 字节），处理方式相同：退化成无冷却，不上抛。FileNotFoundError（从未失败过
        # 的常态）不 warning，其余情形（损坏/权限）才值得留一条日志。
        if not isinstance(e, FileNotFoundError):
            logger.warning("重试冷却状态文件不可读/已损坏，当作无冷却处理：{}（{}）", path, e)
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)}


def _save_retry_state(topics_dir, state: Dict[str, Dict[str, Any]]) -> None:
    """落盘走 write_if_changed（同库既有约定）。与读取同一个原则：这是运行状态的
    维护动作，写失败（权限/磁盘满等）不该向上传播炸穿 build_topics.py 的主循环——
    那样会连累它之后还没跑到的页连正常合成都做不成，比"这次没记上冷却"严重得多。
    """
    try:
        write_if_changed(_retry_state_path(topics_dir),
                         json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    except OSError as e:
        logger.warning("重试冷却状态写入失败，本轮跳过记忆（不影响概念页合成）：{}", e)


def _cooldown_remaining_hours(rec: Optional[Dict[str, Any]], now: datetime) -> float:
    """记录不完整/时间戳解析不出时一律当无冷却（返回 <= 0）——格式异常不该升级成
    "保守拦截"，那会把一条记录的损坏放大成对应页面被永久卡住。"""
    if not isinstance(rec, dict):
        return 0.0
    n = rec.get("consecutive_failures")
    last = rec.get("last_failed_at")
    if not isinstance(n, int) or n <= 0 or not isinstance(last, str):
        return 0.0
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return 0.0
    hours = RETRY_COOLDOWN_HOURS[min(n, len(RETRY_COOLDOWN_HOURS)) - 1]
    return hours - (now - last_dt).total_seconds() / 3600.0


def record_topic_failure(topics_dir, slug: str, *, now: Optional[datetime] = None) -> None:
    """一页合成失败（`TopicResult.status == "failed"`）后登记冷却记忆，连续失败次数
    只增不减（除非合成成功清零，见 `record_topic_success`）。"""
    now = now or datetime.now()
    state = _load_retry_state(topics_dir)
    prev = state.get(slug) or {}
    try:
        n = int(prev.get("consecutive_failures") or 0)
    except (TypeError, ValueError):
        n = 0
    state[slug] = {"last_failed_at": now.isoformat(timespec="seconds"),
                   "consecutive_failures": n + 1}
    _save_retry_state(topics_dir, state)


def record_topic_success(topics_dir, slug: str) -> None:
    """合成成功（`status` 为 `new`/`merged`）即清除该页的冷却记忆——下次它再进入
    路由命中或日历兜底名单时，不该继续被上一轮已经翻篇的失败历史挡住。"""
    state = _load_retry_state(topics_dir)
    if slug in state:
        del state[slug]
        _save_retry_state(topics_dir, state)


def filter_retry_cooldown(topics_dir, slugs: Sequence[str], *,
                          now: Optional[datetime] = None
                          ) -> Tuple[List[str], List[Tuple[str, float]]]:
    """把候选 slug 过一遍冷却状态。返回 `(可提交合成的, 仍在冷却中的 [(slug, 剩余小时)])`，
    两者都保持输入顺序。

    状态文件缺失/损坏时 `_load_retry_state` 已退化成 `{}`，这里自然是"全部放行"——
    冷却机制的默认状态必须是不生效，而不是保守地一律拦截（那样一次状态文件损坏会
    让全部概念页永久卡住，比没有这道机制更糟）。
    """
    now = now or datetime.now()
    state = _load_retry_state(topics_dir)
    allowed: List[str] = []
    cooling: List[Tuple[str, float]] = []
    for slug in slugs:
        remaining = _cooldown_remaining_hours(state.get(slug), now)
        if remaining > 0:
            cooling.append((slug, remaining))
        else:
            allowed.append(slug)
    return allowed, cooling


def attach_titles(evidences: Sequence[Evidence], index: dict) -> None:
    """就地补上论文标题（向量库 chunk 不存 title，证据表里没标题很难核验）。"""
    titles = {
        e.get("citekey"): (e.get("title") or "")
        for e in (index.get("papers") or []) if isinstance(e, dict)
    }
    for ev in evidences:
        if not ev.title:
            ev.title = titles.get(ev.citekey) or None


# ---------------------------------------------------------------------------
# prompt 组装
# ---------------------------------------------------------------------------

def render_evidence_block(evidences: Sequence[Evidence]) -> str:
    """喂给 LLM 的证据块。**只给编号，不给 citekey**——模型看不到 citekey 就写不出
    citekey，编号回译才是唯一的引用通道（见模块 docstring）。"""
    lines = []
    for e in evidences:
        meta = []
        if e.role:
            meta.append(ROLE_LABEL.get(e.role, e.role))
        if e.section:
            meta.append(e.section)
        if e.year:
            meta.append(str(e.year))
        head = "[{}]".format(e.ref)
        if meta:
            head += "（{}）".format(" · ".join(meta))
        lines.append("{} {}".format(head, e.text.strip()))
    return "\n".join(lines)


def build_prompt(spec: TopicSpec, evidences: Sequence[Evidence], template: str,
                 research_interests: str = "", previous_page: str = "") -> str:
    """填充 prompt 模板。previous_page 为空时明确写「无」，避免模型把占位符当内容。"""
    prev = previous_page.strip()
    if len(prev) > 8000:
        # 上一版全文进 prompt 会随版本累积膨胀；截断只影响"修订参考"，证据仍是权威来源
        prev = prev[:8000] + "\n……（上一版本过长，此处截断）"
    return (template
            .replace("{{TOPIC_TITLE}}", spec.title)
            .replace("{{TOPIC_QUESTION}}", spec.question)
            .replace("{{RESEARCH_INTERESTS}}", research_interests.strip() or "（未配置）")
            .replace("{{EVIDENCE_BLOCK}}", render_evidence_block(evidences))
            .replace("{{PREVIOUS_PAGE}}", prev or "（无上一版本，这是首次生成）"))


def load_prompt_template(path) -> str:
    p = Path(path)
    if not p.exists():
        raise TopicError("概念页 prompt 模板不存在：{}".format(p))
    text = p.read_text(encoding="utf-8")
    missing = [ph for ph in ("{{TOPIC_TITLE}}", "{{TOPIC_QUESTION}}", "{{EVIDENCE_BLOCK}}")
               if ph not in text]
    if missing:
        raise TopicError("prompt 模板 {} 缺占位符 {}".format(p, missing))
    return text


# ---------------------------------------------------------------------------
# 解析与校验（防幻觉主防线）
# ---------------------------------------------------------------------------

def parse_synthesis(raw: str) -> dict:
    """LLM 原始输出 -> dict。剥代码围栏，退而求其次截取第一个 { 到最后一个 }。"""
    text = (raw or "").strip()
    if not text:
        raise TopicError("LLM 返回空输出")
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise TopicError("LLM 输出不是 JSON：{}".format(text[:200]))
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError as e:
            raise TopicError("LLM 输出 JSON 解析失败（{}）：{}".format(e, text[:200]))
    if not isinstance(data, dict):
        raise TopicError("LLM 输出的顶层不是 JSON 对象")
    return data


@dataclass
class ValidationReport:
    """校验战果。落进页面 frontmatter，异常值一眼可见（invalid_refs 突然变大 =
    模型开始编号乱编，该换模型或改 prompt 了）。"""
    kept_claims: int = 0
    dropped_claims: int = 0        # 证据编号全部无效 -> 整条丢弃
    invalid_refs: int = 0          # 越界/格式非法的编号
    stripped_cites: int = 0        # 正文里被剥掉的引用标记（[@key] 与不带方括号的裸 @key）
    dropped_disputes: int = 0
    used_refs: int = 0             # 实际被引用到的证据条数（覆盖率分子）


def _clean_text(s: Any, report: ValidationReport) -> str:
    """正文清洗：剥掉模型自写的引用标记（[@key] 与裸 @key，见 _BARE_CITE_RE 顶部注释），
    这是唯一能绕开编号校验的编造通道。"""
    text = str(s or "").strip()
    if not text:
        return ""
    cleaned, n = _BARE_CITE_RE.subn("", text)
    if n:
        report.stripped_cites += n
    # 剥引用后可能留下 "……方法 ，" 之类的空档
    return re.sub(r"\s{2,}", " ", cleaned).strip().rstrip("，,、 ")


def _resolve_refs(refs: Any, ev_map: Dict[str, Evidence],
                  report: ValidationReport) -> List[str]:
    """编号列表 -> 去重后的合法编号列表。越界/格式非法的计入 invalid_refs 并剔除。"""
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, (list, tuple)):
        return []
    out: List[str] = []
    for r in refs:
        key = str(r).strip()
        m = _REF_RE.match(key)
        if m:
            key = "E{}".format(int(m.group(1)))    # e3 / E03 -> E3
        if key in ev_map:
            if key not in out:
                out.append(key)
        else:
            report.invalid_refs += 1
    return out


def validate_synthesis(data: dict, evidences: Sequence[Evidence]
                       ) -> Tuple[dict, ValidationReport]:
    """把 LLM 输出清洗成可渲染的结构，并把每个编号回译成 citekey。

    丢弃而非修补：一条论断的证据编号若全部无效，说明模型在这条上要么编造要么
    张冠李戴，留着它比少一条更危险——概念页的全部价值就是"每句都可追溯"。
    """
    report = ValidationReport()
    ev_map = {e.ref: e for e in evidences}
    used: set = set()

    def _refs_to_keys(refs: List[str]) -> List[str]:
        keys: List[str] = []
        for r in refs:
            used.add(r)
            ck = ev_map[r].citekey
            if ck not in keys:
                keys.append(ck)
        return keys

    sections: List[dict] = []
    for sec in (data.get("sections") or []):
        if not isinstance(sec, dict):
            continue
        claims: List[dict] = []
        for c in (sec.get("claims") or []):
            if not isinstance(c, dict):
                continue
            text = _clean_text(c.get("text"), report)
            refs = _resolve_refs(c.get("evidence"), ev_map, report)
            if not text or not refs:
                report.dropped_claims += 1
                continue
            claims.append({"text": text, "refs": refs, "citekeys": _refs_to_keys(refs)})
            report.kept_claims += 1
        if claims:
            sections.append({"heading": _clean_text(sec.get("heading"), report) or "要点",
                             "claims": claims})

    disputes: List[dict] = []
    for d in (data.get("disputes") or []):
        if not isinstance(d, dict):
            continue
        issue = _clean_text(d.get("issue"), report)
        pos_a = _clean_text(d.get("position_a"), report)
        pos_b = _clean_text(d.get("position_b"), report)
        refs_a = _resolve_refs(d.get("evidence_a"), ev_map, report)
        refs_b = _resolve_refs(d.get("evidence_b"), ev_map, report)
        # 分歧必须两侧都有证据，否则它只是一条普通论断被写成了争议
        if not issue or not (pos_a and refs_a) or not (pos_b and refs_b):
            report.dropped_disputes += 1
            continue
        # 两侧回译出的文献集合若完全相同，就不是分歧，是同一批文献被复述成两段话
        # （实测 shadow-variable.md 踩过：两侧同引 aerts2021Quality 一篇论文的两条批注，
        # LLM 自己在 note 里都写"两者其实是同一现象的两面"）。这里要在 _refs_to_keys
        # 登记"已使用"之前判断——丢弃的分歧不该让它引用的证据被计入 used_refs/标 ●。
        # 部分重叠合法（同一篇论文可能既支持又限定），只拦完全相同的集合。
        if {ev_map[r].citekey for r in refs_a} == {ev_map[r].citekey for r in refs_b}:
            report.dropped_disputes += 1
            continue
        disputes.append({
            "issue": issue,
            "position_a": pos_a, "refs_a": refs_a, "citekeys_a": _refs_to_keys(refs_a),
            "position_b": pos_b, "refs_b": refs_b, "citekeys_b": _refs_to_keys(refs_b),
            "note": _clean_text(d.get("note"), report),
        })

    gaps = [t for t in (_clean_text(g, report) for g in (data.get("gaps") or [])) if t]
    report.used_refs = len(used)
    return {
        "summary": _clean_text(data.get("summary"), report),
        "sections": sections,
        "disputes": disputes,
        "gaps": gaps,
    }, report


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _cite_str(citekeys: Sequence[str]) -> str:
    return " ".join("[@{}]".format(k) for k in citekeys)


def render_generated_block(spec: TopicSpec, synthesis: dict,
                           evidences: Sequence[Evidence]) -> str:
    """哨兵之间的正文：总述 → 小节论断 → 分歧 → 缺口 → 证据表。

    证据表列出全部召回证据（含未被引用的），并标注哪些没被用上。
    """
    used_refs = {r for sec in synthesis["sections"] for c in sec["claims"] for r in c["refs"]}
    # 分歧区引用的证据同样算"被采用"——曾经漏了这一段，只在「⚔️ 分歧与冲突」里出现的
    # 证据会被误标 ○（validate_synthesis 的 report.used_refs 其实一直算了 disputes，
    # 是这里的渲染口径没跟上，两处必须用同一份 used 集合）。
    for d in synthesis["disputes"]:
        used_refs.update(d["refs_a"])
        used_refs.update(d["refs_b"])
    lines: List[str] = ["# {}".format(spec.title), ""]
    lines += ["> {}".format(spec.question), ""]
    if synthesis["summary"]:
        lines += [synthesis["summary"], ""]

    for sec in synthesis["sections"]:
        lines.append("## {}".format(sec["heading"]))
        lines.append("")
        for c in sec["claims"]:
            lines.append("- {} {}".format(c["text"], _cite_str(c["citekeys"])))
        lines.append("")

    if synthesis["disputes"]:
        lines += ["## ⚔️ 分歧与冲突", ""]
        for d in synthesis["disputes"]:
            lines.append("### {}".format(d["issue"]))
            lines.append("")
            lines.append("- **一方**：{} {}".format(d["position_a"], _cite_str(d["citekeys_a"])))
            lines.append("- **另一方**：{} {}".format(d["position_b"], _cite_str(d["citekeys_b"])))
            if d["note"]:
                lines.append("- *为何分歧*：{}".format(d["note"]))
            lines.append("")

    if synthesis["gaps"]:
        lines += ["## 证据缺口", ""]
        lines += ["- {}".format(g) for g in synthesis["gaps"]]
        lines.append("")

    n_papers = len({e.citekey for e in evidences})
    lines += ["## 本页证据（{} 条 · {} 篇）".format(len(evidences), n_papers), ""]
    # 引用率不是单独的质量信号：低可能是 queries 跑偏，也可能是证据池混入了偏题内容；
    # 高也不代表论断都扎实，可能是不相关内容被顺带引用"注水"（详见 AGENTS.md
    # 「怎么读这些数字」，实测踩过某页 98% 引用率里近三分之一是无关方法联想）。
    lines.append("每条可回溯到札记原文；未被引用的证据标 ○。引用率高低不能单独当质量信号——"
                 "判断这一页可不可信，看下面论断是否具体、分歧是否来自不同文献，比看这个比例可靠。")
    lines.append("")
    for e in evidences:
        mark = "●" if e.ref in used_refs else "○"
        meta = [ROLE_EMOJI.get(e.role or "", "") + ROLE_LABEL.get(e.role or "", e.role or "")]
        if e.section:
            meta.append(e.section)
        loc = ""
        if e.note_file:
            loc = " · {}{}".format(e.note_file, ":{}".format(e.note_line) if e.note_line else "")
        lines.append("- {} **{}** `[@{}]` {}{}".format(
            mark, e.ref, e.citekey, " · ".join(m for m in meta if m), loc))
        if e.title:
            lines.append("  <small>{}</small>".format(e.title))
        lines.append("  > {}".format(e.text.strip().replace("\n", " ")))
    return "\n".join(lines)


def _render_frontmatter(items: Dict[str, Any]) -> str:
    """把有序字典渲染成 YAML frontmatter 文本（`---` 包裹）。

    G4：`build_frontmatter` 与 `sync_topics_to_vault` 此前各自维护一份几乎相同的
    序列化实现，都用 `"{}: {}".format(k, ...)` 裸拼键名——两份拷贝已经在值的序列化上
    演化出了行为差异（一份对 list 元素调 `json.dumps(x)`，另一份多包一层 `str(x)`），
    键名侧则共享同一个洞：受管键是硬编码字面量、天然合法，但用户在 Obsidian 属性面板
    或文本编辑器里自加的键（经 `preserved` 混进来）是任意字符串——含 `: ` `#` 或空串的
    键裸拼会写出**语法错误的 YAML**，下次 `split_frontmatter` 解析失败、该页永久卡在
    conflict（审计实测：用户加了个 `备注: 待复核` 这种键就踩上）。

    统一走这一个函数，键名一律用 vault.py 那套 `_SAFE_KEY`/`_YAML_WORDS` 防护判断是否
    要加引号（管理键套上是零成本 no-op，用户键套上才是真正需要的防护），不在本模块
    重新发明；值的序列化也统一成一套逻辑，两处调用方不再各自演化出分歧。
    """
    lines = ["---"]
    for k, v in items.items():
        ks = str(k)
        key = ks if (_SAFE_KEY.match(ks) and ks.lower() not in _YAML_WORDS) else _y(ks)
        if isinstance(v, list):
            lines.append("{}:".format(key))
            lines += ["  - {}".format(_y(x)) for x in v]
        elif isinstance(v, dict):
            # 受管键从不产出 dict；只有用户手写的嵌套 YAML 才可能走到这里。
            # json.dumps 的转义集合是 YAML 双引号标量/流式对象的子集，天然合法。
            lines.append("{}: {}".format(key, json.dumps(v, ensure_ascii=False)))
        else:
            lines.append("{}: {}".format(key, _y(v)))
    lines.append("---")
    return "\n".join(lines)


def build_frontmatter(spec: TopicSpec, synthesis: dict, evidences: Sequence[Evidence],
                      report: ValidationReport, generated_at: str,
                      preserved: Optional[Dict[str, Any]] = None) -> str:
    """受管键由生成器覆盖，用户自加的键原样保留（与 vault.build_frontmatter 同约定：
    受管键在前用新值，用户自加键跟在后面）。"""
    managed: Dict[str, Any] = {
        "topic": spec.slug,
        "title": spec.title,
        "type": "topic",
        "topic_schema": TOPIC_SCHEMA_VERSION,
        "generated_at": generated_at,
        "n_evidence": len(evidences),
        "n_papers": len({e.citekey for e in evidences}),
        "n_claims": report.kept_claims,
        "n_disputes": len(synthesis["disputes"]),
        "n_gaps": len(synthesis["gaps"]),
        "evidence_used": report.used_refs,
        "dropped_claims": report.dropped_claims,
        "invalid_refs": report.invalid_refs,
        "stripped_cites": report.stripped_cites,
        "tags": ["札记/概念页", "概念/{}".format(spec.slug)],
    }
    items = dict(managed)
    for k, v in (preserved or {}).items():
        if k not in managed:
            items[k] = v
    return _render_frontmatter(items)


DEFAULT_GENERATOR = "scripts/build_topics.py"


def assemble(fm: str, gen_block: str, user_zone: str, *,
             generator: str = DEFAULT_GENERATOR) -> str:
    """`generator` 只影响哨兵注释里那句「由 X 生成」的人话提示，不参与哈希、不影响
    解析（`vault.GEN_BEGIN_RE` 对脚本名那段是 `.*`）。参数化是因为 topics/ 下现在
    不止一种产物：概念页由 `build_topics.py` 写，知识层 lint 报告由 `lint_notes.py`
    写——哨兵里指错脚本，人按提示去重跑会跑到一个根本不生成这份文件的命令上。"""
    body = gen_block.rstrip("\n")
    return "{}\n\n{}\n{}\n{}\n\n{}\n".format(
        fm, GEN_BEGIN.format(TOPIC_SCHEMA_VERSION, _gen_hash(body), generator), body,
        GEN_END, user_zone.strip("\n"))


def merge_topic_page(fm: str, gen_block: str, existing: Optional[str], *,
                     generator: str = DEFAULT_GENERATOR
                     ) -> Tuple[Optional[str], str]:
    """合并生成块与用户批注。conflict 时返回 None，调用方不得覆盖（同 vault 约定）。"""
    if existing is None:
        return assemble(fm, gen_block, DEFAULT_USER_ZONE, generator=generator), "new"
    _fm, body = split_frontmatter(existing)
    if _fm is None:
        return None, "conflict"
    user_zone = extract_user_zone(body)
    if user_zone is None:
        return None, "conflict"
    if generated_block_tampered(body):
        return None, "conflict"
    return assemble(fm, gen_block, user_zone, generator=generator), "merged"


def read_existing(path: Path) -> Tuple[Optional[str], Optional[Dict[str, Any]], str]:
    """返回 (原文, frontmatter dict, 上一版生成块正文)。文件不存在给 (None, None, "")。

    G2：只把 FileNotFoundError 当"不存在"，其余 OSError（权限被改、磁盘抖动等瞬时读
    失败）一律向上传播，绝不能吞掉。旧代码用 `except OSError` 把两者一视同仁，导致
    "文件明明在、这次只是读不出来"被误判成"从未存在过"——merge_topic_page 据此走
    "新建"分支，用空白 DEFAULT_USER_ZONE 重建整页，用户几个月的批注被一次 IO 抖动
    静默清空，CLI 还打印 🆕 new、退出码 0（编排者实测复现）。这里不捕获的异常会继续
    向上冒出 build_topic，最终被 scripts/build_topics.py 主循环既有的 except Exception
    接住、落成 status=failed，而不是悄悄新建（同 vault.py::write_vault 主循环对同类
    场景的处理："except OSError: raise，权限/磁盘问题交给 CLI 统一提示"）。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None, ""
    fm, body = split_frontmatter(text)
    prev = ""
    if isinstance(body, str):
        end = body.find(GEN_END)
        prev = body[:end] if end >= 0 else body
        prev = re.sub(r"^<!-- BEGIN GENERATED.*?-->\s*", "", prev.strip(), flags=re.S)
    return text, (fm if isinstance(fm, dict) else None), prev


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------

@dataclass
class TopicResult:
    slug: str
    status: str                   # new / merged / conflict / skipped / failed
    path: Optional[Path] = None
    n_evidence: int = 0
    n_papers: int = 0
    n_disputes: int = 0
    report: Optional[ValidationReport] = None
    error: str = ""
    changed: bool = False


def synthesize_topic(llm, prompt: str, *, model: Optional[str] = None,
                     max_tokens: int = 8000, retries: int = 1) -> dict:
    """调 LLM 合成 + 解析。

    G5：llm.call 抛出的任何异常统一包成 TopicError 再上抛——尤其是回退链整条耗尽时
    llm_client.py 抛的 `RuntimeError("所有 LLM provider 均不可用…")`，本项目实际踩过
    （订阅限流/备用 provider 地区不可用）且是最常见的失败模式。不包一层的话，
    build_topic 里"保留召回统计再上抛"的 except TopicError 分支会被跳过（那个分支的
    注释明确写了是为了不让人被"证据 0 条"误导去调 queries/阈值），落到 CLI 的泛
    except，构造出的 TopicResult 没有 n_evidence/n_papers，会把人错误引向调召回参数，
    而真正的病灶在合成端。

    解析失败重试一次（`retries=1`）：claude-agent 没有原生 json_mode，只能靠指令约束，
    实测会偶发"元描述"式跑偏——模型输出「已完成综述合成，输出为合法 JSON。要点：……」
    这样一段自述，整段没有花括号，parse_synthesis 的首尾花括号兜底也救不回来
    （2026-08-17 cross-site-transportability 页踩过）。这类失败是**偶发的格式跑偏而非
    内容问题**，而重跑一页要重新召回 + 重新合成，白扔掉几分钟；追加一句更严厉的格式
    指令再试一次的代价小得多。注意只重试解析失败，provider 层的错误交给 llm_client
    自己的重试与回退链，不在这里叠加。
    """
    last: Optional[TopicError] = None
    for attempt in range(retries + 1):
        p = prompt
        if attempt:
            p += ("\n\n【上一次输出格式不合规】你上次没有直接输出 JSON。这一次**第一个字符**"
                  "必须是 `{`、**最后一个字符**必须是 `}`，中间是完整的合成结果。"
                  "不要写任何说明、前言、要点总结或代码围栏。")
        try:
            raw = llm.call(p, model=model, max_tokens=max_tokens,
                           temperature=0.3, json_mode=True)
        except TopicError:
            raise
        except Exception as e:
            raise TopicError("LLM 调用失败（{}）：{}".format(type(e).__name__, e)) from e
        try:
            return parse_synthesis(raw)
        except TopicError as e:
            last = e
            if attempt < retries:
                logger.warning("合成输出不是合法 JSON，追加格式指令重试一次：{}", str(e)[:120])
    raise last


def build_topic(spec: TopicSpec, *, store, embed_client, llm, index: dict,
                topics_dir: Path, template: str, research_interests: str = "",
                min_sim: float = DEFAULT_MIN_SIM,
                per_paper_cap: int = DEFAULT_PER_PAPER_CAP,
                bucket_bonus: float = DEFAULT_BUCKET_BONUS,
                model: Optional[str] = None, dry_run: bool = False) -> TopicResult:
    """一个 topic 的完整链路：召回 → 合成 → 校验 → 合并 → 落盘。"""
    path = topics_dir / "{}.md".format(spec.slug)
    evidences = retrieve_evidence(spec, store, embed_client, min_sim=min_sim,
                                  per_paper_cap=per_paper_cap, bucket_bonus=bucket_bonus)
    if not evidences:
        return TopicResult(slug=spec.slug, status="skipped", path=path,
                           error="召回 0 条证据（相似度阈值 {} 或过滤条件过严）".format(min_sim))
    attach_titles(evidences, index)
    n_papers = len({e.citekey for e in evidences})

    # 只取 prev_page 喂 prompt；这份快照的 text/frontmatter 不用来做最终合并——
    # LLM 调用后落盘前会在下面重读一次最新版（X1/W1），用那份而不是这里的旧快照。
    _, _, prev_page = read_existing(path)
    prompt = build_prompt(spec, evidences, template, research_interests, prev_page)
    if dry_run:
        return TopicResult(slug=spec.slug, status="skipped", path=path,
                           n_evidence=len(evidences), n_papers=n_papers,
                           error="dry-run（未调用 LLM）")

    try:
        data = synthesize_topic(llm, prompt, model=model)
    except TopicError as e:
        # 保留召回统计再上抛：失败行只显示"证据 0 条"会把人引向调 queries/阈值，
        # 而真正的病灶在合成端（模型没吐出合法 JSON）。
        return TopicResult(slug=spec.slug, status="failed", path=path,
                           n_evidence=len(evidences), n_papers=n_papers, error=str(e))
    synthesis, report = validate_synthesis(data, evidences)
    if not synthesis["sections"]:
        return TopicResult(slug=spec.slug, status="failed", path=path,
                           n_evidence=len(evidences), n_papers=n_papers, report=report,
                           error="校验后没有任何有效论断（丢弃 {} 条，非法编号 {} 个）".format(
                               report.dropped_claims, report.invalid_refs))

    gen_block = render_generated_block(spec, synthesis, evidences)
    n_disputes = len(synthesis["disputes"])
    # W1（lost update）：synthesize_topic 实测一页要 3-4 分钟，用户完全可能在这期间在
    # Obsidian 里编辑「我的批注」区，也可能在 Properties 面板加一个自定义 frontmatter
    # 键。若仍用 970 行 LLM 调用前那份 `existing`/`old_fm` 快照去合并/建 frontmatter，
    # 这段时间的新改动会被静默覆盖——状态照样是正常的 merged，没有任何报错信号。
    # X1：W1 当时只把正文这半条命救回来（下面 merge_topic_page 用的已经是重读的
    # fresh_existing），但 build_frontmatter 的 preserved= 之前仍传的是旧 old_fm——
    # 同一个 lost-update 漏洞在 frontmatter 分支原样复现（编排者实测复现：用户在合成
    # 期间加的自定义键，状态仍显示正常 merged，键却悄悄消失）。必须在这里、也就是
    # build_frontmatter 调用之前重读一次，preserved= 传新读到的 fresh_fm。
    # 注意 `existing`/`old_fm`/`prev_page`（970 行那份旧快照）不能整体换成重读版——
    # `prev_page` 已经喂进了上面的 prompt，事后再换会让 prompt 与实际处理的旧稿不一致，
    # 只有这里、落盘前最后一次的 frontmatter 合并与正文合并要用最新版。
    fresh_existing, fresh_fm, _ = read_existing(path)
    fm = build_frontmatter(spec, synthesis, evidences, report,
                           datetime.now().isoformat(timespec="seconds"), preserved=fresh_fm)
    content, status = merge_topic_page(fm, gen_block, fresh_existing)
    if content is None:
        return TopicResult(slug=spec.slug, status="conflict", path=path,
                           n_evidence=len(evidences), n_papers=n_papers,
                           n_disputes=n_disputes, report=report,
                           error="哨兵缺失/生成块被手改，未覆盖（请把批注移到「我的批注」下）")
    changed = write_if_changed(path, content)
    return TopicResult(slug=spec.slug, status=status, path=path, n_evidence=len(evidences),
                       n_papers=n_papers, n_disputes=n_disputes, report=report,
                       changed=changed)


# ---------------------------------------------------------------------------
# build_topics.py 子进程结果解读：三条入库脚本（周度/月度/手动）共用同一份
# ---------------------------------------------------------------------------

def notes_dir_is_production(notes_dir) -> bool:
    """notes_dir 是否落在仓库 `output/` 目录下——真实生产札记库的唯一合法位置
    （notes_dir 统一经 `paths.repo_path` 锚定到仓库根，见各脚本"CLI 边界锚定一次"
    的既有约定）。

    `ingest_notes.py`/`backfill_notes.py`/`read_pdf.py` 的 P2 best-effort 触发器
    （`_refresh_topics` 系列）发起子进程前必须先过这一关：它们 spawn 一个独立子进程，
    用 `build_topics.py` 自己的默认 `--config`（`config/scholar.env`）重新加载生产
    notes_dir/向量库，与调用方这里传入的 notes_dir **完全脱钩**。测试用 `tmp_path`
    隔离直调（如 `test_read_pdf_cli.py` 直调 `_rebuild_month(tmp_path, ...)`）若不做
    这层判断，子进程仍会拿 note 文件的**文件名**去匹配生产索引——一旦文件名碰巧撞上
    生产库里真实存在的文件（如当月的"科研札记_YYYY-MM_手动精读.md"，编排者实测撞上
    过：跑测试套件时真的起了一个指向生产 config 的 build_topics.py 子进程，挂了 13
    分钟），会误触发对生产数据的路由甚至真实 LLM 合成——绝不能让单测意外打到生产环境。
    """
    from .paths import repo_path
    try:
        Path(notes_dir).resolve().relative_to(repo_path("output").resolve())
        return True
    except ValueError:
        return False


# 这份表要与 scripts/build_topics.py 模块 docstring 里的退出码说明保持一致，改一处
# 记得改另一处（同 GEN_END/GEN_BEGIN 两边独立维护要保持一致的坑，见模块顶部注释）。
BUILD_TOPICS_EXIT_CODES = {
    1: "有概念页失败或冲突",
    2: "配置/索引/向量库异常（含路由阶段向量库陈旧，见 routing_is_stale）",
    3: "Ollama embedding 不可用",
}


@dataclass
class SubprocessRefreshOutcome:
    """`build_topics.py` 子进程跑完后的结论：ok 供调用方决定要不要标记失败，
    detail 是给日志/notify 看的人话摘要。"""
    ok: bool
    detail: str = ""


# Y4：失败/冲突页枚举最多展示几条，超出的部分折成"共 M 页"（见 summarize_build_topics_run
# 顶部注释）。config/topics.yaml 当前 8 个概念页，5 条足够覆盖绝大多数非全灭场景，
# 又不至于在 8 页全灭时把 stderr 诊断挤出 notify 弹窗的 300 字符预算。
_MAX_FLAGGED_IN_SUMMARY = 5


def summarize_build_topics_run(stdout: str, stderr: str, returncode: int
                               ) -> SubprocessRefreshOutcome:
    """把 `build_topics.py` 子进程的输出压成一条人类可读的结论。

    W3：此前三处集成（周度 ingest_notes.py / 月度 backfill_notes.py / 手动 read_pdf.py）
    对这个子进程都是"stdout 只截尾部几行、stderr 完全不读、退出码在通知文案里被拍扁成
    '退出码 N'"——生产事故里失败页的身份在任何持久化日志里都找不到，只能靠重放
    `--dry-run` 反推。这里把三件事一次性收进 `detail`：哪几页失败或冲突（stdout 里
    `❌ `/`⚠️ ` 开头的行，两种图标对应 build_topics.py::_fmt 的 icon 表）、退出码对应
    什么故障（`BUILD_TOPICS_EXIT_CODES`）、有没有 stderr（W2 的"向量库陈旧拒绝路由"
    信息就打在这里）——三条调用方共用同一份解读，不再各自演化出不同的截断/遗漏方式。

    X2：`_fmt` 的 icon 表里 conflict 页用的是 `⚠️`，不是 `❌`——此前这里只匹配 `❌`
    开头的行，conflict（哨兵缺失/生成块被手改，是三种非正常状态里最需要人工介入的
    一种，比 LLM 抽风更需要被点名）在摘要里完全隐形：退出码与 ok 判定都对，但 notify
    弹窗和日志里看不出到底哪一页冲突了，还是得重放 `--dry-run` 反推——恰恰是这个
    函数本来要解决的问题，被 conflict 状态打了折扣（复审 agent 用"1 页 merged + 1 页
    conflict"的真实 stdout 实测复现：detail 里没有冲突页的 slug）。

    Y4：诊断信息（退出码语义、stderr 摘要）排在失败页枚举**之前**——三处调用方的
    notify 弹窗统一把 `detail` 截到 300 字符（持久化日志里仍是全量 `detail`，见
    ingest_notes.py/backfill_notes.py/read_pdf.py），8 页全失败这类极端场景下失败页
    列表本身就接近/超过 300 字符，此前排在 stderr 之前会把诊断类信息（如 W2 的
    向量库陈旧警告）整个挤出弹窗——只是"弹窗变粗糙"、持久化日志信息仍完整，严重度
    不高，但顺手一起修。失败页列表本身也设了上限（`_MAX_FLAGGED_IN_SUMMARY`），
    超出时截成"前 N 页 + 共 M 页"，不让它自己就把 stderr 挤没。
    """
    if returncode == 0:
        return SubprocessRefreshOutcome(ok=True)
    out_lines = (stdout or "").strip().splitlines()
    flagged = [ln.strip() for ln in out_lines
              if ln.strip().startswith("❌") or ln.strip().startswith("⚠️")]
    reason = BUILD_TOPICS_EXIT_CODES.get(returncode, "未知退出码")
    detail = "退出码 {}（{}）".format(returncode, reason)
    err = (stderr or "").strip()
    if err:
        detail += "；stderr：{}".format(err[:300])
    if flagged:
        shown = flagged[:_MAX_FLAGGED_IN_SUMMARY]
        pages = "；".join(shown)
        if len(flagged) > len(shown):
            pages += "（等，共 {} 页，仅列前 {}）".format(len(flagged), len(shown))
        detail += "；失败/冲突页：{}".format(pages)
    return SubprocessRefreshOutcome(ok=False, detail=detail)


# ---------------------------------------------------------------------------
# 自检：页面上的每个引用都还追得回去吗
# ---------------------------------------------------------------------------

# 证据表条目：`- ● **E1** \`[@key]\` …` + 可选 <small>标题</small> + `  > 原句`
_EVIDENCE_ROW_RE = re.compile(
    r"^- ([●○]) \*\*(E\d+)\*\* `\[@([^\]]+)\]`.*?\n(?:  <small>.*?</small>\n)?  > (.*)$",
    re.M)


@dataclass
class PageAudit:
    slug: str
    n_cites: int = 0
    dead_keys: List[str] = field(default_factory=list)      # 索引里查不到的 citekey
    bare_cites: List[str] = field(default_factory=list)     # 残留的裸引用（G1，见下）
    n_evidence: int = 0
    unanchored: List[str] = field(default_factory=list)     # 原句在索引里逐字找不到
    used_ratio: float = 0.0
    stale: bool = False                                     # 页面比索引旧

    @property
    def ok(self) -> bool:
        return not self.dead_keys and not self.unanchored and not self.bare_cites


def audit_topic_pages(topics_dir: Path, index: dict) -> List[PageAudit]:
    """把「页面上的每个引用是否仍可追溯」做成可重复的检查。

    三件事分开查，因为坏法不同：
      - **死键**：citekey 在索引里不存在（改键/删条目后没重建概念页）。粘进 pandoc
        会渲染成 (key?)；
      - **裸引用**：正文里残留不带方括号的 `@key`（G1）——pandoc-citeproc 认它是合法
        引用语法，会被当真引用去挂书目，但它没经过编号回译，逃过了 `to_wiki_links`
        与死键检测两道防线。这里兜底扫描已生成页面，主要是为了发现 2026-08-17 之前
        （只挡方括号形式那版代码）生成的旧页面里可能混入的漏网之鱼；
      - **失锚**：证据表里的原句在该篇 highlights 里逐字找不到（精读被重写过）。
        这类最隐蔽——citekey 还活着，页面读起来毫无破绽，但那句话已经不是它说的了。
    """
    alive = {p["citekey"] for p in (index.get("papers") or [])
             if isinstance(p, dict) and p.get("citekey") and not p.get("duplicate_of")}
    hl_by_key: Dict[str, set] = {}
    for p in (index.get("papers") or []):
        if isinstance(p, dict) and p.get("citekey"):
            # 换行归一化必须与 render_generated_block 的展示口径一致（那里对 e.text
            # 做过 .replace("\n"," ")）：否则页面上已折叠成单行的引文，与这里原始
            # 带换行的 highlight 字符串永远逐字对不上，会把完全正常的证据误报成
            # "失锚"（G8）。目前全库 highlight 都不含内嵌换行，不是活 bug，但
            # notes_index.py 记录过"多行标题"（邮件解析产物）历史上真实出现过。
            hl_by_key.setdefault(p["citekey"], set()).update(
                (h.get("text") or "").strip().replace("\n", " ")
                for h in (p.get("highlights") or []) if isinstance(h, dict))
    idx_at = str(index.get("generated_at") or "")

    out: List[PageAudit] = []
    for path in sorted(Path(topics_dir).glob("*.md")):
        if not is_topic_page_file(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, _ = split_frontmatter(text)
        if not isinstance(fm, dict) or not fm.get("topic"):
            continue
        a = PageAudit(slug=str(fm["topic"]))
        keys = _CITE_RE.findall(text)
        a.n_cites = len(keys)
        a.dead_keys = sorted({k for k in keys if k not in alive})
        # 裸引用只在**生成块内**扫：用户批注区里写「问下 @导师 这个说法要不要强调」
        # 是完全正常的私人笔记，全文件扫描会把它报成问题，让一个生成块完全健康的页面
        # 变红——`--verify` 的价值全在于"报警了就一定有事"，噪音会直接毁掉这个信号。
        # （dead_keys 仍按全文件扫：批注区里写 `[@key]` 是真打算引用，键失效值得提醒，
        #   且它要求完整的方括号语法，误报率远低于裸 @。）
        gen_block = text
        end = text.find(GEN_END)
        if end >= 0:
            gen_block = text[:end]
        stripped = _BRACKET_CITE_RE.sub("", gen_block)
        a.bare_cites = sorted(set(_BARE_CITE_ONLY_RE.findall(stripped)))
        rows = _EVIDENCE_ROW_RE.findall(text)
        a.n_evidence = len(rows)
        used = sum(1 for mark, _, _, _ in rows if mark == "●")
        a.used_ratio = (used / len(rows)) if rows else 0.0
        a.unanchored = ["{}({})".format(ref, key) for _mark, ref, key, quote in rows
                        if quote.strip() not in hl_by_key.get(key, set())]
        gen_at = str(fm.get("generated_at") or "")
        a.stale = bool(idx_at and gen_at and gen_at < idx_at)
        out.append(a)
    return out


# ---------------------------------------------------------------------------
# Obsidian vault 同步
# ---------------------------------------------------------------------------

VAULT_TOPICS_DIR = "02-主题"

_CITE_RE = re.compile(r"\[@([^\]\s]+)\]")


def to_wiki_links(text: str, filenames: Dict[str, str]) -> str:
    """`[@citekey]` -> `[[文件名|citekey]]`，让概念页连进 Obsidian 关系图。

    这是概念页进 vault 的主要增值：概念与论文之间生出真实的边，per-paper 笔记
    自动获得「哪些概念页引用了我」的反向链接。

    不在 filenames 里的 citekey **保持 `[@key]` 原样**——那是"论文在库里但没进
    vault"（如只有摘要没精读的条目），转成 wiki 链接只会得到一个点开是空白的死链。
    """
    def _sub(m):
        key = m.group(1)
        fname = filenames.get(key)
        if not fname:
            return m.group(0)
        return "[[{}|{}]]".format(fname, key) if fname != key else "[[{}]]".format(key)
    return _CITE_RE.sub(_sub, text)


def sync_topics_to_vault(notes_dir: Path, vault_dir: Path, filenames: Dict[str, str],
                         *, dry_run: bool = False,
                         specs: Optional[Sequence[TopicSpec]] = None) -> Dict[str, Any]:
    """notes_dir/topics/*.md -> vault/02-主题/*.md（citekey 转 wiki 链接）。

    vault 侧是独立的一份：有自己的「我的批注」区，走同一套哨兵合并，冲突时不覆盖。
    权威产物仍是 notes_dir 那份（带 `[@citekey]`，pandoc 可直接挂书目）。

    同步范围是「概念页（frontmatter 有 `topic` 键）+ 知识层 lint 报告
    （`type: lint`）」，见下面循环里的 `is_page`/`is_lint`。`INDEX.md` 与其它没有
    这两种身份标记的文件一律跳过。

    specs（B1，可选）：当前 `topics.yaml` 配置。给了就会给"已不在配置里"的页在 vault
    侧打 frontmatter `retired: true` + tag「札记/概念页-退役」——此前退役页在 vault 里
    与"仍在更新"的页面长得一模一样，唯一的提示只写在 `notes_dir/topics/INDEX.md`，
    而 vault 重度用户几乎不会翻那个文件。不给（默认 None）时这段完全不生效，行为与
    此前一致——当前唯一调用方 `build_vault.py` 还没接这个参数。
    """
    notes_dir, vault_dir = Path(notes_dir), Path(vault_dir)
    # None = 不做退役标记（调用方没传 specs）；非 None 时是"当前仍在配置里的 slug 集合"，
    # 不在其中的就是退役页。
    current_slugs = {s.slug for s in specs} if specs is not None else None
    src_dir = notes_dir / TOPICS_DIRNAME
    out_dir = vault_dir / VAULT_TOPICS_DIR
    report: Dict[str, Any] = {"synced": 0, "new": 0, "merged": 0, "unchanged": 0,
                              "conflicts": [], "pruned": [], "skipped": [],
                              "prune_skipped_reason": ""}
    if not src_dir.exists():
        return report

    seen: set = set()
    # G3：本轮若出现"读不出/解析不出，没法确认这份源文件到底是不是概念页"的情形，
    # 记下原因，稍后让整轮 prune 整体跳过——绝不能让"读失败"被当成"已从配置下线"。
    # 与 render_topics_index/vault.py::write_vault 对 --limit 的既定原则一致：
    # 宁可跳过本轮清理，也不用不完整的视图判断谁该删。
    unresolved: List[str] = []
    for src in sorted(src_dir.glob("*.md")):
        if src.name == "INDEX.md":
            continue
        try:
            raw = src.read_text(encoding="utf-8")
        except OSError as exc:
            # 读不出来 ≠ 已从 topics.yaml 下线：源文件很可能内容完好，只是本轮 IO
            # 抖动（审计实测：chmod 0 模拟瞬时读失败，内容完好的源文件被误判成
            # "已下线"，vault 侧同名文件被 unlink）。这里连它的 slug 都拿不到，
            # 没法只保护这一个 slug，只能让整轮 prune 都不可信。
            report["skipped"].append(src.name)
            unresolved.append("{}（读取失败：{}）".format(src.name, type(exc).__name__))
            continue
        fm, body = split_frontmatter(raw)
        if fm is None:
            # YAML 解析失败：同上，没法确认这份文件当下到底是不是概念页。
            report["skipped"].append(src.name)
            unresolved.append("{}（frontmatter 解析失败）".format(src.name))
            continue
        # 同 render_topics_index：认 `topic` 键，别把用户随手放进 topics/ 的笔记
        # （无 frontmatter 时 split_frontmatter 返回空 dict，但不是 None）当概念页
        # 同步进 vault。这里是**明确判定"不是概念页"**（区别于上面两种"读不出/
        # 解析不出"），可以放心不计入 seen——vault 侧同名旧页该走 prune 就走 prune，
        # 不需要让整轮清理陪绑。
        #
        # 例外：知识层 lint 报告（`_lint.md`，frontmatter `type: lint`）也要过来。
        # 它不是概念页（不进 INDEX、不参与日历兜底、不做退役标记），但它是给人读的
        # 知识产物，而人读东西的地方是 Obsidian——只落在 output/scholar_notes/topics/
        # 等于没人看，里面逐条列出的 citekey 也点不开。转成 wiki 链接后，"这篇撤稿
        # 论文正在给哪几页当地基"才真的能一路点过去。
        is_page = bool(fm.get("topic"))
        is_lint = str(fm.get("type") or "") == "lint"
        if not (is_page or is_lint) or not isinstance(body, str):
            report["skipped"].append(src.name)
            continue
        end = body.find(GEN_END)
        gen = body[:end] if end >= 0 else body
        gen = re.sub(r"^<!-- BEGIN GENERATED.*?-->\s*", "", gen.strip(), flags=re.S)
        slug = str(fm.get("topic") or src.stem)
        if not gen:
            # 身份键确认存在（这份文件确实是概念页 / lint 报告），只是这轮生成块
            # 解出来是空——slug 可信，登记进 seen 保护它自己的 vault 页不被当
            # "已下线"清理掉，但没必要因为这一页拖累其它页的 prune。
            report["skipped"].append(src.name)
            seen.add(slug)
            continue

        seen.add(slug)
        dst = out_dir / "{}.md".format(slug)
        gen_v = to_wiki_links(gen, filenames)
        fm_v = dict(fm)
        fm_v["cssclasses"] = fm.get("cssclasses") or (
            ["lint-report"] if is_lint else ["topic-page"])
        # 退役标记只对概念页有意义：lint 报告永远不在 topics.yaml 里，不做这层判断
        # 会让它每次同步都被打上 `retired: true` + 「已退役」标签，读起来像是一份
        # 作废文件（实际上它是每轮重新生成的最新报告）。
        if is_page and current_slugs is not None and slug not in current_slugs:
            # B1：退役页在 vault 侧此前与"仍在更新"的页面长得一模一样，唯一的提示
            # 只写在 notes_dir/topics/INDEX.md——vault 重度用户几乎不会翻那个文件。
            fm_v["retired"] = True
            tags = list(fm_v.get("tags") or [])
            if "札记/概念页-退役" not in tags:
                tags.append("札记/概念页-退役")
            fm_v["tags"] = tags
        fm_text = _render_frontmatter(fm_v)

        existing = dst.read_text(encoding="utf-8") if dst.exists() else None
        content, status = merge_topic_page(fm_text, gen_v, existing)
        if content is None:
            report["conflicts"].append(slug)
            continue
        if dry_run:
            report[status if status in ("new", "merged") else "unchanged"] += 1
            continue
        if write_if_changed(dst, content):
            report["synced"] += 1
            report["new" if status == "new" else "merged"] += 1
        else:
            report["unchanged"] += 1

    # 概念页从 topics.yaml 删掉后，vault 里的旧页会继续出现在关系图与搜索里。
    # 但它可能带用户批注——与 vault 对落选论文的处理同规矩：写过东西的只报告不删。
    if not dry_run and out_dir.exists():
        if unresolved:
            report["prune_skipped_reason"] = (
                "本轮 {} 个源文件读取/解析失败，无法确认其是否已从 topics.yaml 下线，"
                "跳过本轮 vault 侧旧概念页清理以避免误删：{}".format(
                    len(unresolved), "；".join(unresolved)))
        else:
            for stale in sorted(out_dir.glob("*.md")):
                if stale.stem in seen:
                    continue
                try:
                    _fm, body = split_frontmatter(stale.read_text(encoding="utf-8"))
                    zone = extract_user_zone(body or "")
                except Exception:
                    zone = None
                if zone is None or zone.replace(USER_HEADING, "").strip():
                    report["skipped"].append("{}（已下线但有批注，保留）".format(stale.name))
                else:
                    stale.unlink()
                    report["pruned"].append(stale.name)
    return report


def render_topics_index(topics_dir: Path, specs: Sequence[TopicSpec]) -> str:
    """topics/INDEX.md：概念页目录（对齐 notes_dir/INDEX.md 的角色）。

    **扫描磁盘现状而不是只看本次运行的结果**：`--topic X` 单跑一页时，若只把这一页
    写进目录，其余页会从目录里整体消失（页面文件还在，但没人找得到）。统计数字读各页
    frontmatter，与本次跑没跑过它无关。
    """
    topics_dir = Path(topics_dir)
    by_slug = {s.slug: s for s in specs}
    rows: List[Tuple[str, str]] = []
    retired: List[str] = []
    for p in sorted(topics_dir.glob("*.md")):
        if not is_topic_page_file(p):
            continue
        try:
            fm, _ = split_frontmatter(p.read_text(encoding="utf-8"))
        except OSError:
            continue
        # 认 `topic` 键而不是"有 frontmatter 就算"：split_frontmatter 对无 frontmatter 的
        # 文件返回空 dict（不是 None），用户随手放进 topics/ 的笔记会被列成概念页。
        if not isinstance(fm, dict) or not fm.get("topic"):
            continue
        slug = str(fm["topic"])
        spec = by_slug.get(slug)
        title = str(fm.get("title") or (spec.title if spec else slug))
        question = spec.question if spec else "（已从 topics.yaml 移除）"
        if not spec:
            retired.append(slug)
        rows.append((slug, "| [{}]({}.md) | {} | {} | {} | {} | {} |".format(
            title, slug, question[:40] + ("…" if len(question) > 40 else ""),
            fm.get("n_evidence", "—"), fm.get("n_papers", "—"),
            fm.get("n_claims", "—"),
            # `or "—"` 是真值判断——0 是合法值（真的没有分歧），却会被当成"缺失"
            # 显示成"—"（G7）。同行其余三列都用 .get(key, "—")，这里对齐。
            fm.get("n_disputes", "—"))))

    lines = ["# 概念页索引", "",
             "由 `scripts/build_topics.py` 生成。每页回答一个概念问题，论断均锚定到"
             "札记库的句级证据；新论文入库后重跑本脚本，页面随之更新。", "",
             "| 概念页 | 核心问题 | 证据 | 篇数 | 论断 | 分歧 |",
             "|---|---|---|---|---|---|"]
    lines += [row for _, row in rows]
    lines.append("")
    if retired:
        lines += ["> ⚠️ 以下页面已不在 `config/topics.yaml` 中，内容停在最后一次生成："
                  + "、".join(retired), ""]
    return "\n".join(lines)
