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

还有带批次后缀的变体(如 `科研札记_2026-07-28-TFM_手动精读.md`、
`科研札记_2026-07-27-HuiyingLiang_全文精读.md`),**上面三种模式不完备**。

⚠️ **定位某篇论文在哪个文件,一律以索引条目的 `note_file` 字段为准,绝不要拼文件名**——
单月可能同时存在 6 个以上文件(月度/周/手动/批次变体),而且 citekey 里的年份是**出版年**、
与入库 `month` 常常不是一回事(实测 `lim2025Multicenter` 的 month 是 `2026-07`)。靠猜必错。

每篇 md 配同名 `references.json`(CSL-JSON)。总索引:`literature_index.json`;
完整使用说明:该目录 `AGENTS.md`(先读它——611 行/48KB,**用 Read 分段读,别 cat**)。

**另有一份 per-paper 视图**:`~/Documents/ScholarVault/`(Obsidian vault,431 篇 = 已全文精读的)。
一篇论文一个文件 `01-文献/<citekey>.md`,带 YAML frontmatter(citekey/doi/year/bucket/role/flags/
n_citable 等 30 个字段,**比 grep 月度大文件更适合按属性筛**),正文含句级证据 callout + TF-IDF 相邻文献 +
`_MOC/` 静态索引页。**它是索引的派生视图,不是真相源**——数字与全文以 `literature_index.json` 和月度 md 为准。
索引/概念页/问答一变即自动同步(launchd `com.xlbd.scholar-vault` 监视 `literature_index.json` 与 `topics/`、`topics/qa/` 三条 WatchPaths);手动补跑:
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
- 页底「本页证据」给出每条证据的 `note_file:note_line`。注意这是**篇级**定位(指向该篇
  在札记里的标题行,与索引 `note_line` 同值),不是原句所在行——核验时 Read 到该篇小节后,
  再按证据的 `section` 找那一段(一篇小节常有上百行);○ 表示召回了但未被采用;
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

## 知识层 lint(`topics/_lint.md`)——引到某篇之前先扫一眼

`build_topics.py --verify` 查的是格式(引用还追不追得回去);`scripts/lint_notes.py` 查的是
**整个库摆在一起看有没有问题**,四项(另有派生物新鲜度子项默认随跑,见下):**撤稿**(OpenAlex `is_retracted` + 标题标记)、
**跨文献对撞**(citable ↔ refutable 的跨论文近邻句对,LLM 分五档裁决)、**陈旧论断**
(支撑文献最新的一篇也已 5 年前)、**覆盖缺口**(高优先级精读却连证据池都没进过的论文)。

```bash
cat output/scholar_notes/topics/_lint.md          # 直接读上一轮的报告(月度自动更新)
PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/lint_notes.py --skip-contradictions   # 重跑不花钱的三项
PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/lint_notes.py --offline --skip-stale --skip-coverage   # 只补跑对撞(其余三节结转)
cat "$HOME/Documents/ScholarVault/02-主题/_lint.md"   # vault 里那份副本(有自己独立的批注区,ack 写这儿也算)
```

- **先看报告顶部那两行**:「本轮必须处理」只放硬信号(当前只有撤稿,没有就写"无");
  「本轮状态」写四项各自是 ✅ 本轮刚跑 还是 ⏸ 结转自 N 天前,**每项都带绝对日期和条数**。
- 状态行下方的「🧭 派生物新鲜度」块是独立子项(向量库/vault/时间线xlsx/书目是否落后于索引,
  三态:新鲜/⌛未判定/陈旧),每轮重算不结转;陈旧行会带责任 launchd job 与日志路径。
  ✅ 只表示"本轮真跑过这一项",**不是从时间戳反推的**——同日先全跑再窄跑时,窄跑那几项
  写的是 ⏸ 结转自今天早些时候。**本轮跳过的一节会把上一次的结果原样结转并标明时效**
  (节标题下方那条 `⏸ 本轮未执行……不代表当前状态`),所以窄跑不会再把别的节清空——
  但结转来的结论也**不是**当前状态。
