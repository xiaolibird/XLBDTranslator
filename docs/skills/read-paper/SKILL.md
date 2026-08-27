---
name: read-paper
description: 手动把一篇 PDF 文献做**深度全文精读**并归档进本机科研札记文献库。当用户说"精读这篇 PDF/read paper/手动精读/深读这篇论文/把这个 PDF 加进文献库"并给出 PDF 时使用。区别于自动流水线：agent 亲自读完整本 PDF + 脚本双轨交叉核验，力求彻底通读。
---

> 真相源：本文件在仓库 `docs/skills/read-paper/SKILL.md`；改完须跑
> `bash scripts/install_skills.sh` 同步到 `~/.claude/skills/`。

# 手动 PDF 深度精读（agent 亲读 + 脚本交叉核验）

仓库：`/Users/xiaolibird/Documents/GitHub/XLBDTranslator-dev`（命令都在此目录、加 `PYTHONPATH=.` 运行）。

产物归档为独立的 `科研札记_YYYY-MM_手动精读.{md,docx,references.json,index.json}` 系列，
并进入 `output/scholar_notes/literature_index.json`（手动深读是 keeper，论文写作 agent 优先读到它）。

## 协议（严格按序）

### 1. ingest —— 脚本先出草稿

**单篇：**
```bash
cd /Users/xiaolibird/Documents/GitHub/XLBDTranslator-dev
PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/read_pdf.py ingest <pdf 路径> [--month YYYY-MM] [--title "手动标题"]
```

**批量**：`ingest` 的 `pdf` 参数实际是 `nargs="+"`，支持多路径混写、目录展开、`-r` 递归子目录；
一条命令即可把一批 PDF 都推上草稿：
```bash
PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/read_pdf.py ingest a.pdf b.pdf                    # 多个文件
PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/read_pdf.py ingest ~/Downloads/待读/              # 整个目录（非递归）
PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/read_pdf.py ingest ~/Papers/ -r                   # 递归子目录
```
`--title` 只对单篇有意义（多篇/目录时元数据解析失败的条目会退化成 `anon*` 键，事后单独对
那一篇重跑 `--title` 即可，不影响同批其余篇）。批量按文件名去重（真实路径 resolve 后），
同一篇不会因写法不同被读两遍。

它抽全文、拉 Crossref/arXiv 权威元数据、分块通读出**脚本草稿**，落 `output/scholar_notes/manual/<月>/<paper_id>.paper.json`（status=draft）。每篇单独打印 **bundle 路径**与 **「亲读范围」那一行**（总页数 + 20 页窗口切分，第 2 步照它读）。

已 final 的 bundle **不会被覆盖**，只打印 `⛔ 已 final（在 <月> 桶），跳过`（确需重跑加 `--force`，会丢弃已有核验成果）。守卫**跨月份桶**生效：同一个 PDF 在上个月已精读过，这个月再跑同样被拦——括号里的桶名就是保护它的那一月，不必困惑「我这个月没读过」。写了 `close_reading_final` 却忘翻 `status` 的 bundle 同样受保护。

输出最末的 **「⚠️ 需要注意」块** 必看：索引里已有同文（别白读一遍几个月前已精读的）、元数据不全（会退化成 `anon*` 键、书目缺卷期页，**单独对那一篇**重跑 `--title "精确标题"`——批量时 `--title` 不生效且会在这里报出来）、ingest 失败。

⚠️ **动手前先查索引里有没有同文——ingest 的「已有同文」提示来得太晚。** 那条提示印在跑完之后，
此时 LLM 额度已经烧掉了。实测踩过：2026-07 桶里躺着 4 个 ingest 失败留下的 draft 残留，
看上去像「没读完的欠账」，就 `--force` 重跑了一遍，跑完才被提示这 4 篇**早已在
`科研札记_2026-07_手动精读.md` 里完整入库**（highlights 25–74 条、`reading_depth=chunked`、
`reading_source=manual-pdf`），白烧 88 块分块通读。
**残留的 draft bundle ≠ 待办**：ingest 失败会留下 draft，而那篇论文完全可能后来由别的路径读完了。
所以看到 draft 先按 citekey / 标题查 `literature_index.json`：
```bash
python3 -c "
import json;ps=json.load(open('output/scholar_notes/literature_index.json'))['papers']
for p in ps:
    if '关键词' in (p.get('title') or ''):
        print(p['citekey'], p.get('series'), p.get('month'), p.get('reading_depth'), len(p.get('highlights') or []))"
```
**查到已入库后，分两种情况——不要一律当垃圾清掉**：

