---
name: read-book
description: 把一整本书（教科书/工具书/专著，300-700 页）按目录切章、对论文研究问题分诊、只深读值得读的章，产出带原书页码锚的可引用札记并归档进本机文献库。当用户说"精读这本书/把这本教科书加进文献库/read book/这本书太厚我读不完/按章深读"并给出 PDF 时使用。区别于 read-paper：那是一篇论文一次读完，这是先分诊再按章读，且多一道确定性引文回验。
---

> 真相源：本文件在仓库 `docs/skills/read-book/SKILL.md`；改完须跑
> `bash scripts/install_skills.sh` 同步到 `~/.claude/skills/`。

# 整本书深度精读（目录切章 + 分诊 + 亲读 + 引文回验）

仓库：`/Users/xiaolibird/Documents/GitHub/XLBDTranslator-dev`（命令都在此目录、加 `PYTHONPATH=.` 运行）。
Python：`/Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12`

产物归档为 `科研札记_<YYYY-MM-DD>-<BookSlug>_书籍精读.{md,docx,references.json,index.json}`，
并进入 `literature_index.json`（series=book）与向量库（带章节/页码元数据）。

## 这条链路为什么不同于 read-paper

一本 700 页的书不能当成一篇长论文读，三处结构性差异：

1. **切分按目录，不按页窗**。20 页硬切必然拦腰截断论证。章节边界由出版方给定，免费且准确。
2. **先分诊、再决定读什么**。教科书里与某篇论文相关的往往只有几章；「不读哪些章」必须是
   有记录、可复核的决定，而不是一次沉默的放弃。
3. **引文必须能 grep 回原文**。书籍札记的价值全在可直接引用的逐字引句上，
   而书级摘要最主要的错误类别恰恰是「需要全书上下文才能发现的不忠实引用」，
   且 LLM 忠实性裁判对这类错误不可靠。所以回验是字符串比对，不是再叫一个模型判断。

## 协议（严格按序）

### 1. manifest —— 建结构脊柱

```bash
PYTHONPATH=. python3.12 scripts/book_digest.py manifest <pdf> \
  --slug <BookSlug> --type book|chapter \
  [--printed-toc-pages 5-10] [--split-level 2] [--page-offset -12] \
  --set title="…" --set "authors=A; B" --set edition=3rd \
  --set publisher=… --set year=2019 --set isbn=… --set citekey=…
```

**`--type` 决定引用粒度，选错要重来**：
- `book`（专著，如 Little & Rubin）：全书一个 citekey，章是精读分节，引用时用页码定位符
  `[@little2020rubin, p. 247]`。
- `chapter`（编著文集，如 JAMA Users' Guides）：各章作者不同，**章才是可引单元**，
  每章一条索引记录、各有 citekey、CSL type=chapter。

**书目字段必须用 `--set` 给全，不要让脚本猜**。PDF 元数据经常是空的或错的
（Little & Rubin 那本连标题都没有）。书目错一个字，全书的引用就全错。

**没切出章节时**（`⚠️ 没切出章节`）：该书 PDF 没有书签。给 `--printed-toc-pages`
指向书自己印的目录页（PDF 页码，可用 `Read <pdf> pages=1-20` 找），脚本会解析印刷目录。
**不会**退回页窗盲切——那正是这条链路要消灭的做法。

核对输出的三件事：章数是否与书相符、页码范围是否落在正文（不含参考文献/索引）、
页偏移是否正确（`原书页码 = PDF 页序 + offset`，随便挑一页 `Read` 一下验证）。

### 2. questions —— 按书扩展分诊轴（**几乎总是必要，别跳过**）

`config/topics.yaml` 的问题是**札记库的概念页轴**，措辞带着 EHR/ML 的具体口径，
未必是这本书回答问题的层次。两次实测，两种失配：

- **书不在那条轴上**：JAMA Users' Guides 按那 8 问分诊，Harm 章得 0 分——裁决实质
  正确（那章确实不谈缺失机制），但结论「整本不用读」是错的，那本书的价值在方法学
  评价框架。补 5 个方法学问题后，9/29 章入选。
