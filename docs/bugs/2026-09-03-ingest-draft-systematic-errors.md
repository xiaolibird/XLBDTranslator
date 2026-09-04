# 分块通读草稿的四类系统性错误（一批四篇全中）

日期：2026-09-03（写于 2026-09-04）
状态：**部分修复（2026-09-04）**——第 2/3/4 类已修，第 1 类（分数抽成整数）未修，见文末。原状态：未修
严重度：中——草稿本就要 agent 亲读核验，不会直接进库；但其中两类会**在核验时被当成真值抄走**，
且「画像污染」是已修问题的复发
发现者：xiaolibird / Claude（2026-09-03 景观地基精读入库会话，4 篇 / 43 块通读）

---

## 速查

| 项 | 内容 |
|---|---|
| 触发命令 | `PYTHONPATH=. python3.12 scripts/read_pdf.py ingest <PDF...>`（本轮模型 sonnet / claude CLI） |
| 具体位置 | `src/scholar/pdf_ingest.py:445 _read_one`（逐块通读）与 `:567 _pack_chunk_notes`（汇总） |
| 报错情况 | **无报错**，`draft_status=ok`。错误全在草稿正文里：① 分数被抽成整数（`8/3`→`83`）② 跨算例参数串位 ③ Code/Data availability 漏报成「原文未报告」④「对我研究的联想」判定与本人研究无关 |
| 影响 | ①② 是「看起来正常的错数字」，只要核验时偷懒一次就进库；③④ 均为**已修问题复发** |
| 复现测试 | 无。本轮 43 块通读中该四类共 8 处，样本见正文 |

---

产生链路：`src/scholar/pdf_ingest.py` 的 `_read_one`（:445，逐块 LLM 通读）
与 `_pack_chunk_notes`（:567，汇总）。本轮模型为 sonnet（claude CLI）。

---

## 1. 数学分数被抽成整数（静默造假数）

**位置**：抽文 → `_read_one` 之间。原文 `β3 = 8/3`（PDF 里是分数排版），草稿写成 **`β3=83`**。

```
草稿：3D Lorenz系统(β1=10, β2=28, β3=83，噪声强度D=1)
原文：where β1 = 10, β2 = 28 and β3 = 8/3.   （NSR 11:nwae052, p.8, 式 21 下方）
```

危害在于它**看起来像个正常参数**——不像 OCR 乱码，核验时若不逐个回原文就会抄走。
Lorenz 的 8/3 是教科书常数，这次能一眼看出；换成论文自定义的分数就无从判别。

**建议**：抽文阶段对形如 `a/b` 的行内分数做保护（PyMuPDF 抽文时分子分母常被拆到两行、
再被拼接成 `83`）；或在 chunk prompt 里加一条「遇到看起来像整数的量纲异常值，回原文确认是否为分数」。

## 2. 跨算例配置串位（把 A 实验的超参挂到 B 实验上）

**位置**：`_pack_chunk_notes` 汇总阶段（同一块内相邻两段被合并叙述）。

同一篇（EPR-Net）里三处串位，全部把 **3D Lorenz** 的设置挂到了 **12D 高斯混合** 上：

| 草稿写的 | 原文实际归属 |
|---|---|
| 「12D GMM 实验用 σ=5」 | Lorenz 的数据增强噪声 |
| 「12D GMM 实验的训练配置为 Adam、lr 0.001、batch 2048、λ1=10.0、λ2=1.0」 | Lorenz：「We directly train the **3D** potential V(x;θ) by enhanced EPR (16) with λ1 = 10.0 …」 |
| 「该 GMM 此前在 [23] 中考虑过，取 D=20」 | 原文 “This model was also considered in [23] with D = 20” 中的 “This model” 指 **Lorenz** |

这类错误比第 1 类更危险：数字本身是对的，**只是挂错了对象**，逐个数字回查原文也验不出来，
必须连同上下文一起核。

**建议**：chunk prompt 里要求每条方法/参数句显式标注「属于哪个算例/数据集」，
汇总时把无归属的参数句降级或丢弃。

## 3. Code / Data availability 漏报成「原文未报告」

**位置**：`_read_one` 的「实验方法」节。

Lynn 2021（PNAS）草稿写：

```
[None] 原文未报告：代码或数据的公开获取方式。
```

原文 p.6 有完整 Data Availability：

> The data analyzed in this paper and the code used to perform the analyses are publicly available
> at GitHub, github.com/ChrisWLynn/Broken_detailed_balance (54).

这是 SKILL 里已经点名警告过的老坑（声明排版上紧挨参考文献、离方法节很远），
本轮**再次复发**。说明靠 prompt 提醒不足以覆盖。