- **旧版已是核验过的完整终稿 → 丢弃新草稿**。判据：md 小节里 8 个标准分节齐全、末尾有
  「交叉核验记录」、highlights 数量与篇幅相称（229 页博论 74 条 / 12.8k 字符是健康的）。
  此时新跑的脚本草稿只有几十句且未核验，合并只会稀释。把 draft 挪进
  `manual/_stale_drafts/<时间戳>/`（**带月份前缀**避免跨月同名覆盖），并在同目录写一份
  `_why.json` 记下依据，免得日后翻到又当成欠账。
- **旧版明显读得不全 → 合并，别另起炉灶**。判据：分节缺失、没有交叉核验记录、
  或篇幅与页数严重不匹配（几百页的书只有两三千字）。做法是把新的完整精读写进**同一个
  bundle** 的 `close_reading_final` 再 finalize——手动深读会成为 keeper、旧条目自动标
  `duplicate`（ingest 末尾那句「继续 finalize 则手动深读成为 keeper」就是这个机制）。
  **不要**为了「保留两版」去造第二个 citekey，那会让同一篇论文在库里裂成两个身份。

**看 `draft_status`**：若为 `ok`，走下面正常协议（2–5 步，你亲读核验脚本草稿）；
若为 **`api_error`**（LLM 无额度/鉴权失败，如 402/401/403），脚本草稿这一轨作废，改走「回退协议」。

**看 `draft_note`**：`draft_status: ok` 时它通常为空；若出现「块笔记超汇总预算」，说明这篇太长
（块数 × 单块笔记长度 > 60000 字符），汇总时每块笔记都被均摊裁剪过——**被裁的部分不在草稿里**，
亲读核验时对论文后段（结果/局限/附录）不能依赖草稿，必须自己从 PDF 补齐。

### 2. 亲读整本 PDF（不可跳过）
用 **Read 工具**按 ingest 打印的「亲读范围」窗口**从头读到尾**读完整篇 PDF（`Read <pdf>` 带 `pages` 参数循环），
**不允许**只读摘要/结论就下笔。边读边记：研究问题、方法与数据、关键数字与效应量、图表/附录要点、局限。

**判草稿「编造」之前，必须先确认自己已读到最后一页。** 曾经只读到 31 页 PDF 的第 12 页，
就断言草稿引用的 Table 15/21/24 是脚本瞎编——那些表在第 13 页之后的附录里，每个数都对。
附录（补充表、敏感性分析、完整超参）恰恰是脚本草稿最爱引、也最容易被误判为伪造的地方。
若「亲读范围」显示**页数未知**（PyMuPDF 读不出），自己先确认总页数再动笔。

### 3. 交叉核验（先独立、后对照）
- 先基于**你自己的通读**写分节精读；
- 再打开 bundle 的 `close_reading_script`，**逐条比对**：每个数字、效应量、样本量、方法主张都回到 PDF 原文核验；
- 分歧一律**以你从 PDF 亲证的为准**；脚本有而你漏的，抽查属实才保留；脚本编造/串页的，删。

