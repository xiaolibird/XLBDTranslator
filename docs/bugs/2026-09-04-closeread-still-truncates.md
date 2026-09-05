# 长论文仍被截尾：137,400 字符上限对 80 页以上的论文不够

日期：2026-09-04
状态：**已修复（2026-09-05，两轮读）**，见文末。原状态：未修复，推后（2026-09-04 台账批不做）——需「正文/附录两轮、各自独立预算」的设计改动，不是抬一个数；且不能单独抬 `closeread_max_chars`。原状态同。
严重度：低——只影响少数超长论文的尾部覆盖，不影响正确性
发现者：xiaolibird / Claude（2026-09-03 补深度重读会话）

---

## 位置

- `src/scholar/settings.py:218` `closeread_max_chars`，当前默认 `137400`
- 与 `src/scholar/pdf_ingest.py` 的 `_SYNTH_NOTES_BUDGET` **零和耦合**，
  该处注释写明：块数一涨每块配额就摊薄（11 块时每块 5,432 字符，实测块笔记约 5.8k，已在裁剪），
  「只抬本值不抬预算 = 拿已读章节的保真度去换尾部覆盖」
- 相关测试：`test/test_closeread_chunk_coupling.py`

## 报错情况

不报错。产出行末尾会带 `截断` 标记：

```
✅ 带tag 12 → 33 条（总句 67）| 全文=True 源=local-pdf 块=11 截断
```

## 证据

`cai2026Contrastive`（arXiv 2403.14926，**86 页**）：切出 11 块，仍标记截断。
该篇是 JASA 长文，正文加附录远超上限。

## 影响面

小。库里 80 页以上的论文是少数（多为统计期刊长文与带长附录的会议论文）。
但对这些篇目而言，被截掉的往往正是附录里的实验细节与补充结果——
恰好是「实验方法」「结果与效应量」两节最需要的材料。

## 相关

`2026-09-04-truncated-flag-conflates-two-causes.md`（另一会话同日提出）讲的是同一个
`fulltext_truncated` 字段**混用了两种成因**：`fulltext_chars` 恰等于上限的是总上限截尾，
小于上限的是单页上限削页。那份给出的判据可以直接用来筛出本条真正影响的篇目——
只有「总上限截尾」那一类才是这里说的问题，削页那类抬上限也没用。

## 建议修法

**不要单独抬 `closeread_max_chars`**，那是拿已读部分的保真度换尾部覆盖。
两个数得一起动，改完跑 `test_closeread_chunk_coupling.py`。

或者换思路：对超长论文改用**两轮**——第一轮正文、第二轮附录，各自独立预算，
而不是把整本塞进一个固定汇总预算里摊薄。

---

## 修复（2026-09-05）

没抬 `closeread_max_chars`——那与汇总预算是零和耦合（抬上限 = 拿已读章节的保真度换尾部覆盖，
`test_closeread_chunk_coupling.py` 钉着）。改成**两轮读**：

- `closereading._pdf_text_ex` 抽文时把总上限之外的余量原样带回（`PdfText.tail`）；
- `read_tail_appendix`：尾段先用 `strip_reference_block` 剔掉参考文献区（按引文标记密度识别，
  不认标题——10 份真 PDF 里标题独占一行的只有 4 份），再按**独立预算**
  `closeread_tail_max_chars=46200 / closeread_tail_max_chunks=4`（恰好 4 块，耦合同款测试钉住）
  切块通读、独立汇总，产出的分节以「附录·」前缀并入 `sections`。`notes_index._CR_SECTION_RE`
  照常解析，highlights 的 section 溯源能区分两轮，vault 单篇页一眼看出哪些句子来自尾段；
- 数字回查的对照源把尾段一并算进去（否则附录里的数值会被判成幻觉）；
- `CloseReading.tail_chars` / 索引 `fulltext_tail_chars` 记第二轮真正喂进去的字符数；None = 没跑
  （没截断 / 开关关 / 尾段去掉引文后不足 2000 字符）。`body_chars` 仍只算第一轮。
- 三条链路都接了：周度 ingest / 月度 backfill_notes / backfill_deepread（含本地 PDF 路径）。
  `closeread_tail_max_chars=0` = 关掉第二轮，行为与旧版逐字节一致。

测试：`test/test_truncation_two_pass.py`（④ 节）。存量 14 篇总上限截尾的 keeper 可按
`fulltext_truncated_by_total_cap == true` 挑出来重跑（见 truncated-flag 那条）。
