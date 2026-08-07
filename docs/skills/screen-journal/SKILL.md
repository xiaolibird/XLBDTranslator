---
name: screen-journal
description: 对指定学术期刊做全量文献筛选——PubMed 检索、标题级黑名单预筛、filter-v3 LLM 三态裁决，产出候选列表供人类勾选入库。支持分步执行（先看降量再续跑 LLM），可用于任何 PubMed 索引期刊。区别于 scholar-search：本 skill 是对一整份期刊做批量裁决并落盘候选文件，会话结束后仍在；scholar-search 是临时对话检索，不裁决、不入库、什么都不留。
---

> 真相源：本文件在仓库 `docs/skills/screen-journal/SKILL.md`；改完须跑
> `bash scripts/install_skills.sh` 同步到 `~/.claude/skills/`。

# 期刊全量筛选

仓库：`/Users/xiaolibird/Documents/GitHub/XLBDTranslator-dev`（命令都在此目录运行）：
```bash
cd /Users/xiaolibird/Documents/GitHub/XLBDTranslator-dev
```

针对指定 PubMed 索引期刊，做两阶段多轮降量筛选：
1. **标题级黑名单**：砍掉明确无关的板块（影像/视觉/可穿戴/纯组学等）
2. **filter-v3 LLM 裁决**：用现成的三态方法学审稿 prompt 判 INCLUDE/MAYBE/EXCLUDE

最终产出候选 JSON + 人类可读 Markdown，人工勾选后经 `ingest_notes.py --papers` 入库。

## 两步走

```bash
# Step 1: 检索+黑名单（纯 PubMed HTTP，不花钱。跑完检查降量分布是否合理）
PYTHONPATH=. python scripts/screen_journal.py "NPJ Digit Med" \
    --from 2021-08-01 --to 2026-08-01 --stage search

# Step 2: filter-v3 裁决（LLM 批量送审，需花钱。候选列表含决策+维度+角色+一句话用途）
PYTHONPATH=. python scripts/screen_journal.py "NPJ Digit Med" --stage filter
```

也可以一步到底：
```bash
PYTHONPATH=. python scripts/screen_journal.py "NPJ Digit Med" \
    --from 2021-08-01 --to 2026-08-01 --stage all
```

⚠️ **一次只做一个期刊**：`search_results.json` 是**固定文件名**（不带期刊名），Step 1 每跑一次
就整份覆盖；filter 阶段读它时**不校验期刊是否与本次 `--stage filter` 传入的期刊名一致**——
换期刊前必须先把上一个期刊的 search→filter 走完（或至少导出完 candidates），否则会拿着期刊 A
的检索结果误判成期刊 B 的候选，且无报错提示。另外，filter 阶段传入的期刊位置参数本身**不生效**
（`cmd_filter` 只读 `search_results.json`，不消费该参数）——输出文件名按 `search_results.json`
里落盘的期刊名命名，找不到 `{期刊B}_candidates.json` 大概率就是踩了这个坑，去检查
`search_results.json` 里的期刊到底是谁。

## LLM 不可用时降级

`--stage filter`/`--stage all` 默认送 LLM 三态裁决，DeepSeek 欠费/鉴权失败/限流时可以主动加
`--no-llm` 退化成纯关键词白名单筛选：每篇命中白名单词的论文直接成为候选，`decision` **统一写成
`KEYWORD`**（不是 INCLUDE/MAYBE/EXCLUDE 三态之一），`role`/`one_line` 是占位值（`role="NONE"`，
`one_line="关键词命中: <词>"`），`confidence` **恒为 0.5**（不是真实置信度，只是占位数字）。
`filter_method` 字段会是 `"keyword_only"`：
```bash
PYTHONPATH=. python scripts/screen_journal.py "NPJ Digit Med" --stage filter --no-llm
```