- **书在轴上、但答的是下一层**：Little & Rubin 是缺失数据教科书，正对 topics.yaml，
  第 1 章却只得 1 分——而那一章含 Definition 1.1 与 MCAR/MAR/MNAR 机制分类法，
  正是论文引用机制定义的经典出处。原因是问题问的是「判断 **EHR 数据**的缺失是否
  属于 MNAR，**现有方法**有哪些」，而基础统计教科书回答的是其下的**形式化定义与
  理论**。补理论层问题（形式化定义/似然理论/可识别性/EM 机制/缺失模式）后才对得上。

判断方法：先翻目录，问自己「这本书能回答的问题里，哪些是 topics.yaml 没问的，
或问得比它更具体/更抽象的？」把它们补上再分诊。

```bash
PYTHONPATH=. python3.12 scripts/book_digest.py questions --slug <Slug>          # 看当前轴
PYTHONPATH=. python3.12 scripts/book_digest.py questions --slug <Slug> \
  --add "slug|标题|这一问要问什么"
```

先翻目录判断：这本书能回答的问题里，有哪些是 topics.yaml 没问的？把它们补上再分诊。

### 3. triage —— 逐章打分，产出深读队列

```bash
PYTHONPATH=. python3.12 scripts/book_digest.py triage --slug <Slug>            # dry-run
PYTHONPATH=. python3.12 scripts/book_digest.py triage --slug <Slug> --apply
```

每章 1 次 LLM 调用（只喂章标题+子节+首尾几页，不喂全章）。产出
`books/<Slug>/triage.md` 热力图与深读队列（max ≥ 2 入选）。

**热力图最该看的是空列**：某个研究问题全书没有一章 ≥2，结论是「这本书回答不了它」，
应去别处取证，**不是**把门槛调低。分诊失败的章按「选中」处理——一次 LLM 故障
不该静默地把一章从视野里抹掉。

### 4. read —— 建 draft bundle 并亲读

```bash
PYTHONPATH=. python3.12 scripts/book_digest.py read --slug <Slug> --apply
# 只读指定章：--only 7,14-15
```

打印每章的亲读计划（`Read pages=<起>-<止>` 的 PDF 页序 + 对应原书页码 + 写回路径）。

**亲读要求（与 read-paper 第 2–3 步同源）**：
- 用 Read 工具**亲自读完该章的每一页**，不要只读草稿；
- 若该章有 `ch??.legacy.md`（旧手工 digest 导入的对照基线），**先独立读、后对照**：
  它没经过引文回验，且是分块 LLM 通读的产物，与原文冲突时以 PDF 为准；
- 逐字英文引句用**双引号包裹**并标 `page` 为**原书页码**（不是 PDF 页序）——
  回验会把它 grep 回原文，对不上即拒收；
- 不要写省略号引文（`"EM ... convergence"`）：那种引句永远无法逐字回验；
- 引句里**不要嵌套双引号**（`"…the model is \"just-identified\""`）：抽取器按成对双引号切片，
  嵌套会把引句劈开，且转义容易把反斜杠写进正文。改用中文引号或改写句式。

**强烈建议：写稿前先批量预验候选引句**。实测这样做能把回验通过率从一次 81% 提到一次 100%，
且能在落盘前抓出自己的转写错误：

```python
from src.scholar.book_ingest import BookManifest, page_index_for
from src.scholar.quote_verify import verify_quote
idx = page_index_for(BookManifest.load(Path("output/scholar_notes/books/<Slug>")))
for pg, q in [("377", "the data supply no evidence for λ: …"), ...]:
    c = verify_quote(q, pg, idx)
    if not c.ok: print("⛔", pg, c.reason, q[:70])
```

⚠️ 预验的必须是**最终要落盘的那个片段**。实测踩过：预验时是一整句、写稿时把它拆成
「引句A」+ 中文 +「引句B」两段，拆出来的新片段没验过，结果三条里两条不匹配。