### 4. 写回 bundle（终稿）
把合并终稿写进 bundle 的这两个字段，并把 `status` 改为 `"final"`：
- `close_reading_final`：严格 `CloseReading` JSON —— `{"from_full_text": true, "source": "manual-pdf", "sections": [{"heading": "研究问题", "sentences": [{"text": "…", "tag": null}]}, …]}`。
  分节建议：研究问题 / 方法与数据 / **实验方法** / 结果与效应量 / 图表与补充材料要点 / 局限与可质疑点 / 对我研究的联想 / 逐节通读要点。
  其中 **实验方法** 节（2026-08-15 新增，存量已于 08-16 全部回填）：从原论文整理，标准是「他人照着能复现」——数据集/队列与划分（比例、分层、防泄漏措施）、预处理、模型配置与超参（优化器/学习率/batch/epoch/随机种子/硬件）、评估协议与指标、基线及其配置、代码与数据可得性（仓库链接、许可）。**原文未报告的项要显式写「原文未报告：X」，不许省略、更不许推测填补。**
  每项尽量标原文页码；可移植的做法打〔方法论借鉴〕，具体数字/配置打〔可引用证据〕。
  ⚠️ **「代码与数据可得性」最容易漏**——论文的 Code/Data availability 声明排版上紧挨参考文献、离方法节很远，读到那里时注意力已经松了。实测漏过一次（原文 p.11 明写 GitHub + Zenodo 链接，精读里整个维度消失）。务必单独写一句，有链接就抄全，没有就写「原文未报告」。
  句级 `tag` **只用**三值之一或 null（按对后续工作流的用途）：`"可引用证据"`（含具体数字/效应量/可溯源结果）/ `"可反驳观点"`（作者主张/可质疑处，写 critique 的靶子）/ `"方法论借鉴"`（可迁移的方法思路）。纯背景/动机置 null。这些句子会被索引聚合成 `highlights[]` 供工作流按 role（citable/refutable/method）跨库检索。
- `cross_check_report`：`{"corrected": [{"page": 7, "note": "脚本写 AUC=0.91，原文 Table 2 为 0.87，已改"}], "added": ["脚本漏了敏感性分析…"], "verified_count": 23}`。
  ⚠️ **形状是机器门禁**：报告必须是 JSON 对象，`corrected`/`added` 必须是**数组**。写成字符串会被
  逐字符切成十几条假纠错条，因此现在会被直接拒收并在 finalize 输出里点名（见第 5 步）。
  `verified_count` 显式写 0 的 bundle 也会被拒——核验了几项就写几项。
  纠错条会渲进札记的「交叉核验记录」节但**不打句级 tag**：实测这类条目里压倒性是「草稿写错了」
  而非论文本身的可质疑处，打成〔可反驳观点〕会污染 `scholar-write` 的取证轴。论文级的可质疑处
  请写进 `close_reading_final` 的「局限与可质疑点」节。
- `one_line`（顶层，可选但推荐）：一句话说清这篇对研究者的用处（≤30字），会成为索引的「一句话用处」检索字段；脚本失败时它是占位符，务必覆盖。

（保留 bundle 其余字段不动；用 Read 读原 bundle → Write 回写整个 JSON。）

⚠️ **`sections` 不许出现占位符——落盘的才算数。** 实测踩过：一篇 24 页论文精读做完了，
回写时 `close_reading_final` 却成了 `{"from_full_text": true, "source": "manual-pdf",
"sections": "SEE_ABOVE_PLACEHOLDER"}`——正文一个字没进文件（同批 `cross_check_report`
反而写得完整正确，所以从产物上看很像"成功了"）。诱因是**整份 bundle 一次 Write 太长**
（含 `close_reading_script` 时轻松几万字符），于是把最长的字段偷懒成一句占位符。
后果是 finalize 判该 bundle 结构非法、**整月拒绝重建并退出码 1**。

**bundle 大（>30KB）或 sections 长时，改分步落盘**：先把 `close_reading_final` 单独写成
一个临时 JSON 文件（可分多次 Write/Edit 逐节追加），确认它是合法 JSON 后再 merge 回 bundle：
```python
import json
crf = json.load(open('<临时文件>'))                       # 合法性在这一步就暴露
p = 'output/scholar_notes/manual/<月>/<paper_id>.paper.json'
d = json.load(open(p)); d['close_reading_final'] = crf; d['status'] = 'final'
json.dump(d, open(p, 'w'), ensure_ascii=False, indent=2)
```
**写完必须自检**：读回 bundle，确认 `close_reading_final['sections']` 是 **list**（不是 str）、
每个元素是 dict、各节句数与你的终稿对得上，再进第 5 步。

