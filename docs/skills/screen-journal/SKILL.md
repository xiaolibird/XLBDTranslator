---
name: screen-journal
description: 对指定学术期刊做全量文献筛选——PubMed 检索、标题级黑名单预筛、filter-v2 LLM 三态裁决，产出候选列表供人类勾选入库。支持分步执行（先看降量再续跑 LLM），可用于任何 PubMed 索引期刊。
---

# 期刊全量筛选

针对指定 PubMed 索引期刊，做两阶段多轮降量筛选：
1. **标题级黑名单**：砍掉明确无关的板块（影像/视觉/可穿戴/纯组学等）
2. **filter-v2 LLM 裁决**：用现成的三态方法学审稿 prompt 判 INCLUDE/MAYBE/EXCLUDE

最终产出候选 JSON + 人类可读 Markdown，人工勾选后经 `ingest_notes.py --papers` 入库。

## 两步走

```bash
# Step 1: 检索+黑名单（纯 PubMed HTTP，不花钱。跑完检查降量分布是否合理）
PYTHONPATH=. python scripts/screen_journal.py "NPJ Digit Med" \
    --from 2021-08-01 --to 2026-08-01 --stage search

# Step 2: filter-v2 裁决（LLM 批量送审，需花钱。候选列表含决策+维度+角色+一句话用途）
PYTHONPATH=. python scripts/screen_journal.py "NPJ Digit Med" --stage filter
```

也可以一步到底：
```bash
PYTHONPATH=. python scripts/screen_journal.py "NPJ Digit Med" \
    --from 2021-08-01 --to 2026-08-01 --stage all
```

## 产出文件

全部在 `output/journal_screen/`：

| 文件 | 内容 |
|------|------|
| `search_results.json` | Stage 1 完整输出（命中+排除+黑名单分布） |
| `{journal}_candidates.json` | Stage 2 候选（INCLUDE+MAYBE+THREAT） |
| `{journal}_candidates.md` | 人类可读候选列表，逐条勾选 |

## 候选列表字段

每条候选含：
- `title` / `authors` / `year` / `doi` / `pmid`
- `decision`: INCLUDE / MAYBE
- `bucket`: 纳入维度 A-G
- `role`: CITE_SUPPORT / CITE_CONTRAST / MUST_ENGAGE / BACKGROUND
- `one_line`: ≤30 字用途说明
- `flags`: THREAT / BENCHMARK / OVERCLAIM_PRECEDENT
- `confidence`: 0.0-1.0
- `in_library`: 是否已在本地札记库
- `library_neighbors`: 本地近邻文献（含 citekey + 相似度）

## 入库

勾选 candidates.md 里的 DOI → 保存为 `picks.txt`（一行一个）：

```bash
PYTHONPATH=. python scripts/ingest_notes.py --papers picks.txt
```

## 黑名单

期刊定向黑名单只打**标题**（不碰摘要），防止误杀。词表见 `src/scholar/journal_screen.py` 中的 `JOURNAL_BLACKLIST_CATEGORIES`，覆盖五大类：
- A: 医学影像/视觉（npj DM 最大板块）
- B: 可穿戴/传感器/移动健康
- C: 纯 NLP/LLM 无临床深度
- D: 纯组学/测序（但如果标题同时含 EHR/missingness 则豁免）
- F: COVID 专项（2021-2022 灌水，无 EHR/missingness/causal 等保护词时排除）

## 面向 agent 自动化

agent 调用时可跳过人工审查，直接按置信度阈值入库：
```bash
# agent: 筛选 + 自动入库（只取 INCLUDE+THREAT 且 confidence≥0.7 的）
PYTHONPATH=. python scripts/screen_journal.py "NPJ Digit Med" \
    --from 2021-08-01 --to 2026-08-01 --stage all

# 提取高置信候选 DOI
jq -r '.candidates[] | select(.decision=="INCLUDE" and .flags | index("THREAT")) | .doi' \
    output/journal_screen/npj_digit_med_candidates.json > picks.txt

# 入库
PYTHONPATH=. python scripts/ingest_notes.py --papers picks.txt
```