- **写作取证时**:如果 `_lint.md` 的撤稿一节点了名,那个 citekey 一条都不能引
  (哪怕那一节是结转来的,也先按有问题处理再去核实;那一节里"已被概念页引用:X、Y"
  同样是结转那一轮的页面快照)。「⚔️ 跨文献对撞」一节是现成的 discussion 素材
  (哪两篇的做法不可直接比)。**🔁「单篇内部自相矛盾」那一档不要跨两篇核对数字**——
  问题出在它点名的那一篇自己的原文里(如"本文附录 I 与附录 R 不一致")。
  **「🔀 方法学分歧 / 📐 适用范围限定」默认折在 `<details>` 里,那一档不是待办,
  是写作素材**——两篇阈值/插补/校正不同是文献的永久属性,没有任何操作能让它消失;
  写 related work / discussion 时展开来抄,别当缺陷去修。
  **「⚔️ 结论冲突」确认是真的之后**:概念页**不会自动知道这件事**(合成时读的是句级证据,
  不读 lint 报告,重合成也不会写进去)。把结论写进 `_lint.md` 的 `## 我的批注` 区
  ——哪两篇、谁的做法在什么条件下更可信——写 discussion 时回来抄,再写一行 ack 折起来。
  想让某一页反映它只能手动编辑那页的「我的批注」区(生成块不能手改,会判 conflict)。
- **读报告时看分母**:frontmatter 里对应计数是 `null` 而不是 `0`,`null` 的语义是
  **"从来没执行过"**(跑过一次就会一直结转)。撤稿那节永远带着"2256 篇里有 DOI 的
  1898 篇、解析到 1875 篇"这样一行——**「0 篇撤稿」从来不等于「库是干净的」**。
- **陈旧论断那节按「撑着它的那篇老文献」聚合**:"N 篇老文献是 M 条论断的唯一地基",
  可执行单位是那几篇(去补一轮新文献),不是逐条核对论断。报告直接给集中度
  ("N 篇各撑 ≥2 条,合计 X%,先补这几篇")与命令
  `scripts/search_pubs.py "<该篇的主题词>" --days 1825`(即 skill `scholar-search`)。
  ⚠️ 这一节的量**日历驱动**:阈值 = 当前年 − 5,每年 1 月自己长一截,报告会在 delta 下面
  注明"其中 N 条是阈值前移掉进来的,不是内容变化";锚文献上限 `--stale-anchor-limit`(默认 20)。
- **覆盖缺口那节的「碰不到」不等于「与这些概念无关」**:8 页当前全部触及各自
  `max_evidence` 上限,所以它只意味着"没挤进任何一页的 top-`max_evidence`"。名单里
  「最近 3 个月新入库」那一档尤其不是缺口信号;「值得考虑开新页」那一档按年份**正序**
  (最老优先),标「元数据可疑」的是索引里 `year` 写错了(只标不改)。
  想判断真缺口,对那一篇跑 `notes_search.py`。
- 陈旧/孤儿两节开头各有一行 delta(本轮新增 / 与上轮相同 / 已消失),先看那一行再决定
  要不要往下读——这两节是每月自动跑的,大部分内容与上轮相同。
- 对撞是唯一调 LLM 的一项,月度自动跑时被 `--skip-contradictions` 关掉;想看要手动跑。
  超过 45 天没跑时报告顶部与 stdout 会提醒补跑。
- **确认过某条不是问题(三节都支持)**:写一行 `- ack: <ID> <说明>`。ID 就印在条目末尾:
  对撞与陈旧论断是 8 位哈希(`#ab12cd34`),孤儿论文**直接就是 citekey**
  (`#walker2009Evaluation`)。连反引号一起复制也认,有序列表前缀/全角冒号/大小写混排也认。
  **写在哪一份文件**:`output/scholar_notes/topics/_lint.md` 或
  `~/Documents/ScholarVault/02-主题/_lint.md` 的「我的批注」区都行(**两份都读、取并集**;
  vault 那份有它自己独立的批注区)——报告顶部会把当轮生效的两个绝对路径逐字印出来。
  下轮它折进 `<details>`;**结转来的那一节同样会折**(月度自动化不跑对撞,那节年年是
  结转的,不折就等于 ack 永远不生效)。ID 一条都没对上时报告顶部会列出来提醒。
  原文一变 ID 就变、会重新展开。ID 可以含任意非空白字符(库里有 40 个非 ASCII citekey,
  4 个首字符就不是 ASCII),照抄报告里印的那一串即可。
  **ack 会让队列往前走**:孤儿那节的 `--orphan-limit` 是在分完 ack 之后才截断的,
  处理掉列出的 25 篇,下轮后面那批顶上来,小标题与顶部状态行都写"待办 N"。
  ⚠️ **概念页一被重合成,那一页的陈旧 ack 会全部失效并重新报成"新增"**——陈旧 ID 是
  `sha1(slug + 论断文本)`,重合成会重写论断文本。设计如此(内容变了就是新论断),
  但第一次撞上会以为是 bug。孤儿那节不受影响(ID 就是 citekey)。

