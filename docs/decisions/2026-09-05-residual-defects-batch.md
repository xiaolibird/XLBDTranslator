# 2026-09-05 残余缺陷批：4 条推后 + 1 条默认关 → 全部收掉，3 轮对抗审核 + 3 项压测

> 本文是执行留痕与「为什么长这样」的权威出处。逐条诊断与修法细节在 `docs/bugs/*.md` 各自文末的
> 「修复（2026-09-05）」节，本文只记**跨条目的决策、协议与教训**。上一批见
> `2026-09-04-bugs-ledger-cleanup.md`；本批是它的续篇——把它推后的四条与默认关的一条收掉。
>
> 流程：用户拍板「四项代码 + 三项存量数据操作全做」→ 编排者实现 → 3 轮 × 3 个**无记忆** subagent
> 对抗审计（镜片每轮全换：契约回归/对抗构造/数据脚本 → 无人值守/文档诚实/变异审计 → 上批契约/数据操作方案/留痕可读性），
> 每条发现由 3 名独立怀疑者证伪、多数证伪即淘汰 → 编排者修 → 3 项压测（真网络 / 409 份真 PDF / 数据操作彩排）。

## 一、这批做了什么

| 条目 | 修法要点 |
|---|---|
| 长论文截尾（推后→修） | **两轮读**：抽文把总上限之外的余量原样带回（`PdfText.tail`），`read_tail_appendix` 先按引文标记密度剔参考文献区（边界收缩到行），再按独立预算 46200/4 块切块通读、独立汇总，分节以「附录·」前缀并入。不抬 `closeread_max_chars`——那与汇总预算零和耦合。EPMC 正文同样带尾段。 |
| `fulltext_truncated` 混用两种成因（推后→修） | 五处联动一次做完：`truncated_by_total_cap` / `truncated_by_page_cap` / `tail_chars` → sidecar/索引 → vault front matter（受管键元组）→ ingest 回读 → backfill_deepread 写回。缺失 = 未知，`SCHEMA_VERSION` 不动。 |
| 分数抽成整数（推后→修） | 读 drawing 层认分数线：短细横线、**孤立**（同高度无别的线段）、上下各恰一个短词块且居中重叠、`_fraction_like` 排除表格/引文/小数形态；文本层只在几何认出的两个词块在 `get_text("words", sort=True)` 里**相邻**且文本唯一匹配时改成 `a/b`。409 份真 PDF 修回 11 处、抽查零误改。 |
| 取全文四通道（默认关→默认开） | 三道闸：每批额度 80（= 候选 20 篇 × 4 路，真网络实测 24 只够 6 篇）、按宿主熔断 3 次 + 30 分钟半开、路间 1.5s；EPMC 两次请求与 arXiv 标题路都改走 `polite_get`；5xx 服务端故障也计终局失败（否则 503 潮永不跳闸、整批被记 no_output）。被熔断/额度抑制的篇目记 `routes_suppressed`，账本当暂时性失败（不进永久跳过）。 |
| 存量缺 DOI（推后→第一步落地） | `scripts/inventory_missing_ids.py` 只读盘点 317 篇：Crossref + OpenAlex（`filter=title.search`）三分类 + 建议放弃；两源 DOI 不一致归 ambiguous；不改索引。回填仍须走「改身份键要扫派生物」。 |
| 定位号存量（36→227 条） | `scripts/backfill_article_numbers.py`：只碰缺 page 且带非 arXiv DOI 的期刊条目、只改 page、`--apply` 前备份原件、`--limit` 用完的候选记「未回查」不混进「源里也没有」。不用 `repair_references --force`（整月重建）。 |

## 二、跨条目的决策

1. **「新能力一律默认关」这次被推翻了三次，理由各不同，都要写清。** 四通道默认开是用户拍板；前提是三道闸 + 真网络 24 篇实测（限流被熔断挡在批内、额度未耗尽）。两轮读默认开：它只在被总上限截尾的篇目上触发（现库 14 篇），成本上界写进三处注释（18 次/篇、90 次/批），`closeread_tail_max_chars=0` 一键回退。分数修复默认开：409 份真 PDF 全量扫描 11 处修回零误改，且改写门槛是几何 + 文本同位 + 唯一匹配三重。
2. **判据要与真实数据对齐，不与想象对齐。** 参考文献区不认标题（10 份里 6 份认不出），认引文标记密度；分数不按正则猜（会改到别处的数字），按 drawing 层几何 + 阅读顺序相邻；额度按候选篇数定（按裸调用数定时 6 篇就吃光）。三处都是先在真 PDF / 真网络上量了再定。
3. **暂时性失败必须与确定性失败分开记账——这条上一批立的规矩，本批新加的闸必须遵守。** 熔断/额度抑制造成的「没全文」若记成 `no_output` 就会进永久跳过集，正是上一批 quota-failure 那条的复发形态。故 `resolve_oa_pdf` 把抑制写进 `extra`，一路带到账本。
4. **第二轮是加法，任何路径上都不许变成减法。** 第二轮异常只记 diag、第一轮结果保留；数字回查的对照源只加真正喂进第二轮的尾段，不加整段原始尾段（被剔的引文里年份/页码会给编出来的数字背书）。
5. **存量数据操作先在完整副本上彩排一遍，再碰真库。** 三步（改键 → 定位号回填 → 重建）在 scratch 副本上全过：书目不丢键、只改 page、备份齐、全局书目跟上。

## 三、协议与教训

