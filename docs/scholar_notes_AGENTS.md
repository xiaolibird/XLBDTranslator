# 科研札记文献库 · Agent 使用说明

> 本文件由 XLBDTranslator-dev 的 `scripts/notes_index.py` 自动部署(源文件在仓库 `docs/scholar_notes_AGENTS.md`,勿直接改这份副本)。
> 面向对象:在**任何项目**(尤其论文写作项目)中工作、需要检索/引用文献的 Claude agent。

本目录是一个按月精选的文献库:Gmail Scholar 提醒 + PubMed + arXiv → LLM 方法学三态筛选 → 优先级排序 → top-5 强模型**全文精读**(句级三色联想标记,关联研究主线 MNAR / MA-GCT / EHR 缺失机制)。

## 目录结构

| 文件 | 内容 |
|---|---|
| `literature_index.json` | **机器可读总索引**(先查这个,再读原文) |
| `INDEX.md` | 人读索引(统计 + 按月表格) |
| `科研札记_YYYY-MM_全文精读.md` | 月度札记正文:速览表 + 逐篇(裁决/摘要/全文精读三色标注) |
| `科研札记_YYYY-MM_全文精读.references.json` | 该月 CSL-JSON 参考文献(pandoc 可直接用) |
| `科研札记_YYYY-MM_全文精读.index.json` | 该月结构化 sidecar(索引数据源,一般不用直接读) |
| `科研札记_YYYY-MM_全文精读.docx` | 样式化 Word 版(人读,agent 勿解析) |

## 检索流程(四步法)

1. **先查索引**:`jq` 过滤 `literature_index.json` 的 `papers[]`,拿到 citekey / note_file / note_line;
2. **再读原文**:按 `note_file` 打开对应月札记,`grep -nF '[@<citekey>]'` 定位到该篇小节,读裁决、摘要与「全文精读」节(`〔方法学创新〕〔重要发现〕〔研究背景〕` 是句级联想标记);
3. **引用**:正文用 pandoc 语法 `[@citekey]`;
4. **配书目**:把用到的月份的 `references.json` 合并进论文的 bibliography(配方见下)。

## 索引 schema(`papers[]` 每条)

`citekey, citekey_source("zotero"|"fallback"|"unknown"|"missing"——missing=占位键勿引用), doi, arxiv_id, title, title_zh, authors[], year, month("YYYY-MM"), journal, url, priority_tier("high"|"mid"|"low"), priority_rank, priority_score, decision("INCLUDE"|"MAYBE"), one_line(一句话用处), bucket[], role, confidence, flags[], has_full_text_reading, reading_source, tag_counts{三色标记计数}, note_file, note_line, note_heading, references_json, dedup_key, duplicate_of(非 null=跨月重复条目,检索时应过滤), duplicate_months[]`

顶层还有 `months{}`(覆盖月份)与 `citekey_collisions[]`(撞键警告,见下)。

## 查询配方(在本目录下执行)

```bash
# 关键词检索(标题+一句话用处),排除重复条目 —— 最常用
jq -r '.papers[] | select(.duplicate_of == null)
       | select((.title + " " + .one_line) | test("MNAR|missing|缺失"; "i"))
       | [.citekey, .month, .priority_tier, .title] | @tsv' literature_index.json

# 只要 INCLUDE 且有全文精读的高优先级文献
jq -r '.papers[] | select(.duplicate_of == null and .decision == "INCLUDE"
       and .has_full_text_reading and .priority_tier == "high")
       | [.citekey, .note_file] | @tsv' literature_index.json

# 按年份区间 + 按方法学创新标记数排序
jq -r '.papers[] | select(.duplicate_of == null and .month >= "2025-01" and .month <= "2025-12")
       | [(.tag_counts["方法学创新"] // 0), .citekey, .title] | @tsv' literature_index.json | sort -rn

# 定位并阅读某篇的精读原文
grep -nF '[@xu2026Development]' 科研札记_2026-05_全文精读.md   # 拿行号后 Read 该节

# 合并 bibliography(⚠️ 合并前先看撞键警告,见下节)
jq -r '.citekey_collisions' literature_index.json               # 必须为 [] 才能安全合并
jq -s 'add | unique_by(.id)' 科研札记_*_全文精读.references.json > bibliography.json

# 体检:索引是否落后于札记(数量不一致→先跑 scripts/notes_index.py)
ls 科研札记_*_全文精读.md | wc -l; jq '.months | length' literature_index.json
```

## ⚠️ citekey 注意事项(重要)

- 多数 citekey 是 **headless 兜底键**(`作者姓+年+标题词`,`citekey_source: "fallback"`),**不是** Zotero/Better BibTeX 权威键。跨系统对账(Zotero、他人书目)一律以 **DOI / dedup_key** 为论文身份,citekey 只在「本索引 + 对应月 references.json」闭包内有效。
- `citekey_collisions` 非空 = 不同论文共用同一键,`jq unique_by(.id)` 合并会**静默吞掉一篇**;在 XLBDTranslator-dev 仓库跑 `python scripts/notes_index.py --fix-collisions` 自动改键(保最早月不动,后出现者加 b/c 后缀,md+references.json 同步改)后再合并。
- **升级为权威键的路径**(人在时做):按索引 DOI 批量导入 Zotero → BBT 生成正式 citekey → 论文 md 里 `sed` 替换旧键 → bibliography 换 BBT 自动导出。

## 可拷贝到论文项目 CLAUDE.md 的片段

```markdown
## 文献库
精选文献札记库(按月,含全文精读)在:
`/Users/xiaolibird/Documents/GitHub/XLBDTranslator-dev/output/scholar_notes/`
找文献四步法:1) jq 查该目录 literature_index.json(过滤 duplicate_of==null);
2) 按 note_file+citekey grep 定位札记原文精读节;3) 正文引用 [@citekey];
4) 合并对应月 references.json 进 bibliography(先确认 citekey_collisions 为空)。
详细配方读该目录的 AGENTS.md。⚠️ citekey 是兜底键,跨系统对账以 DOI 为准。
```
