# -*- coding: utf-8 -*-
"""对札记库提一个问题，并把答案归档回库里（核心逻辑在 src/scholar/qa.py）。

`notes_search.py` 把相关句子捞给你看，答案在你脑子里、用完即弃；这里多走一步——
把「问题 + 答案 + 每条论断的句级出处 + 这次召回没覆盖到的部分」落成
`output/scholar_notes/topics/qa/<slug>.md`，让探索**复合**而不是每隔几个月重问一遍。

    python scripts/ask_notes.py "MNAR 诊断在纵向 EHR 上到底能不能做？"
    python scripts/ask_notes.py "..." -q "informative missingness" -q "缺失机制可检验性"
    python scripts/ask_notes.py "..." --dry-run     # 只看召回了什么，不调 LLM、不落盘
    python scripts/ask_notes.py --list              # 列出已归档的问答
    python scripts/ask_notes.py --verify            # 自检归档页的引用是否仍可追溯

同一个问题再问一次会**原地更新**那一页（slug 由问题文本决定），不会堆出第二份答案。
问之前会先查两件事，措辞接近的都会提示你：

  1. **已归档的问答**——这条是"探索复合"最直接的兑现，它拦住的正是「同一个问题被问第四次」；
  2. **概念页**（`config/topics.yaml`）——同一个问题若已有一页概念页在回答，
     那一页几乎总是更好的选择：证据面更宽、随新论文**自动重合成**。
     实测撞过一次：问答页 40 条证据 / 45% 引用率，而 `mnar-diagnosis` 是 70 条 / 100%，
     23/40 个 citekey 重合——**就那个问题而言，那 90 秒不值**。

两项查重都走语义（embedding），Ollama 不可用时降级回词面重合并明说降级了。

## 什么时候不该用它（三行路由）

  - 概念级、会反复用、希望自动保鲜 → **概念页**（必要时往 `config/topics.yaml` 加一条）
  - 临时的、具体的、一次性的       → **本脚本**
  - 只想看有哪些句子、自己判断     → `notes_search.py` / `notes_query.py`（role 硬门槛用后者）；
    写稿取证走 `scholar-write`

`-q/--query` 是**额外的检索词**，不改变问题本身：证据召回是每个 query 各检索一次后
合并，而不是拼成一句（拼接会把几个概念平均成一个语义中点，谁也召不准）。中文查询能
命中英文原文，但概念换述的召回率实测只有 ~30%，所以关键术语的中英两种写法都值得给。

## 重问：`--reask` / `--reask-all`（2026-09-17）

检索参数（`-q`/`--role`/`--min-sim`/`--max-evidence`/`--per-paper-cap`）从这一版起
随每一页归档进 frontmatter（`ask_query`/`ask_role`/`ask_min_sim`/`ask_max_evidence`/
`ask_per_paper_cap`），重问时不再需要自己记得当年是怎么问出来的：

    python scripts/ask_notes.py --reask qa-mnar-ehr-1f424623      # 重问一页，原样复现当年的检索
    python scripts/ask_notes.py --reask A --reask B                # 可重复，重问多页
    python scripts/ask_notes.py --reask-all                        # 打印「计划重问 N 页」，不真跑
    python scripts/ask_notes.py --reask-all --yes                  # 真的按顺序全部重问一遍
    python scripts/ask_notes.py --reask-all --dry-run               # 每页只跑召回，不调 LLM、不落盘

**合并语义**：命令行显式给出的 `-q`/`--role`/`--min-sim`/`--max-evidence`/
`--per-paper-cap` **覆盖**那一页的存档值（不是追加/合并成并集）；没显式给的字段
用存档值；那一页压根没存过参数（2026-09-17 之前生成的旧页）时退回问题本身 +
当前的默认/命令行参数，并打印一句明确提示。

`--reask-all` 默认只打印计划（多少页、每页问题）不动手，因为它可能一次触发几十次
LLM 调用；加 `--yes` 才会真的执行。`--dry-run` 与两者组合时**只跑召回**不调 LLM，
所以不受 `--yes` 门槛限制。`--reask`/`--reask-all` 批量跑时**单页失败不中断其余
页**，处理完打印一份汇总（成功/冲突/失败各多少页、各自是哪几页）。

退出码：0 成功 / 1 归档页有冲突或自检发现不可追溯的引用 /
2 配置/索引/向量库异常、slug 已被别的问题占用、校验后没有任何有出处的论断 /
3 Ollama embedding 不可用（证据召回依赖语义检索，无法降级）。
`--reask`/`--reask-all` 批量跑完后：本轮出现过冲突页则退 1，没有冲突但出现过失败页
则退 2（同 `scripts/lint_notes.py` 的既有优先级：发现优先于故障）；全部成功退 0。
"""
import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.paths import repo_path                                   # noqa: E402
from src.scholar.schema import ScholarSettings                            # noqa: E402
from src.scholar.embeddings import (                                      # noqa: E402
    EmbeddingClient, EmbeddingError, resolve_embedding_base_url,
)
from src.scholar.embed_store import (                                     # noqa: E402
    DB_NAME, INDEX_NAME, VectorStore, VectorStoreError, model_matches,
    read_index_generated_at,
)
from src.scholar.llm_client import LLMClient                              # noqa: E402
from src.scholar.research_profile import get_default_research_interests   # noqa: E402
from src.scholar.notes_index import load_index_file, write_if_changed     # noqa: E402
from src.scholar import qa as Q                                           # noqa: E402
from src.scholar.thresholds import QA_MIN_INDEPENDENT_SOURCES            # noqa: E402
from src.scholar import coverage as COV                                   # noqa: E402
from src.scholar import topics as T                                       # noqa: E402
from src.utils.logger import get_logger                                   # noqa: E402

logger = get_logger("ask_notes")

# 哨兵：区分"用户没给这个参数"与"用户给的值恰好等于代码默认值"。三个数值参数
# （--min-sim/--max-evidence/--per-paper-cap）与两个可重复参数（-q/--role）都要能
# 分辨这两种情形——重问时"命令行显式给出的参数覆盖存档值"这句承诺得靠这个哨兵
# 才立得住：如果 argparse 的 default 直接写成代码默认值，`--reask` 时压根看不出
# 用户到底有没有敲 --max-evidence，只能被迫"永远用命令行值覆盖"或"永远用存档值
# 覆盖"，两者都会悄悄违背"未显式给的字段用存档值"这条约定。
_UNSET = object()


def _load_index(index_path: Path):
    """读索引（单点实现在 notes_index.load_index_file，别再各写各的）。"""
    data, err = load_index_file(index_path)
    if err:
        print(err, file=sys.stderr)
    return data


# ---------------------------------------------------------------------------
# 检索参数解析：CLI 显式给出 > 存档值（--reask 才有）> 代码默认值
# ---------------------------------------------------------------------------

