# -*- coding: utf-8 -*-
"""知识层 lint CLI（核心逻辑在 src/scholar/lint.py）。

`build_topics.py --verify` 问的是「这一页的引用还追得回去吗」（格式）。这里问的是
另一类问题：**把整个库摆在一起看，知识本身有没有问题**——

  python scripts/lint_notes.py                      # 四项全跑（对撞要调 LLM）
  python scripts/lint_notes.py --skip-contradictions  # 只跑不花钱的三项（月度自动跑的形状）
  （另有「派生物新鲜度」子项默认随跑：向量库/vault/时间线xlsx/书目 vs 索引，纯计算，
   生产库上才执行；--skip-freshness 跳过。它不在四节的结转机制里，每轮重算。）
  python scripts/lint_notes.py --offline            # 不联网（跳过撤稿检查）
  python scripts/lint_notes.py --dry-run            # 只统计候选，不调 LLM、不落盘

产物：output/scholar_notes/topics/_lint.md（`_` 前缀 + 无 `topic` 键，双重保证它
不会被当成一页概念页参与索引/审计/vault 同步/日历兜底，见 lint.is_topic_page_file）。

**报告是「库的当前状态」，不是「最后一次调用的快照」**：本轮跳过的一项会把上一版
那一节**原样结转**并标明时效（"以下是 12 天前那次运行的结果，不代表当前状态"），
frontmatter 的各计数与 `checks_ran_at` 同样结转。所以 `--offline --skip-stale
--skip-coverage` 这种窄跑不会再把撤稿那一节清空（见 lint.split_lint_sections）。

**报告同时是「给人看的待办」**：对撞/陈旧/孤儿三节的每一条都带一个稳定 ID，用户在
「我的批注」区写 `- ack: <id> <说明>` 就能把它折进 `<details>`。ack 读**两份**文件——
`output/scholar_notes/topics/_lint.md` 与 vault 副本 `<vault>/02-主题/_lint.md`
（`--vault-dir` / 环境变量 `SCHOLAR_VAULT_DIR` / 探测 `~/Documents/ScholarVault`），
取并集：人读报告的地方是 Obsidian，而那份副本有它自己独立的批注区。
折叠对**结转来的那一节**同样生效（lint.fold_acked_blocks，纯文本、不重跑 LLM）——
月度自动化固定跑 `--skip-contradictions`，不这么做 ack 在默认节奏下永远不生效。

退出码：
  0  跑完，没有需要立刻人工介入的发现
  1  **发现已撤稿论文仍在库中**（唯一会让自动化链路喊人的硬信号）
  2  配置/索引/向量库异常，或报告落盘冲突，**且本轮没有撤稿命中**
**1 优先于 2**：撤稿命中与落盘冲突同时发生时退 1，冲突的事实同时打进 stdout 与
stderr（`summarize_lint_run` 两边都读）。理由是"发现了撤稿"这个信息绝不能因为报告
写不进磁盘而丢失——那正是这套链路反复强调要避免的"发现被静默降级"。
这份表同时是 lint.LINT_EXIT_CODES 的权威出处，改一处记得改另一处。
（没有"Ollama 不可用"这一档：对撞是库内向量两两点积，不 embed 任何新文本，
 因此与 build_topics.py 不同，这条链路根本不依赖 Ollama 在线。）

为什么只有撤稿退非零：矛盾/陈旧/缺口都是「值得人看一眼」，天天退非零会让 launchd
日志里的红色失去意义；而库里躺着一篇被撤销的论文，是必须当天处理的（它可能正在给
某几页概念页的论断当地基，报告里会点名是哪几页）。
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.paths import repo_path                                   # noqa: E402
from src.scholar.schema import ScholarSettings                            # noqa: E402
from src.scholar.embed_store import (                                     # noqa: E402
    DB_NAME, INDEX_NAME, VectorStore, VectorStoreError, read_index_generated_at,
)
from src.scholar.llm_client import LLMClient                              # noqa: E402
from src.scholar import lint as L                                         # noqa: E402
from src.scholar import topics as T                                       # noqa: E402
from src.utils.logger import get_logger                                   # noqa: E402

logger = get_logger("lint_notes")


def main() -> int:
    ap = argparse.ArgumentParser(description="札记库知识层 lint：矛盾/撤稿/陈旧/缺口")
    ap.add_argument("--config", default="config/scholar.env")
    ap.add_argument("--prompt", default=L.DEFAULT_PROMPT, help="对撞裁决 prompt 模板")
    ap.add_argument("--skip-contradictions", action="store_true",
                    help="跳过跨文献对撞（唯一要调 LLM 的一项；月度无人值守链路默认这么跑）")
    ap.add_argument("--offline", action="store_true",
                    help="不联网，跳过撤稿检查（报告里会写明这一项本轮未执行）")
    ap.add_argument("--skip-stale", action="store_true", help="跳过陈旧论断检查")
    ap.add_argument("--skip-coverage", action="store_true", help="跳过覆盖缺口统计")
    ap.add_argument("--skip-freshness", action="store_true",
                    help="跳过派生物新鲜度子项（向量库/vault/时间线xlsx/书目 vs 索引；"
                         "纯计算不联网。跳过时报告里整块缺席、frontmatter 整键缺席——"
                         "它每轮重算，不走四节的结转机制）")
    ap.add_argument("--grace-seconds", type=int, default=None,
                    help="覆写 freshness 全部子项的宽限窗（秒）。默认按子项分档"
                         "（vault/xlsx 600、向量库 1800）；验收/排障时用 0 强制"
                         "把「源侧刚变」的未判定态压成陈旧态")
    ap.add_argument("--pair-min-sim", type=float, default=L.DEFAULT_PAIR_MIN_SIM,
                    help="对撞候选的相似度下限（默认 {}；**不要照抄概念页召回的 0.55**，"
                         "那是 query↔证据口径，见 lint.DEFAULT_PAIR_MIN_SIM）".format(
                             L.DEFAULT_PAIR_MIN_SIM))
    ap.add_argument("--max-pairs", type=int, default=L.DEFAULT_MAX_PAIRS,
                    help="单次运行最多送多少对给 LLM 裁决（默认 {}，<=0 不限）".format(
                        L.DEFAULT_MAX_PAIRS))
    ap.add_argument("--batch-size", type=int, default=L.DEFAULT_BATCH_SIZE,
                    help="每批喂几对（默认 {}）".format(L.DEFAULT_BATCH_SIZE))
    ap.add_argument("--stale-years", type=int, default=L.DEFAULT_STALE_YEARS,
                    help="论断的最新支撑文献早于这么多年即标记（默认 {}）".format(
                        L.DEFAULT_STALE_YEARS))
    ap.add_argument("--stale-anchor-limit", type=int, default=L.DEFAULT_STALE_ANCHOR_LIMIT,
                    help="陈旧那一节最多逐条列几篇锚文献（默认 {}，<=0 不限）。"
                         "这一节的量是**日历驱动**的：阈值是 `当前年 - --stale-years`，"
                         "所以每年 1 月它自己就会变长一截（2027 年 62 条、2028 年 116 条），"
                         "没有 cap 的话它是三节里唯一会无声长到上千行的".format(
                             L.DEFAULT_STALE_ANCHOR_LIMIT))
    ap.add_argument("--orphan-limit", type=int, default=L.DEFAULT_ORPHAN_LIMIT,
                    help="孤儿论文最多列几篇（默认 {}，<=0 不限）".format(L.DEFAULT_ORPHAN_LIMIT))
    ap.add_argument("--recent-months", type=int, default=L.DEFAULT_RECENT_MONTHS,
                    help="孤儿名单里「最近入库、多半只是还没排进前 max_evidence 名」"
                         "那一档的窗口（默认 {} 个月）".format(L.DEFAULT_RECENT_MONTHS))
    ap.add_argument("--contradiction-reminder-days", type=int,
                    default=L.DEFAULT_CONTRADICTION_REMINDER_DAYS,
                    help="距上次跨文献对撞超过这么多天就在报告与 stdout 里提醒补跑"
                         "（默认 {}；对撞是四项里唯一花钱的一项，月度自动化不跑它）".format(
                             L.DEFAULT_CONTRADICTION_REMINDER_DAYS))
    ap.add_argument("--topics-config", default="config/topics.yaml",
                    help="概念页配置（只用来读各页 max_evidence，判断证据厚度是"
                         "「真的薄」还是「卡在配置上限」）")
    ap.add_argument("--vault-dir", default=None,
                    help="Obsidian vault 根目录。**报告的 ack 会同时读 vault 那份副本**"
                         "（L1：人读报告的地方是 Obsidian，而 sync_topics_to_vault 给"
                         "vault 副本自己独立的批注区，此前在那里写 ack 静默无效）。"
                         "不给时读环境变量 SCHOLAR_VAULT_DIR，再不给就探测 "
                         "~/Documents/ScholarVault（探测不到就只读札记库那一份）")
    ap.add_argument("--mailto", default="", help="OpenAlex polite pool 的联系邮箱")
    ap.add_argument("--model", default=None, help="覆盖裁决用的 LLM 模型")
    ap.add_argument("--dry-run", action="store_true",
                    help="只统计候选与纯计算项，不调 LLM、不落盘")
    ap.add_argument("--json", action="store_true", help="额外把可机读摘要打到 stdout")
    args = ap.parse_args()

    cfg = repo_path(args.config)
    settings = ScholarSettings.from_env_file(cfg) if cfg.exists() else ScholarSettings()
    notes_dir = repo_path(settings.processing.notes_dir)
    index_path = notes_dir / INDEX_NAME
    topics_dir = notes_dir / T.TOPICS_DIRNAME

    if not index_path.exists():
        print("找不到索引：{}\n先跑：PYTHONPATH=. python scripts/notes_index.py".format(index_path),
              file=sys.stderr)
        return 2
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        assert isinstance(index.get("papers"), list)
    except Exception as exc:
        print("索引损坏（{}）：{}".format(type(exc).__name__, index_path), file=sys.stderr)
        return 2
    if not topics_dir.exists():
        # 四项里有三项读概念页。没有概念页时撤稿检查仍然成立且有价值，所以不退出，
        # 只把话说清楚——静默产出一份"0 条陈旧论断、0 个孤儿"的报告会读成"一切正常"。
        print("ℹ️ 还没有概念页目录（{}）：陈旧论断/覆盖缺口两项本轮无从谈起，"
              "撤稿检查照常。先跑 scripts/build_topics.py 生成概念页。".format(topics_dir),
              file=sys.stderr)

    counts = L.LintCounts()

    # ---- 撤稿（唯一联网项，放最前：它是唯一会改变退出码的一项，先拿到结果）----
    retraction = L.RetractionScan(skipped=True)
    if not args.offline:
        proc = settings.processing
        # 与 ingest/backfill/read_pdf 取联系邮箱的既有口径一致（zotero_email 优先，
        # 回落 external_email）——OpenAlex 的 mailto 只是 polite pool 的礼貌参数，
        # 不是密钥，缺了也能查，只是配额更紧。
        mailto = args.mailto or getattr(proc, "zotero_email", "") or \
            getattr(proc, "external_email", "") or ""
        print("☣️ 撤稿检查：查 OpenAlex is_retracted……")
        retraction = L.check_retractions(index, mailto=mailto)
        # 只数**未处置**的：已打 `⚑ RETRACTED` 的那些按新口径会永远留在库里
        # （札记保留、只踢出向量库），算进去的话每月都退 1、每月都弹通知。
        counts.retracted = len(retraction.unhandled)
        counts.scan_failed_dois = retraction.n_failed
        print("   keeper {} 篇 · 有 DOI {} · 解析到 {}（{:.0%}）· 无 DOI {} · 查无 {} · 未查成 {}".format(
            retraction.n_papers, retraction.n_with_doi, retraction.n_resolved,
            retraction.coverage, retraction.n_no_doi, retraction.n_unresolved,
            retraction.n_failed))
        for h in retraction.unhandled:
            print("   🚨 已撤稿**且未标记**：[@{}] {}".format(h.citekey, h.title[:80]))
            print("      处置：在札记的「裁决」行加 `⚑ RETRACTED`，再跑 "
                  "notes_index.py + notes_embed.py 把它踢出向量库")
        for h in retraction.hits:
            if h.acknowledged:
                print("   ✅ 已撤稿已标记（札记保留、已不在向量库）：[@{}]".format(h.citekey))

    # ---- 对撞（唯一花钱项）----
    candidates = []
    verdicts = []
    vrep = L.VerdictReport()
    # 「本轮到底有没有真的裁决过」是报告里必须说清的一件事——没裁决而报告写成
    # 「本轮没有判定为张力的句对」，读起来就是「查过了，很干净」，是这份报告最该
    # 避免的失败模式（同撤稿那节写分母的理由）。只有真的调过 LLM 才置 False。
    contradictions_skipped = True
    if not args.skip_contradictions:
        db_path = notes_dir / DB_NAME
        try:
            store = VectorStore.load(db_path)
        except VectorStoreError as e:
            print("❌ 向量库不可用：{}".format(e), file=sys.stderr)
            return 2
        # 陈旧库的 citekey 可能是已改名/已删除的旧键（同 build_topics.py 的处理）：
        # 只提醒不阻塞——这里是人工发起的命令，用户看得见这行。
        warn = store.freshness_warning(read_index_generated_at(index_path))
        if warn:
            print(warn, file=sys.stderr)
        # 注意这条链路**不需要 EmbeddingClient**：候选生成是库内向量两两点积
        # （citable 子集 × refutable 子集），从头到尾没有"把一句新文本变成向量"的
        # 需求，所以既不依赖 Ollama 在线，也不存在"查询向量与库向量模型不一致"的
        # 风险（一个库只有一个模型）。build_topics.py 那边要 probe 是因为它要现场
        # embed queries，那个理由在这里不成立——照抄过来只会凭空多一个能挂的依赖。
        print("⚔️ 对撞候选：全库跨论文「可引用证据 ↔ 可反驳观点」句对（≥{}）……".format(
            args.pair_min_sim))
        candidates = L.find_contradiction_candidates(
            store, min_sim=args.pair_min_sim, max_pairs=args.max_pairs)
        L.attach_pair_titles(candidates, index)
        counts.candidates = len(candidates)
        print("   候选 {} 对".format(len(candidates)))
        for p in candidates[:5]:
            print("   {} {:.3f}  [@{}] ↔ [@{}]".format(p.ref, p.score, p.a.citekey, p.b.citekey))

        if args.dry_run:
            print("   --dry-run：不调 LLM 裁决")
            # 候选数是真跑出来的，即使没裁决也如实记；contradictions 保持 None
            # （未裁决），两者语义不同，别用 0 把它们抹平。
        elif not candidates:
            # 候选 0 对是**真实且已完成**的扫描结论（"全库没有任何一对同题反向证据
            # 达到相似度阈值"），不是"没跑"。标成 skipped 会让报告谎报未执行。
            contradictions_skipped = False
            counts.contradictions = 0
            counts.batches_failed = 0
        else:
            try:
                template = L.load_prompt_template(repo_path(args.prompt))
            except T.TopicError as e:
                print("❌ {}".format(e), file=sys.stderr)
                return 2
            llm = LLMClient(settings.llm)
            try:
                verdicts, vrep = L.adjudicate_contradictions(
                    llm, candidates, template,
                    batch_size=args.batch_size, model=args.model)
            finally:
                llm.close()
            contradictions_skipped = False
            counts.contradictions = sum(
                1 for v in verdicts if v.relation in L.REPORTABLE_RELATIONS)
            counts.batches_failed = vrep.batches_failed
            print("   裁决 {} 对 · 张力 {} 处 · 失败 {} 批".format(
                vrep.judged, counts.contradictions, vrep.batches_failed))

    # ---- 陈旧论断 / 覆盖缺口（纯计算）----
    stale_claims = []
    if not args.skip_stale:
        stale_claims = L.find_stale_claims(topics_dir, index, max_age_years=args.stale_years)
        counts.stale_claims = len(stale_claims)
        print("⏳ 证据基础可能过时的论断：{} 条".format(len(stale_claims)))
    coverage = L.CoverageReport()
    if not args.skip_coverage:
        # A2：把 topics.yaml 的 max_evidence 带进来，才分得清"这一页真的薄"与"这一页
        # 卡在配置上限"。加载失败不阻塞——lint 的其它三项与它无关，报告那边 specs 为
        # None 时会如实说"无从判断"并**不给**那句诱导性建议。
        specs = None
        try:
            specs = T.load_topic_specs(repo_path(args.topics_config))
        except Exception as e:
            print("⚠️ 概念页配置读不到（{}），各页证据厚度这一节将无从判断是否卡在配置上限：{}"
                  .format(args.topics_config, e), file=sys.stderr)
        coverage = L.coverage_report(topics_dir, index, orphan_limit=args.orphan_limit,
                                     specs=specs, recent_months=args.recent_months)
        counts.orphans = coverage.n_orphans_total
        print("🕳 覆盖：{}/{} keeper 进过证据池 · 高优先级精读孤儿 {} 篇"
              "（其中最近 {} 个月新入库 {} 篇）".format(
                  coverage.n_cited, coverage.n_keeper, coverage.n_orphans_total,
                  args.recent_months, coverage.n_orphans_recent))

    # ---- ack 的两个来源（L1/N2）----
    # 报告被 sync_topics_to_vault 同步进 vault 后，`merge_topic_page` 给 vault 副本自己
    # 独立的一份「我的批注」区，而报告在那里逐字教用户写 `- ack: …`——首版只读札记库
    # 那一份，用户在 Obsidian 里写什么都没用，且无提示无警告。按项目既定认知「人读东西
    # 的地方是 Obsidian」，这是主路径。
    #
    # vault 路径不在 ScholarSettings 里（`build_vault.py` 把它做成必填 CLI 参数，理由是
    # vault 是用户资产、不能有默认写入路径）。这里是**只读**，所以可以带一层探测：
    # 显式 --vault-dir > 环境变量 > 默认位置（探测不到就安静跳过）。
    vault_topics = None
    # 默认位置的探测只在 notes_dir 确实是生产库时才做（同 T.notes_dir_is_production 的
    # 既有用法）：单测用 tmp_path 隔离，绝不能顺手把用户真实 vault 里的 ack 读进来，
    # 让测试结果取决于这台机器上那份文件的内容。
    #
    # A4：**环境变量与默认探测同一档，不与 `--vault-dir` 同档**。首版的
    # `args.vault_dir or os.environ.get(...) or ""` 让 `SCHOLAR_VAULT_DIR` 整个绕过了
    # 这道门，于是 `SCHOLAR_VAULT_DIR=~/Documents/ScholarVault pytest test/test_lint.py`
    # 会让 tmp_path 跑出来的报告里写进**生产 vault 的绝对路径**，`read_lint_acks` 也真的
    # 去读那个文件——本机那份恰好还不存在所以目前只是路径泄漏，但 vault 只要同步过一次，
    # 生产 acks 就会去折叠 tmp 报告里的条目，测试结果取决于用户批注内容。这正是
    # `notes_dir_is_production` 那段注释记录的「单测意外打到生产」事故形状，守卫此前
    # 只挡住了默认探测那一条腿。
    #
    # 判据是「这是不是用户对**这次调用**的明确动作」：`--vault-dir` 是（无条件生效），
    # 环境变量是**环境级**的、跟这次调用无关（与默认探测同等对待）。
    env_vault = os.environ.get("SCHOLAR_VAULT_DIR") or ""
    if env_vault and not T.notes_dir_is_production(notes_dir):
        env_vault = ""
    raw_vault = args.vault_dir or env_vault
    cand = (Path(raw_vault).expanduser() if raw_vault
            else (Path.home() / "Documents" / "ScholarVault"
                  if T.notes_dir_is_production(notes_dir) else None))
    # 探测的是**目录**而不是 `_lint.md` 文件：首次运行时 vault 里那份副本还不存在
    # （它由 build_vault.py 下一次同步才生成），而恰恰是这一次的报告要把 vault 那条
    # 路径写进指引里告诉用户"你也可以写在那儿"。
    probe = (cand / T.VAULT_TOPICS_DIR) if cand else None
    if probe is not None and probe.is_dir():
        vault_topics = probe
    elif raw_vault:
        # 显式给了路径却找不到：说清楚，别让用户以为自己在 Obsidian 里写的 ack 生效了。
        print("⚠️ vault 概念页目录不存在（{}）：本轮只读札记库那一份 ack"
              "（在 Obsidian 里写的不生效）".format(probe), file=sys.stderr)
    ack_files = [topics_dir / L.LINT_REPORT_NAME]
    if vault_topics is not None:
        ack_files.append(vault_topics / L.LINT_REPORT_NAME)
    acks = L.read_lint_acks(topics_dir, extra_dirs=[vault_topics] if vault_topics else None)
    if acks:
        print("✔️ 读到 {} 条 ack（来源：{}）".format(
            len(acks), "、".join(str(p) for p in ack_files)))

    # ---- 派生物新鲜度（freshness；纯计算，不进四节的结转机制）----
    # 生产守卫用 **exact-match** 口径（同 sync_vault.py 的 is_real——2026-09-01
    # 假数据盖真表事故的守卫），**不是** notes_dir_is_production 的"output/ 之下"
    # 口径：freshness 读的是全局单例派生物（真实桌面 xlsx 的 sidecar、
    # ~/Documents/ScholarVault/_meta.json），output/ 下随便一个副本库拿自己的索引去
    # 比真实派生物只会得出假"陈旧"，再照报警执行同步就是对生产库误操作。非生产库
    # 时整块不执行（比"全部未判定"更强的隔离：tmp 库的 CLI 测试报告逐字节不变，
    # 191 个既有用例的零改动承诺靠它成立）。
    freshness_block = None
    freshness_stale = None
    if not args.skip_freshness:
        is_prod = notes_dir.resolve() == repo_path("output/scholar_notes").resolve()
        if not is_prod:
            print("🧭 freshness：非生产库，本轮未执行（生产守卫，不拿 tmp 索引比对真实派生物）")
        else:
            from src.scholar import lint_freshness as F
            # 时间线路径复用 export_timeline_xlsx 的单一出处（文件名口径二次实现
            # 漂移 = 永久假阳性）；vault 根复用上面 ack 已解析的 cand（探测成功才用）
            from scripts.export_timeline_xlsx import DEFAULT_OUT, _meta_path
            grace = ({k: args.grace_seconds for k in F.GRACE_DEFAULTS}
                     if args.grace_seconds is not None else None)
            # vault_topics 非 None 恰等价于"cand 存在且其概念页目录探测成功"
            fresh_vault = cand if vault_topics is not None else None
            rep = F.check_freshness(
                index_path, notes_dir, fresh_vault, DEFAULT_OUT, _meta_path(DEFAULT_OUT),
                grace_overrides=grace)
            for ln in rep.stdout_lines():
                print(ln)
            freshness_block = rep.render_block()
            freshness_stale = rep.n_stale

    now = datetime.now()
    body = L.render_lint_report(
        verdicts=verdicts, candidates=candidates, verdict_report=vrep,
        retraction=retraction, stale_claims=stale_claims, coverage=coverage,
        cited_by_page=L.cited_by_page(topics_dir),
        params={"pair_min_sim": args.pair_min_sim, "max_pairs": args.max_pairs,
                "batch_size": args.batch_size},
        contradictions_skipped=contradictions_skipped,
        stale_skipped=args.skip_stale, coverage_skipped=args.skip_coverage,
        stale_years=args.stale_years,
        stale_anchor_limit=args.stale_anchor_limit,
        now=now,
        # A1：跳过的一节从上一版结转；A6：已 ack 的张力折叠。两者都读现存报告，
        # 读不到/解析不出一律降级成空（绝不因为上一版被改坏就整份报告生不出来）。
        previous=L.read_previous_lint(topics_dir),
        acks=acks,
        # L1：报告里那句 ack 指引写死绝对路径，并把两份文件都点名——否则用户仍然不知道
        # 自己该在哪一份里写才算数。
        ack_files=[str(p) for p in ack_files],
        # L6：顶部状态行一行两用（时效 + 计数）。结转那半的计数从上一版 frontmatter 取，
        # 与落盘时的 carry_forward_counts 同一份数据、不同时机。
        counts=counts, previous_counts=L.read_previous_lint_counts(topics_dir),
        contradiction_reminder_days=args.contradiction_reminder_days)

    topics_dir.mkdir(parents=True, exist_ok=True)
    path, status = L.write_lint_report(topics_dir, body, counts, dry_run=args.dry_run,
                                       now=now, freshness_block=freshness_block,
                                       freshness_stale=freshness_stale)
    icon = {"new": "🆕", "merged": "✅", "unchanged": "＝", "conflict": "⚠️"}.get(status, "?")
    print("\n{} 报告：{}（{}）".format(icon, path, status))
    conflict_msg = ("⚠️ 报告落盘冲突：哨兵缺失或生成块被手改，本轮报告未写入磁盘"
                    "——把批注移到「我的批注」下后重跑。")
    if status == "conflict":
        # B2：**两个流都打**。撤稿命中时退出码被 1 占用（发现优先于工具故障），
        # 冲突这件事只能靠输出传下去，而 summarize_lint_run 在退出码 1 的分支里读的
        # 是 stderr，退出码 2 的分支里也读 stderr——两边都写才不会漏。
        print(conflict_msg)
        print(conflict_msg, file=sys.stderr)

    # A5：对撞离上次太久时提醒补跑。从刚渲染出来的 section 标记里反解 ran_at，
    # 与报告正文用的是同一份数据（顺带证明标记确实解析得回来）。
    reminder = L.contradiction_reminder(
        {k: v[0] for k, v in L.split_lint_sections(body).items() if v[0]},
        now, args.contradiction_reminder_days)
    if reminder:
        print(reminder)

    if args.json:
        machine = dict(counts.__dict__)
        # freshness 不进 LintCounts（不结转），但机读摘要不该比 frontmatter 少料；
        # 与"跳过即缺席"同口径：没跑就没这个键
        if freshness_stale is not None:
            machine["n_freshness_stale"] = freshness_stale
        print(json.dumps(machine, ensure_ascii=False, sort_keys=True))

    # counts.retracted 为 None 表示 --offline（没查），不该退 0 也不该退 1——
    # 退 0 就是在说"查过了没问题"。这里退 0 但上面报告与 stdout 都写明了未执行，
    # 调用方（月度触发器）拿 `--offline` 跑的前提就是它自己知道没查。
    #
    # B2：撤稿判定**优先于**落盘冲突。首版是先 `if status == "conflict": return 2`，
    # 于是一份跟撤稿毫无关系的手改冲突就能把已经查到的撤稿警报压成"lint 未跑成"的
    # 普通通知。退出码只有一个，让发现赢。
    if counts.retracted:
        return 1
    return 2 if status == "conflict" else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)
