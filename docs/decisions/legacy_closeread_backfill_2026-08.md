# 存量 auto 精读的深度缺口调查（2026-08-23）

起因：用户问「入库依靠的是精读后的文本，精读本身是否需要重跑」。前面几轮审计验证的
都是「向量 ↔ 文本」的一致性，这一问指向上游——**文本 ↔ 论文原文**的保真度。

## 结论先说

**是的，334 篇需要重跑，但不是因为「读错」，是因为「只读了一半」。** 单篇 PoC 实测
可取证句 **6 → 76 条（12.7 倍）**。但**执行不是跑个命令**：现有三个 backfill 脚本
全都不适用，且天真的写回方案风险面是 1608 篇而非 109 篇（见「写回难点」）。

## 一、缺口的来源：2026-08-01 的深读改造

`docs/closeread_deep_rollout.md` 记着：auto 精读在 **2026-08-01 之前是「一次单跳」、
40k 字符预算**，263 篇缓存 PDF 实测**平均只覆盖论文 56.6% 的页面，全覆盖仅 19/263
（7.2%）**。改造后（分块深读、120k）覆盖 97.6%、全覆盖 234/263。

开关 `PROCESSING__CLOSEREAD_DEEP=true` 已于 2026-08-01 打开——**新入库的走深读，
问题是纯存量的，没有在继续产生半截札记**。

## 二、库的真实构成（keeper 2256 篇）

| 类别 | 篇数 | 平均 highlights | 说明 |
|---|---|---|---|
| 纯题录（无 reading_source） | 1582 | 0 | 筛选阶段就决定不精读，**不是缺陷** |
| `reading_source=abstract` | 84 | 4.5 | 明确只读摘要 |
| **`reading_depth=unknown-legacy`** | **334** | **11.4** | **2026-08-01 前的单跳产物 = 缺口** |
| `reading_depth=chunked` | 253 | 56.1 | 深读 + 交叉核验，当前最高标准 |

auto 有全文精读的 356 篇里，**334 篇（94%）是改造前的单跳产物**，只有 21 篇吃到深读。

这 334 篇**100% 缺「实验方法」和「交叉核验记录」**两节。注意
`scripts/backfill_methods.py` 那次「实验方法回填 221 篇」**一篇都没覆盖到它们**——
该脚本 `_iter_bundles()` 只扫 `notes_dir/"manual"`，而这批全是 `series=auto`，
没有 bundle。回填后有「实验方法」的 236 篇里 233 篇是 manual。

## 三、PoC：单篇重读实测（零风险，未写盘）

`meng2021Mimicif`（MIMIC-IF，arXiv 2102.06761），用当前 deep 配置重读：

| | 旧（单跳 legacy） | 新（deep，11 块，sonnet） |
|---|---|---|
| 可取证句 | **6** | **76**（带 role tag 46） |
| sections | 3（关键结论/对我研究的联想/方法与数据） | 8 |
| 新增 | — | 实验方法、结果与效应量、局限与可质疑点、图表与补充材料要点、研究问题、逐节通读要点 |

质量抽验（「实验方法」节）：数据 60/20/20 划分、AutoInt/LSTM/TCN/Transformer/IMV-LSTM
模型清单、AUPRC+AUROC 指标、基线输入 `x'∼U[0,1]`（ArchDetect 取 `x'=0`）、
AutoInt 对类别特征无梯度故梯度类方法不适用——都是「他人可照做」的粒度。

耗时：11 块并发 + 汇总，约 **12 分钟/篇**（比 rollout 文档记的 173 秒慢得多，
因为那是 4 并发批处理的摊薄值，单篇跑没有并发收益）。

**顺带发现**：该篇正文 167539 字符，`closeread_max_chars=120000` **仍被截断**
（日志 `⚠️ 精读偏薄(0): 可取证句 46 条，正文 120000/167539 字符（已截断）`）。
rollout 文档说 p90 约 14 万字符，故长论文仍会截尾——这是独立于本课题的一个小缺口。

## 四、写回难点（为什么不能直接跑）

目标 109 篇（INCLUDE + tier=high）散在 **56 个月度札记文件**里，而这 56 个文件
**总共含 1608 篇论文**。

现有工具无一可用：

| 工具 | 为什么不行 |
|---|---|
| `backfill_methods.py` | 只扫 `notes_dir/manual` 的 bundle；auto 链路没有 bundle |
| `backfill_notes.py --force` | 重做整月（重跑 Gmail/PubMed/arXiv 抓取 + LLM 三态筛选），会改变库的构成，太重 |
| `read_pdf.py regen` | 按现有 bundle 重建札记，**不重读原文** |
| `ingest_notes.py --papers` | 走 `run_ingest`，见下 |

`run_ingest` 的两个坑：