def _resolve_ask_params(args, archived: "Q.ArchivedAskParams"
                        ) -> Tuple[List[str], List[str], float, int, int]:
    """合并出这一次实际要用的五个检索参数。

    `archived` 对一次全新的提问传 `Q.ArchivedAskParams()`（`has_params=False`）
    即可复用同一份合并逻辑——"没有存档"与"这一页压根没存过参数"是同一件事，
    不必为全新提问单独写一套解析。

    顺序：命令行**显式**给出的值（`args.xxx is not _UNSET` / 不是 `None`）覆盖
    存档值；存档页有这个字段就用存档值；否则退回代码默认值。三档缺一不可——
    只有前两档、没有第三档的话，全新提问（archived.has_params 恒为 False）就
    拿不到任何默认值了。
    """
    extra_queries = (list(args.query) if args.query is not None
                     else (list(archived.extra_queries) if archived.has_params else []))
    # `--role all` = 显式清空角色限制（三个角色都要）。--reask 会沿用存档里的 role，而 argparse
    # 的 append 没有「清空」写法；2026-10-03 实测一次沿用的 `--role citable` 把「标成可反驳观点
    # 但含硬数字」的句子整批挡掉，却无从取消。
    if args.role is not None:
        roles = [] if "all" in args.role else list(args.role)
    else:
        roles = list(archived.roles) if archived.has_params else []
    min_sim = (args.min_sim if args.min_sim is not _UNSET else
              (archived.min_sim if archived.has_params and archived.min_sim is not None
               else T.DEFAULT_MIN_SIM))
    max_evidence = (args.max_evidence if args.max_evidence is not _UNSET else
                    (archived.max_evidence
                     if archived.has_params and archived.max_evidence is not None
                     else Q.DEFAULT_QA_MAX_EVIDENCE))
    per_paper_cap = (args.per_paper_cap if args.per_paper_cap is not _UNSET else
                     (archived.per_paper_cap
                      if archived.has_params and archived.per_paper_cap is not None
                      else Q.DEFAULT_QA_PER_PAPER_CAP))
    return extra_queries, roles, min_sim, max_evidence, per_paper_cap


def _validate_numeric_params(min_sim: float, max_evidence: int, per_paper_cap: int
                             ) -> Optional[str]:
    """合并结果落地前的最后一道校验，返回错误说明或 None。

    `--reask` 的合并结果可能来自**存档值**（理论上应当合法，因为写的时候就经过
    这道检查了）而不是这一次的命令行——但防御性检查成本几乎为零，留着比信任
    磁盘上的数字更稳：手改 frontmatter 把 `ask_per_paper_cap` 改成 0 这种情形，
    早发现比在 `retrieve_qa_evidence` 内部炸出 TopicError 更容易定位。

    M2（2026-09-17 R1 对抗审核）：`min_sim` 补上这一档，与另外两个参数**同等对待**
    ——硬拒绝，不是 `qa._fm_num` 那种"静默退化成没存过再回落默认值"。两处校验
    职责不同：`_fm_num` 管"这个从磁盘读出来的值能不能信"，读不出来就当没存过，
    静默退化是对的（旧页本来就没这个字段，不该报错）；这里管的是"这一次真的要
    拿去检索的值对不对"——不管它是命令行敲的、还是从存档一路合并过来的，到了
    这一步就已经决定要用它了，NaN/Inf/越界还硬跑下去只会在 `retrieve_evidence`
    里悄悄把相似度下限废掉（`max(nan, x)` 恒假），比直接报错更难查。
    """
    if math.isnan(min_sim) or math.isinf(min_sim):
        return "min_sim 不能是 NaN/Inf（收到 {}）".format(min_sim)
    if not (-1.0 <= min_sim <= 1.0):
        return "min_sim 必须落在 [-1, 1] 区间内（收到 {}，这是余弦相似度的合法范围）".format(
            min_sim)
    if per_paper_cap <= 0:
        return ("per_paper_cap 必须 ≥ 1（收到 {}）。想「不限制」就给一个大于 "
                "max_evidence 的数".format(per_paper_cap))
    # MINOR-4（2026-09-17 R2 对抗审核）：上界与 `qa.ASK_NUMERIC_UPPER_BOUND` 同一个
    # 常量——存档侧（`qa._fm_num`）越界退化成 None+warning，这里是 CLI 侧硬拒绝，
    # 两个方向的"多大算离谱"必须对得上，不能各写一份数字。
    if per_paper_cap > Q.ASK_NUMERIC_UPPER_BOUND:
        return "per_paper_cap 超出合法上界 {}（收到 {}）".format(
            Q.ASK_NUMERIC_UPPER_BOUND, per_paper_cap)
    if max_evidence <= 0:
        return "max_evidence 必须 ≥ 1（收到 {}）".format(max_evidence)
    if max_evidence > Q.ASK_NUMERIC_UPPER_BOUND:
        return "max_evidence 超出合法上界 {}（收到 {}）".format(
            Q.ASK_NUMERIC_UPPER_BOUND, max_evidence)
    return None


# ---------------------------------------------------------------------------
# 单次问答：召回 -> 合成 -> 校验 -> 落盘。`--reask` 与一次全新提问共用这一份实现
# ——两者的差别只在"question/slug 从哪来、检索参数怎么合并"，问完之后的流程
# 逐字相同，写两份必然会在某一次改动时只改中一份（本仓库反复出现的教训）。
# ---------------------------------------------------------------------------

@dataclass
class AskOutcome:
    """一次问答的落地结果（供单次调用打印摘要，也供 --reask/--reask-all 批量汇总）。

    status ∈ new / merged / unchanged / conflict / taken / skipped（0 证据）/
    dry_run / failed。**failed 是本文件自己新增的一档**（原单问题流程遇到这些
    情形直接 `return 2`），批量场景不能让一页的失败中断其余页，所以必须有个
    "记下来、继续下一页"的落点。
    """
    slug: str
    question: str
    status: str
    detail: str = ""
    n_evidence: int = 0
    n_papers: int = 0


