# 已知缺陷台账

每条一份 md，文件名 `YYYY-MM-DD-<短名>.md`。开头建议带**速查表**（触发命令 / 具体位置 / 报错情况 / 影响 / 复现测试）——
先写清「在哪、什么现象、怎么复现」，修法只作为备选记录。本目录是待处理台账，不是决策记录（决策进 `docs/decisions/`）。

> ⚠️ `.gitignore` 里 `docs/*` 是全忽略、靠 `!` 白名单逐个放行的（`.gitignore:111` 起）。
> **新建其它 docs 子目录时记得补放行**，否则文件 git 根本看不见。

> 下表按目录实际内容生成，是**快照**（2026-09-04 台账批清理后）。多个会话会并发往这里加条目，
> 增删后请重新核对，不要只信这张表。已修条目的「修复」细节都在各自文末的「修复（2026-09-04 台账批）」节。

---

## 已修复 / 已防复发（20 条，2026-09-04 台账批）

| 缺陷 | 改在哪 | 状态 |
|---|---|---|
| [finalize 整月重建的并发缺口：盘上有 bundle 的论文会被静默抹出札记](2026-09-03-finalize-concurrency.md) | `scripts/read_pdf.py` 锁 + 提交前重查 | **已修复**（c3eb40a） |
| [finalize「僵死」的候选根因：卡的不是向量同步，是它之后的 topics 子进程](2026-09-03-finalize-topics-mistaken-for-hang.md) | `topics.trigger_topic_refresh` / `_run_refresh_child` | **已修复**：pid/耗时/日志路径三行可见；子进程随父死；stdout 落 `logs/topics_refresh/`；SKILL 改写 |
| [父进程被杀后 build_topics 子进程变孤儿](2026-09-04-topics-subprocess-orphaned-on-parent-kill.md) | 同上 | **已修复**（同一处改动） |
| [finalize 收尾在向量库同步处僵死不退出](2026-09-04-finalize-vector-sync-hang.md) | 同上 | **归因已明、已修复**：卡的是向量同步之后的 topics 子进程 |
| [元数据退化到 pdf-llm、DOI 全空，但 ingest 回执不报](2026-09-03-metadata-degradation-not-flagged.md) | `pdf_ingest.resolve_metadata(diag)` / `read_pdf._is_thin_metadata` | **已修复**：判据改为书目可用性，回执带退化原因 |
| [ingest 的 `degraded` 状态：全部块都成功也会触发，原因不落盘](2026-09-04-ingest-degraded-silent.md) | `pdf_ingest.ingest_pdf` 五态 / SKILL | **已修复**：新增 `synth_failed`，失败原因进 `draft_note`，回退协议扩到三态 |
| [分块通读草稿的四类系统性错误](2026-09-03-ingest-draft-systematic-errors.md) | `pdf_ingest.extract_availability_statements` / `--context` / prompt | **部分修复**：②③④ 已修；① 分数抽成整数未修 |
| [`digest --month` 静默整篇覆盖历史月度札记](2026-09-04-digest-month-overwrite.md) | `cli.run_digest` 预检 + `workflow._step_sync_zotero` 守卫 + `--overwrite-notes` | **已修复**：拒绝 + 逃生门 + 覆盖前备份四件套 |
| [Crossref 的 article-number 被丢弃](2026-09-04-crossref-article-number-lost.md) | `crossref.parse_crossref_work` | **代码已修**；存量 36 条回填待跑（命令见文末） |
| [索引没有 keeper 视图](2026-09-04-index-keeper-view-missing.md) | `notes_index.iter_keepers` / `keepers_by_citekey` | **已修复**：embed_store 与 backfill_deepread 已委托 |
| [auto 月度札记缺 sidecar：写入失败被静默吞掉](2026-09-04-auto-sidecar-missing.md) | `notes.write_notes` | **路线 A 已修**：error 级 + `sidecar_ok`；存量 43 月认赔 |
| [摘要降级通路对存量脚本实质失效](2026-09-04-abstract-fallback-dead.md) | `backfill_deepread.abstract_from_note_md` + `--accept-abstract` | **已修复（产出）**：abstracts.json 查不到时回读 md `### 摘要`。**收货默认关**——不加 `--accept-abstract` 时连摘要都不供给（省掉必被拒收的那次 LLM）|
| [duplicate 条目的行内 citekey 只告警不修](2026-09-04-inline-citekey-mismatch-warn-only.md) | `notes_index.fix_inline_citekeys` / `scripts/notes_index.py --fix-inline-citekeys` | **已修复**：三处原子齐改工具（默认 dry-run）+ 告警升 **error 级**、清单挂进索引 `stale_inline_citekeys`。**刻意不 notify**（持久状态每周每月弹同一条会把告警面训练成噪音）|
| [手动精读升级已入库论文时 keeper 拿到后缀键、基键从全局书目消失](2026-09-04-manual-upgrade-citekey-suffix.md) | `notes.write_notes(existing_key_owners)` / `notes_index.existing_citekey_owners` | **A+B 均已修**：同一论文继承基键；存量 `lin2025Addressing` 用工具处置（未在生产库执行） |
| [精读节标题是机读锚点：加后缀会静默丢 reading_source](2026-09-04-closeread-heading-contract.md) | 测试 | **已防复发**：另两对渲染↔解析契约补了往返测试 |
| [Zotero connector 的 saveItems 吞掉 target](2026-09-04-zotero-saveitems-target-ignored.md) | `zotero_sync.save_items(target)` + `updateSession` | **已修**（随本批提交）；BBT 非 ASCII 键加告警 |
| [额度耗尽被记成 `no_output`：35 篇「全文已到手但没读成」混进待下载清单](2026-09-04-quota-failure-looks-like-no-fulltext.md) | `closereading.is_llm_unavailable` + `diag` 出参 / `backfill_deepread.classify_failure` | **已修复**：账本分 `llm_unavailable`、计入熔断、重跑不再跳过 |
| [`replace_closeread` 吞掉精读节之后到下一篇之间的全部内容](2026-09-04-replace-closeread-eats-trailing-content.md) | `backfill_deepread.replace_closeread` | **已修复**（审计中发现）；存量 169 篇的篇末 `---` 已丢，补不补见该文末 |
| [一次 run 内第二篇的备份会覆盖第一篇的原件](2026-09-04-backup-stamp-overwrites-original.md) | `backfill_deepread.backup_files` | **已修复**（审计中发现，判 BLOCKER）：同名已存在就不覆盖 |
| [`find_local_pdf` 单向词面匹配会认下别篇论文的 PDF](2026-09-04-find-local-pdf-one-way-match.md) | `backfill_deepread.find_local_pdf` | **已修复**（审计中发现）：加反向覆盖 + 实词下限，与 fulltext 同一套判据 |