⚠️ 文末「面向 agent 自动化」那条 jq（按（`decision=="INCLUDE"` 或 `flags` 含 `THREAT`）且
`confidence>=0.7` 筛）只对**LLM 模式**的候选文件有效——`--no-llm` 产出的候选全是
`decision="KEYWORD"`、`confidence` 全 0.5，原样套用会静默筛出空 `picks.txt`。`--no-llm`
模式下改用（等价于全量通过，因为关键词模式本身就是唯一的筛子，仍保留 `select(.doi)` 环节
过滤无 DOI 条目）：
```bash
jq -r '.candidates[] | select(.decision=="KEYWORD") | select(.doi != null and .doi != "") | .doi' \
    output/journal_screen/npj_digit_med_candidates.json > picks.txt
```
或直接打开 `candidates.md` 人工看。

诊断：
- **主动降级**：先查 `config/scholar.env` 里 LLM provider 的账户额度/密钥是否失效；确认无法
  短期恢复就用 `--no-llm` 出候选（此时 `filter_method` 会显式变成 `keyword_only`），不要等额度
  回来再筛，候选列表随时能补跑 filter 覆盖。
- **LLM 全挂但没加 `--no-llm`**：`classify_segments`→`_step_filter_papers` 某批 LLM 调用抛异常
  时，`workflow._fallback_partition` 接管该批：命中内置白名单关键词（`PROCESSING__WHITELIST`，
  默认非空，EHR/missingness 等术语）的论文照常判 `INCLUDE`（`one_line` 形如「白名单命中: EHR」），
  **未命中的论文不会被判 `EXCLUDE` 或静默消失**，而是挂 `verdict="undecided"`；
  `journal_screen.py` 把这批标成 `decision="UNDECIDED"`（三态之外的第四态，`one_line` 固定为
  「LLM 裁决失败待人工复核」）**照样计入候选**，不计入 `EXCLUDE`。LLM 全挂时的表现是：
  `filter_method` 仍显示 `"llm"`、`candidates_count` 接近全部送审数量（未消失）、
  `filter_stats.UNDECIDED` 很高且 `candidates.json` 里堆满 `decision="UNDECIDED"`、
  `one_line="LLM 裁决失败待人工复核"` 的条目，只有少数命中白名单词的论文是真 `INCLUDE`，
  **过程中不报错**。看到「候选列表里全是 UNDECIDED、一句话用途清一色『LLM 裁决失败待人工复核』」
  就是这个症状，别当成这些论文真的全部值得纳入或该期刊全是相关文献，去查 LLM 账户/密钥、
  用 `--no-llm` 降级或修好后重跑 filter 覆盖。
- **PubMed 限流**（Step 1 检索报错或异常慢）：NCBI 对匿名请求限流严格，检查
  `config/scholar.env` 里 email 是否配置（脚本会带上作为 E-utilities 的 `email` 参数）；
  确有限流就降低 `--max-results` 或分批跑，别当成期刊为空。

## 产出文件

全部在 `output/journal_screen/`：

| 文件 | 内容 |
|------|------|
| `search_results.json` | Stage 1 完整输出（命中+排除+黑名单分布） |
| `{journal}_candidates.json` | Stage 2 候选（INCLUDE+MAYBE+UNDECIDED，THREAT 等旗标见 flags 字段） |
| `{journal}_candidates.md` | 人类可读候选列表，逐条勾选 |

## 候选列表字段

每条候选含：
- `title` / `authors` / `year` / `doi` / `pmid`
- `decision`: INCLUDE / MAYBE（LLM 正常裁决）/ UNDECIDED（LLM 批次调用失败时的回退态，
  未命中白名单、待人工复核，见下文「LLM 全挂」诊断）；`--no-llm` 时统一为 KEYWORD（占位态，非真实裁决）
- `bucket`: 纳入维度 A-G
- `role`: CITE_SUPPORT / CITE_CONTRAST / MUST_ENGAGE / BACKGROUND
- `one_line`: ≤30 字用途说明
- `flags`: THREAT / BENCHMARK / OVERCLAIM_PRECEDENT
- `confidence`: 0.0-1.0
- `in_library`: 是否已在本地札记库
- `library_neighbors`: 本地近邻文献（含 citekey + 相似度）

## 入库