def _run_one(question: str, slug: str, *, qa_dir: Path, index: dict, titles: Dict[str, str],
            topic_specs: Sequence[Any], template: str, store, embed_client,
            settings: ScholarSettings, topic_vecs,
            extra_queries: Sequence[str], roles: Sequence[str],
            min_sim: float, max_evidence: int, per_paper_cap: int,
            model: Optional[str], dry_run: bool,
            no_dedup_check: bool, no_topic_check: bool, show: int = 10,
            audit_mode: str = "theory", audit_model: Optional[str] = None,
            strict: bool = False, audit_template: Optional[str] = None,
            theory: Optional[bool] = None,
            theory_min_sim: float = Q.DEFAULT_THEORY_MIN_SIM,
            theory_max: int = Q.DEFAULT_THEORY_MAX_BACKFILL,
            force_synthesize: bool = False) -> AskOutcome:
    """问一个问题（`question`）、落到确定的 `slug`。`store`/`embed_client`/
    `topic_specs`/`template`/`topic_vecs` 都是调用方已经准备好、可能跨多次调用
    复用的资源，本函数不负责创建或关闭它们（`embed_client.close()` 由最外层的
    `main()` 在全部问题问完之后统一做一次）。

    与旧版单问题流程唯一的行为差异：不良结局不再让整个进程 `sys.exit`，而是
    包成 `AskOutcome(status="failed"/"taken"/...)` 返回——`main()` 据此决定单个
    问题时的退出码，或在批量场景里继续处理下一页。

    WO-5 证据充分性闸门（分布判据，见 thresholds「证据充分性」）：召回之后、合成之前，按
    该问题自身的全库相似度分布判一次，独立来源不足时**不调 LLM**，归档「库内证据不足」存根页
    （`status="insufficient"`，退出码 0，存根页本身就是一个答案：问过了，库里不够）。
    `force_synthesize` 跳过闸门照常合成，页面标注“证据不足时被强制生成”。
    """
    try:
        spec = Q.build_qa_spec(question, extra_queries=extra_queries, roles=roles,
                               max_evidence=max_evidence)
    except T.TopicError as e:
        print("❌ {}".format(e), file=sys.stderr)
        return AskOutcome(slug=slug, question=question, status="failed", detail=str(e))
    spec.slug = slug

    # slug 占用检查放在最前：它不花钱、不联网，而它拦住的是最坏的一种——
    # 拿别人那一页的答案当"上一版"喂给这次的 prompt，让模型去融合两个不相干的问题。
    # 对 --reask 这一路是纯防御性的：question 本来就是从这一页自己的 frontmatter
    # 读出来的，正常情况下这里必然一致；只有磁盘在两次读之间被并发改动才会触发。
    taken_by = Q.archived_question(qa_dir, slug)
    if taken_by and Q.question_key(taken_by) != Q.question_key(question):
        print("❌ slug '{}' 已经属于另一个问题：\n   «{}»\n"
              "   换一个 --slug，或者留空让它按问题自动生成。\n"
              "   （要更新那一页就用它自己的问题重问：{}）".format(
                  slug, taken_by, Q.update_command(slug, taken_by)), file=sys.stderr)
        return AskOutcome(slug=slug, question=question, status="taken", detail=taken_by)

    related_topics: List[str] = []
    if not no_dedup_check:
        res = Q.find_similar_questions(qa_dir, question, exclude_slug=slug,
                                       embed_client=embed_client)
        if res.degraded:
            print("⚠️ {}（已知盲区见 qa.question_similarity 的 docstring）".format(
                res.degraded), file=sys.stderr)
        if res.hits:
            print("💡 你问过意思接近的问题（{}）：".format(
                "语义" if res.mode == "embedding" else "词面"))
            for p, score in res.hits:
                print("   {:.0%} 像 · {} · {}".format(score, p.slug, p.title))
                print("        {}".format(p.path))
            top = res.hits[0][0]
            print("   要**更新那一页**（而不是另存一页）：\n     {}".format(
                Q.update_command(top.slug, top.question or top.title)))
            print("   确认是新问题就继续，本次会另存一页。\n")

    if not no_topic_check:
        hits = Q.find_similar_topics(topic_specs, question, embed_client, topic_vecs=topic_vecs)
        related_topics = [s.slug for s, _sc in hits]
        for s, sc in hits:
            print("📎 `topics/{}.md`（{}）已经在回答这个问题（{:.0%} 像）——"
                  "它证据面更宽、随新论文**自动重合成**。".format(s.slug, s.title, sc))
        if hits:
            print("   概念级、会反复用的问题就去读那一页；只是临时想问一句才继续。\n")

    print("🔎 «{}»  →  {}".format(question, slug))
    print("   参数：角色={} | min-sim={} | per-paper-cap={} | max-evidence={} | -q×{}".format(
        "/".join(roles) if roles else "全部", min_sim, per_paper_cap, max_evidence,
        len(extra_queries)))
    recall = Q.recall_qa_evidence(spec, store, embed_client, min_sim=min_sim,
                                  per_paper_cap=per_paper_cap, theory=theory,
                                  theory_min_sim=theory_min_sim,
                                  theory_max_backfill=theory_max)
    evidences, nearby = recall.evidences, recall.nearby
    if recall.theory_mode:
        print("📐 理论模式：从 {} 篇补捞 {} 条定理/命题句（阈值 {}）".format(
            recall.theory_papers, recall.theory_backfilled, theory_min_sim))
    if recall.roles_counted:
        print("⚠️ 角色过滤（{}）挡掉 {} 条高分证据，其中 {} 条含数字；"
              "问数字/事实时考虑 --role all".format(
                  "/".join(roles), recall.role_filtered_out, recall.role_filtered_numeric))
    recall_stats = {"theory_mode": recall.theory_mode,
                    "theory_backfilled": recall.theory_backfilled,
                    "role_filtered_out": recall.role_filtered_out,
                    "role_filtered_numeric": recall.role_filtered_numeric}
    if not evidences:
        print("⚠️ 召回 0 条证据（相似度阈值 {}）。这多半是真的——库里没有这方面的句级"
              "证据。可以试：降低 --min-sim、用 -q 补几个别的说法（尤其英文写法）、"
              "或先跑 `scripts/notes_search.py <关键词>` 看看有没有只有标题级的命中。"
              .format(min_sim))
        return AskOutcome(slug=slug, question=question, status="skipped",
                          detail="召回 0 条证据")
    T.attach_titles(evidences, index)
    n_papers = len({e.citekey for e in evidences})
    print("   证据 {} 条 / {} 篇（相似度 {:.2f}~{:.2f}）".format(
        len(evidences), n_papers, evidences[-1].score, evidences[0].score))

    ctx = COV.build_context(index, certification_path=repo_path("config/filter_eval/certification.json"))
    suff = COV.qa_dynamic_verdict(spec, store, embed_client, ctx.keeper_of)
    ctx.sufficiency = suff
    d = suff.detail
    print("📏 证据充分性（分布判据）：独立来源 {} 篇达标（要求 ≥ {}），动态阈值 {:.3f}{}，最高来源 {:.3f}  → {}".format(
        suff.n_sources, suff.min_sources, suff.sim_threshold,
        "（下限起作用）" if d.get("floor_binding") else "", suff.top1,
        "不足" if suff.refuse else "充足"))
    stub_command = "{} --force-synthesize".format(Q.update_command(slug, question))
    if suff.refuse and not force_synthesize:
        if dry_run:
            print("【dry-run】证据不足：真跑会归档存根页（不调用 LLM）。归档路径会是：{}/{}.md".format(
                qa_dir, slug))
            return AskOutcome(slug=slug, question=question, status="dry_run",
                              n_evidence=len(evidences), n_papers=n_papers)
        ctx.status = COV.EVIDENCE_STATUS_INSUFFICIENT
        stub_qa = {"answer": "", "points": [], "caveats": [], "gaps": [], "inline_refs": []}
        path, status = Q.write_qa_page(
            qa_dir, slug, question, stub_qa, evidences, Q.QAValidationReport(),
            related_topics=related_topics, nearby_papers=nearby, titles=titles,
            extra_queries=extra_queries, roles=roles, min_sim=min_sim,
            max_evidence=max_evidence, per_paper_cap=per_paper_cap, recall_stats=recall_stats,
            coverage=ctx, stub=suff, force_command=stub_command)
        if status == "regressed":
            msg = ("证据数倒退：这一页以前有足够的独立来源，这次判不足（{} < {}）——多半是向量库/阈值"
                   "故障而非真实状态，未覆盖。先查向量库同步；确认无误可加 --force-synthesize。".format(
                       suff.n_sources, suff.min_sources))
            print("❌ {}".format(msg), file=sys.stderr)
            return AskOutcome(slug=slug, question=question, status="failed", detail=msg,
                              n_evidence=len(evidences), n_papers=n_papers)
        if status == "taken":
            print("❌ slug '{}' 在这次归档期间被另一个问题占用了，本次未落盘。".format(slug),
                  file=sys.stderr)
            return AskOutcome(slug=slug, question=question, status="taken",
                              n_evidence=len(evidences), n_papers=n_papers)
        print("\n📭 库内证据不足（独立来源 {} < {}），未调用 LLM，已归档存根页：{}（{}）".format(
            suff.n_sources, suff.min_sources, path, status))
        print("   认为是误拒：{}".format(stub_command))
        if status == "conflict":
            print("   哨兵缺失或生成块被手改，未覆盖——把批注移到「我的批注」下后重跑。",
                  file=sys.stderr)
            return AskOutcome(slug=slug, question=question, status="conflict",
                              n_evidence=len(evidences), n_papers=n_papers)
        return AskOutcome(slug=slug, question=question, status="insufficient",
                          detail="独立来源 {} < {}".format(suff.n_sources, suff.min_sources),
                          n_evidence=len(evidences), n_papers=n_papers)
    ctx.status = COV.EVIDENCE_STATUS_FORCED if suff.refuse else COV.EVIDENCE_STATUS_OK

    if dry_run:
        show = max(1, int(show))
        for e in evidences[:show]:
            print("   {:<4} {:.3f} [@{}] {}{}".format(
                e.ref, e.score, e.citekey, Q.THEORY_BACKFILL_TAG if getattr(e, "theory_backfill", False) else "",
                e.text[:70]))
        if len(evidences) > show:
            print("   …… 还有 {} 条（--dry-run 只列前 {}，--show N 可调）".format(
                len(evidences) - show, show))
        print("【dry-run】未调用 LLM、未落盘。归档路径会是：{}/{}.md".format(qa_dir, slug))
        return AskOutcome(slug=slug, question=question, status="dry_run",
                          n_evidence=len(evidences), n_papers=n_papers)

    prompt = Q.build_qa_prompt(question, evidences, template,
                               get_default_research_interests(),
                               Q.previous_answer(qa_dir, slug, question))
    llm = LLMClient(settings.llm)
    synth_meta: Dict[str, Any] = {}
    try:
        try:
            data = Q.synthesize_qa(llm, prompt, model=model, meta_out=synth_meta)
        except T.TopicError as e:
            print("❌ 合成失败（召回本身正常：{} 条证据 / {} 篇）：{}".format(
                len(evidences), n_papers, e), file=sys.stderr)
            return AskOutcome(slug=slug, question=question, status="failed",
                              detail=str(e), n_evidence=len(evidences), n_papers=n_papers)

        qa, report = Q.validate_qa(data, evidences)
        qa["gap_hits"] = Q.recheck_gaps(qa["gaps"], store=store, embed_client=embed_client,
                                        topic_specs=topic_specs, topic_vecs=topic_vecs,
                                        titles=titles,
                                        exclude_citekeys={e.citekey for e in evidences})
        # v3 B：蕴含审计（复用同一个 llm；失败降级为「未经审计」，绝不阻断归档）
        if audit_mode == "off":
            Q.run_audit(llm, qa, evidences, "", question=question, mode="off")
        else:
            try:
                a_tpl = audit_template or Q.load_audit_template(
                    repo_path(Q.DEFAULT_AUDIT_PROMPT))
            except T.TopicError as e:
                qa["audit"] = {"status": "failed", "mode": audit_mode, "reason": str(e),
                               "strict": bool(strict), "n_audited": 0, "n_unfaithful": 0,
                               "n_uncertain": 0, "dropped": []}
            else:
                Q.run_audit(llm, qa, evidences, a_tpl, question=question, mode=audit_mode,
                            model=audit_model or model, strict=strict, report=report)
    finally:
        llm.close()
    ctx.synth = COV.synth_fields(synth_meta, model)

    if not Q.is_archivable(qa):
        print("❌ 校验后没有任何带出处的论断（丢弃 {} 条，非法编号 {} 个）——"
              "多半是模型没按编号引用。重跑一次通常就好。".format(
                  report.dropped_claims, report.invalid_refs), file=sys.stderr)
        return AskOutcome(slug=slug, question=question, status="failed",
                          detail="校验后没有任何带出处的论断", n_evidence=len(evidences),
                          n_papers=n_papers)

    path, status = Q.write_qa_page(qa_dir, slug, question, qa, evidences, report,
                                   related_topics=related_topics, nearby_papers=nearby,
                                   titles=titles, extra_queries=extra_queries, roles=roles,
                                   min_sim=min_sim, max_evidence=max_evidence,
                                   per_paper_cap=per_paper_cap, recall_stats=recall_stats,
                                   coverage=ctx)
    if status == "taken":
        print("❌ slug '{}' 在这次合成期间被另一个问题占用了，本次未落盘。".format(slug),
              file=sys.stderr)
        return AskOutcome(slug=slug, question=question, status="taken",
                          n_evidence=len(evidences), n_papers=n_papers)
    icon = {"new": "🆕", "merged": "✅", "unchanged": "＝", "conflict": "⚠️"}.get(status, "?")
    if ctx.status == COV.EVIDENCE_STATUS_FORCED:
        print("⚠️ 证据不足时被强制生成（--force-synthesize）：页面已标注，结论请当线索而非定论。")

    print()
    if qa["answer"]:
        print("💬 {}".format(qa["answer"]))
    print("   依据 {} 条 · 注意事项 {} 条 · 本次没覆盖到 {} 条 · 引用率 {:.0%}".format(
        len(qa["points"]), len(qa["caveats"]), len(qa["gaps"]),
        report.used_refs / len(evidences) if evidences else 0.0))
    flagged = [(g, h) for g, h in (qa.get("gap_hits") or {}).items() if h]
    for g, hits in flagged:
        print("   ⚠️ 「{}」——但库里可能有：{}".format(
            g[:40], "、".join(
                ("topics/{}.md".format(h.key) if h.kind == "topic" else "[@{}]".format(h.key))
                for h in hits)))
    flags = []
    ainfo = qa.get("audit") or {}
    n_theory = sum(1 for it in (qa["points"] + qa["caveats"]) if it.get("theory"))
    n_ungrounded = sum(1 for it in (qa["points"] + qa["caveats"]) if it.get("ungrounded"))
    if ainfo.get("status") == "failed":
        print("   ⚠️ 蕴含审计失败，本页未经审计：{}".format(ainfo.get("reason")))
    elif ainfo.get("status") == "ok":
        print("   🔍 蕴含审计 {} 条（理论类 {}）：不忠实 {} · 无法判断 {}{}".format(
            ainfo.get("n_audited", 0), n_theory, ainfo.get("n_unfaithful", 0),
            ainfo.get("n_uncertain", 0),
            "（--strict 已丢弃不忠实论断）" if ainfo.get("strict") and ainfo.get("dropped") else ""))
    if n_ungrounded:
        print("   ⚠️ {} 条论断含所引证据里找不到的数字（页面上已逐条标出）".format(n_ungrounded))
    if report.dropped_claims:
        flags.append("丢弃 {} 条无出处论断".format(report.dropped_claims))
    if report.invalid_refs:
        flags.append("非法编号 {}".format(report.invalid_refs))
    if report.stripped_cites:
        flags.append("剥离模型自写引用 {}".format(report.stripped_cites))
    if flags:
        print("   ⚑ {}".format("，".join(flags)))
    print("\n{} 归档：{}（{}）".format(icon, path, status))
    if status == "conflict":
        print("   哨兵缺失或生成块被手改，未覆盖——把批注移到「我的批注」下后重跑。",
              file=sys.stderr)
    return AskOutcome(slug=slug, question=question, status=status,
                      n_evidence=len(evidences), n_papers=n_papers)


