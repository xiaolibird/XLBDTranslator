---
name: scholar-notes
description: 在本机的科研札记文献库(按月精选+全文精读,MNAR/MA-GCT/EHR缺失机制方向)中查找、阅读、引用文献。当用户要"找文献/查札记/文献索引/找参考文献/scholar notes",或写论文需要相关工作支撑时使用。
---

# 科研札记文献检索

文献库(按月精选,LLM 三态筛选 + top-5 全文精读):
`/Users/xiaolibird/Documents/GitHub/XLBDTranslator-dev/output/scholar_notes/`

覆盖 2023-01 至今,每月一篇 `科研札记_YYYY-MM_全文精读.md` + 同名 `references.json`(CSL-JSON)。
总索引:`literature_index.json`;完整使用说明:该目录 `AGENTS.md`(先读它)。

## 四步法

1. **查索引**(始终过滤 `duplicate_of == null`):
   ```bash
   cd /Users/xiaolibird/Documents/GitHub/XLBDTranslator-dev/output/scholar_notes
   jq -r '.papers[] | select(.duplicate_of == null)
          | select((.title + " " + .one_line) | test("<关键词正则>"; "i"))
          | [.citekey, .month, .priority_tier, .has_full_text_reading, .title] | @tsv' literature_index.json
   ```
   常用过滤字段:`decision=="INCLUDE"`、`has_full_text_reading`、`priority_tier=="high"`、
   `month >= "2025-01"`、`tag_counts.citable`（口径为 role slug:citable/refutable/method）。
1b. **句级调取(highlights)**:每条论文带 `highlights[]`,项为 `{role, tag, section, text}`,
   `role` ∈ `citable`(可引用证据) / `refutable`(可反驳观点) / `method`(方法论借鉴)。
   工作流按用途跨全库直取句子(不必打开 md):
   ```bash
   # 某主题下所有"可引用证据"(带出处):
   jq -r '.papers[] | select(.duplicate_of==null)
          | . as $p | .highlights[] | select(.role=="citable")
          | [$p.citekey, .section, .text] | @tsv' literature_index.json
   # 某篇的所有"可反驳靶子"(写 critique 用):
   jq -r '.papers[] | select(.citekey=="<citekey>")
          | .highlights[] | select(.role=="refutable") | .text' literature_index.json
   # 全库"方法论借鉴"灵感库:  select(.role=="method")
   ```
   历史条目的 role 由旧标记规则近似映射(方法学创新→method、重要发现→citable、研究背景→丢弃);
   手动精读的 refutable 还含对抗核验的纠错条。新精读由 LLM/subagent 直接精确产出三类。
2. **读原文**:`grep -nF '[@<citekey>]' <note_file>` 拿行号,Read 该小节——重点是「全文精读」节
   (句级标记:`〔可引用证据〕`取证 / `〔可反驳观点〕`靶子 / `〔方法论借鉴〕`方法思路,以及「对我研究的联想」小节;历史札记可能仍是旧标记〔方法学创新/重要发现/研究背景〕)。
3. **引用**:论文正文用 pandoc 语法 `[@citekey]`。
4. **书目**:直接用现成的全局书目 `all_references.json`(全库去重合并,含全文精读+手动精读两个系列,
   由 `scripts/notes_index.py` 自动刷新、勿手改):
   ```bash
   pandoc draft.md --citeproc --bibliography=output/scholar_notes/all_references.json -o draft.docx
   ```
   用前确认 `jq '.citekey_collisions' literature_index.json` 为 `[]`(非空先跑 `notes_index.py --fix-collisions`)。
   写作取证的完整流程(按 role 轴 query → 写稿 → 出稿)见 skill `scholar-write`。

## 注意

- citekey 多为兜底键(非 Zotero/BBT 权威键):跨系统对账一律以 **DOI/dedup_key** 为论文身份;升级路径见 AGENTS.md。
- 索引可能落后于札记:`ls 科研札记_*_全文精读.md | wc -l` 与 `jq '.months|length'` 不一致时,先在
  XLBDTranslator-dev 仓库跑 `python scripts/notes_index.py` 再检索。
- docx 是人读版,agent 只解析 md/json。
