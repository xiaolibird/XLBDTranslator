# 已知缺陷台账

每条一份 md，文件名 `YYYY-MM-DD-<短名>.md`。开头建议带**速查表**（触发命令 / 具体位置 / 报错情况 / 影响 / 复现测试）——
先写清「在哪、什么现象、怎么复现」，修法只作为备选记录。本目录是待处理台账，不是决策记录（决策进 `docs/decisions/`）。

> ⚠️ `.gitignore` 里 `docs/*` 是全忽略、靠 `!` 白名单逐个放行的（`.gitignore:111` 起）。
> 本目录已加 `!docs/bugs/`；**新建其它 docs 子目录时记得补放行**，否则文件 git 根本看不见。

> 下表按目录实际内容生成，是**快照**。多个会话会并发往这里加条目（本次生成时就有另一会话在写），
> 增删后请重新核对，不要只信这张表。

---

## 待处理（20 条）

| 缺陷 | 具体位置 | 状态 |
|---|---|---|
| [finalize「僵死」的候选根因：卡的不是向量同步，是它之后的 topics 子进程](2026-09-03-finalize-topics-mistaken-for-hang.md) | `scripts/read_pdf.py:757-777`（`_rebuild_month` 尾部，锁外… | **候选根因，待抓栈证实**——本条是 |
| [分块通读草稿的四类系统性错误（一批四篇全中）](2026-09-03-ingest-draft-systematic-errors.md) | `src/scholar/pdf_ingest.py:445 _read_one`（逐块通读）与 `:5… | **未修** |
| [元数据退化到 pdf-llm、DOI 全空，但 ingest 回执的「⚠️ 需要注意」块不报](2026-09-03-metadata-degradation-not-flagged.md) | `scripts/read_pdf.py:186-188` `_print_attention` 的 `… | **未修** |
| [摘要降级通路对存量脚本实质失效：abstracts.json 只有 3 篇，而摘要明明存在 md 里](2026-09-04-abstract-fallback-dead.md) | `src/scholar/closereading.py:226` | **未修复** |
| [auto 月度札记缺 sidecar：阅读深度量尺永久不可恢复，且写入失败被静默吞掉](2026-09-04-auto-sidecar-missing.md) | — | **未修复**（已定位根因与影响面，修法有两条路待选） |
| [精读节标题是机读锚点：往里加后缀会静默丢掉 reading_source（已踩过一次）](2026-09-04-closeread-heading-contract.md) | — | **未发生于主干**（我引入后当轮撤回），已留回归测试防复发 |
| [长论文仍被截尾：137,400 字符上限对 80 页以上的论文不够](2026-09-04-closeread-still-truncates.md) | `src/scholar/settings.py:218` | **未修复**（上限已从 120k 抬到 137,400，仍不够；且不能单独再抬） |
| [Crossref 的 article-number 被丢弃：e-locator 期刊的书目条目没有定位号](2026-09-04-crossref-article-number-lost.md) | `src/scholar/crossref.py:89` | **未修** |
| [`digest --month` 会静默整篇覆盖历史月度札记，无存在性检查、无备份、无提示](2026-09-04-digest-month-overwrite.md) | `src/scholar/notes.py:344` | **未修复** |
| [finalize 收尾在向量库同步处僵死不退出（无报错、0% CPU）](2026-09-04-finalize-vector-sync-hang.md) | finalize 收尾的向量库同步：`src/scholar/embed_store.py:818 sy… | **症状已确证，阻塞点未定位**——四条最可能的嫌疑已逐一排除，需下次复现时抓栈 |
| [取全文只走两条路：月度流水线系统性漏掉本来拿得到的全文](2026-09-04-fulltext-routes-too-narrow.md) | `src/scholar/fulltext.py:85` | **未修复**（补抓脚本里已有可用实现，尚未下沉进主链路） |
| [索引没有 keeper 视图：按 citekey 建字典必被 duplicate 覆盖，验收已两次误判](2026-09-04-index-keeper-view-missing.md) | — | **未修复** |
| [ingest 的 `degraded` 状态：全部块都成功也会触发，原因不落盘，且 skill 协议里没有这个状态](2026-09-04-ingest-degraded-silent.md) | `src/scholar/pdf_ingest.py:917` | **未修复** |
| [duplicate 条目的行内 citekey 只告警不修：每轮重建都复读同一批，改一条要手工动三处派生物](2026-09-04-inline-citekey-mismatch-warn-only.md) | `src/scholar/notes_index.py:835` | **未修**（本次手工修了 3 条，机制照旧） |
| [手动精读升级已入库论文时，keeper 拿到后缀键、干净基键被 duplicate 占住并从全局书目里消失](2026-09-04-manual-upgrade-citekey-suffix.md) | `notes_index.py:836` | **未修**。本轮已人工处置 1 例（`shi2025Federated`），存量还剩 … |
| [存量条目缺 DOI 从未回补：36 篇连身份标识都没有，取全文与去重同时失效](2026-09-04-missing-identifier-backfill.md) | `src/scholar/crossref.py:111` | **未修复** |
| [P4 的五处门/证据缺口（三轮对抗审期间实测发现）](2026-09-04-p4-gate-gaps.md) | `tests/test_mnn_line.py:198` | **未修复**（全部为实测确认，非推断） |
| [父进程被杀后 build_topics 子进程变孤儿：概念页在无人知情的情况下继续重写](2026-09-04-topics-subprocess-orphaned-on-parent-kill.md) | `src/scholar/topics.py:1455` | **未修** |
| [`fulltext_truncated` 混用两种成因，覆盖率百分比是误导性指标](2026-09-04-truncated-flag-conflates-two-causes.md) | `src/scholar/closereading.py:34` | **未修复**（轻，且已确认不影响证据质量——见「这条为什么不紧急」） |
| [Zotero connector 的 saveItems 吞掉 target：条目静默落进「未归档条目」](2026-09-04-zotero-saveitems-target-ignored.md) | — | **已修**（改动未提交，见文末） |

## 已修复 / 待归档（1 条）

| 缺陷 | 具体位置 | 状态 |
|---|---|---|
| [finalize 整月重建的并发缺口：盘上有 bundle 的论文会被静默抹出札记](2026-09-03-finalize-concurrency.md) | `scripts/read_pdf.py:408` —— `if (broken or skipped)… | **已修复**（2026-09-04，见文末「修复」一节）。诊断部分原样保留——它论证了… |
---

