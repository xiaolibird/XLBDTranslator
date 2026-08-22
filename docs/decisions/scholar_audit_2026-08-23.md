# scholar 三链路对抗式审计（2026-08-23）

对**精读逻辑 / 向量化入库 / 混合检索**三条链路做的三轮对抗式审计。每轮由一个提案方
subagent 与一个对抗方 subagent 独立作业，编排者裁决收敛后才动代码。起点 `ffd05c4`，
四个 commit：`f3defbc`（精读）→ `6ff24e0`（向量化）→ `c31e64a`（检索）→ `b7b98ed`（R2 复审）
→ `7e11e04`（R3 变异终审）。测试 1600 → **1655 passed**。

## 方法

- **提案方 P**：证据四元组废票制（HEAD 现场引文 / 具体触发输入 / 爆炸半径 / diff 级修案 +
  调用方 grep 清单 + 绑定测试名），缺一项作废；配额上限而非下限；只给「文件+一句话疑点」
  不给论证过程，逼它自建证据链。
- **对抗方 O**：四态强制裁决（维持/驳回/降级/改案）。驳回只有四种合法理由：引文与 HEAD
  不符、上游守卫证明触发不可达、指名道姓的测试/契约破坏、docs/decisions 或注释里的文档化
  设计意图。「概率低」只能降级。每个「维持」必须附「我用什么输入试图击破而失败」。
- **纪律**：agent 全程只读；每轮首尾核对 `git status` 与真库 `embeddings.sqlite3` 的
  size/mtime；所有构造实验在 tmp；高危修复变异检验（守卫逐个取反，绑定测试必须转红）。

对抗确实起作用了，三轮各抓到一次**方向性纠错**：R1 的 O 否决了 P 的 `_valid_report`
（会让空报告从「拒收」变「放行」，门禁语义倒退）；R2 抓出 R1 **自己引入**的同款静默丢数据；
R3 抓出 22 处「修了但测试没咬住」的守卫和 3 条恒绿/非歧视性的假测试。

## 修了什么（26 条，按危害）

### 数据面：静默且不可逆

| # | 链路 | 问题 | 代码位置 |
|---|---|---|---|
| 1 | 向量化 | `⚑ RETRACTED` 撤稿踢库对**全部有 sidecar 的月份**失效（真库 40/83 月、1019/2343 篇 = 43%）。md 被声明为 flags 的唯一真相源，但 sidecar 分支从不回读它；`lint.py` 也读索引，于是会**永远**报「已撤稿且未标记」，用户反复加标记反复无效 | `notes_index.build_month_entries` sidecar 分支 |
| 2 | 精读 | 一份 agent 手写坏的 bundle 抛裸 traceback 炸掉**整月**归档，异常里不含文件名 | `read_pdf._rebuild_month` |
| 3 | 精读 | 读不出的 bundle 从回执里消失：既不在 papers 也不在 skipped，`_report_final` 照打 ✅，一篇已核验论文永久蒸发 | `read_pdf._rebuild_month` / `_report_final` |
| 4 | 精读 | bundle 的 `month` 字段与所在目录不一致 → 按月重建扫空桶，退出码 0、打印「归档 X：0 篇」，那篇彻底消失 | `read_pdf.cmd_finalize` |
| 5 | 精读 | `cross_check_report.corrected` 写成字符串被逐字符切片，一句纠错变成十几条单字 highlights 全进 refutable 取证轴 | `read_pdf._inject_cross_check` |
| 6 | 精读 | **R1 自引入**：别名兼容用链式 or，而旧 schema 的 `added_new` 是**计数不是数组**（磁盘上恰好 1 份），会把一篇已归档论文拒收 → 下次 finalize 抹掉 | 同上 |
| 7 | 精读 | **净删除止损闸**（R3）：拒收在整月重建语义下 = 从已归档 md 里删除。红回执只是事后通知，现在改为动库前拦下、整月一字不动 | `read_pdf._rebuild_month` |
| 8 | 精读 | `find_final_bundle` 只扫同月桶，`--month` 缺省即当月 → 整目录在月边界后重跑完全绕过守卫。磁盘上已留下 3 组同 paper_id 跨桶双 final，2 组把 citekey 分裂成两个键 | `pdf_ingest.find_final_bundle` |
| 9 | 精读 | **R1 自引入**：跨桶扫描只比 `pdf_path`，而那 3 组事故的 pdf_path **全不相同**（PDF 重下载/移出待读目录）——对自己举证的动机零覆盖 | 同上 |
| 10 | 向量化 | `sync_store` 的 expected 字典推导式 last-wins：citekey 撞键时甲的篇级向量被乙静默覆盖，而甲存活的 highlight 被下游按 citekey 反查到**乙**的身份，跨篇张冠李戴 | `embed_store.sync_store` |
| 11 | 向量化 | 增量同步不校验 `schema_version` 却在收尾无条件写它 → 一次增量就把旧库静默盖章成新版，永久解除读侧唯一的版本闸；而 watcher 是自动跑的，人没有介入窗口 | 同上 |
| 12 | 精读 | `write_bundle` 与 `backfill_methods` 的 final bundle 回写非原子 | `pdf_ingest.write_bundle` |

