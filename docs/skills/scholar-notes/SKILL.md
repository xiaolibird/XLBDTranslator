---
name: scholar-notes
description: 在本机的科研札记文献库(按月精选+全文精读,MNAR/MA-GCT/EHR缺失机制方向)中查找、阅读、引用文献。当用户要"找文献/查札记/文献索引/找参考文献/scholar notes",或写论文需要相关工作支撑时使用。
---

> 真相源：本文件在仓库 `docs/skills/scholar-notes/SKILL.md`；改完须跑
> `bash scripts/install_skills.sh` 同步到 `~/.claude/skills/`。

# 科研札记文献检索

文献库(按月精选,LLM 三态筛选 + top-5 全文精读):
`/Users/xiaolibird/Documents/GitHub/XLBDTranslator-dev/output/scholar_notes/`

覆盖 2023-01 至今。文件有三个系列,命名即口径:
- `科研札记_YYYY-MM_全文精读.md` —— 历史按月回填(2023-01 → 2026-06)
- `科研札记_YYYY-MM-DD_全文精读.md` —— **周札记**(周一日期),2026-07 起改为每周一自动入库
- `科研札记_YYYY-MM(-DD)_手动精读.md` —— 手动 PDF 深度精读(agent 交叉核验)

每篇 md 配同名 `references.json`(CSL-JSON)。总索引:`literature_index.json`;
完整使用说明:该目录 `AGENTS.md`(先读它)。

**另有一份 per-paper 视图**:`~/Documents/ScholarVault/`(Obsidian vault,431 篇 = 已全文精读的)。
一篇论文一个文件 `01-文献/<citekey>.md`,带 YAML frontmatter(citekey/doi/year/bucket/role/flags/
n_citable 等 30 个字段,**比 grep 月度大文件更适合按属性筛**),正文含句级证据 callout + TF-IDF 相邻文献 +
`_MOC/` 静态索引页。**它是索引的派生视图,不是真相源**——数字与全文以 `literature_index.json` 和月度 md 为准。
索引一变即自动同步(launchd `com.xlbd.scholar-vault` 监视 `literature_index.json`);手动补跑:
`PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/sync_vault.py --vault-dir ~/Documents/ScholarVault`(加 `--force` 忽略陈旧判定)。
⚠️ 该目录含用户手写内容(`## 我的札记` 与自加的 frontmatter 键/tag),**不要直接编辑或覆盖那部分**。

## 先看概念页(topics/)——按概念找答案,往往一步到位

`output/scholar_notes/topics/<slug>.md` 是**按概念横切全库的活综述**(与上面按论文组织的
视图正交)。用户问「MNAR 诊断有哪些方法」「跨中心迁移的证据怎么说」「库里有哪些反对
XX 的证据」这类**概念级问题**时,**先读概念页再决定要不要下钻单篇**:它已经把几十篇里
的相关句子合成好了,比现跑一轮检索再逐篇读快得多。

```bash
cat output/scholar_notes/topics/INDEX.md          # 有哪些概念页
cat output/scholar_notes/topics/mnar-diagnosis.md # 读某一页
```

读法要点:
- 每条论断后的 `[@citekey]` 由**证据编号回译**产生(合成时 LLM 只能引用召回集合里的编号,
  越界即剔除;模型自己写出的引用标记——不论带不带方括号——也会被剥离),citekey 因此
  不是模型现编的,而是来自召回集合。`build_topics.py --verify` 会扫描死键与残留裸引用
  (`bare_cites`)兜底,看到 ⚠️ 再去核实,没有异常不必逐条复核;
- 页底「本页证据」给出每条证据的 `note_file:note_line`,要核验原句照着点进去;○ 表示
  召回了但未被采用;
- 「⚔️ 分歧与冲突」是**有意保留的矛盾**,写 critique / discussion 时是现成的靶子;
- **引用率不能单独当质量信号**:○ 多不一定是 `queries` 跑偏,也可能是证据池混进了「对我
  研究的联想」这类主观批注(已默认排除,见 `config/topics.yaml` 的 `exclude_sections`);
  ● 多也不代表论断都扎实,可能是无关内容被顺带引用"注水"。判断可信度看论断是否具体、
  分歧是否来自不同文献,比看这两个比例数字可靠;