## 已实现但**默认关闭**——月度链路的行为与修复前一致，要不要打开由你决定（1 条）

| 缺陷 | 改在哪 | 状态 |
|---|---|---|
| [取全文只走两条路（自评高严重度：每月都在少读，漏判率约 13.5%）](2026-09-04-fulltext-routes-too-narrow.md) | `fulltext.resolve_oa_pdf(extra_routes)` + `settings.fulltext_extra_routes` | **代码已下沉、开关默认 False**。打开：`config/scholar.env` 加 `PROCESSING__FULLTEXT_EXTRA_ROUTES=true`。默认关的理由：每篇候选多打 2~3 个 API，真网络下的限流表现没验证过；月度择优阶段会放大成几十次请求 |

## 待处理（4 条）

| 缺陷 | 具体位置 | 状态 |
|---|---|---|
| [长论文仍被截尾：137,400 字符上限对 80 页以上不够](2026-09-04-closeread-still-truncates.md) | `src/scholar/settings.py` `closeread_max_chars` | **推后**：需「正文/附录两轮」设计，不能单独抬上限 |
| [`fulltext_truncated` 混用两种成因](2026-09-04-truncated-flag-conflates-two-causes.md) | `src/scholar/closereading.py:34` | **推后**：低严重度，要新增索引字段五处联动 |
| [存量条目缺 DOI 从未回补](2026-09-04-missing-identifier-backfill.md) | `src/scholar/crossref.py` `crossref_lookup` | **推后**：第一步是联网只读盘点，属数据操作 |
| [P4 的五处门/证据缺口](2026-09-04-p4-gate-gaps.md) | `~/Desktop/Lab/P4` | **不属本仓库**，应整体搬去该仓库 |

## 已修但有存量数据待处置（跑之前先 dry-run）

| 事项 | 命令 |
|---|---|
| 陈旧行内键 / 后缀 keeper（`lin2025Addressing` 等） | `PYTHONPATH=. python3.12 scripts/notes_index.py --fix-inline-citekeys`（看计划）→ 加 `--apply` |
| 36 条 e-locator 书目补定位号 | 逐月 `scripts/repair_references.py --month <月> --force --email <邮箱> --dry-run` → 去掉 `--dry-run` → `scripts/notes_index.py` |
| Zotero 里三条未归档条目 | 人在 Zotero 里拖进 ScholarDigest |
| 169 篇被吞掉的篇末 `---` | 见 `2026-09-04-replace-closeread-eats-trailing-content.md` 文末的 dry-run 脚本（纯版面，不影响解析，补不补由你定）|
| 35 篇被记成 `no_output` 的额度失败 | 额度恢复后按 `2026-09-04-quota-failure-looks-like-no-fulltext.md` 文末命令 `--citekey` 点名重跑 |

---

## 已排除（记录以免重复排查）

| 曾疑为缺陷 | 结论 |
|---|---|
| 索引里 84 组「撞键」 | **非缺陷**。全部是同一篇论文的多条记录，去重层已正确标 `duplicate_of`；过滤掉 duplicate 后每组只剩 1 条存活，`notes_index.py --fix-collisions` 跑下去会改 **0 条**。其中 62 组是跨月重复命中（`dedup_key` 相同），22 组是同一篇拿到两个身份键（预印本 vs 正刊、抓到 DOI vs 退化成标题键），后者由标题相似度层正确合并 |
| arXiv 版本号在身份键与冲突守卫两处剥法不一致 | **非缺陷，是刻意设计**。`_ARXIV_VER_RE`（`notes_index.py:159`）只用于**比较层**归一；`dedup_key_fields` 的 arxiv 档刻意保留 `vN`——身份键要精确、冲突守卫要宽松，取向相反。该处注释已明写「别顺手把它也统一了」（修复见 commit `ae42b41`） |
| 「按论文 id append / 单篇 upsert」替代整月重写 | **不成立**。月度 md 小节标题内嵌全月排名与优先级档位（`_priority_tier(rank, total)` 依赖全月总数），新增一篇会改动其后所有篇的标题；且全局索引是并查集 + 相似度合并的全量计算，无法增量。回归测试：`test_adding_a_paper_renumbers_existing_headings` |
