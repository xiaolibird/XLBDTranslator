# 给论文写作 agent 的文献库使用 prompt

> 用法:整段拷贝到论文项目的 CLAUDE.md,或作为对话开场直接发给论文写作 agent。

---

## 文献库(必读)

我有一个按月精选、带全文精读的本地文献库,写论文引用相关工作**优先从这里取**,不要凭记忆编造文献:

```
/Users/xiaolibird/Documents/GitHub/XLBDTranslator-dev/output/scholar_notes/
```

覆盖 2021-01 至今(每月自动更新,另有不定期手动精读),自动系列每月一篇 `科研札记_YYYY-MM_全文精读.md`,手动系列 `科研札记_YYYY-MM_手动精读.md`,均配同名 `references.json`(CSL-JSON)。论文方向:**MNAR 缺失机制 / MA-GCT / EHR 表示学习 / 跨中心可迁移性**。库里每篇论文都经过 LLM 方法学三态审稿(INCLUDE/MAYBE),高优先级论文有**全文级精读**(研究问题/方法与数据/关键结论/可质疑点/对我研究的联想),句级标记 `〔可引用证据〕〔可反驳观点〕〔方法论借鉴〕`(历史条目可能仍是旧标记 `〔方法学创新〕〔重要发现〕〔研究背景〕`)。

### 检索四步法

1. **查索引** `literature_index.json`(始终过滤 `duplicate_of == null`):
   ```bash
   cd /Users/xiaolibird/Documents/GitHub/XLBDTranslator-dev/output/scholar_notes
   jq -r '.papers[] | select(.duplicate_of == null)
          | select((.title + " " + .one_line) | test("<关键词正则>"; "i"))
          | [.citekey, .month, .priority_tier, .has_full_text_reading, .title] | @tsv' literature_index.json
   ```
   常用过滤:`.decision=="INCLUDE"`、`.has_full_text_reading`、`.priority_tier=="high"`、`.month >= "2025-01"`、`(.tag_counts.method // 0) > 2`(方法论借鉴标记数;`tag_counts` 只有 `method`/`citable`/`refutable` 三个 role 键,没有中文键)、`.role=="MUST_ENGAGE"`(必须正面回应的对手文献)、`.flags`(THREAT=威胁我方主张的证据,写讨论时要主动引)。
2. **读精读原文再引用**:`grep -nF '[@<citekey>]' <note_file>` 拿行号,读该小节的「全文精读」——**引用主张必须核对精读原文**(尤其「可质疑点」),不能只凭索引里的 one_line 下笔。
3. **引用**:正文一律 pandoc 语法 `[@citekey]`;需要 arXiv/DOI 时用索引里的字段,不要自己回忆。
4. **书目**:直接挂全局书目(已全库去重合并,含全文精读+手动精读两系列,由 `scripts/notes_index.py` 自动维护,勿手改;只 glob `_全文精读` 系列会静默漏掉只在 `_手动精读` 系列里的 citekey):
   ```bash
   jq '.citekey_collisions' literature_index.json     # 必须为 [] 或已人工处理,否则合并会静默吞掉同键论文
   pandoc draft.md --citeproc \
     --bibliography=/Users/xiaolibird/Documents/GitHub/XLBDTranslator-dev/output/scholar_notes/all_references.json \
     -o draft.docx
   ```

### 硬性规则

- **不虚构文献**:库里检索不到就明说"文献库无此支撑",列出缺口让我决定(补检索或换论证),禁止编造 citekey/DOI/结论。
- **citekey 是临时兜底键**(`作者姓+年+标题词`,非 Zotero/BBT 权威键):论文投稿前会统一替换,跨系统对账一律以 **DOI** 为论文身份;正文引用格式保持 `[@citekey]` 即可,替换是机械操作。
- 争议性主张优先引 `has_full_text_reading == true` 的文献(结论经全文核验),仅摘要级(`reading_source=="abstract"`)的作背景引用。
- 更多配方与体检命令见库目录里的 `AGENTS.md`;若本机装有全局技能,直接说"用 scholar-notes 找文献"即可。