**建议**：把「Code/Data availability」做成确定性抽取——正文里 grep
`(Code|Data)\s+(availability|and code)` / `github\.com` / `zenodo`，命中就把原句喂给汇总，
而不是指望分块 LLM 在最后一块里注意到。

## 4. 「对我研究的联想」画像污染复发（4 篇中 3 篇）

草稿在三篇里写了同一句式的自我否定：

- Xu 2013：「与我关注的 EHR 缺失机制……在研究问题、方法体系和数据形态上均无直接交集」「不必强行建立关联」
- EPR-Net：「在领域和方法框架上都相距较远，不宜牵强附会」
- Lavenant：「与我关注的 EHR 缺失机制及其跨中心可迁移性问题在主题上关联很弱，不建议强行嫁接」

而这四篇恰恰是《景观类精读清单》A 层「地基」文献，是本轮精读的**全部目的**。
即模型手里的研究画像只有「EHR 缺失机制」一条，凡不匹配的都判为无关。

memory 记录此问题「已靠改 prompt + 换 sonnet 解决」——**本轮是复发**（模型仍是 sonnet）。

**建议**：把研究画像从 prompt 里的静态描述改成**按本批次动态注入**（例如 ingest 时可传
`--context "景观-流形-缺失：本批为 A 层地基文献"`）；或者干脆把这一节的生成关掉，
交给亲读的 agent 写——它本来就掌握本轮读这批的理由，而分块 LLM 不掌握。

---

## 影响与现状

四类错误本轮**全部在交叉核验中被抓出并改正**（详见
`output/scholar_notes/manual/2026-09/*.paper.json` 的 `cross_check_report`），
入库产物是干净的。但：

- 第 1、2 类是「看起来正常的错数字」，依赖核验者逐条回原文，属于**只要有一次偷懒就会进库**的错误；
- 第 3 类已复发一次，第 4 类已复发两次（memory 里记过一次修复）；
- 本批 43 块通读里这四类共 8 处，命中率不算低。

## 附带一条（非本类，顺手记）

Lavenant 那篇 24 块超出汇总预算，触发
`_pack_chunk_notes:604` 的均摊裁剪（`24 块 / 107235 字符 > 66000`，19 块被裁尾部）。
机制本身是设计内的，且 `draft_note` 有如实记录、SKILL 也提示了「被裁部分不在草稿里」。
只是想指出：**76 页的论文会稳定触发**，长文献将常态性地只有前半段草稿可用，
第 2 类串位错误在被裁的块里也无从核对。

---

## 修复（2026-09-04 台账批）

- **第 3 类（Code/Data availability 漏报）→ 确定性抽取**：`pdf_ingest.extract_availability_statements(full_text)` 纯正则抓
  `(code|data|…) availability` / `publicly available at` / `github|gitlab|zenodo|osf|figshare|huggingface` 等原句片段（句边界截断、去重、≤6 条），
  `ingest_pdf` 喂进 `synthesize_deep_read(..., availability=…)`，`_SYNTH_PROMPT` 新增槽位并要求「实验方法」节以它为准、不得写「原文未报告」。
  抽不到时槽位写「程序未抓到明确的可得性声明」（不是空串）。测试：`test_pdf_ingest.py::test_extract_availability_statements_*` / `test_synthesize_feeds_availability_statements_into_prompt`。
- **第 4 类（画像污染复发）→ 按批动态注入**：`read_pdf.py ingest --context "本批阅读目的"`，拼在研究画像后（`【本批阅读目的】…`），
  `_SYNTH_PROMPT` 规则 4 补一句「若写明了本批阅读目的，按那个目的写联想」。只影响「对我研究的联想」一节。
- **第 2 类（跨算例串位）→ prompt 层**：`_CHUNK_PROMPT` 要求每条参数/数字注明所属算例，归属不明写「归属未明」；`_SYNTH_PROMPT` 要求不得把 A 算例设置挂到 B。
  这是 prompt 改动，无法离线验证效果，靠后续批次的 `cross_check_report` 观察复发率。
- **第 1 类（`8/3`→`83`）→ 未修**：损坏发生在**抽文层**（PyMuPDF 把分子/分母当成两行普通文本，
  分数线是 drawing 不是字符），要修得读 PDF 的 drawing 层去认那条横线——成本不小且要真 PDF 才能验，
  本轮不做。第 4 轮审计指出「启发式风险高」这个理由说得不准：几何上其实是确定的，
  风险在工程量而非误伤。仍靠亲读核验逐个回原文；下次做时按「读 drawing 层认分数线」这条路走，
  别用正则猜。
- 附带那条（24 块超汇总预算的均摊裁剪）是设计内行为，与 `2026-09-04-closeread-still-truncates.md` 一起推后。
