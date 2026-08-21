# -*- coding: utf-8 -*-
"""概念页构建 CLI（核心逻辑在 src/scholar/topics.py）。

把 paper-centric 的札记库编译出 concept-centric 的一层：每页回答一个概念问题
（「关于 MNAR 诊断，我库里的共识、分歧与缺口是什么」），论断全部锚定到句级证据。

  python scripts/build_topics.py                      # 构建 config/topics.yaml 里的全部概念页
  python scripts/build_topics.py --topic mnar-diagnosis --topic shadow-variable
  python scripts/build_topics.py --dry-run            # 只跑召回，看每页能捞到多少证据，不调 LLM
  python scripts/build_topics.py --list               # 列出配置里的概念页，不做任何事

产物：output/scholar_notes/topics/<slug>.md（+ topics/INDEX.md 目录页）。
页面里 `<!-- BEGIN/END GENERATED -->` 之间会被重建覆盖，批注请写在「我的批注」下——
哨兵缺失或生成块被手改时该页**不覆盖**、报 conflict 让人工处理（同 vault 约定）。

退出码：0 全部成功 / 1 有页面失败或冲突 / 2 配置或索引/向量库异常（含路由阶段判定
向量库过陈旧、拒绝相信"召回不到"，见 T.routing_is_stale）/ 3 Ollama 不可用。
这份表同时是 T.BUILD_TOPICS_EXIT_CODES 的权威出处，改一处记得改另一处。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json                                                               # noqa: E402

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
from src.scholar.notes_index import load_index_file                       # noqa: E402
from src.scholar.research_profile import get_default_research_interests   # noqa: E402
from src.scholar import topics as T                                       # noqa: E402
from src.utils.logger import get_logger                                   # noqa: E402

logger = get_logger("build_topics")


def _load_index(index_path: Path):
    """读索引（单点实现在 notes_index.load_index_file，别再各写各的）。"""
    data, err = load_index_file(index_path)
    if err:
        print(err, file=sys.stderr)
    return data


def _fmt(r: T.TopicResult) -> str:
    icon = {"new": "🆕", "merged": "✅", "conflict": "⚠️", "skipped": "⏭️", "failed": "❌"}
    line = "{} {:<28} 证据 {:>3} 条 / {:>2} 篇".format(
        icon.get(r.status, "?"), r.slug, r.n_evidence, r.n_papers)
    if r.report:
        line += " · 论断 {} · 分歧 {}".format(r.report.kept_claims, r.n_disputes)
        flags = []
        if r.report.dropped_claims:
            flags.append("丢弃 {}".format(r.report.dropped_claims))
        if r.report.invalid_refs:
            flags.append("非法编号 {}".format(r.report.invalid_refs))
        if r.report.stripped_cites:
            flags.append("剥离裸引用 {}".format(r.report.stripped_cites))
        if flags:
            line += " · ⚑ " + "，".join(flags)
        if r.n_evidence:
            line += " · 引用率 {:.0%}".format(r.report.used_refs / r.n_evidence)
    if r.error:
        line += "\n      └─ {}".format(r.error)
    elif r.status in ("new", "merged") and not r.changed:
        line += " · 内容未变"
    return line


def main() -> int:
    ap = argparse.ArgumentParser(description="从札记库句级证据合成概念页（topic pages）")
    ap.add_argument("--config", default="config/scholar.env")
    ap.add_argument("--topics-config", default=T.DEFAULT_CONFIG, help="概念页定义（yaml）")
    ap.add_argument("--prompt", default=T.DEFAULT_PROMPT, help="合成 prompt 模板")
    ap.add_argument("--topic", action="append", default=[],
                    help="只构建指定 slug（可重复）；缺省构建全部")
    ap.add_argument("--affected-by", action="append", default=[], metavar="CITEKEY",
                    help="只构建被这些新论文影响的页（可重复）：先跑召回，看它们有没有真的"
                         "挤进某页的证据集，没挤进的页不重合成。入库流水线用这个避免全量重跑")
    ap.add_argument("--affected-by-note", default=None, metavar="NOTE_MD",
                    help="同 --affected-by，但直接给一份札记 md（取它带进来的全部 citekey），"
                         "周度/月度入库收尾用")
    ap.add_argument("--min-sim", type=float, default=T.DEFAULT_MIN_SIM,
                    help="召回相似度下限（默认 {}）".format(T.DEFAULT_MIN_SIM))
    ap.add_argument("--per-paper-cap", type=int, default=T.DEFAULT_PER_PAPER_CAP,
                    help="单篇论文最多贡献几条证据（默认 {}）".format(T.DEFAULT_PER_PAPER_CAP))
    ap.add_argument("--bucket-bonus", type=float, default=T.DEFAULT_BUCKET_BONUS,
                    help="topics.yaml 里 buckets 命中的排序加成，只影响排序不影响是否入选"
                         "（默认 {}）".format(T.DEFAULT_BUCKET_BONUS))
    ap.add_argument("--stale-after-days", type=float, default=30.0, metavar="DAYS",
                    help="仅路由模式（--affected-by/--affected-by-note）生效：页面超过这么多天"
                         "没更新就强制重合成一次，不管本轮有没有新论文挤进它的证据集——W8 兜底，"
                         "避免一次性合成失败的页只能靠'未来新论文恰好命中'才被自然重试（默认 30，"
                         "<=0 关闭）")
    ap.add_argument("--stale-max-per-run", type=int, default=3, metavar="N",
                    help="X3：--stale-after-days 兜底命中多页时，单次运行最多强制拉回几个"
                         "最旧的（按 generated_at 从旧到新截断），其余留给下次触发——W8 兜底本是"
                         "为一次性合成失败兜底，若同一批页恰好同期过期（实测过 8 页同批上线、"
                         "同一天全部过期），无上限会让单次运行连续跑 8×3-4 分钟 LLM 合成，正是 "
                         "P2 路由机制本来要避免的成本形状，也更容易撞订阅限流（默认 3，<=0 不限）")
    ap.add_argument("--model", default=None, help="覆盖合成用的 LLM 模型")
    ap.add_argument("--dry-run", action="store_true", help="只跑召回统计，不调用 LLM、不落盘")
    ap.add_argument("--list", action="store_true", help="列出配置里的概念页后退出")
    ap.add_argument("--verify", action="store_true",
                    help="只自检已生成的页面（引用是否仍可追溯），不调 LLM、不改文件")
    args = ap.parse_args()

    try:
        specs = T.load_topic_specs(repo_path(args.topics_config))
    except T.TopicError as e:
        print("❌ {}".format(e), file=sys.stderr)
        return 2

    if args.topic:
        want = set(args.topic)
        unknown = want - {s.slug for s in specs}
        if unknown:
            print("❌ 配置里没有这些概念页：{}\n可用：{}".format(
                sorted(unknown), ", ".join(s.slug for s in specs)), file=sys.stderr)
            return 2
        specs = [s for s in specs if s.slug in want]

    if args.list:
        print("共 {} 个概念页（{}）：".format(len(specs), args.topics_config))
        for s in specs:
            # buckets 现在是排序加成而非过滤条件（软加权，见 T.DEFAULT_BUCKET_BONUS），
            # 所以一律标"全库"，命中的维度只是排序偏好，不是范围限定。
            scope = "全库"
            if s.buckets:
                scope += "（偏好 " + "/".join(s.buckets) + "）"
            if s.roles:
                scope += " · 角色 " + "/".join(s.roles)
            print("  {:<28} {}  [{} · 上限 {} 条]".format(s.slug, s.title, scope, s.max_evidence))
        return 0

    cfg = repo_path(args.config)
    settings = ScholarSettings.from_env_file(cfg) if cfg.exists() else ScholarSettings()
    notes_dir = repo_path(settings.processing.notes_dir)
    index_path = notes_dir / INDEX_NAME
    db_path = notes_dir / DB_NAME
    topics_dir = notes_dir / T.TOPICS_DIRNAME

    index = _load_index(index_path)
    if index is None:
        return 2

    if args.verify:
        audits = T.audit_topic_pages(topics_dir, index)
        if not audits:
            print("没有已生成的概念页：{}".format(topics_dir))
            return 0
        print("自检 {} 页（索引 generated_at = {}）\n".format(
            len(audits), index.get("generated_at")))
        print("{:<30} {:>6} {:>6} {:>6} {:>7} {:>7} {:>6}".format(
            "概念页", "引用", "死键", "裸引", "证据", "失锚", "引用率"))
        bad = 0
        for a in audits:
            print("{:<30} {:>6} {:>6} {:>6} {:>7} {:>7} {:>5.0%}{}".format(
                a.slug, a.n_cites, len(a.dead_keys), len(a.bare_cites), a.n_evidence,
                len(a.unanchored), a.used_ratio, "  ⏳旧" if a.stale else ""))
            if a.dead_keys:
                print("     ⚠️ 死键（索引里不存在，粘进 pandoc 会渲染成 (key?)）：{}".format(
                    ", ".join(a.dead_keys[:6])))
            if a.bare_cites:
                # 正常流程下 validate_synthesis 已经把裸引用剥干净了，这里出现只可能是
                # 用旧代码生成、此后没重跑过的历史页面（见 G1）——pandoc 会把它当真引用
                # 去挂书目，且它没经过编号回译，是能绕开死键检测的编造通道。
                print("     ⚠️ 裸引用（不带方括号的 @key，历史遗留页面才会出现）：{}".format(
                    ", ".join(a.bare_cites[:6])))
            if a.unanchored:
                print("     ⚠️ 失锚（原句已不在该篇 highlights 里）：{}".format(
                    ", ".join(a.unanchored[:6])))
            if not a.ok:
                bad += 1
        stale = [a.slug for a in audits if a.stale]
        print()
        if bad:
            print("❌ {} 页存在不可追溯的引用，重跑 build_topics.py 修复".format(bad))
        else:
            print("✅ 全部引用可追溯（{} 处引用 · {} 条证据）".format(
                sum(a.n_cites for a in audits), sum(a.n_evidence for a in audits)))
        if stale:
            print("⏳ {} 页比索引旧，重跑可纳入新入库的论文：{}".format(len(stale), ", ".join(stale)))
        return 1 if bad else 0

    try:
        template = T.load_prompt_template(repo_path(args.prompt))
    except T.TopicError as e:
        print("❌ {}".format(e), file=sys.stderr)
        return 2

    try:
        store = VectorStore.load(db_path)
    except VectorStoreError as e:
        print("❌ 向量库不可用：{}".format(e), file=sys.stderr)
        return 2

    # 陈旧库的 citekey 可能是已改名/已删除的旧键，粘进概念页就是死引用。只提醒不阻塞
    # （对齐 notes_search 的处理）：这里是人工发起的命令，用户看得见这行。
    # 下面 P2 路由块会复用同一个 idx_generated_at 再做一次更严格的判断（W2）。
    idx_generated_at = read_index_generated_at(index_path)
    warn = store.freshness_warning(idx_generated_at)
    if warn:
        print(warn, file=sys.stderr)

    embed_client = EmbeddingClient(
        base_url=resolve_embedding_base_url(settings.llm),
        model=settings.llm.embedding_model,
    )
    try:
        info = embed_client.probe()
    except EmbeddingError as e:
        print("❌ Ollama embedding 不可用：{}\n（概念页依赖语义召回，无法降级）".format(e),
              file=sys.stderr)
        embed_client.close()      # 吹毛求疵：早退路径此前不在 try/finally 覆盖范围内，没关过
        return 3
    if not model_matches(store.model, info["model"]):
        print("❌ 向量库模型 '{}' 与当前配置 '{}' 不一致，检索结果不可信。"
              "先跑 scripts/notes_embed.py --full 重建。".format(store.model, info["model"]),
              file=sys.stderr)
        embed_client.close()
        return 2

    # P2 路由：只重合成真被新论文影响的页。判据是"新论文有没有挤进这页的证据集"
    # （召回不花钱，拿它当判据最准），而不是"有点相关"——排在 max_evidence 之外的
    # 证据对页面内容毫无影响，为它跑一次 LLM 合成纯属浪费。
    affected_keys = list(args.affected_by)
    if args.affected_by_note:
        affected_keys += sorted(T.new_citekeys_from_note(index, args.affected_by_note))
        if not affected_keys:
            print("ℹ️ 札记 {} 没有带进任何新 citekey（可能全部是跨月重复），无页需要更新".format(
                args.affected_by_note))
            embed_client.close()      # 吹毛求疵：早退路径此前不在 try/finally 覆盖范围内
            return 0
    if affected_keys:
        # Y1：stale 集合先算——它只读页面 frontmatter 的 generated_at，不碰向量库，
        # 不该被下面的向量库陈旧判断连带挡住。W2（路由不可信要拒绝）与 W8（日历兜底
        # 不依赖路由）两条修复单看都对，此前的顺序（先判向量库陈旧就整体 return 2）
        # 会让向量库长期不可用时 W8 兜底——它存在的唯一理由就是给"因故障错过更新的页"
        # 一条不依赖路由的补课通道——也一并失效。合并逻辑见 T.plan_synthesis_targets。
        stale_all = []
        if args.stale_after_days > 0:
            stale_all = T.stale_topic_slugs(topics_dir, specs, max_age_days=args.stale_after_days)

        # W2：路由用"召回不到"当判据，前提是向量库真的含有本批新论文的 embedding。
        # sync_store 在 ingest/backfill 里失败被吞掉后，路由会永远误判"无需更新"、
        # 退出码 0，新论文的证据永久错过任何概念页——这里拒绝相信这种情形下的路由结论。
        routing_ok = not T.routing_is_stale(store, idx_generated_at)
        hits = []
        if routing_ok:
            print("🔎 路由：{} 篇新论文，检查哪些概念页受影响……".format(len(affected_keys)))
            hits = T.affected_topics(specs, store, embed_client, affected_keys,
                                     min_sim=args.min_sim, per_paper_cap=args.per_paper_cap,
                                     bucket_bonus=args.bucket_bonus)
            for slug, keys in hits:
                print("   {:<28} ← {}".format(slug, ", ".join(keys[:4]) or "（召回失败，保守重跑）"))
        else:
            # Y1：只让路由命中这部分失效，不连带关掉不依赖向量库的日历兜底——两者
            # 判据完全独立。下面 plan_synthesis_targets(routing_ok=False, ...) 只取
            # stale_all 那一路，hits 留空不会被采用。
            print("❌ 向量库落后当前索引，路由结果不可信：新论文的 embedding 可能还没同步"
                  "进库，此时「召回不到」不能当「不影响任何页」，本轮跳过新论文路由"
                  "（日历兜底不依赖向量库，仍会照常执行）。先跑 "
                  "PYTHONPATH=. python scripts/notes_embed.py 增量同步后重试以恢复路由。",
                  file=sys.stderr)

        # Y2：候选提交合成前先过一遍失败冷却过滤，且要在 --stale-max-per-run 截断
        # 之前做——冷却中的页很可能恰好也是长期过期的页（合成一直失败 -> generated_at
        # 一直不前进 -> 越拖越 stale），先截断再过滤会让"每轮最多 N 个"的名额被注定
        # 过滤掉的页占满，这一轮真正能推进的页反而排不上号。路由命中同样先过一遍：
        # 见 T.filter_retry_cooldown 顶部注释——重放一次回退链要吃满 12 次底层调用，
        # 不管触发源是路由还是日历。
        candidates = list(dict.fromkeys([s for s, _ in hits] + list(stale_all)))
        allowed, cooling = T.filter_retry_cooldown(topics_dir, candidates)
        if cooling:
            print("🧊 {} 页仍在失败冷却期，本轮跳过（合成成功一次即清除记录）：{}".format(
                len(cooling),
                "；".join("{}（还剩 {:.1f}h）".format(s, h) for s, h in cooling)))
        allowed_set = set(allowed)
        hits = [(s, ks) for s, ks in hits if s in allowed_set]
        stale_all = [s for s in stale_all if s in allowed_set]

        # W8：一次性合成失败的页，此前只能等"未来新论文恰好挤进它的证据集"才会被自然
        # 重试，这个概率随语料增长递减——不依赖路由命中的兜底：页面超过 --stale-after-days
        # 天没更新就强制重合成一次，即便本轮没有新论文挤进它的证据集（或路由本身不
        # 可信，见上面 Y1）。
        # X3：全部命中的过期页不能无条件一次性拉回——同一批上线的页容易在同一个日历
        # 阈值上同时过期（复审 agent 用仓库真实 config/topics.yaml 复现：8 页全部命中），
        # 无上限会让单次运行连续跑数十分钟 LLM 合成，正是 P2 路由本来要避免的成本形状。
        # stale_topic_slugs 已按 generated_at 从旧到新排序，plan_synthesis_targets 只
        # 截断不重排，其余留给下次触发自然摊开，撞限流的风险不会集中在一次运行里引爆。
        plan = T.plan_synthesis_targets(hits, stale_all, routing_ok=routing_ok,
                                        stale_max_per_run=args.stale_max_per_run)
        if plan.stale_selected:
            msg = "⏰ {} 页超过 {:.0f} 天未更新，本次强制重合成最旧的 {} 个（不依赖本轮路由命中）：{}".format(
                len(plan.stale_selected) + len(plan.stale_held_back), args.stale_after_days,
                len(plan.stale_selected), ", ".join(plan.stale_selected))
            if plan.stale_held_back:
                msg += "；另有 {} 页留给下次触发：{}".format(
                    len(plan.stale_held_back), ", ".join(plan.stale_held_back))
            print(msg)
        want_slugs = set(plan.want_slugs)

        if not routing_ok and not want_slugs:
            # Y1：路由不可信、又没有日历兜底可做（或全在冷却中）——这一轮确实什么都
            # 做不了，报错退出而不是假装"无需更新"（与此前行为一致）。
            embed_client.close()
            return 2

        if not want_slugs:
            print("✅ 新论文没有进入任何概念页的证据集，也没有页面超期未更新，无需重新合成"
                  "（它们仍在札记库与向量库里，只是不改变这几页的结论）")
            embed_client.close()
            return 0
        specs = [s for s in specs if s.slug in want_slugs]

    llm = None if args.dry_run else LLMClient(settings.llm)
    interests = get_default_research_interests()

    results = []
    # 连续失败熔断：LLM 回退链一旦整条耗尽（订阅限流 + 备用 provider 地区不可用），
    # 后面每一页都会以同一个错误再失败一次——实测 8 页里 7 页排队陪葬，每页还各自
    # 先跑完召回。连挂 2 页就停，把剩下的留给补跑，而不是空转到底。
    CONSECUTIVE_FAIL_LIMIT = 2
    streak = 0
    try:
        for spec in specs:
            if streak >= CONSECUTIVE_FAIL_LIMIT:
                remaining = [s.slug for s in specs[specs.index(spec):]]
                print("\n🛑 连续 {} 页失败（多半是 LLM 回退链已整条耗尽），中止剩余 {} 页：{}".format(
                    streak, len(remaining), ", ".join(remaining)))
                print("   修好 provider 后补跑：{}".format(
                    " ".join("--topic {}".format(s) for s in remaining)))
                break
            print("▶ {}（{}）".format(spec.slug, spec.title))
            try:
                r = T.build_topic(
                    spec, store=store, embed_client=embed_client, llm=llm, index=index,
                    topics_dir=topics_dir, template=template, research_interests=interests,
                    min_sim=args.min_sim, per_paper_cap=args.per_paper_cap,
                    bucket_bonus=args.bucket_bonus,
                    model=args.model, dry_run=args.dry_run,
                )
            except T.TopicError as e:
                r = T.TopicResult(slug=spec.slug, status="failed", error=str(e))
            except Exception as e:      # 单页失败不该带走整批（LLM 偶发超时/网络抖动）
                logger.exception("概念页 {} 构建失败".format(spec.slug))
                r = T.TopicResult(slug=spec.slug, status="failed",
                                  error="{}: {}".format(type(e).__name__, e))
            results.append(r)
            # Y2：跨触发失败冷却记忆——无论本次是全量构建、--topic 显式指定还是 P2
            # 路由触发，失败都登记、成功都清除，这样"是否在冷却"的判断不依赖谁触发
            # 了这次合成。dry-run 下 status 恒为 skipped（build_topic 在调 LLM 前就
            # 提前返回），自然不会走到这两支，不需要额外的 not args.dry_run 判断。
            if r.status == "failed":
                T.record_topic_failure(topics_dir, spec.slug)
            elif r.status in ("new", "merged"):
                T.record_topic_success(topics_dir, spec.slug)
            streak = streak + 1 if r.status == "failed" else 0
            print("  " + _fmt(r))
    finally:
        embed_client.close()
        if llm is not None:
            llm.close()

    if not args.dry_run and topics_dir.exists():
        # 目录页按磁盘现状重建，与本次跑了哪几页无关（--topic 单跑不该让其余页失踪）。
        # specs 用**全集**而不是过滤后的，否则没跑的页会被误标成「已从 topics.yaml 移除」。
        all_specs = T.load_topic_specs(repo_path(args.topics_config))
        idx_path = topics_dir / "INDEX.md"
        from src.scholar.notes_index import write_if_changed
        write_if_changed(idx_path, T.render_topics_index(topics_dir, all_specs))
        print("\n📇 概念页索引：{}".format(idx_path))

    print("\n{}".format("─" * 60))
    for r in results:
        print(_fmt(r))
    bad = [r for r in results if r.status in ("failed", "conflict")]
    print("{} 页成功 · {} 页跳过 · {} 页失败或冲突".format(
        sum(1 for r in results if r.status in ("new", "merged")),
        sum(1 for r in results if r.status == "skipped"), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
