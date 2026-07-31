# auto 精读分块深读（closeread_deep）灰度说明

> 本文是 `config/scholar.env` 里那段灰度注释的版本化副本。该 env 文件在 `.gitignore` 内，
> 换机或误删即丢失，而它是打开开关前唯一的操作说明，故在此留档。改开关时两处同步更新。

## 开关

`PROCESSING__CLOSEREAD_DEEP`（`ScholarSettings.processing.closeread_deep`），**默认 False**。

打开后 auto 精读从「一次单跳」变成「切块逐块通读 + 汇总」，一篇论文的 LLM 调用从 1 次放大到
N+1 次（实测一批约 42 次、最坏 65 次），走的是同一份 Claude 订阅额度。

相关预算：`closeread_max_chars` 默认 120000（深读模式下的正文上限），`closeread_top_n` 默认 5。

## 开关的实际暴露面

谁会因为这一行立刻改变行为：

- **周度 ingest**（`com.xlbd.scholar-weekly-ingest.plist`，周一 09:30，
  `scripts/ingest_notes.py --auto --top-n 5`，close_read 默认开）——**一开就走深读**
- **月度 backfill**（`com.xlbd.scholar-monthly-backfill.plist`，每月 1 日 21:30，`--prev-month`）——同上
- **周度 digest**（`com.xlbd.scholar-digest.plist`，周一 09:00）——**不受影响**：它今天根本不跑精读
  （`closeread_enabled` 默认 False、env 未设 `PROCESSING__CLOSEREAD_ENABLED`、plist 也不传 `--close-read`）

若将来另行打开 `PROCESSING__CLOSEREAD_ENABLED`（或给 digest plist 补 `--close-read`），周一会连着
触发两批深读（09:00 + 09:30），共用同一份订阅额度与模块级 `_AGENT_SEMAPHORE(4)`，届时才需要
重新评估这 30 分钟间隔。

## 实测数据（2026-08-01，本地 263 篇缓存 PDF）

覆盖页比例 = 40000/120000 预算能读到第几页 ÷ 总页数。零 LLM 成本模拟。

| 配置 | 平均覆盖页 | 中位数 | 全覆盖篇数 | 平均喂入字符 |
|---|---|---|---|---|
| A 改造前（40k，无单页上限） | 56.6% | 54.5% | 19/263 | 39,648 |
| B 只加单页上限（40k + 20k） | 56.8% | 54.5% | 19/263 | 39,648 |
| C 深读（120k + 20k） | **97.6%** | **100%** | **234/263** | 79,610 |

**覆盖率的杠杆是预算 40k→120k，不是单页上限。** 单页上限只让 263 篇里的 4 篇（1.5%）
多读到内容；预算放宽则让 244 篇（92.8%）覆盖变多。

**但单页上限的真实价值在信噪比，不在覆盖率**（下方端到端实测才暴露出来）：manual 链路
不设单页上限，同一篇切出的 20 块里有 8 块是病态页噪声（草稿自陈第 3–7、9、20 块
「未提取到有效内容」、第 8 块乱码）；auto deep 有单页上限，11 块全部有效。
所以两条改动是分工而非重复——预算负责「读到多少页」，单页上限负责「每块是不是废话」。

分块数：min 3 / median 7 / p90 11 / max 11，**0 篇触到 `max_chunks=12`**。
平均每篇 LLM 调用 ≈ 8.3 次（块 + 1 次汇总），`top_n=5` 时单次运行约 42 次。

### 端到端实测（hu2024Enhancing，真实 LLM 调用）

同一篇论文三种读法的 highlights 条数，**纯脚本产出口径**（不含 agent 亲读补充）：

| 读法 | 块数 | highlights | 分节 |
|---|---|---|---|
| auto 单跳（改造前） | 1 | 15 | 5 |
| **auto 深读（本改造）** | 11 | **31** | 7 |
| manual 脚本草稿 | 20 | 24 | 7 |
| manual 终稿（+ agent 亲读交叉核验） | 20 | 53 | 7 |

auto 深读 31 条**已超过 manual 脚本草稿的 24 条**——剩下的 31→53 差距全部来自 agent
亲读那一层，而无人值守流水线本就不具备该层。即 S3 已经打到脚本能力的上限。

单篇耗时 173 秒（11 块，4 并发）。按 `top_n=5` 估算单次运行约 15 分钟，在 09:30 窗口内。

数值捕获抽查（这些数字全部位于改造前被截断的页上，改造前一个都拿不到）：
Macro-ROC-AUC 79.41→77.44 ✓、四范式 sensitivity SD 0.0488/0.0405/0.0266 ✓、
三数据集规模 112,120 / 239,716 / 191,229 ✓、CD=3.68 ✓、AttGAN 架构 ✓、参数量十倍 ✓。
未捕获：七指标 Nemenyi 排名里的 6.5（该数字只在 Fig 4 正文段落一句带过）。

表格解禁（S4）实测 5 篇真实 EPMC 全文：文本 +0~24%，可引用的小数数字
**291 → 690（+137%）**，`tr/td/th` 补换行后粘连数字串 0 处。
注意 EPMC 路线在 250 篇 auto 存量里只占 3 篇，影响面小但对命中的篇效果显著。

## 灰度顺序

1. 手工跑一次 `PYTHONPATH=. python scripts/ingest_notes.py --auto --top-n 5`，用 `/usr/bin/time` 记墙钟。
   注意：那才是真正走深读的产线；给 digest 计时测不到任何精读调用，除非命令写成
   `scholar_main.py digest --days 8 --close-read`。
2. 墙钟超 25 分钟先降 `--top-n` 或确认 09:30 窗口够长，再决定是否打开开关。
3. 打开后首次真实 auto 跑完，复查索引里新条目的 `fulltext_truncated` 比例与 `reading_depth` 取值
   （见下）。

## 量尺字段（索引 v4）

`literature_index.json` 每条新增四个字段：`fulltext_chars`、`fulltext_chars_raw`、
`fulltext_truncated`、`reading_depth`。

`reading_depth` 四态（与 `output/scholar_notes/AGENTS.md` 逐字一致，不得有第二套定义）：

| 取值 | 含义 |
|---|---|
| `chunked` | manual 全部 + 开关打开后的 auto |
| `single-call` | 开关关闭时新跑的 auto |
| `unknown-legacy` | 本次改造之前的 auto 存量（一篇不重跑，只标记） |
| 键缺失/null | 非精读条目 |

当前分布（2026-08-01）：chunked 151、unknown-legacy 250、null 1689。

三个字符数字段在存量条目上一律缺失——按「缺失=未知，不猜填」原则，不回填猜测值。
新值要等下一次 auto 精读跑完才会出现。
