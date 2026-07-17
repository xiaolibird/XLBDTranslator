---
name: read-paper
description: 手动把一篇 PDF 文献做**深度全文精读**并归档进本机科研札记文献库。当用户说"精读这篇 PDF/read paper/手动精读/深读这篇论文/把这个 PDF 加进文献库"并给出 PDF 时使用。区别于自动流水线：agent 亲自读完整本 PDF + 脚本双轨交叉核验，力求彻底通读。
---

# 手动 PDF 深度精读（agent 亲读 + 脚本交叉核验）

仓库：`/Users/xiaolibird/Documents/GitHub/XLBDTranslator-dev`（命令都在此目录、加 `PYTHONPATH=.` 运行）。

产物归档为独立的 `科研札记_YYYY-MM_手动精读.{md,docx,references.json,index.json}` 系列，
并进入 `output/scholar_notes/literature_index.json`（手动深读是 keeper，论文写作 agent 优先读到它）。

## 协议（严格按序）

### 1. ingest —— 脚本先出草稿
```bash
cd /Users/xiaolibird/Documents/GitHub/XLBDTranslator-dev
PYTHONPATH=. python scripts/read_pdf.py ingest <pdf 路径> [--month YYYY-MM] [--title "手动标题"]
```
它抽全文、拉 Crossref/arXiv 权威元数据、分块通读出**脚本草稿**，落 `output/scholar_notes/manual/<月>/<paper_id>.paper.json`（status=draft）。记下打印的 **bundle 路径**。

**看 `draft_status`**：若为 `ok`，走下面正常协议（2–5 步，你亲读核验脚本草稿）；
若为 **`api_error`**（LLM 无额度/鉴权失败，如 DeepSeek 402），脚本草稿这一轨作废，改走「回退协议」。

### 2. 亲读整本 PDF（不可跳过）
用 **Read 工具**按 20 页窗口**从头读到尾**读完整篇 PDF（`Read <pdf>` 带 `pages` 参数循环），
**不允许**只读摘要/结论就下笔。边读边记：研究问题、方法与数据、关键数字与效应量、图表/附录要点、局限。

### 3. 交叉核验（先独立、后对照）
- 先基于**你自己的通读**写分节精读；
- 再打开 bundle 的 `close_reading_script`，**逐条比对**：每个数字、效应量、样本量、方法主张都回到 PDF 原文核验；
- 分歧一律**以你从 PDF 亲证的为准**；脚本有而你漏的，抽查属实才保留；脚本编造/串页的，删。

### 4. 写回 bundle（终稿）
把合并终稿写进 bundle 的这两个字段，并把 `status` 改为 `"final"`：
- `close_reading_final`：严格 `CloseReading` JSON —— `{"from_full_text": true, "source": "manual-pdf", "sections": [{"heading": "研究问题", "sentences": [{"text": "…", "tag": null}]}, …]}`。
  分节建议：研究问题 / 方法与数据 / 结果与效应量 / 图表与补充材料要点 / 局限与可质疑点 / 对我研究的联想 / 逐节通读要点。
  句级 `tag` **只用**三值之一或 null（按对后续工作流的用途）：`"可引用证据"`（含具体数字/效应量/可溯源结果）/ `"可反驳观点"`（作者主张/可质疑处，写 critique 的靶子）/ `"方法论借鉴"`（可迁移的方法思路）。纯背景/动机置 null。这些句子会被索引聚合成 `highlights[]` 供工作流按 role（citable/refutable/method）跨库检索。
- `cross_check_report`：`{"corrected": [{"page": 7, "note": "脚本写 AUC=0.91，原文 Table 2 为 0.87，已改"}], "added": ["脚本漏了敏感性分析…"], "verified_count": 23}`。
- `one_line`（顶层，可选但推荐）：一句话说清这篇对研究者的用处（≤30字），会成为索引的「一句话用处」检索字段；脚本失败时它是占位符，务必覆盖。

（保留 bundle 其余字段不动；用 Read 读原 bundle → Write 回写整个 JSON。）

### 5. finalize —— 归档
```bash
PYTHONPATH=. python scripts/read_pdf.py finalize <bundle 路径>
```
它从当月**全部 final bundle** 重建手动精读四件套并刷新索引（同月可多篇追加、幂等）。
交叉核验报告会自动渲染为精读末节「交叉核验记录」。

### 6. 汇报
给用户：归档的 md/docx 路径、本月手动深读篇数、索引撞键组数（非 0 时提示先跑
`PYTHONPATH=. python scripts/notes_index.py --fix-collisions`）；若 ingest 提示"索引里已有同文"，
说明这篇现已成为 keeper（自动浅读版被标 duplicate）。

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
- **不得**编造 PDF 里没有的引用、数据或结论。
- 跨系统对账以 **DOI** 为论文身份（citekey 是本库兜底键）。
- ingest 失败常见原因：PDF 是扫描件/加密（抽不出文本）——反馈用户换可选中文字的 PDF。
