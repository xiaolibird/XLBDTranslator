# 科研札记文献库 · Agent 使用说明

> 本文件由 XLBDTranslator-dev 的 `scripts/notes_index.py` 自动部署(源文件在仓库 `docs/scholar_notes_AGENTS.md`,勿直接改这份副本)。
> 面向对象:在**任何项目**(尤其论文写作项目)中工作、需要检索/引用文献的 Claude agent。

本目录是一个按月精选的文献库:Gmail Scholar 提醒 + PubMed + arXiv → LLM 方法学三态筛选 → 优先级排序 → top-5 强模型**全文精读**(句级角色标记 可引用证据/可反驳观点/方法论借鉴,聚合成 `highlights[]` 供工作流按用途调取,关联研究主线 MNAR / MA-GCT / EHR 缺失机制)。

## 目录结构

| 文件 | 内容 |
|---|---|
| `literature_index.json` | **机器可读总索引**(先查这个,再读原文) |
| `INDEX.md` | 人读索引(统计 + 按月表格) |
| `all_references.json` | **全局书目**(全库去重合并的 CSL-JSON,pandoc 直接挂;自动刷新,勿手改。只含 `duplicate_of==null` 的键——渲染月度 md 本身请仍用同月 references.json) |
| `科研札记_YYYY-MM_全文精读.md` | **自动**月度札记(`series:"auto"`):Gmail/检索 → 三态筛选 → top-5 全文精读 |
| `科研札记_YYYY-MM_手动精读.md` | **手动**深度精读(`series:"manual"`):人给 PDF,agent 亲读整本 + 脚本交叉核验,通读更彻底 |
| `科研札记_YYYY-MM_{全文,手动}精读.references.json` | 该札记 CSL-JSON 参考文献(pandoc 可直接用) |
| `科研札记_YYYY-MM_{全文,手动}精读.index.json` | 该札记结构化 sidecar(索引数据源,一般不用直接读) |
| `科研札记_YYYY-MM_{全文,手动}精读.docx` | 样式化 Word 版(人读,agent 勿解析) |
| `manual/YYYY-MM/*.paper.json` | 手动精读的中间 bundle(内部用,勿检索) |

## 语义检索(中文找英文表述、换述同义词用)

`jq`/`notes_query.py` 是精确子串匹配；查不到换述表达（如中文"缺失机制不可忽略"查不到英文
"informative missingness"）时改用 `PYTHONPATH=. python scripts/notes_search.py <查询...>
[--role ...] [--json]`(默认 `--mode hybrid`=向量+BM25 关键词融合)。句级证据同样只覆盖库内
480 篇精读文献,语义命中若标注"该篇无精读句级证据"是真的没有,不是没搜到。

## 检索流程(四步法)

1. **先查索引**:`jq` 过滤 `literature_index.json` 的 `papers[]`,拿到 citekey / note_file / note_line;
2. **再读原文**:按 `note_file` 打开对应月札记,`grep -nF '[@<citekey>]'` 定位到该篇小节,读裁决、摘要与「全文精读」节(`〔可引用证据〕〔可反驳观点〕〔方法论借鉴〕` 是句级角色标记;历史札记可能仍是旧标记 `〔方法学创新〕〔重要发现〕〔研究背景〕`)。**句级取证不必打开 md**——直接查条目的 `highlights[]`(见下);
3. **引用**:正文用 pandoc 语法 `[@citekey]`;
4. **配书目**:直接挂全局书目 `all_references.json`(已全库去重合并,不必自己拼月度文件;配方见下)。

## 索引 schema(`papers[]` 每条)