# ---------------------------------------------------------------------------
# --reask：把一个已归档 slug 解析成 (question, slug, 五个检索参数)
# ---------------------------------------------------------------------------

def _load_reask_target(qa_dir: Path, raw_slug: str, args
                       ) -> Tuple[Optional[Tuple[str, str, List[str], List[str], float, int, int]],
                                  Optional[str]]:
    """把一个 `--reask` 目标（用户敲的原始字符串）解析成
    `(question, slug, extra_queries, roles, min_sim, max_evidence, per_paper_cap)`；
    解析失败时返回 `(None, 失败原因)`，调用方据此记一条 failed outcome 并继续下一个。

    `Q.qa_slug("", explicit=raw_slug)`：`explicit` 分支完全不看 `question` 参数
    （只做形状校验/归一化，见 `qa.qa_slug` 的实现），借它一并归一化用户输入
    （容许漏打 `qa-` 前缀、大小写不一致，同 `--slug` 现有的宽容度）。**这条路径
    从不碰 `qa_dir`**——`qa_slug` 的"digest 口径迁移"回退（拿问题文本重算新
    digest、发现磁盘上没有新文件、扫存量页按 `question_key` 认领旧文件）只在
    "拿问题文本走非 explicit 分支"时生效；`--reask` 给的是 slug 不是问题文本，
    这条既有回退天然覆盖不到（MINOR-1，2026-09-17 R2 对抗审核）。

    真实场景：`qa-mnar-ehr-1f424623.md` 是 2026-08-17 之前的旧 digest 口径
    文件名，从未被重命名过（`qa_slug` 的迁移回退向来是"沿用旧文件名"，不是
    "改名成新文件名"，见该函数 docstring）；用户如果拿"按今天的算法重新计算
    会得到的 slug"（`qa-mnar-ehr-82bb128f`）来 `--reask`，会因为这个文件名
    在磁盘上从不存在而报"没有已归档页面"，即便那个问题明明已经归档过。
    """
    try:
        slug = Q.qa_slug("", explicit=raw_slug)
    except T.TopicError as e:
        return None, "slug 不合法：{}".format(e)
    text, fm, _prev = Q.read_existing(qa_dir / "{}.md".format(slug))
    if text is None:
        # 反查：对存量每一页重算"如果按今天的算法从它的问题文本重新生成 slug，
        # 会不会正好等于用户输入的这个"——找到就说明那一页身份相同，只是文件名
        # 停留在更旧的 digest 口径上。**不传 `qa_dir=`**：那会触发 `qa_slug` 自己
        # 的迁移回退（"新文件不存在就沿用旧文件名"），对每一页都会算出**它自己
        # 的**旧文件名而不是"今天的算法会给它取的新名字"，与这里想问的问题
        # （"这个用户输入的新名字属于哪一页"）方向正好相反。
        fallback = None
        for page in Q.list_qa_pages(qa_dir):
            if not page.question:
                continue
            try:
                recomputed = Q.qa_slug(page.question)
            except T.TopicError:
                continue
            if recomputed == slug:
                fallback = page
                break
        if fallback is None:
            return None, ("没有已归档页面：{}.md（先跑 --list 看看实际文件名——"
                          "如果是 2026-08-17 之前的旧页，文件名可能还停留在"
                          "更早的 digest 口径上）".format(slug))
        print("💡 {} 没有直接对应的文件，已按问题身份映射到 {}"
              "（旧 digest 口径文件名，从未被重命名过）。".format(slug, fallback.slug))
        slug = fallback.slug
        text, fm, _prev = Q.read_existing(fallback.path)
    if fm is None:
        return None, "frontmatter 解析失败（YAML 损坏？），无法读取问题与参数"
    question = str(fm.get("question") or "").strip()
    if not question:
        return None, "frontmatter 缺 question 键，无法重问"
    archived = Q.read_archived_ask_params(fm)
    if not archived.has_params:
        # MAJOR-R3-1（2026-09-17 R3 对抗审核）：不能断言"2026-09-17 之前的产物"——
        # has_params=False 也可能是哨兵字段（ask_max_evidence）本身被手改坏了，
        # 那种情形下页面完全可能是本轮功能上线**之后**生成的，断言日期是事实错误。
        # `Q.read_archived_ask_params` 内部已经对"哨兵存在但非法"单独打过 warning，
        # 这里只说"没读到"，不猜原因。
        print("⚠️ {} 未读到有效的检索参数（旧页或字段异常）："
              "本次重问只用它的问题本身 + 当前的默认/命令行参数。".format(slug))
    extra_queries, roles, min_sim, max_evidence, per_paper_cap = _resolve_ask_params(
        args, archived)
    return (question, slug, extra_queries, roles, min_sim, max_evidence, per_paper_cap), None