- **无记忆审计员的第 1 轮在会话限额上死了 11 名怀疑者，3 条发现没人裁决。** 编排者自核后一并修，并在简报里加「第 1 轮已修」清单让第 2 轮怀疑者据此淘汰陈旧发现。教训：证伪阶段的 agent 数 = 发现数 × 3，16 条就是 48 个 agent；把额度留给证伪。
- **测试替身兼容不能靠「替身身份比较」一招——spy 型替身会递归。** 拆成「查替身 → impl」两层，薄包装直接调 impl。
- **真网络与真 PDF 的压测由编排者自跑，agent 只读快照。** 上一批学到的「变异运行器会留下未还原的变异」这次没再犯：所有会改文件的东西都在 scratch 副本上。
- **「暂时性失败分开记账」别只认「被我们主动抑制」的那半边，还得认「路由真的报错」。** 第 1、2 轮只把熔断/额度抑制接进了 `routes_suppressed`，第 3 轮发现 5xx 服务端故障与跳闸前的传输异常仍会静默落进 `no_output` 永久跳过（第 3 轮 R3-A1）：`polite_get` 对 5xx 原本 `record(url, True)` 清连败，等于 503 潮永不触发熔断、整批被当确定性失败沉淀。修法 = 5xx 也按终局失败计（会跳闸）。剩下「跳闸前的头 1~2 篇」因熔断暖机窗口仍可能被误记，这是「先失败 N 次才知道在故障」的固有下界，已在注释里说明。

## 四、用户可见的变化

- 精读札记里被总上限截尾的篇目会多出「附录·…」分节；索引/vault 多三个字段。
- PDF 里的叠式分数（8/3、1/n、|Pg|/|P|）不再被抽成 83、1n、|Pg||P|。
- 周度 ingest / 月度 backfill 的精读环节会多打 API：择优阶段 20 篇候选约 +2~4 分钟；日志里可能出现「⛔ … 连续 3 次限流/失败，本进程内暂停请求该宿主」。
- `settings.processing` 新增 `closeread_tail_max_chars` / `closeread_tail_max_chunks` / `pdf_fraction_fix` / `fulltext_extra_max_per_batch` / `fulltext_host_breaker_failures`；`fulltext_extra_routes` 默认 True。
- 两个新脚本：`scripts/inventory_missing_ids.py`、`scripts/backfill_article_numbers.py`。

## 五、存量数据操作（按序；每步回滚点在括号里）

（压测通过后由编排者在真库执行；结果填在这里）

1. `scripts/notes_index.py --fix-inline-citekeys --apply`（改 `lin2025Addressing` 等陈旧行内键）→ 按工具回执跑 topics 刷新与向量库同步。
   **回滚**：改键前已把 refs/index/含行内键的 md 打进 `scratchpad/prod-backup-20260905/refs_index_md.tgz`（扁平路径），
   还原 = `tar -xzf <该 tgz> -C output/scholar_notes/` 再 `scripts/notes_index.py` 重建。注意：`fix_inline_citekeys` 自身没有
   像 `backfill_deepread` 那样的 restore 子命令，`RENAME_PARTIAL`（写盘失败且回滚也失败）只能靠这个 tgz 兜底——**这是已知缺口，未在本批补**（第 3 轮审计 R3-B2）。
2. `scripts/backfill_article_numbers.py --email <邮箱> --apply`（约 227 条缺 page 的期刊条目按 DOI 回查 Crossref 补定位号）。
   **回滚**：原件整份在 `.refs_backfill_backup/<时间戳>/`，还原 = `cp <备份>/<文件名> output/scholar_notes/`（脚本本身无 restore 子命令，就是整份拷回）。
3. `scripts/notes_index.py` 重建 `all_references.json`。**回滚**：重建是幂等派生，回滚 = 修好源文件（前两步）后重跑本步即可。
4. `scripts/inventory_missing_ids.py --email <邮箱>`（只读；报告进 `output/scholar_notes/_inventory/`，不碰索引）。
5. `scripts/backfill_deepread.py run --expand --apply --redo --citekey zhu2025Fedweight --citekey zhao2025Multimodal --citekey hong202624hour`（真 LLM）。
   **必须带 `--redo`**：这 3 篇在 `backfill_expand_progress.json` 里是 2026-09-04 旧口径记的 `reason: "no_output"` 且 `had_fulltext: true`
   （额度耗尽却记成「没正文」的那批），不加 `--redo` 会被 `deterministic_failures` 当确定性失败静默跳过、白跑且无提示
   （第 3 轮审计 R3-B1；全库同型陈旧条目共 7 篇，其余 4 篇 `liao2021Improving / li2023Prediction / jin2024Transferb / yoo2025Review` 若要一并救回，可同批 `--citekey` 或迁移其 reason）。
   **回滚**：`.backfill_deepread_backup/<时间戳>/` + `backfill_deepread.py restore` 子命令。

## 六、验证

- 全量 `pytest test/ -q`：**2284 passed / 2 skipped**；`ruff check --select E9,F63,F7,F82 src scripts test` 全过。
- 3 轮 × 3 个无记忆 subagent 对抗审计（镜片每轮全换），每条发现由 3 名独立怀疑者证伪、多数证伪即淘汰。
  第 3 轮三镜片：上批契约 / 数据操作方案 / 留痕可读性——各报 1 条 MAJOR + 若干 MINOR，全部吸收：
  R3-A1（5xx 计终局失败）、R3-B1（数据操作第 5 步必须 `--redo`）、R3-B2（改键还原命令写全）、
  R3-C（8→6、228→227、316→317、README 快照说明）。
- 3 项压测：真网络（24 篇冻结样本，限流被熔断挡在批内、额度未耗尽）、409 份真 PDF 分数扫描
  （11 处修回、抽查零误改）、数据操作彩排（scratch 副本上改键/回填/重建全过）。