`citekey, citekey_source("zotero"|"fallback"|"unknown"|"missing"——missing=占位键勿引用), series("auto"自动流水线|"manual"手动深读), doi, arxiv_id, title, title_zh, authors[], year, month("YYYY-MM"), journal, url, priority_tier("high"|"mid"|"low"), priority_rank, priority_score, decision("INCLUDE"|"MAYBE"), one_line(一句话用处), bucket[], role(筛选角色,非句级), confidence, flags[], has_full_text_reading, reading_source, tag_counts{role计数:citable/refutable/method}, highlights[](句级可调取,见下), note_file, note_line, note_heading, references_json, dedup_key, duplicate_of(非 null=重复条目,检索时应过滤), duplicate_months[]`

**`highlights[]`——句级取证的核心**:每项 `{role, tag, section, text}`。`role` 是按**对后续工作流的用途**的三分:
`citable`(可引用证据:含数字/效应量/可溯源结果)、`refutable`(可反驳观点:作者主张/可质疑处,写 critique 的靶子;手动精读还含对抗核验的纠错条)、`method`(方法论借鉴:可迁移的方法思路)。`tag` 是对应的中文原标记,`section` 是精读分节名(溯源用)。工作流按 role 跨全库直取句子,无需打开 md。历史条目的 role 由旧标记规则近似映射(方法学创新→method、重要发现→citable、研究背景→丢弃),新精读由 LLM/subagent 直接精确产出。

顶层还有 `months{}`(按**文件 stem** 键,含 month/series)与 `citekey_collisions[]`(撞键警告,见下)。

**keeper 规则**:同一论文多处出现时,`series:"manual"`(手动深读)恒为 keeper(即使月份晚于自动版),
自动浅读版被标 `duplicate_of`。所以按 `duplicate_of == null` 过滤后,你读到的就是**最彻底的那版精读**。

## 阅读深度量尺 `reading_depth`(⚠️ 库里并存两代精读,取证前先看这个)

条目上还有一把阅读深度量尺:`reading_depth, fulltext_chars(真正喂进 LLM 的正文字符数),
fulltext_chars_raw(抽取到的原始正文长度), fulltext_truncated`。

`reading_depth` **四态**(与仓库 `src/scholar/schema.py` 的字段注释逐字一致,全库只此一份定义):

| 值 | 含义 |
|---|---|
| `chunked` | manual 全部 + 开关打开后的 auto |
| `single-call` | auto 单跳 |
| `unknown-legacy` | 仅 auto 存量条目(由回填写入) |
| 键缺失 / null | 只可能出现在 `has_full_text_reading == false` 的非精读条目上 |

**下游(`notes_query` / skill `scholar-write` / Obsidian vault)必须这样用**:

- `unknown-legacy` = **深度未知**,可能只覆盖正文前 40k 字符、且集中在靠前的几页(方法/结果常被砍掉)。
  这批条目一律**不重跑**(重跑要数千次 LLM 调用并改写全部历史 md/references/vault,爆炸半径远超收益);
  真要引用其中某篇时,走 skill `read-paper` 对那一篇**手动重读**——个案实测能从十来条句级标记涨到 57 条,
  是效果最好的补救。
- **别按 `highlights` 条数横向排序取证**:新老两代精读的产出密度天差地别,按条数排会系统性偏向新札记。
  要比"读得深不深"请看 `reading_depth`,不要拿 `tag_counts` 当代理指标。
- `fulltext_truncated`:**缺失 = 未知**,`false` = 确认未截断,二者禁止混同(`fulltext_chars` /
  `fulltext_chars_raw` 同理,存量回填一律留缺失,不猜不填)。

```bash
# 取证前先分层:看这批候选各是什么深度
jq -r '.papers[] | select(.duplicate_of == null and .has_full_text_reading)
       | [(.reading_depth // "MISSING"), .series, .citekey] | @tsv' literature_index.json | sort | uniq -c

# 只要读得最彻底的(manual 深读 + 开关打开后的 auto 分块精读)
jq -r '.papers[] | select(.duplicate_of == null and .reading_depth == "chunked")
       | [.citekey, .month, .title] | @tsv' literature_index.json
```

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