_BATCH_ICON = {"new": "🆕", "merged": "✅", "unchanged": "＝", "conflict": "⚠️",
              "taken": "❌", "skipped": "⏭️", "failed": "❌", "dry_run": "🔍",
              "insufficient": "📭"}
_BATCH_ORDER = ("new", "merged", "unchanged", "insufficient", "dry_run", "skipped", "conflict",
                "taken", "failed")


def _print_batch_summary(outcomes: Sequence[AskOutcome]) -> None:
    """`--reask`/`--reask-all` 跑完后的汇总：**单页失败不能只体现在滚动过去的
    stdout 里**——几十页一起跑，中间某一页的失败信息早被后面几十页的输出冲走，
    不专门汇总一次，"这次批量重问到底哪几页没成功"这件事实际上无从得知。
    """
    print("\n{}".format("=" * 60))
    print("重问汇总（共 {} 页）".format(len(outcomes)))
    for st in _BATCH_ORDER:
        items = [o for o in outcomes if o.status == st]
        if not items:
            continue
        print("  {} {}（{} 页）：{}".format(
            _BATCH_ICON.get(st, "?"), st, len(items), "、".join(o.slug for o in items)))
    failed = [o for o in outcomes if o.status == "failed"]
    if failed:
        print("\n失败详情：")
        for o in failed:
            print("  {}：{}".format(o.slug, o.detail))
    conflicted = [o for o in outcomes if o.status == "conflict"]
    if conflicted:
        print("\n以下页生成块被手改，未覆盖（把批注移到「我的批注」下后重跑）：{}".format(
            "、".join(o.slug for o in conflicted)))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="对札记库提问，并把答案归档回 topics/qa/（探索复合，不再用完即弃）")
    ap.add_argument("question", nargs="*", help="要问的问题（一句话）")
    ap.add_argument("-q", "--query", action="append", default=None, metavar="TERM",
                    help="额外的检索词（可重复）：每个词各检索一次后合并，不改变问题本身。"
                         "关键术语的中英两种写法都给，召回会明显变好。"
                         "配合 --reask：给了就整体覆盖那一页的存档检索词，不是追加")
    ap.add_argument("--slug", default=None,
                    help="指定归档文件名（小写 ASCII 字母数字与连字符；自动加 qa- 前缀）。"
                         "缺省按问题文本自动生成——同一个问题因此总是落在同一页上。"
                         "不能与 --reask/--reask-all 同用（那两者已经知道目标 slug）")
    ap.add_argument("--config", default="config/scholar.env")
    ap.add_argument("--prompt", default=Q.DEFAULT_PROMPT, help="合成 prompt 模板")
    ap.add_argument("--max-evidence", type=int, default=_UNSET,
                    help="喂给 LLM 的证据上限（默认 {}，上界 {}——超过按越界拒绝，"
                         "不是静默截断）。配合 --reask：给了就覆盖那一页的"
                         "存档值，不给就沿用存档值（旧页则退回这个默认值）".format(
                             Q.DEFAULT_QA_MAX_EVIDENCE, Q.ASK_NUMERIC_UPPER_BOUND))
    ap.add_argument("--min-sim", type=float, default=_UNSET,
                    help="召回相似度下限（默认 {}，句级证据口径；paper 级链路是另一套取值，"
                         "别互相照抄，标定依据见 src/scholar/thresholds.py）。"
                         "合法范围 [-1, 1]（余弦相似度本身的界，不是 {} 那个通用数值"
                         "上界——[-1, 1] 更紧，以它为准）。"
                         "配合 --reask 的覆盖/沿用语义同 --max-evidence。"
                         "负值/inf 请用 `--min-sim=-0.5`/`--min-sim=-inf` 这种等号写法——"
                         "argparse 只认得形如 -1.5 的负数会被当成值，"
                         "-inf 没有数字，空格分开的 `--min-sim -inf` 会被误认成一个"
                         "未知选项".format(T.DEFAULT_MIN_SIM, Q.ASK_NUMERIC_UPPER_BOUND))
    ap.add_argument("--per-paper-cap", type=int, default=_UNSET,
                    help="单篇论文最多贡献几条证据（默认 {}，上界 {}；比概念页的 {} 小是"
                         "**有意的**，一个具体问题要的是把最相关那几篇挖深，不是每篇各"
                         "一句）。配合 --reask 的覆盖/沿用语义同 --max-evidence".format(
                             Q.DEFAULT_QA_PER_PAPER_CAP, Q.ASK_NUMERIC_UPPER_BOUND,
                             T.DEFAULT_PER_PAPER_CAP))
    ap.add_argument("--topics-config", default="config/topics.yaml",
                    help="概念页配置；用来提示「这个问题已经有一页概念页在回答」，"
                         "以及给 gaps 回查提供概念页问题。读不出就跳过这两项")
    ap.add_argument("--role", action="append", default=None,
                    choices=["citable", "refutable", "method", "all"],
                    help="只用该角色的证据（可重复）；缺省三者都要。`--role all` 显式清空角色限制"
                         "（--reask 默认沿用存档里的 role，想去掉它就用这个）。"
                         "注意：citable 会挡掉「标成可反驳观点但含硬数字」的句子，问数字/事实时别限角色。"
                         "配合 --reask 的覆盖/沿用语义同 --max-evidence")
    ap.add_argument("--show", type=int, default=10, metavar="N",
                    help="--dry-run 时列出前 N 条证据（默认 10；想确认某篇有没有被召回就调大）")
    ap.add_argument("--model", default=None, help="覆盖合成用的 LLM 模型")
    ap.add_argument("--audit", choices=list(Q.AUDIT_MODES), default="theory",
                    help="蕴含审计（一次额外 LLM 调用，逐条判转述是否忠实于所引原句）："
                         "theory=只审理论类论断（默认）、all=审全部依据与注意事项、off=关闭。"
                         "审计失败不阻断归档，页面会明说「未经审计」")
    ap.add_argument("--audit-model", default=None,
                    help="审计用的 LLM 模型（缺省与 --model/合成相同）")
    ap.add_argument("--strict", action="store_true",
                    help="审计判「不忠实」的论断直接丢弃（默认只标 ❌ 不删），"
                         "被丢的原文与理由列在页尾审计记录里")
    ap.add_argument("--theory", dest="theory", action="store_true", default=None,
                    help="理论模式：对候选池里全部论文的定理/命题类句子按自身得分补捞"
                         "（放宽阈值，见 --theory-min-sim；补捞句在证据表标「（理论补捞）」）。"
                         "缺省按问题/-q 是否命中理论关键词（定理/保证/下界/可识别/不可能…）自动触发")
    ap.add_argument("--no-theory", dest="theory", action="store_false",
                    help="关闭理论模式（即使问题命中理论关键词）")
    ap.add_argument("--theory-min-sim", type=float, default=Q.DEFAULT_THEORY_MIN_SIM,
                    metavar="X", help="理论补捞的放宽相似度阈值（默认 {}，每篇上限 {} 句）".format(
                        Q.DEFAULT_THEORY_MIN_SIM, Q.DEFAULT_THEORY_PER_PAPER))
    ap.add_argument("--theory-max", type=int, default=Q.DEFAULT_THEORY_MAX_BACKFILL, metavar="N",
                    help="理论补捞的总条数上限（默认 {}，按句分数从高到低取）".format(
                        Q.DEFAULT_THEORY_MAX_BACKFILL))
    ap.add_argument("--no-dedup-check", action="store_true",
                    help="跳过「这个问题是不是问过了」的查重提示（不影响概念页比对）")
    ap.add_argument("--no-topic-check", action="store_true",
                    help="跳过「已经有一页概念页在回答这个问题」的比对。注意它同时会让"
                         "归档页顶部那条「相关概念页」消失——两件事是同一份数据")
    ap.add_argument("--force-synthesize", action="store_true",
                    help="证据不足（分布判据：独立来源 < {}）时也照常调 LLM 合成，页面标注“证据不足时被强制生成”。"
                         "默认遇到证据不足会归档存根页、不调 LLM（退出码仍为 0）".format(
                             QA_MIN_INDEPENDENT_SOURCES))
    ap.add_argument("--dry-run", action="store_true",
                    help="只跑召回，看能捞到多少证据，不调 LLM、不落盘。"
                         "与 --reask/--reask-all 组合时对每一页都只跑召回")
    ap.add_argument("--list", action="store_true", help="列出已归档的问答后退出")
    ap.add_argument("--verify", action="store_true",
                    help="只自检已归档页（死键/失锚/正文残留证据编号/可达性/防线版本），"
                         "不调 LLM、不联网、不改文件")
    ap.add_argument("--reask", action="append", default=[], metavar="SLUG",
                    help="重问已归档的某一页（可重复）：读它存档的问题与检索参数原样重问，"
                         "落回同一页。命令行显式给出的检索参数覆盖存档值。"
                         "不能与位置参数问题/--slug/--reask-all 同用")
    ap.add_argument("--reask-all", action="store_true",
                    help="按最近更新排序逐页重问全部已归档问答（同 --list 的排序）。"
                         "默认只打印计划不动手，加 --yes 才真的执行（LLM 有成本）；"
                         "--dry-run 时不受 --yes 门槛限制，因为它不调 LLM")
    ap.add_argument("--yes", action="store_true",
                    help="配合 --reask-all：跳过确认，真的执行（而不是只打印计划）")
    args = ap.parse_args()

    reask_mode = bool(args.reask) or args.reask_all
    if args.reask and args.reask_all:
        ap.error("--reask 与 --reask-all 不能同时使用（--reask-all 已经覆盖全部已归档页）")
    if reask_mode and any(w and w.strip() for w in args.question):
        ap.error("--reask/--reask-all 不能与位置参数问题同时使用——它们已经带着"
                 "各自要重问的问题，另给一个新问题没有意义")
    if reask_mode and args.slug:
        ap.error("--reask/--reask-all 不能与 --slug 同时使用——它们已经知道目标 slug")
    # 提前校验：这两个数不合法时 `retrieve_qa_evidence` 会抛 TopicError，而那发生在
    # **整轮宽召回跑完之后**，而且以裸 traceback + 退出码 1（文档里 1 的含义是
    # "归档页有冲突"）呈现。只在**显式给了**时才检查——`_UNSET` 交给 `_resolve_ask_params`
    # 去决定最终值，那里（`_validate_numeric_params`）会在合并结果落地前再查一次。
    if args.per_paper_cap is not _UNSET and args.per_paper_cap <= 0:
        ap.error("--per-paper-cap 必须 ≥ 1（收到 {}）。想「不限制」就给一个大于 "
                 "--max-evidence 的数".format(args.per_paper_cap))
    if args.theory_max < 0:
        ap.error("--theory-max 不能为负（收到 {}）".format(args.theory_max))
    if not (-1.0 <= args.theory_min_sim <= 1.0) or args.theory_min_sim != args.theory_min_sim:
        ap.error("--theory-min-sim 必须落在 [-1, 1] 区间内（收到 {}）".format(args.theory_min_sim))
    if args.max_evidence is not _UNSET and args.max_evidence <= 0:
        ap.error("--max-evidence 必须 ≥ 1（收到 {}）".format(args.max_evidence))

    cfg = repo_path(args.config)
    settings = ScholarSettings.from_env_file(cfg) if cfg.exists() else ScholarSettings()
    notes_dir = repo_path(settings.processing.notes_dir)
    qa_dir = Q.qa_dir_for(notes_dir)
    index_path = notes_dir / INDEX_NAME

    if args.list:
        pages = Q.list_qa_pages(qa_dir)
        if not pages:
            print("还没有归档过任何问答：{}".format(qa_dir))
            return 0
        print("已归档 {} 条问答（{}）：\n".format(len(pages), qa_dir))
        for p in pages:
            print("  {:<10} {}".format((p.generated_at or "")[:10], p.title))
            print("  {:<10} └─ {}.md · 依据 {} 条 · 证据 {} 条 · 首次问 {}".format(
                "", p.slug, p.n_points, p.n_evidence, (p.first_asked_at or "?")[:10]))
        return 0

    index = _load_index(index_path)
    if index is None:
        return 2

    if args.verify:
        audits = Q.audit_qa_pages(qa_dir, index)
        if not audits:
            print("没有已归档的问答：{}".format(qa_dir))
            return 0
        print("自检 {} 页（索引 generated_at = {}；当前防线版本 v{}）\n".format(
            len(audits), index.get("generated_at"), Q.QA_SCHEMA_VERSION))
        bad = 0
        for a in audits:
            print("{:<34} 引用 {:>3} · 死键 {:>2} · 证据 {:>3} · 失锚 {:>2} · "
                  "残留编号 {:>2} · 防线 v{}{}".format(
                      a.slug, a.n_cites, len(a.dead_keys), a.n_evidence,
                      len(a.unanchored), len(a.residual_refs),
                      a.schema_version if a.schema_version else "?",
                      " ⚠️" if a.outdated else ""))
            if a.dead_keys:
                print("     ⚠️ 死键（索引里不存在，粘进 pandoc 会渲染成 (key?)）：{}".format(
                    ", ".join(a.dead_keys[:6])))
            if a.unanchored:
                print("     ⚠️ 失锚（原句已不在该篇 highlights 里）：{}".format(
                    ", ".join(a.unanchored[:6])))
            if a.slug_mismatch:
                # 这里**不能印 `mv A.md B.md`**：`slug_mismatch` 只在
                # `qa_slug(question, qa_dir=)` 返回了别的 slug 时才置位，而那个函数的
                # 两条返回路径给的都是**磁盘上真实存在的文件**（页面自己一定能匹配
                # 自己，扫描不会落空）。所以 `want != slug` ⇒ `want.md` 必然已存在 ⇒
                # `mv` 100% 是覆盖操作，覆盖掉的正是重问时会落到的那一页。
                # 这是"同身份有两页"，不是"名字起错了"——正确的动作是归并，不是改名。
                print("     ⚠️ 与另一页同属一个问题：重问会落到 {}，**这一页从此不可达**。"
                      .format(a.slug_mismatch))
                print("        核对两页内容后归并（保留想留的那份，删掉另一份）；"
                      "不要 mv 覆盖——目标文件就是那页可达的。")
            if a.residual_refs:
                # 归档页是**生成它那一刻的代码**的快照：这些编号是在正文回译防线落地
                # 之前写下的，页面上留着一个只对当时那次召回有意义的交叉引用。
                print("     ⚠️ 残留证据编号（正文里的 E<n>，摘去写稿后毫无意义）：{}".format(
                    ", ".join(a.residual_refs[:8])))
            if not a.ok:
                bad += 1
        stale = [a.slug for a in audits if a.outdated]
        print()
        if stale:
            # 这一项**不花 LLM、纯本地扫描**，所以敢每次都报：它回答的是
            # "这一页是哪一版防线产的"，而不是"这一页现在有没有毛病"。
            print("⚠️ {} 页由旧版防线生成（低于 v{}），重问一次即可刷新：{}".format(
                len(stale), Q.QA_SCHEMA_VERSION, "、".join(stale[:8])))
            print("   {}".format(Q.update_command(stale[0], "<那一页的问题>")))
        unaudited = [a.slug for a in audits if a.unaudited]
        if unaudited:
            # 纯信息：v2 旧页没有审计字段、或审计关闭/失败。不影响退出码。
            print("ℹ️ {} 页未经蕴含审计（v2 旧页/审计关闭/审计失败）：{}".format(
                len(unaudited), "、".join(unaudited[:8])))
        if bad:
            # 问答页不像概念页那样随新论文自动重合成，改过的 citekey 可能挂很久，
            # 所以这里要把"怎么修"说清楚，而不是只报告。
            print("❌ {} 页存在不可追溯的引用或残留编号。重问一次那个问题即可刷新：\n   {}".format(
                bad, Q.update_command("<slug>", "<问题>")))
        else:
            print("✅ 全部引用可追溯（{} 处引用 · {} 条证据）".format(
                sum(a.n_cites for a in audits), sum(a.n_evidence for a in audits)))
        return 1 if bad else 0

    question = " ".join(w for w in args.question if w and w.strip()).strip()
    if not reask_mode and not question:
        ap.error("要问什么？把问题作为位置参数传进来（或用 --list / --verify / --reask）")

    # --reask-all：先打印计划，不加 --yes（也不是纯 --dry-run 场景）就到此为止——
    # 它可能一口气触发几十次 LLM 调用，"默认预览、显式确认才真跑"是对成本负责。
    reask_targets: List[str] = []
    if reask_mode:
        if args.reask_all:
            pages = Q.list_qa_pages(qa_dir)
            if not pages:
                print("没有已归档的问答，--reask-all 无事可做：{}".format(qa_dir))
                return 0
            print("计划重问 {} 页（按最近更新排序，同 --list）：".format(len(pages)))
            for p in pages:
                print("  {:<34} «{}»".format(p.slug, (p.title or p.question or "")[:70]))
            if not args.yes and not args.dry_run:
                print("\n加 --yes 才会真的执行（LLM 有成本）；"
                      "或加 --dry-run 只看各页召回、不调用 LLM、不落盘。")
                return 0
            reask_targets = [p.slug for p in pages]
        else:
            # 去重保序：`--reask a --reask a` 不该把同一页问两遍。
            reask_targets = list(dict.fromkeys(s for s in args.reask if s and s.strip()))
            if not reask_targets:
                ap.error("--reask 至少要给一个 slug")

    # MINOR-R3-2（2026-09-17 R3 对抗审核）：参数解析 + 校验挪到这里——在
    # `VectorStore.load()`/`EmbeddingClient` 构造/`embed_client.probe()`（Ollama
    # 探活）**之前**。`_validate_numeric_params` 是纯算术，不依赖网络；此前排在
    # Ollama 探活之后，"Ollama 不可用 + 参数越界"同时发生时用户先看到的是网络
    # 故障（退出码 3），而参数错误（改一下命令行就能自己修）才是唯一需要他动手
    # 的那个。批量场景同理：对每个 `--reask` 目标"读到 archived 参数后立即校验"，
    # 不等到网络资源都就绪了才轮到。
    #
    # 预校验阶段产出两样东西：单问题路径是 `resolved_single`（一个校验通过的
    # 元组）或提前 `return 2`；批量路径是 `prevalidated_targets`（校验通过、
    # 还没跑的目标列表）+ `prevalidate_outcomes`（预校验阶段就能定论的失败，
    # 不需要碰任何网络资源，直接计入最终汇总）。
    resolved_single: Optional[Tuple[str, str, List[str], List[str], float, int, int]] = None
    prevalidated_targets: List[Tuple[str, str, List[str], List[str], float, int, int]] = []
    prevalidate_outcomes: List[AskOutcome] = []

    if reask_mode:
        for raw in reask_targets:
            target, err = _load_reask_target(qa_dir, raw, args)
            if err:
                print("❌ {}：{}".format(raw, err), file=sys.stderr)
                prevalidate_outcomes.append(AskOutcome(slug=raw, question="", status="failed",
                                                        detail=err))
                continue
            q_text, slug, extra_queries, roles, min_sim, max_evidence, per_paper_cap = target
            bad = _validate_numeric_params(min_sim, max_evidence, per_paper_cap)
            if bad:
                print("❌ {}：{}".format(slug, bad), file=sys.stderr)
                prevalidate_outcomes.append(AskOutcome(slug=slug, question=q_text,
                                                        status="failed", detail=bad))
                continue
            prevalidated_targets.append(target)
    else:
        try:
            # 传 qa_dir：新键的文件不存在、而旧 digest 口径的文件存在且问题相同时沿用
            # 旧 slug。不传的话 2026-08-17 口径迁移之前建的页面全部变成孤儿——同一个
            # 问题会另开一页，而 `--verify` 报绿（见 Q.qa_slug 的「digest 口径迁移」）。
            slug = Q.qa_slug(question, args.slug, qa_dir=qa_dir)
        except T.TopicError as e:
            print("❌ {}".format(e), file=sys.stderr)
            return 2
        extra_queries, roles, min_sim, max_evidence, per_paper_cap = _resolve_ask_params(
            args, Q.ArchivedAskParams())
        bad = _validate_numeric_params(min_sim, max_evidence, per_paper_cap)
        if bad:
            print("❌ {}".format(bad), file=sys.stderr)
            return 2
        resolved_single = (question, slug, extra_queries, roles, min_sim, max_evidence,
                           per_paper_cap)

    outcomes: List[AskOutcome] = list(prevalidate_outcomes)

    # MINOR-R3-2 附带：批量场景若预校验后**没有任何目标活下来**（全部读不到/
    # 参数越界），就没有理由再去碰 VectorStore/Ollama——不仅浪费，且如果 Ollama
    # 恰好也不可用，会把已经定论的"参数错误"盖成一个不相关的网络故障退出码
    # （3），与这条 finding 要堵的问题是同一件事，只是发生在"批量里全军覆没"
    # 这个边界情况。单问题路径不需要这个判断：`resolved_single is None` 时
    # 前面已经 `return 2` 了，走不到这里。
    if reask_mode and not prevalidated_targets:
        _print_batch_summary(outcomes)
    else:
        # 概念页配置：用来提示"这个问题已经有一页在回答"，以及给 gaps 回查当比对基准。
        # 读不出就跳过这两项（它们是增益，不是主链路），但要说一声。
        topic_specs: List[Any] = []
        try:
            topic_specs = T.load_topic_specs(repo_path(args.topics_config))
        except T.TopicError as e:
            print("⚠️ 概念页配置读不出（{}）：跳过「已有概念页在回答」提示与 gap 回查".format(e),
                  file=sys.stderr)

        try:
            template = Q.load_prompt_template(repo_path(args.prompt))
        except T.TopicError as e:
            print("❌ {}".format(e), file=sys.stderr)
            return 2

        try:
            store = VectorStore.load(notes_dir / DB_NAME)
        except VectorStoreError as e:
            print("❌ 向量库不可用：{}".format(e), file=sys.stderr)
            return 2
        # 陈旧库的 citekey 可能是已改名/已删除的旧键，粘进归档页就是死引用。
        # 只提醒不阻塞（同 build_topics.py/notes_search.py 的既有处理）：这是人工发起的命令。
        warn = store.freshness_warning(read_index_generated_at(index_path))
        if warn:
            print(warn, file=sys.stderr)

        embed_client = EmbeddingClient(
            base_url=resolve_embedding_base_url(settings.llm),
            model=settings.llm.embedding_model,
        )
        try:
            try:
                info = embed_client.probe()
            except EmbeddingError as e:
                print("❌ Ollama embedding 不可用：{}\n（证据召回依赖语义检索，无法降级）".format(e),
                      file=sys.stderr)
                return 3
            if not model_matches(store.model, info["model"]):
                print("❌ 向量库模型 '{}' 与当前配置 '{}' 不一致，检索结果不可信。"
                      "先跑 scripts/notes_embed.py --full 重建。".format(
                          store.model, info["model"]), file=sys.stderr)
                return 2

            # 概念页问题的向量算一次传下去，整个运行期间（不管问几个问题）只算一次
            # （B7：首版在查重、概念页比对、gap 回查三处各 embed 了一遍；批量重问场景
            # 这个理由更强——N 页各自重算 N 遍纯属浪费）。
            topic_vecs = Q.topic_vectors(topic_specs, embed_client)
            titles = {p["citekey"]: (p.get("title") or "")
                     for p in (index.get("papers") or [])
                     if isinstance(p, dict) and p.get("citekey")}

            common = dict(qa_dir=qa_dir, index=index, titles=titles, topic_specs=topic_specs,
                         template=template, store=store, embed_client=embed_client,
                         settings=settings, topic_vecs=topic_vecs, model=args.model,
                         dry_run=args.dry_run, no_dedup_check=args.no_dedup_check,
                         no_topic_check=args.no_topic_check, show=args.show,
                         audit_mode=args.audit, audit_model=args.audit_model,
                         strict=args.strict, theory=args.theory,
                         theory_min_sim=args.theory_min_sim, theory_max=args.theory_max,
                         force_synthesize=args.force_synthesize)

            if reask_mode:
                for target in prevalidated_targets:
                    q_text, slug, extra_queries, roles, min_sim, max_evidence, per_paper_cap = \
                        target
                    print("\n{}".format("-" * 60))
                    try:
                        outcome = _run_one(q_text, slug, extra_queries=extra_queries,
                                           roles=roles, min_sim=min_sim,
                                           max_evidence=max_evidence,
                                           per_paper_cap=per_paper_cap, **common)
                    except Exception as e:      # noqa: BLE001 — 单页失败不中断其余页
                        print("❌ {} 处理失败（{}: {}）".format(slug, type(e).__name__, e),
                              file=sys.stderr)
                        outcome = AskOutcome(slug=slug, question=q_text, status="failed",
                                             detail="{}: {}".format(type(e).__name__, e))
                    outcomes.append(outcome)
                _print_batch_summary(outcomes)
            else:
                question, slug, extra_queries, roles, min_sim, max_evidence, per_paper_cap = \
                    resolved_single
                outcomes.append(_run_one(question, slug, extra_queries=extra_queries,
                                         roles=roles, min_sim=min_sim,
                                         max_evidence=max_evidence,
                                         per_paper_cap=per_paper_cap, **common))
        finally:
            embed_client.close()

    # 目录页按磁盘现状重建（不是只写本次这些）：单跑/批量跑都不该让其余问答从
    # 目录里失踪。批量场景收在循环外只重建一次——N 页各自重建 N 遍纯属浪费，
    # `render_qa_index` 本就是"扫磁盘现状"，最后建一次结果完全一样。
    #
    # **`dry_run`/`skipped`/`failed`/`taken` 都不算"写过"**：这四种结局都没有
    # 调用过 `write_qa_page`（dry-run 提前退出、0 证据提前退出、合成/校验失败、
    # slug 占用），此时目录内容与运行前逐字节相同，重建只是空转 IO。dry-run 更是
    # **不能**碰——它对用户的承诺是"未调用 LLM、未落盘"，INDEX.md 也是盘。
    if qa_dir.exists() and any(o.status in ("new", "merged", "unchanged", "conflict", "insufficient")
                               for o in outcomes):
        idx = qa_dir / "INDEX.md"
        write_if_changed(idx, Q.render_qa_index(qa_dir))
        print("📇 问答目录：{}".format(idx))
    if any(o.status in ("new", "merged", "insufficient") for o in outcomes):
        # B3 后续：vault job 的 WatchPaths 已补上 `topics/` 与 `topics/qa/`（W5 完整
        # 修复），归档落盘会在节流窗口（约 2 分钟）后自动同步进 Obsidian——原来这里
        # 教用户手动跑的是一件如今已自动化的事。保留兜底命令给"怀疑没触发"的场景。
        print("🔄 Obsidian 那份会在约 2 分钟内自动同步（vault job 盯着 topics/qa/）。"
              "没出现再手动跑：\n"
              "   PYTHONPATH=. python scripts/sync_vault.py "
              "--vault-dir ~/Documents/ScholarVault --force")

    if reask_mode:
        if any(o.status == "conflict" for o in outcomes):
            return 1
        # M1（2026-09-17 R1 对抗审核）：漏了 taken。单问题路径（下面几行）把
        # taken 与 failed 同等对待（都退 2）——slug 被另一个问题占用是真实故障，
        # 不比"合成失败"轻。批量路径此前只查 failed，taken 页存在时退出码仍是
        # 0，会被下游 launchd/编排脚本当成"全部成功"，而汇总里其实打着 ❌。
        if any(o.status in ("failed", "taken") for o in outcomes):
            return 2
        return 0
    o = outcomes[0]
    if o.status == "conflict":
        return 1
    if o.status in ("taken", "failed"):
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)