**写回 bundle**（`books/<Slug>/ch??.chapter.json`）：
- `close_reading_final`：严格 `CloseReading` JSON，句级 `tag` 只用
  `"可引用证据"` / `"可反驳观点"` / `"方法论借鉴"` / null，每句尽量带 `"page"`；
- `cross_check_report`：`{"corrected": [...], "added": [...], "verified_count": N}`，
  数组必须是数组，`verified_count` 显式写 0 会被拒；
- `chapter_meta`（**编著文集必填**）：`{"title": "章标题", "authors": ["章作者"],
  "one_line": "这一章对研究者的用处"}`——章作者是它成为可引单元的前提；
- `status` 改成 `"final"`。

bundle >30KB 时分步落盘（同 read-paper 第 4 步的占位符事故）。

### 5. verify —— 引文回验（**不可跳过**）

```bash
PYTHONPATH=. python3.12 scripts/book_digest.py verify --slug <Slug>
```

把每条逐字引句归一化后 grep 回被引页 ±1。未通过的会打印原因：
- `页码锚错误：标注 p.X，实际在 p.Y` → 改页锚；
- `缺页码锚（原文在 p.N）` → 补上；
- `未找到该引句（可能被改写或杜撰）` → **回原文重抄**，不要微调让它通过；
- `引句过短` → 引全一点，或改写成不带引号的中文归纳。

通过率 <80% 的章进不了库。实测这道检查抓到过真错误：旧 digest 把 Little & Rubin
的正式定义 1.1 引成 `"would be meaningful if observed"`，原文是
`"would be meaningful **for analysis** if observed"`——一条被当作原文定义写进摘要的漏词引文。

### 6. finalize —— 归档

```bash
PYTHONPATH=. python3.12 scripts/book_digest.py finalize --slug <Slug>
```

三道门全过的章 → 札记四件套 + 刷索引。门禁：有 `cross_check_report`、
未自报 `verified_count=0`、引文回验通过率达标。

⚠️ **净删除止损闸**（同 read-paper）：有 bundle 被拒收且本轮会净删掉上一轮已归档的
条目时，**整本一字不动**并退出码 2。修好被点名的 bundle 再重跑；确要删加 `--allow-removals`。

随后确认可检索（**dense 模式**，hybrid 按 RRF 名次排序会给假阴性）：
```bash
PYTHONPATH=. python3.12 scripts/notes_embed.py                       # 增量同步
PYTHONPATH=. python3.12 scripts/notes_search.py "<该章核心论断>" \
  --book <书的 citekey> --mode dense --min-score 0.62 --cite
```
`--cite` 会输出带页码定位符的引用串 `[@little2020rubin, p. 247]`。

### 7. 随时看进度

```bash
PYTHONPATH=. python3.12 scripts/book_digest.py status --slug <Slug>
```
逐章列出分诊分、是否在队列、bundle 状态与门禁结论。

## 旧手工 digest 的导入

```bash
PYTHONPATH=. python3.12 scripts/book_digest.py import-legacy --slug <Slug> \
  --dir <旧 digest 目录> [--apply]
```

它出三份体检：章 ↔ 旧文件映射、**未覆盖的正文页**（补洞目标）、逐字引句回验通过率。

⚠️ **旧 digest 只进草稿轨，不当终稿**。实测 18 份 Rubin 旧 digest（约 230KB）里
逐字英文引句总共只有 7 条，页码引用多是章节/公式指针——几乎没有可回验的东西，
而它们正是分块 LLM 通读的产物。当终稿入库等于把未经核验的归纳变成可引用文献。
`--apply` 只写 `ch??.legacy.md` 与 draft bundle，仍然要走第 4 步亲读。

## 汇报

做完向用户报：读了哪几章（占全书多少页）、**没读哪些章及其分诊依据**、
引文回验通过率与被打回重抄的条数、覆盖缺口（哪些研究问题这本书回答不了）、
归档路径与新增可引用证据条数。