## 已排除（记录以免重复排查）

| 曾疑为缺陷 | 结论 |
|---|---|
| 索引里 84 组「撞键」 | **非缺陷**。全部是同一篇论文的多条记录，去重层已正确标 `duplicate_of`；过滤掉 duplicate 后每组只剩 1 条存活，`notes_index.py --fix-collisions` 跑下去会改 **0 条**。其中 62 组是跨月重复命中（`dedup_key` 相同），22 组是同一篇拿到两个身份键（预印本 vs 正刊、抓到 DOI vs 退化成标题键），后者由标题相似度层正确合并 |
| arXiv 版本号在身份键与冲突守卫两处剥法不一致 | **非缺陷，是刻意设计**。`_ARXIV_VER_RE`（`notes_index.py:159`）只用于**比较层**归一；`dedup_key_fields` 的 arxiv 档刻意保留 `vN`——身份键要精确、冲突守卫要宽松，取向相反。该处注释已明写「别顺手把它也统一了」（修复见 commit `ae42b41`） |
| 「按论文 id append / 单篇 upsert」替代整月重写 | **不成立**。月度 md 小节标题内嵌全月排名与优先级档位（`_priority_tier(rank, total)` 依赖全月总数），新增一篇会改动其后所有篇的标题；且全局索引是并查集 + 相似度合并的全量计算，无法增量。回归测试：`test_adding_a_paper_renumbers_existing_headings` |