⚠️ **句级 `page` 必须是字符串，写成整数会让整月重建把这篇拒收。** `CloseReadSentence.page`
的类型是 `Optional[str]`（书籍链路要放 `"241-259"` 这种区间，所以是 str 不是 int）。标页码时
很容易顺手写 `"page": 7`，pydantic 会对每一句报 `Input should be a valid string`——一篇 82 句
的精读就是 77 条 validation error，finalize 打印 `⛔ bundle 结构非法，跳过` 然后**按剩下的
bundle 重建整月**，这篇静默不在产物里。实测踩过：DrFuse 那篇核验全做完了，第一次 finalize
出来 95 篇、它不在其中，回看日志才发现。
自检时把 `page` 一并查了（顺带查 `tag` 只用三值或 null）：
```python
tp = {type(x.get('page')).__name__ for s in crf['sections'] for x in s['sentences']}
tg = {x.get('tag') for s in crf['sections'] for x in s['sentences']}
assert tp <= {'str', 'NoneType'}, tp          # 整数会被拒收
assert tg <= {'可引用证据', '可反驳观点', '方法论借鉴', None}, tg
```
`cross_check_report` 里 `corrected[].page` 是另一回事，那里用 int 正常——别把两处搞混。

### 5. finalize —— 归档
```bash
PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/read_pdf.py finalize <bundle 路径>
```
它从当月**全部 final bundle** 重建手动精读四件套并刷新索引（同月可多篇追加、幂等）。
交叉核验报告会自动渲染为精读末节「交叉核验记录」。

⚠️ **finalize 会在动库之前先止损**：整月重建是**整篇重写**，一份 bundle 被拒收就等于把那篇
已归档论文从 md/references/sidecar/索引/书目/向量库里一起删掉。所以如果本轮重建会**净删除**
上一轮已归档的条目，finalize 会**整月一字不动**并打印 `⛔ … 整月未改动`，退出码 1。
处理方式：修好被点名的 bundle JSON 再重跑。确实不想要那几篇了才加 `--allow-removals`。

⚠️ **finalize 的输出有两条必看行**：
- `⏭ 跳过 N 篇 draft（未 agent 核验）` —— 这些还没做核验；
- `⛔ N 篇 bundle 读不出/结构非法，**未入库**` —— 这些**核验做完了但 JSON 坏了**，一篇都没进库。
  看到它必须修好那份 JSON 再重跑 finalize，**不要**重做核验。
  另：bundle 的 `month` 字段必须与它所在的 `manual/<月>/` 目录一致，否则 finalize 直接拒绝
  （按月重建会扫空桶，那篇会静默消失）。

⚠️ **finalize 可能在向量库同步那步僵死不退出**（本机 Ollama 没起来时）：四件套与索引其实
**早已写完**，进程却挂在那里 0% CPU 干等（实测挂了 28 分钟）。判断方法——看
`科研札记_<月>_手动精读.md` 的 mtime 已更新、`literature_index.json` 能正常解析且含新篇，
就说明归档已完成，剩下的等待是空转，杀掉即可（退出码 144 = SIGTERM，不是失败）。
随后起 Ollama 再补跑一次增量同步：
```bash
PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/notes_embed.py
```
验证新篇真的可检索（**dense 模式**，hybrid 按 RRF 名次排序会给假阴性）：
```bash
PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/notes_search.py "<新篇的核心论断>" --mode dense --min-score 0.62 --limit 5
```
（注意是 `--limit`，没有 `--top`。）