- **它是派生物**,内容全部来自 `highlights[]`。有疑问以月度札记原文为准。
- ⚠️ **要把具体数字写进稿子,先点开证据表核对原句**。防线保证「citekey 与原句真实存在」,
  **不保证转述没有失真**——从证据到论断由 LLM 完成,靠 prompt 约束而非程序强制。
  实测过:一组并列数字里最不利的那个被静默丢掉(66.0/4.4/70.2/64.4 写成范围「64.4–70.2」,
  真实是 4.4–70.2),且同一页正文写对了、分歧区仍写错。结论性判断可直接读页面,
  **效应量/百分比一律回证据表看原句**,「⚔️ 分歧与冲突」里的数字尤其要核。

Obsidian 侧同一批内容在 `~/Documents/ScholarVault/02-主题/`,citekey 已转成 `[[wiki 链接]]`,
可在关系图里看概念↔论文的连边;per-paper 笔记的反向链接会显示「哪些概念页引用了我」。

重建(新论文入库后,或改了 `config/topics.yaml` 的主题定义):
```bash
PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/build_topics.py [--topic <slug>] [--dry-run]
```
⚠️ 概念页同样含用户手写区(`## 我的批注`),**不要直接编辑生成块**——重建时会判冲突并拒绝覆盖。

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

## 语义检索

`jq`/`notes_query.py` 是精确子串匹配，查不到换述表达（如中文"缺失机制不可忽略"查不到英文
"informative missingness"）。这类场景改用语义检索（在 XLBDTranslator-dev 仓库根目录跑）：

```bash
PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/notes_search.py <中文或英文查询...> [--role citable|refutable|method] [--limit N] [--cite] [--json]
```

`--mode` 默认 `hybrid`（向量 + BM25 关键词 RRF 融合，也可 `--mode dense`/`--mode sparse`；
`sparse` 查询时不需要 Ollama，但仍要求向量库已构建）。分工：确切术语/citekey/role 硬门槛
用 `notes_query`；中文找英文文献、换述同义词、或 `notes_query` 空手时用 `notes_search`。

⚠️ **覆盖面警告**：有句级证据（highlights）的条目约 508 篇（截至 2026-08，占 keeper 的 24%），
语义检索的向量库同理。命中若标注"该篇无精读句级证据"，是真的没有，不是这次没搜到——别当成
"库里没有这方面内容"。

同步机制：向量库随周度自动入库（`ingest_notes.py`）best-effort 自动同步，Ollama 没起时只
跳过不报错。**手动改了月度/手动精读 md 里的句子或标签后**，向量库不会自动跟上，需要手跑：
```bash
PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/notes_index.py && PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/notes_embed.py
```

## 往库里加论文

三条入口,都在 XLBDTranslator-dev 仓库里跑:

```bash
# 1) 本周 Scholar 告警(周一 09:00 digest 已判过,复用裁决不重跑筛选)
PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/ingest_notes.py --list          # 先看判出了什么
PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/ingest_notes.py --pick 2,3,5    # 只入这几篇
PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/ingest_notes.py --auto          # 全入（launchd 周一 09:30 自动跑这条）

# 2) 任意 DOI / arXiv id / 标题(可能压根没在告警里出现过),一行一个
PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/ingest_notes.py --papers papers.txt

# 3) 本地 PDF(单篇、整个目录、或递归)——走三段式 agent 交叉核验,见 skill `read-paper`
PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/read_pdf.py ingest ~/Downloads/待读/
```

前两条产出周札记 `科研札记_YYYY-MM-DD_全文精读.md` 并自动刷索引;第三条产出手动精读系列。
三条都做**跨库去重**(dedup_key 从 `literature_index.json` 恢复),重复的论文不会二次入库。

## 注意

- citekey 多为兜底键(非 Zotero/BBT 权威键):跨系统对账一律以 **DOI/dedup_key** 为论文身份;升级路径见 AGENTS.md。
- 索引可能落后于札记:`.months` 按文件 stem 键,含 auto+manual 两系列,不能直接跟只数
  `_全文精读` 的 `wc -l` 比;要对齐口径用 `ls 科研札记_*_全文精读.md | wc -l` 与
  `jq '[.months[]|select(.series=="auto")]|length'`,不一致时先在 XLBDTranslator-dev 仓库跑
  `/Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/notes_index.py` 再检索。
- docx 是人读版,agent 只解析 md/json。