### 正确性 / 语义

| # | 链路 | 问题 |
|---|---|---|
| 13 | 检索 | **`--min-score` 在 hybrid 下只约束 dense 泳道**，BM25 单路命中无门槛占 `--limit` 名额，最终按 RRF 排序 → **门槛越高结果越脏，反向单调**。离题探针 `--min-score 0.95` 返回 108 篇；`--cite --min-score 0.7` 对全库无一条 ≥0.7 的 query 吐出可粘贴引用串（假引用进稿子） |
| 14 | 检索 | 聚组代表按 RRF 选，展示分不是 paper 侧最高余弦：1898/28076 个 (query,citekey) 对低报、最大 0.080，其中 **72 例展示 <0.62 而真实 ≥0.62**，正好落在 skill 判重线两侧 |
| 15 | 检索 | `--json` 不导出 `score_from`，消费方无法区分 0.71 是篇级还是句级——而「≥0.62 判库内同篇」只对篇级成立，skill 推荐的正是 `--json` |
| 16 | 检索 | `score_from` 靠比大小反推，纯关键词命中的行恒被标「句」，与紧邻的「该篇无精读句级证据」自相矛盾 |
| 17 | 精读 | 纠错条冒充〔可反驳观点〕：实测 **918/3630（25.3%）** 的 manual refutable 取证条是「Opus 原稿写错了」这类草稿勘误而非论文的可质疑处，而 manual 恒为 keeper、topics 还加权 |
| 18 | 精读 | `synthesize_deep_read` 裸 `[:60000]` 切块笔记：切点落在 JSON 串中间，尾部整块静默消失，而尾部正是结果/局限/附录。实证一份**已归档 final** 的 229 页/49 块 bundle 每块只摊到 1224 字符，其草稿用「论文目录中提及的附录 D」描述附录内容 |
| 19 | 精读 | **R1 自引入**：均摊预算没扣 JSON 开销 → 还剩几百字符头寸就去丢整块（40/49/60 块各白丢 1/1/2 块） |
| 20 | 精读 | **R1 自引入**：`budget_info` 只接了 manual 一条链路，而「auto 侧 max_chunks=12 兜着」的前提是错的（12 块 × 5.8k 字符即超）。auto 产出全库约 90% 的札记且无亲读核验兜底，裁剪零留痕 = `chunked/12 块` 虚报 |
| 21 | 精读 | `cross_check_report` 的别名 schema（`corrections`/`additions`）被吞成「纠错 0 处、补漏 0 处」（存量约 10 篇） |
| 22 | 精读 | 显式 `verified_count=0` 的 bundle 自己 finalize 被拒，却能靠同月兄弟搭车进整月重建 |
| 23 | 向量化 | dedup_key 漂移让 abstracts.json 的键成孤儿 → ab: 厚向量被当「该删的」删掉且无自动补抓入口，删除量在闸的放行窗口内时完全静默（加只读漂移检测） |
| 24 | 向量化 | 改键收尾是全仓第 5 份 best-effort 复制且唯一不 notify 的一份；`--fix-collisions` 与 `audit_citekeys_vs_pmlr` 的退出码此前恒 0 |
| 25 | 精读 | `is_credit_error` 漏认 403（改为直接复用 `llm_client._FATAL_CODE_RE`，不再手抄第三遍） |
| 26 | 精读 | 中文译文为空时连完好的英文摘要一起丢（改为按「是否走过抢救分支」分流） |

另有 8 条低危（notify 截断 120→240、`notes_embed` 把一切 `OperationalError` 说成「被并发写
锁定」、`--title` 批量静默丢弃、`chunk_text` 的 overlap 钳位、sidecar 读失败升级 notify、
死 import 等），见各 commit message。

## 证伪了什么（14 条，附证据）

对抗最有价值的部分之一是**否掉假缺陷**。逐条留档，避免下一轮重复：

- **freshness 用字典序比 ISO 时间戳会虚假 stale** —— 假。`source_generated_at` 是逐字抄索引的
  `generated_at`（`timespec="seconds"`，定长 19 字符），带微秒的 `built_at` 只出现在 `--stats`。
- **`split("\n")[1]` 反推 one_line 会因 title 换行错位** —— 假。`str.replace` 无 count 参数替换
  全部，真库 2254 条 `p:` chunk 换行数恒为 1。
- **嵌入文本无长度上限会被 Ollama 静默截断** —— 真库 max 长度 highlight 2113 / abstract 1185 /
  paper 384，bge-m3 上下文 8192 token，4 倍余量。
- **漂移会撞死 ab: 删除闸导致增量永久卡死** —— 假。全库「键还能再升级且有摘要」的 keeper
  只有 70 篇 < cap 89.9；改 citekey 与 dedup_key 完全正交（实测把全部 2343 条的 citekey 都改掉，
  dedup_key 变化 0 条）。该天花板已写进 `_AB_DELETE_*` 的战史注释。