`candidates.md` 是给人看的表格，勾选是纯人工动作（打勾/删行），本身不产出机器可读文件——
需要人工把选中的条目转成 `ingest_notes.py --papers` 认的格式：**`picks.txt`，一行一个
标识符（DOI 优先；没 DOI 的条目退而用标题整行），无表头、无逗号分隔**。支持 `#` 起头的整行
注释（`parse_identifiers` 会跳过，行内 `#` 不当注释、DOI 里出现也不受影响），人工筛选时可以
直接注释掉不选的行而不必删除：

```
10.1038/s41746-024-01234-5
10.1038/s41746-024-05678-9
```

保存后：
```bash
PYTHONPATH=. python scripts/ingest_notes.py --papers picks.txt
```

## 黑名单

期刊定向黑名单只打**标题**（不碰摘要），防止误杀。词表见 `src/scholar/journal_screen.py` 中的
`JOURNAL_BLACKLIST_CATEGORIES`，覆盖五类词表 + COVID 专项规则：
- A: 医学影像/视觉（npj DM 最大板块）
- B: 可穿戴/传感器/移动健康
- C: 纯 NLP/LLM 无临床深度
- D: 纯组学/测序（但如果标题同时含 EHR/missingness 则豁免）
- E: 明显不相关（区块链、3D 打印等）
- COVID 专项规则（不在 `JOURNAL_BLACKLIST_CATEGORIES` 字典内，独立判断）：标题含
  COVID/COVID-19/SARS-CoV-2 且不含 EHR/missingness/causal 等保护词时排除
  （针对 2021-2022 COVID 灌水期）

## 面向 agent 自动化

agent 调用时可跳过人工审查，直接按置信度阈值入库：
```bash
# agent: 筛选 + 自动入库（取 INCLUDE 或带 THREAT 旗标的、且 confidence≥0.7 的——两条件是并集，
# 任一满足就要人工看：INCLUDE 是裁决判它该入库，THREAT 是裁决认为它挑战/威胁现有结论，
# 优先级不同但都不该漏过；confidence 门槛对两者都生效，防低置信度噪声）
PYTHONPATH=. python scripts/screen_journal.py "NPJ Digit Med" \
    --from 2021-08-01 --to 2026-08-01 --stage all

# 提取高置信候选 DOI（括号必须加：jq 里 | 的优先级低于 and，裸写 .flags | index("THREAT")
# 会被解析成 .decision==".." and .flags 先求值成布尔再 | index(...)，对布尔值 index 报错
# "Cannot index boolean with string"）
jq -r '.candidates[] | select((.decision=="INCLUDE" or (.flags | index("THREAT"))) and .confidence >= 0.7) | select(.doi != null and .doi != "") | .doi' \
    output/journal_screen/npj_digit_med_candidates.json > picks.txt

# 入库
PYTHONPATH=. python scripts/ingest_notes.py --papers picks.txt
```

无 DOI 的候选（`.doi` 为 `null` 或空字符串 `""`）不会被上面这条自动管线捡进 `picks.txt`——
`select(.doi != null and .doi != "")` 会把它们滤掉，避免 jq -r 打印出字面量字符串 `null`
或空行混进 `picks.txt`。
`ingest_notes.py` 对无法识别的标识符会退化成 `crossref_lookup` 裸标题模糊检索，
命中什么就无条件入库（`force_include=True`），字面量 `"null"` 会静默检索出一篇不相关文献
写进文献库、且不报错。这些无 DOI 但仍达标（INCLUDE 或 THREAT、confidence≥0.7）的候选，
按上文「入库」节的约定人工核对标题、把标题整行补进 `picks.txt`。

## 之后

入库产物就是普通周札记（`科研札记_YYYY-MM-DD_全文精读.md`），此后用 `scholar-notes` skill 查、
用 `scholar-write` skill 取证写作。候选里标了 `THREAT`/`MUST_ENGAGE` 或 `confidence` 高的重点
文献，值得再走 `read-paper` skill 做全文深度精读（比自动浅读更彻底）。