# 按年份区间 + 按方法论借鉴标记数排序
jq -r '.papers[] | select(.duplicate_of == null and .month >= "2025-01" and .month <= "2025-12")
       | [(.tag_counts.method // 0), .citekey, .title] | @tsv' literature_index.json | sort -rn

# 【句级调取】某主题下所有"可引用证据"(带出处 citekey+section),写作直接取证
jq -r '.papers[] | select(.duplicate_of == null)
       | select((.title + " " + .one_line) | test("MNAR|缺失"; "i"))
       | . as $p | .highlights[] | select(.role == "citable")
       | [$p.citekey, .section, .text] | @tsv' literature_index.json

# 【句级调取】某篇的所有"可反驳靶子"(写 critique 用)
jq -r '.papers[] | select(.citekey == "mesinovic2026Retracted")
       | .highlights[] | select(.role == "refutable") | .text' literature_index.json

# 【句级调取】全库"方法论借鉴"灵感库
jq -r '.papers[] | select(.duplicate_of == null)
       | . as $p | .highlights[] | select(.role == "method")
       | [$p.citekey, .text] | @tsv' literature_index.json

# 定位并阅读某篇的精读原文
grep -nF '[@xu2026Development]' 科研札记_2026-05_全文精读.md   # 拿行号后 Read 该节

# 配书目:直接用全局书目(已全库去重合并,含全文精读+手动精读两系列)
jq -r '.citekey_collisions' literature_index.json               # 必须为 [] 才能安全引用
pandoc draft.md --citeproc --bibliography=all_references.json -o draft.docx
# (按 role 取证 → 写稿 → 出稿的完整写作流:skill `scholar-write`;检索 CLI:scripts/notes_query.py)

# 体检:索引是否落后于札记(数量不一致→先跑 scripts/notes_index.py)
# 注意口径要对齐:months 按文件 stem 键,含 auto+manual 两系列,不能直接对 wc -l 全文精读
ls 科研札记_*_全文精读.md | wc -l; jq '[.months[] | select(.series=="auto")] | length' literature_index.json
```

## ⚠️ citekey 注意事项(重要)

- 多数 citekey 是 **headless 兜底键**(`作者姓+年+标题词`,`citekey_source: "fallback"`),**不是** Zotero/Better BibTeX 权威键。跨系统对账(Zotero、他人书目)一律以 **DOI / dedup_key** 为论文身份,citekey 只在「本索引 + 对应月 references.json」闭包内有效。
- `citekey_collisions` 非空 = 不同论文共用同一键,合并书目时同键**只保留 keeper 那篇**(另一篇引不到);在 XLBDTranslator-dev 仓库跑 `python scripts/notes_index.py --fix-collisions` 自动改键(保最早月不动,后出现者加 b/c 后缀,md+references.json 同步改)后再合并。
- **升级为权威键的路径**(人在时做):按索引 DOI 批量导入 Zotero → BBT 生成正式 citekey → 论文 md 里 `sed` 替换旧键 → bibliography 换 BBT 自动导出。

## 可拷贝到论文项目 CLAUDE.md 的片段

```markdown
## 文献库
精选文献札记库(按月,含全文精读)在:
`/Users/xiaolibird/Documents/GitHub/XLBDTranslator-dev/output/scholar_notes/`
找文献四步法:1) jq 查该目录 literature_index.json(过滤 duplicate_of==null),
或按 role 取证 `python scripts/notes_query.py <关键词> --role citable|refutable|method`;
2) 按 note_file+citekey grep 定位札记原文精读节;3) 正文引用 [@citekey];
4) 书目挂该目录 all_references.json(全库已去重合并;先确认 citekey_collisions 为空)。
要读原文 PDF:`python scripts/locate_pdf.py <citekey|DOI|arXiv号|标题>` 直接给出本地路径
(Zotero→札记索引→Spotlight 三级回退;退出码 1=本地没有,需去下载)。
详细配方读该目录的 AGENTS.md。⚠️ citekey 是兜底键,跨系统对账以 DOI 为准。
```