- **`_library_neighbors` 的 `top_k=k*2` 会让近邻 <k** —— 假。真库每 citekey 的 paper 侧
  chunk 数分布 `{2:1798, 1:456}`，无一篇 ≥3，鸽笼下 `k*2` 是紧界。
- **BM25 每查询全库重 tokenize 会撞 rag_bench 的 120s 超时** —— 假。实测 paper 掩码 0.042s、
  highlight 掩码 0.185s，两掩码不相交（和 = 22592 = 全库，不是「两遍全库」），余量约 500 倍。
- **`meta is None` 静默丢弃有危害** —— 真库有 ab: 无 p: 的 citekey 0 个、有 highlight 无 p: 的 0 个。
- **`bucket_bonus` 会「买」过门槛** —— topics 的门槛比较在 boost 之前，已有测试锁死。
- **`cmd_regen` 零门禁** —— 门禁已下沉到 `_rebuild_month`，regen 与 finalize 共用同一道闸。
- **`find_duplicate` 位置导致白烧 LLM** —— 它本就不是门禁，前移省不了任何调用。
- **`update_index` 只看 md mtime 会漏改动** —— 逐个核对全部 sidecar 写者，md 无条件原子写作为
  事务提交标记，不存在只改 sidecar 不改 md 的路径。
- **403 会让每块吃满 4 次重试退避** —— 半径被降级：`HTTPStatusError` 不在 `_call_once` 的可重试
  元组里，不吃退避。真实危害只剩 `draft_status` 落成 `empty` 而非 `api_error` 的误导。
- **embedding 模型只差 tag 会静默重嵌全库** —— 机制真，但 `LLM__EMBEDDING_MODEL` 这个配置键在
  整个仓库零命中，且半径是一次性重嵌无污染。归入将来「换 embedding 模型」那批。
- **「向量库无自动触发」（项目记忆里的旧结论）** —— 已过时：改键路径早已内置同步，且有
  launchd watcher 兜底。真正缺的是 dedup_key 侧的对账（即 #23）。

## 遗留课题（有结论，未做）

1. **默认检索模式 hybrid vs dense**：dense 在 75-case bench 上 56@1/68@5 优于 hybrid 的
   53/63，但逐 case 差分显示**两种模式的失败集完全不相交**（都丢出 top-10 的 = 0/75，
   并集 @10 = 75/75，单模式最好 70/75）；「hybrid 召回 + 余弦排序」经实测与纯 dense 逐 case
   等价，不是折中方案。真实性质是在两种互补失败模式间二选一，而现有 case 集无一类是为隔离
   BM25 价值设计的，裁决不了。**结论：不改默认，改为零代码的用法升级**（skill 教「两轮取
   并集」）。要重开这个课题，先给 case 集补一类「罕见英文术语/缩写」query。详见
   `rag_bench_baseline_2026-08.md` 的 R3 订正节。
2. **`backfill_abstracts` 的月度调度**：月度 job 存在但 plist 是单程序调用、无 shell，
   「加一行」不成立；且该脚本末行 `return 1 if failures`，而线上有 456 条 failures 每轮重试，
   天真接线会让月度 job 恒定报失败。它还会引入无人值守的联网抓取——属调度行为变更，
   留给用户决定。#23 的漂移告警已覆盖真实盲区。
3. **manual 链路的 final 从不过数值防线**：不建议加防线。`verify_citable_numbers` 是纯文本
   子串匹配、只能作用在**脚本草稿**上，而进库的是 agent 手写的 `close_reading_final`——
   它救不了库，还可能让 agent 照抄一份被机器降过级的草稿。真缺口要靠 finalize 时重抽 PDF
   正文，是新功能。正确的收口是 #7 的止损闸（已做）。
4. **低危无回归保护的守卫**（丢块方位、notify 240、backfill_methods 的两处原子写、跨桶命中
   点名所在桶等 7 处）：变异后全绿，补测成本大于收益，记账即可。

## 上线注意

- **#1 的撤稿修复上线后须跑一次** `notes_index.py --full` + `notes_embed.py`。真库 40 个
  sidecar 月、1019 篇逐条对比 lost=0 / gained=0，对现有 flags 是零改变——它只是打通了
  ⚑ 这条路。
- **#17 的存量清理是被动的**：918 条冒充 refutable 的纠错条会随各月 `regen` 重建自动清掉，
  不重建的月份保持原样。
- **#13/#14 是行为变化**：`docs/skills/scholar-search/SKILL.md` 与
  `rag_bench_baseline_2026-08.md` 已同批更新。注意 skill 里「要按分数看排名请用
  `--mode dense`」那段**一个字都没放宽**——RRF 非分数序与 min_score 是正交问题，
  实测某 query 的全库最高余弦篇修复前排第 10、修复后仍排第 9。