1. **citekey 抖动**。它无条件走 `enrich_segments`（Crossref/arXiv/translation-server）
   与 `resolve_citekeys`（Zotero BBT），没有跳过开关。代码注释自己写着「citekey 抖动，
   同 backfill_notes.run_month / read_pdf._rebuild_month 同源坑」。而改 citekey 必须
   扫派生物（向量库无自动触发）。
   *可绕开*：`write_notes` 接受显式 `citekeys` 映射 + `explicit_citekey_source`
   （`read_pdf._rebuild_month` 正是这么沿用自己的兜底键的），从 sidecar 取现成键即可。
2. **整篇覆盖的代价不成比例**。`write_notes` 是整篇重写 md/references/sidecar，
   非目标篇要靠 `_rehydrate_close_readings` 从 md 回读。而该函数 docstring 明说：
   *「model/read_at/body_chars 等量尺字段 md 没存、回读后为默认值」*。
   即为了 109 篇，要让**1499 篇无辜论文丢掉量尺字段**，并把 56 个文件的全部内容
   置于「解析失败即静默丢数据」的风险下。

## 五、推荐方案：文本级手术，不整篇重写

只替换目标篇在 md 里的**精读节**，其余字节不动，风险面从 1608 篇降到 109 篇。

可行性依据：md 的精读节有明确的行首正则契约（`notes_index._SECTION_RE` /
`_CLOSEREAD_RE` / `_CR_SECTION_RE` / `_TAG_LINE_RE`），`_rehydrate_close_readings`
已证明可无损解析；反向渲染复用 `notes._paper_section` 的精读部分即可。

需要实现（参照 `backfill_methods.py` 的形态：独立进度账本 + 断点续跑 + 核验闸）：

1. 从 `literature_index.json` 选目标集（`reading_depth=unknown-legacy` +
   decision/tier 过滤），按 `note_file` 分组；
2. 逐篇：重建 `PaperSegment`（元数据从 sidecar，**不重新 enrich**）→
   `close_read_segments(deep=True)` 重读；
3. **文本级替换** md 中该篇的精读节 + 同步更新 `.index.json` sidecar 的
   `highlights`/`tag_counts`/`reading_depth`/`has_full_text_reading`；
4. 收尾刷 `literature_index.json` → 向量库 `notes_embed.py`（chunk id 内容寻址，
   自动删旧嵌新，**不需要 `--full`**）；
5. 闸：写盘前 `fact_check`（数字/URL 编造）、写盘后页码对账，参照 backfill_methods。

**安全前提**：札记是 GENERATED 区 + 用户区分离（`vault.merge_note` /
`extract_user_zone`），重跑只覆盖生成区，人工批注保留。

## 六、成本重估

原估「109 篇约 1.5 小时」**只算了 LLM 时间，不成立**：

- 工具开发：脚本 + 测试（无现成入口，见第四节）
- LLM：单篇约 12 分钟（PoC 实测），109 篇即便 4 并发也要 **5 小时以上**，
  远超原估的 1.5 小时；走 Claude 订阅额度
- 原文重抓：这批 `reading_source` 是 arxiv 162 / unpaywall 145 / europepmc 27，
  本地 `output/scholar_pdfs` 是 hash 命名，命中率未核

## 七、工具与首篇全链路验证（2026-08-23 完成）

按第五节的方案落地了 `scripts/backfill_deepread.py`（文本级手术 + 账本断点续跑 +
自带备份），并用 `meng2021Mimicif` 走完了**含写回**的全链路。

### 实测结果

| 环节 | 结果 |
|---|---|
| 重读 | 可取证句 **6 → 73**，section 3 → 8（11 块分块深读，约 7 分钟） |
| md 手术 | 精读节 41 → 91 行；同文件 30 篇论文里**其余 29 篇字节全等**，篇内标题/裁决/摘要原样 |
| sidecar | 只有目标条目变化，正确补上 `fulltext_chars=120000` / `_raw=167539` / `_truncated=True` / `reading_depth=chunked` |
| 索引 | 增量**只重解析 1 个文件、沿用 82**；highlights 6 → 42；`tag_counts` 由 `{method:3,citable:3}` 变为 `{method:18,citable:12,refutable:12}` |
| **citekey 稳定性** | `citekey` / `citekey_source` / `dedup_key` / `note_line` **全部未变**（这正是绕开 run_ingest 要防的） |
| 向量库 | 42 条 highlight chunk，text_hash 全匹配、L2 范数=1、抽样重嵌余弦=1.0 |
| 全库口径 | unknown-legacy 334 → **333**，chunked 253 → **254** |

### 端到端检索验证（最有说服力的一条）

拿只有新精读才有的内容去查，`--limit 3`：

- `Equal Opportunity 与 Equalized Odds 公平性定义` → **@1，余弦 0.8211**，
  命中句正是新精读的「公平性定义采用两种常用标准：Equal Opportunity（跨群体真阳性率
  相等）与 Equalized Odds（在此基础上再要求假阳性率相等）」
- `去偏方法分为预处理 处理中 后处理三类` → **@1，余弦 0.8104**

这两条在旧精读里**完全不存在**（旧的只有 6 条、3 节）。

### 一个值得记的收获：旧精读缺的不只是量