## 问答归档(`topics/qa/`)——先看问过没有,再决定要不要重新检索

**先路由,再决定用不用它**:

| 这个问题是…… | 用什么 |
|---|---|
| 概念级、会反复用、希望**自动保鲜** | **概念页** `topics/<slug>.md`(新增就往 `config/topics.yaml` 加一条) |
| 临时的、具体的、一次性的 | **`scripts/ask_notes.py`**(本节) |
| 只想看有哪些句子、自己判断 | `notes_search.py`(语义) / `notes_query.py`(role 硬门槛);写稿取证走 `scholar-write` |

⚠️ **已有概念页覆盖的问题不要用 ask_notes**:实测同一个 MNAR 诊断问题,问答页 40 条
证据 / 45% 引用率,而 `mnar-diagnosis.md` 是 70 条 / 100% 且自动重合成——**不值那 90 秒**。
脚本现在会自动比一遍概念页并提示,但判断权在你。

具体的、临时冒出来的问题用 `scripts/ask_notes.py` 问,
答案会连同每条论断的句级出处一起归档,下次直接读。

```bash
PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/ask_notes.py --list   # 先看问过什么
cat output/scholar_notes/topics/qa/INDEX.md                                                              # 或读目录页
PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/ask_notes.py "<问题>" -q "<英文术语>" -q "<换个说法>"
```

- **同一个问题再问一次是原地更新那一页**(空白/标点/全半角/零宽字符都归一化,
  少打一个问号不会另开一页),不会堆出第二份答案;问之前会自动做**语义**查重并给出
  slug、路径与可直接粘的更新命令 `ask_notes.py "<那一页自己的问题>" --slug <slug>`（换问法覆盖那一页没有路径）。
  Ollama 不可用时降级回词面重合,并会明说降级了。
- **`--slug` 不会静默覆盖别人的问答**:那个 slug 已属于另一个问题时直接报错退 2。
- 每条论断带 `[@citekey]`,页底证据表给 `note_file:note_line`。**要把具体数字写进稿子,
  先点开原句核对**——防线保证 citekey 与原句真实存在,不保证转述没有失真。
  证据召回是**窄而深**(28 条 / 单篇最多 3 条),把最相关那几篇挖到第 2、第 3 句。
- 「⚠️ 用之前要知道的」是限制条件;「**本次召回没覆盖到的**」**不等于"库里没有"**——
  它只是这一次那几十条证据没能回答的部分。脚本会拿每条空白回查向量库与概念页,
  命中就标「⚠️ 但库里可能有」。没标记也别当结论,先查 `topics/INDEX.md` 与 `notes_search.py`
  再决定要不要去补文献。
- ⚠️ **归档问答不在向量库里**(概念页也不在,是同一笔已知欠账),`notes_search.py` 搜不到。
  要找旧问答就用 `--list`、读 `topics/qa/INDEX.md`、或在 Obsidian 的 `02-主题/问答/` 里全文搜。
- ⚠️ **Obsidian 那份要等下一次索引重建才出现**(vault 同步只被 `literature_index.json` 触发,
  归档问答不动索引)。急用先跑:
  ```bash
  PYTHONPATH=. python scripts/sync_vault.py --vault-dir ~/Documents/ScholarVault --force
  ```