### 6. 汇报
给用户：归档的 md/docx 路径、本月手动深读篇数、索引撞键组数（非 0 时提示先跑
`PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/notes_index.py --fix-collisions`）；若 ingest 提示"索引里已有同文"，
说明这篇现已成为 keeper（自动浅读版被标 duplicate）。

此后可用 `scholar-notes` skill 按 role/highlights 查这批新入库的文献；写论文取证用
`scholar-write` skill 按 citable/refutable/method 三轴调证据。

## 批量协议（多篇 PDF 一起精读）

Step 1 一条命令能同时 ingest 一批 PDF，但第 2–4 步（亲读→核验→写回）**必须逐 bundle 做**——
LLM/agent 一次只能扎实读一篇，混批读会互相串页/串数字。协议：

1. Step 1 批量 ingest，拿到全部 bundle 路径列表（打印里每篇一段，逐条记下）。
2. **对每个 bundle 循环**做第 2–4 步：亲读整本 PDF → 交叉核验草稿 → 写回该 bundle 的
   `close_reading_final`/`cross_check_report`、`status="final"`。一篇写完再开下一篇，
   不要并行开多个 PDF 同时读（上下文会话里容易读串）。
3. 全批都写成 `final` 后，**只需跑一次 `finalize`**（任取批内一个 bundle 路径即可）：
   `finalize` 是按月重建，会把当月目录下所有 `final` bundle 一并归档，不必每篇单独 finalize。
   跨月批次（同批 PDF 落在不同月）要对每个涉及的月各跑一次 finalize。
4. 汇报时给全批的篇数、跨月分布、索引撞键组数；哪几篇因元数据解析失败退化成 `anon*` 键要
   单独点出，方便事后 `--title` 重跑纠正。

## 回退协议（`draft_status: api_error` —— API 没钱时）

脚本深读那一轨（DeepSeek 分块通读）不可用，但**交叉核验不能丢**——改用**两个 subagent 对抗生成**，
一个 Opus 出稿、一个 Sonnet 对抗核验，二者对同一 PDF 互相制衡：

1. **并行 spawn 两个 subagent**（都能用 Read 工具读 PDF）：
   - **Opus 生成者**（`Agent`，model=`opus`）：亲读整本 PDF（20 页窗口读完），产出严格 `CloseReading` JSON 深读稿（分节 + 句级角色 tag：可引用证据/可反驳观点/方法论借鉴）。
   - **Sonnet 对抗者**（`Agent`，model=`sonnet`）：**独立**亲读同一 PDF，产出自己的深读稿；随后拿到 Opus 稿，逐条**质疑**每个数字/效应量/方向/方法主张，回 PDF 原文判真伪，列出分歧与纠错。
2. **主 agent（你）裁决合并**：以 PDF 亲证为准，把两稿收敛成一份 `close_reading_final`；两模型分歧处，回 PDF 定夺，胜出方进终稿。
3. `cross_check_report` 记录对抗过程：`{"corrected": [{"page": N, "note": "Opus 称 AUC=0.91，Sonnet 查 Table 2 为 0.87，PDF 亲证取 0.87"}], "added": [...], "verified_count": K, "adversarial": {"generator": "opus", "critic": "sonnet", "disagreements": M}}`。
4. 顶层 `one_line` 照填；`status="final"` → `finalize`。

> 要点：Opus 与 Sonnet **各自独立读 PDF**（不是让 Sonnet 只审 Opus 的字），这样对抗才有信息增益；
> 最终真值锚点始终是 PDF 原文，不是任一模型的说法。若只剩一个模型可用，退化为「单 subagent 生成 + 你亲读核验」。

## 硬规则
- **不得**只读摘要就精读；数字/结论必须 PDF 亲证。
- **不得**在没读到最后一页时断言草稿造假——先核对总页数（见第 2 步）。
- **不得**编造 PDF 里没有的引用、数据或结论。
- 跨系统对账以 **DOI** 为论文身份（citekey 是本库兜底键）。
- ingest 失败常见原因：PDF 是扫描件/加密（抽不出文本）——反馈用户换可选中文字的 PDF。
