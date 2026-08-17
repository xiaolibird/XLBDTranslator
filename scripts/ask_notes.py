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

退出码：0 成功 / 1 归档页有冲突或自检发现不可追溯的引用 /
2 配置/索引/向量库异常、slug 已被别的问题占用、校验后没有任何有出处的论断 /
3 Ollama embedding 不可用（证据召回依赖语义检索，无法降级）。
"""
import argparse
import json
import sys
from pathlib import Path

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
from src.scholar.notes_index import write_if_changed                      # noqa: E402
from src.scholar import qa as Q                                           # noqa: E402
from src.scholar import topics as T                                       # noqa: E402
from src.utils.logger import get_logger                                   # noqa: E402

logger = get_logger("ask_notes")


def _load_index(index_path: Path):
    if not index_path.exists():
        print("找不到索引：{}\n先跑：PYTHONPATH=. python scripts/notes_index.py".format(index_path),
              file=sys.stderr)
        return None
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
        assert isinstance(data.get("papers"), list)
        return data
    except Exception as exc:
        print("索引损坏（{}）：{}".format(type(exc).__name__, index_path), file=sys.stderr)
        return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="对札记库提问，并把答案归档回 topics/qa/（探索复合，不再用完即弃）")
    ap.add_argument("question", nargs="*", help="要问的问题（一句话）")
    ap.add_argument("-q", "--query", action="append", default=[], metavar="TERM",
                    help="额外的检索词（可重复）：每个词各检索一次后合并，不改变问题本身。"
                         "关键术语的中英两种写法都给，召回会明显变好")
    ap.add_argument("--slug", default=None,
                    help="指定归档文件名（小写 ASCII 字母数字与连字符；自动加 qa- 前缀）。"
                         "缺省按问题文本自动生成——同一个问题因此总是落在同一页上")
    ap.add_argument("--config", default="config/scholar.env")
    ap.add_argument("--prompt", default=Q.DEFAULT_PROMPT, help="合成 prompt 模板")
    ap.add_argument("--max-evidence", type=int, default=Q.DEFAULT_QA_MAX_EVIDENCE,
                    help="喂给 LLM 的证据上限（默认 {}）".format(Q.DEFAULT_QA_MAX_EVIDENCE))
    ap.add_argument("--min-sim", type=float, default=T.DEFAULT_MIN_SIM,
                    help="召回相似度下限（默认 {}；**不要照抄 notes_search 的 0.65**，"
                         "那是 paper 级口径，见 topics.DEFAULT_MIN_SIM 的实测依据）".format(
                             T.DEFAULT_MIN_SIM))
    ap.add_argument("--per-paper-cap", type=int, default=Q.DEFAULT_QA_PER_PAPER_CAP,
                    help="单篇论文最多贡献几条证据（默认 {}；比概念页的 {} 小是**有意的**，"
                         "一个具体问题要的是把最相关那几篇挖深，不是每篇各一句）".format(
                             Q.DEFAULT_QA_PER_PAPER_CAP, T.DEFAULT_PER_PAPER_CAP))
    ap.add_argument("--topics-config", default="config/topics.yaml",
                    help="概念页配置；用来提示「这个问题已经有一页概念页在回答」，"
                         "以及给 gaps 回查提供概念页问题。读不出就跳过这两项")
    ap.add_argument("--role", action="append", default=[],
                    choices=["citable", "refutable", "method"],
                    help="只用该角色的证据（可重复）；缺省三者都要")
    ap.add_argument("--model", default=None, help="覆盖合成用的 LLM 模型")
    ap.add_argument("--no-dedup-check", action="store_true",
                    help="跳过「这个问题是不是问过了」的查重提示（不影响概念页比对）")
    ap.add_argument("--no-topic-check", action="store_true",
                    help="跳过「已经有一页概念页在回答这个问题」的比对。注意它同时会让"
                         "归档页顶部那条「相关概念页」消失——两件事是同一份数据")
    ap.add_argument("--dry-run", action="store_true",
                    help="只跑召回，看能捞到多少证据，不调 LLM、不落盘")
    ap.add_argument("--list", action="store_true", help="列出已归档的问答后退出")
    ap.add_argument("--verify", action="store_true",
                    help="只自检已归档页（死键/失锚/正文残留证据编号/可达性/防线版本），"
                         "不调 LLM、不联网、不改文件")
    args = ap.parse_args()
    # 提前校验：这两个数不合法时 `retrieve_qa_evidence` 会抛 TopicError，而那发生在
    # **整轮宽召回跑完之后**，而且以裸 traceback + 退出码 1（文档里 1 的含义是
    # "归档页有冲突"）呈现。与本文件"slug 占用检查放最前：它不花钱、不联网"同一条原则。
    if args.per_paper_cap <= 0:
        ap.error("--per-paper-cap 必须 ≥ 1（收到 {}）。想「不限制」就给一个大于 "
                 "--max-evidence 的数".format(args.per_paper_cap))
    if args.max_evidence <= 0:
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
    if not question:
        ap.error("要问什么？把问题作为位置参数传进来（或用 --list / --verify）")

    try:
        # 传 qa_dir：新键的文件不存在、而旧 digest 口径的文件存在且问题相同时沿用旧 slug。
        # 不传的话 2026-08-17 口径迁移之前建的页面全部变成孤儿——同一个问题会另开一页，
        # 而 `--verify` 报绿（见 Q.qa_slug 的「digest 口径迁移」一节）。
        slug = Q.qa_slug(question, args.slug, qa_dir=qa_dir)
        spec = Q.build_qa_spec(question, extra_queries=args.query, roles=args.role,
                               max_evidence=args.max_evidence)
    except T.TopicError as e:
        print("❌ {}".format(e), file=sys.stderr)
        return 2
    spec.slug = slug

    # slug 占用检查放在最前：它不花钱、不联网，而它拦住的是最坏的一种——
    # 拿别人那一页的答案当"上一版"喂给这次的 prompt，让模型去融合两个不相干的问题。
    taken_by = Q.archived_question(qa_dir, slug)
    if taken_by and Q.question_key(taken_by) != Q.question_key(question):
        print("❌ slug '{}' 已经属于另一个问题：\n   «{}»\n"
              "   换一个 --slug，或者留空让它按问题自动生成。\n"
              "   （要更新那一页就用它自己的问题重问：{}）".format(
                  slug, taken_by, Q.update_command(slug, taken_by)), file=sys.stderr)
        return 2

    # 概念页配置：用来提示"这个问题已经有一页在回答"，以及给 gaps 回查当比对基准。
    # 读不出就跳过这两项（它们是增益，不是主链路），但要说一声。
    topic_specs = []
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
    llm = None
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

        # 查重放在召回之前、合成之前：它可能让整轮合成都不必跑。
        # 走 embedding 而不是词面重合——词面档实测漏的正是"三个月后换个说法再问一次"
        # 那一档（0.10~0.17 全漏），而报的是完全不同的问题（0.42~0.67）。
        # 这里已经 probe 过 Ollama，多算一次几乎不加成本。
        related_topics: list = []
        # 概念页问题的向量算一次传下去（B7：首版在查重、概念页比对、gap 回查三处各
        # embed 了一遍）。**放在两个 --no-* 开关之外**：gap 回查也要用它，而那一段
        # 不受这两个开关控制——放在 if 里会在带 `--no-dedup-check` 时抛 NameError。
        topic_vecs = Q.topic_vectors(topic_specs, embed_client)
        if not args.no_dedup_check:
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
                # 印**那一页自己的问题**，不是「<你的新问法>」——占用检查拿 question_key
                # 比对，任何真正的新问法粘进去必然被拒（A2）。
                print("   要**更新那一页**（而不是另存一页）：\n     {}".format(
                    Q.update_command(top.slug, top.question or top.title)))
                print("   确认是新问题就继续，本次会另存一页。\n")

        # A4：同一个问题若已有一页概念页在回答，那一页几乎总是更好的选择。
        # **独立于 `--no-dedup-check`**（B1）：首版把这段放在那个 if 里，于是带上
        # `--no-dedup-check` 重跑一页时，生成块顶部那条 `📎 相关概念页` 会被静默删掉，
        # 而状态仍是 merged ✅、退出码 0——两件不同的事共用一个开关，关掉一个连带
        # 关掉另一个，且文案里一个字都没说。要单独关用 `--no-topic-check`。
        if not args.no_topic_check:
            hits = Q.find_similar_topics(topic_specs, question, embed_client,
                                         topic_vecs=topic_vecs)
            related_topics = [s.slug for s, _sc in hits]
            for s, sc in hits:
                print("📎 `topics/{}.md`（{}）已经在回答这个问题（{:.0%} 像）——"
                      "它证据面更宽、随新论文**自动重合成**。".format(s.slug, s.title, sc))
            if hits:
                print("   概念级、会反复用的问题就去读那一页；只是临时想问一句才继续。\n")

        print("🔎 «{}»".format(question))
        # 返回 (证据, 差一点没进来的论文)：后者是 B2 的可见性补偿，渲染成页面上
        # 那一行「本次未纳入的近邻论文」——窄而深把 11 名之后的论文整篇砍掉之后，
        # 它们连被肉眼逮到的机会都没有了（上一轮的漏网证据就是那么被逮到的）。
        evidences, nearby = Q.retrieve_qa_evidence(spec, store, embed_client,
                                                   min_sim=args.min_sim,
                                                   per_paper_cap=args.per_paper_cap)
        if not evidences:
            # 召回 0 条是**诚实的信号**而不是故障：库里确实没有这方面的句级证据。
            # 但要把"下一步能做什么"说清楚，否则用户只会以为工具坏了。
            print("⚠️ 召回 0 条证据（相似度阈值 {}）。这多半是真的——库里没有这方面的句级"
                  "证据。可以试：降低 --min-sim、用 -q 补几个别的说法（尤其英文写法）、"
                  "或先跑 `scripts/notes_search.py <关键词>` 看看有没有只有标题级的命中。"
                  .format(args.min_sim))
            return 0
        T.attach_titles(evidences, index)
        # gap 回查命中与「近邻论文」两处都要标题：只印裸 citekey 的话读者无从判断，
        # 只能一条条点开，而句级通道实测只有约四成相关（见 Q.GapHit 的 docstring）。
        titles = {p["citekey"]: (p.get("title") or "")
                  for p in (index.get("papers") or [])
                  if isinstance(p, dict) and p.get("citekey")}
        n_papers = len({e.citekey for e in evidences})
        print("   证据 {} 条 / {} 篇（相似度 {:.2f}~{:.2f}）".format(
            len(evidences), n_papers, evidences[-1].score, evidences[0].score))

        if args.dry_run:
            for e in evidences[:10]:
                print("   {:<4} {:.3f} [@{}] {}".format(
                    e.ref, e.score, e.citekey, e.text[:70]))
            if len(evidences) > 10:
                print("   …… 还有 {} 条（--dry-run 只列前 10）".format(len(evidences) - 10))
            print("\n【dry-run】未调用 LLM、未落盘。归档路径会是：{}/{}.md".format(qa_dir, slug))
            return 0

        # `previous_answer` 带上问题：显式复用别人的 slug 时不能把那一页的答案
        # 当成"上一版"喂进来（prompt 铁律要求模型把它当草稿修订，于是会去融合
        # 两个不相干的问题）。上面的 slug 占用检查已经先拦了一道，这里是第二道。
        prompt = Q.build_qa_prompt(question, evidences, template,
                                   get_default_research_interests(),
                                   Q.previous_answer(qa_dir, slug, question))
        llm = LLMClient(settings.llm)
        try:
            data = Q.synthesize_qa(llm, prompt, model=args.model)
        except T.TopicError as e:
            # 保留召回统计再报错：只说"失败"会把人引向调 queries/阈值，
            # 而真正的病灶在合成端（同 topics.build_topic 的既有处理）。
            print("❌ 合成失败（召回本身正常：{} 条证据 / {} 篇）：{}".format(
                len(evidences), n_papers, e), file=sys.stderr)
            return 2

        qa, report = Q.validate_qa(data, evidences)
        # A1 的回查：每条 gap 拿它自己的文本再问一次库（一次批量 embedding）。
        # 必须在 embed_client 关掉之前做，所以整段留在 try 里。
        qa["gap_hits"] = Q.recheck_gaps(qa["gaps"], store=store,
                                        embed_client=embed_client,
                                        topic_specs=topic_specs,
                                        topic_vecs=topic_vecs,
                                        titles=titles,
                                        exclude_citekeys={e.citekey for e in evidences})
    finally:
        embed_client.close()
        if llm is not None:
            llm.close()

    if not Q.is_archivable(qa):
        # 判据是"有没有剩下带出处的论断"，**不看 `answer`**：`answer` 允许没有编号的
        # 前提是"它是下面各条的概括"，两节全空时剩下的只是一句纯 LLM 断言。
        print("❌ 校验后没有任何带出处的论断（丢弃 {} 条，非法编号 {} 个）——"
              "多半是模型没按编号引用。重跑一次通常就好。".format(
                  report.dropped_claims, report.invalid_refs), file=sys.stderr)
        return 2

    path, status = Q.write_qa_page(qa_dir, slug, question, qa, evidences, report,
                                   related_topics=related_topics,
                                   nearby_papers=nearby, titles=titles)
    if status == "taken":
        # 理论上前面已经拦过；这里兜住"合成期间别人往这个 slug 上写了另一个问题"。
        print("❌ slug '{}' 在这次合成期间被另一个问题占用了，本次未落盘。".format(slug),
              file=sys.stderr)
        return 2
    icon = {"new": "🆕", "merged": "✅", "unchanged": "＝", "conflict": "⚠️"}.get(status, "?")

    print()
    if qa["answer"]:
        print("💬 {}".format(qa["answer"]))
    print("   依据 {} 条 · 注意事项 {} 条 · 本次没覆盖到 {} 条 · 引用率 {:.0%}".format(
        len(qa["points"]), len(qa["caveats"]), len(qa["gaps"]),
        report.used_refs / len(evidences) if evidences else 0.0))
    # 回查命中要在 stdout 上也说一次：这一节按设计没有出处可点，用户照着它去补文献
    # 之前唯一的机会就是这里（"答错了还能核对，指错方向不会去核对"）。
    flagged = [(g, h) for g, h in (qa.get("gap_hits") or {}).items() if h]
    for g, hits in flagged:
        print("   ⚠️ 「{}」——但库里可能有：{}".format(
            g[:40], "、".join(
                ("topics/{}.md".format(h.key) if h.kind == "topic" else "[@{}]".format(h.key))
                for h in hits)))
    flags = []
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

    # 目录页按磁盘现状重建（不是只写本次这一条）：单跑一个问题不该让其余问答从目录里失踪。
    if qa_dir.exists():
        idx = qa_dir / "INDEX.md"
        write_if_changed(idx, Q.render_qa_index(qa_dir))
        print("📇 问答目录：{}".format(idx))

    if status in ("new", "merged"):
        # B3：Obsidian 那条发现路径**不会自己被叫起来**——`com.xlbd.scholar-vault` 的
        # WatchPaths 只盯 `literature_index.json`，而归档一次问答不动索引，
        # 也没有 StartInterval。实测：最后一次 vault 同步 14:19，问答页 19:01，
        # 至今没同步过。所以这里必须把命令给出来，而不是让文档写着"去 Obsidian 搜"。
        print("🔄 Obsidian 那份要等下一次索引重建（周度/月度）才出现。急用先跑：\n"
              "   PYTHONPATH=. python scripts/sync_vault.py "
              "--vault-dir ~/Documents/ScholarVault --force")

    return 1 if status == "conflict" else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)