- ⚠️ 与概念页一样含用户手写区(`## 我的批注`),**不要直接编辑生成块**——会判冲突并拒绝覆盖。
- `--verify` 还会报**残留证据编号**与**防线版本**(旧版本的页面标 ⚠️ 并给出重跑命令)。

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
   # 某篇的所有"可反驳靶子"(写 critique 用)——按 citekey 精确查**也要**带 duplicate_of
   # 过滤:库里有 87 条重复条目,同 citekey 可能命中新旧两条且 one_line 内容不同:
   jq -r '.papers[] | select(.citekey=="<citekey>" and .duplicate_of==null)
          | .highlights[] | select(.role=="refutable") | .text' literature_index.json
   # 全库"方法论借鉴"灵感库:  select(.role=="method")
   ```
   历史条目的 role 由旧标记规则近似映射(方法学创新→method、重要发现→citable、研究背景→丢弃);
   手动精读的 refutable 还含对抗核验的纠错条。新精读由 LLM/subagent 直接精确产出三类。
2. **读原文**:索引条目自带定位字段,直接取用,不要按 month 猜文件名:
   ```bash
   jq -r '.papers[] | select(.citekey=="<citekey>" and .duplicate_of==null)
          | [.note_file, .note_line, .note_heading] | @tsv' literature_index.json
   ```
   `note_file` 是裸文件名(拼上库目录前缀),`note_line` 是该篇标题行的 1-based 行号——
   用 Read 带 offset 直达该小节;`grep -nF '[@<citekey>]' <note_file>` 只作行号漂移时的兜底。
   重点是「全文精读」节(句级标记:`〔可引用证据〕`取证 / `〔可反驳观点〕`靶子 /
   `〔方法论借鉴〕`方法思路,以及「对我研究的联想」小节;历史札记可能仍是旧标记
   〔方法学创新/重要发现/研究背景〕)。
3. **引用**:论文正文用 pandoc 语法 `[@citekey]`。
4. **书目**:直接用现成的全局书目 `all_references.json`(全库去重合并,含全文精读+手动精读两个系列,
   由 `scripts/notes_index.py` 自动刷新、勿手改):
   ```bash
   pandoc draft.md --citeproc --bibliography=output/scholar_notes/all_references.json -o draft.docx
   ```
   用前确认 `jq '.citekey_collisions' literature_index.json` 为 `[]`(非空先跑 `notes_index.py --fix-collisions`)。
   写作取证的完整流程(按 role 轴 query → 写稿 → 出稿)见 skill `scholar-write`。

### 索引 schema 速查(`literature_index.json` 的 `.papers[]`,共 33 键,常用这些)

| 字段 | 含义 |
|---|---|
| `citekey` / `doi` / `arxiv_id` | 身份键(citekey 是引用键;跨源身份是 `dedup_key`) |
| `title` / `title_zh` / `year` / `journal` | 基本书目 |
| `month` | 归属月份键(仅分组用,**定位文件用 note_file**) |
| `series` | `auto`(月度回填+周札记) / `manual`(手动精读) / `book`(整本书按章精读) **三分** |
| `note_file` / `note_line` / `note_heading` | 札记定位:裸文件名 / 标题行号(1-based) / 标题行原文 |
| `decision` / `priority_tier` / `one_line` | 筛选裁决 / 优先级档 / 一句话判词 |
| `role` / `bucket` / `flags` | 主 role 轴 / 研究维度 / 旗标(含 `⚑ RETRACTED`) |
| `has_full_text_reading` / `reading_depth` | 是否有全文精读 / 精读深度 |
| `highlights[]` | 句级证据 `{role, tag, section, text}`；书籍条目**额外带 `pages`**(原书页码锚)与 `chapter` |
| 书籍/章节字段 | `entry_type`(`book`专著/`chapter`编著的一章，缺席=文章) / `isbn` / `publisher` / `edition` / `editors[]` / `book_key`(章所属书的 citekey) / `container_title` / `chapter_number` / `page_range` |
| `duplicate_of` | 非 null = 重复条目(指向 keeper 的 dedup_key)——**取数一律过滤掉** |

## 语义检索

`jq`/`notes_query.py` 是精确子串匹配，查不到换述表达（如中文"缺失机制不可忽略"查不到英文
"informative missingness"）。这类场景改用语义检索（在 XLBDTranslator-dev 仓库根目录跑）：

```bash
PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/notes_search.py <中文或英文查询...> [--role citable|refutable|method] [--series auto|manual|book] [--book CITEKEY] [--limit N] [--cite] [--json]
```

**书籍证据**：`--series book` 只看书、`--book <citekey>` 只看某一本（专著传书的 citekey，编著传所属书的 `book_key`，两侧都收）。书籍命中的句子在人读模式显示为 `⟨p.247⟩`，`--cite` 会产出带 pandoc 页码定位符的引用串 `[@little2020rubin, p. 247]`——**引用几百页的书必须带定位符**，不带等于没标出处。

`--mode` 默认 `hybrid`（向量 + BM25 关键词 RRF 融合，展示集默认再经 bge-reranker 重排——头行会标「已重排」，余弦分不再单调递减，判强弱看分不看位次；也可 `--mode dense`/`--mode sparse`；
`sparse` 查询时不需要 Ollama，但仍要求向量库已构建）。分工：确切术语/citekey/role 硬门槛
用 `notes_query`；中文找英文文献、换述同义词、或 `notes_query` 空手时用 `notes_search`。

⚠️ **覆盖面警告**：有句级证据（highlights）的条目约三成（精确数见 output/scholar_notes/AGENTS.md，实时数以 literature_index.json 为准），
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