`tag_counts` 从 `{method:3, citable:3}` 变成含 **`refutable:12`**——**旧精读一条
「可反驳观点」都没有**。写作时找反驳材料正靠这一类，这是比「条数少」更要命的结构性缺失。

### 踩到的坑

- `PaperMetadata` **没有 `abstract` 字段**（摘要挂在 `PaperSegment.original_abstract`）。
  pydantic 静默吞掉未知入参，直到读取时才 `AttributeError`。
- `_collect_highlights` 的三元组顺序是 **`(heading, tag, text)`**，不是 `(heading, text, tag)`。
- `notes_index.py` 收尾会调 `sync_store_best_effort` **自动同步向量库**，所以之后再跑
  `notes_embed.py` 报「新嵌 0」是幂等的正确表现，不是没同步——一度被这个误导，
  靠 rowid（新 chunk 位于表尾最大的 42 条）才确认插入确实发生过。

### 回归

`test/test_backfill_deepread.py` 9 条，重点不是「能不能改对」而是**「会不会改到别人」**：
越界检测、篇内边界、渲染与解析无损往返、同 citekey 重复时拒绝盲猜、sidecar 三种形态。
全量 **1677 passed**。

## 八、批量执行结果（2026-08-24 完成）

108 篇 INCLUDE+high 全部跑完（08:02 → 19:43，约 11.7 小时，均速 7.0 分钟/篇），
再加失败重试与首篇，账本 **done 103 / failed 6**。

### 最终数字

| 指标 | 结果 |
|---|---|
| 可取证句（带 role tag，即进向量库的） | **1087 → 4018**（3.7 倍，净增 2931） |
| 全部句子（含无 tag 的，人读可见） | 1024 → 6241（6.1 倍） |
| 单篇倍数 | 中位 6.7x，最低 1.5x，最高 15.2x |
| 涉及札记文件 | 54 个 |
| 向量库 | 25557 == 25557，待嵌 0 待删 0 |

### 验收（scripts/verify_deepread_batch.py）

**全部通过**：越界 **0 处**（54 个文件逐篇比对备份，非目标篇字节全等）、身份键 0 失踪、
变薄 0 篇、向量库已同步。

### 能力变化（比数量更重要）

role 分布 `method 1744 / refutable 1174 / citable 1100`——**旧精读几乎没有
refutable**（可反驳观点），而写作找反驳材料正靠这一类。
section 分布新增 `实验方法 675`、`局限与可质疑点 933`、`结果与效应量 966` 条。

端到端检索实证（这些查询在旧库里查不到任何东西）：

| 查询 | 命中 | 余弦 |
|---|---|---|
| 训练集验证集测试集划分比例与随机种子 | 精确命中 8:1:1 划分、随机种子 | 0.77–0.78 |
| 代码是否公开 GitHub 仓库地址 | 命中真实仓库 URL | 0.73–0.75 |
| 作者承认的局限性与未来工作 | 命中作者自陈局限 | 0.74 |

### 批量跑暴露的问题（全是统计口径，无数据损坏）

1. **账本 new 与 old 不同口径**：`old` 取自索引 highlights（只含带 tag 句），`new` 却数
   全部句子，导致「净变差不写盘」那道闸形同虚设——`rekkas2023Standardized` 带 tag 从
   34 掉到 30 仍被判成增长。已改为同口径，该篇重跑后 30 → 42。
2. **验收把 duplicate 当 keeper**：跨月重复的论文索引里有多条，字典被 duplicate 覆盖，
   把已改厚的 keeper 误报成变薄（`bauer2025Sepsis` 实际 11 → 29 被读成 6）。
3. **`--only-failed` 下熔断误伤**：那批本就是抓不到全文的，连续失败是常态而非通路故障，
   一进去就连挂 3 篇直接中止，后面能捞回的根本没轮到。修掉后立刻捞回 3 篇。

### 已知限制：reading_depth 在无 sidecar 的札记里写不回去

83 篇 md 只有 40 篇有 sidecar；无 sidecar 时 `notes_index` 走 md-parse 分支，而 **md 里
没有任何字段能承载 `reading_depth`**，它会按老规则推回 `unknown-legacy`（68 篇如此）。
highlights 与向量库都已正确更新，这纯是标签失真。**不为一个标签手工伪造 sidecar**——
那会让 notes_index 改走 sidecar 分支，风险远大于收益。对策是认账本不认标签：`scan`
一律扣除账本已完成的，验收也只把仍是 unknown-legacy 的计入提示。

## 九、剩余待办

- **6 篇需手动下全文**（出版商站点反爬），清单见
  `output/scholar_notes/需下载全文清单.md`，按必要性分三档。真正值得下的只有
  `keloth2024Large`（CITE_SUPPORT 却只有 7 条）。补好 PDF 后：
  `run --only-failed --pdf-dir <dir> --apply`。
- **剩余 235 篇**（账本未完成的 unknown-legacy，其中 INCLUDE 待跑 112 篇）尚未重跑，
  按需再开批次即可。
- `closeread_max_chars=120000` 对 16.7 万字符的长论文仍截尾（独立小缺口）。
