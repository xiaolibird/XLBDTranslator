# `find_local_pdf` 用单向词面覆盖认 PDF：一篇长综述会被当成本篇，把别人的全文喂进精读

日期：2026-09-04
状态：**已修复（2026-09-04 台账批）**
严重度：中高——不报错、不丢数据，但**把 A 论文的全文当成 B 论文的精读进库**，是最难事后发现的一类污染
发现者：2026-09-04 台账批第 3 轮对抗审计（验伪镜片用**真实 PDF 目录**复现）

---

## 速查

| 项 | 内容 |
|---|---|
| 触发命令 | `scripts/backfill_deepread.py run --pdf-dir <手工下载目录> --apply` |
| 具体位置 | `scripts/backfill_deepread.py::find_local_pdf` 的标题打分 |
| 报错情况 | **无**。日志只打一行「按首页标题认出 X → citekey（重合 100%）」 |
| 影响 | 那篇的精读内容整段来自另一篇论文；`reading_source=local-pdf`、`has_full_text_reading=True`，事后无从分辨 |
| 复现测试 | `test/test_backfill_deepread.py::test_find_local_pdf_refuses_a_long_review_that_contains_the_title` |

## 根因

```python
score = len(want & head_tokens) / len(want)      # 只算单向覆盖
if best_score >= 0.6: ...
```

`want` 是目标标题的实词。一篇**综述**的首页往往把本领域的常见词全写进去了——目标标题越短，
`want` 越小，越容易被整个包含，`score` 直接冲到 1.0。阈值 0.6 拦不住「被包含」。

这是同一个缺陷的**第三份副本**：`src/scholar/fulltext.route_arxiv_title`（本批第 1 轮已修）、
`scripts/fetch_missing_pdfs.route_arxiv`（本批第 2 轮改成委托 fulltext），再加这一处。

## 修复

加**反向覆盖**门槛（命中 PDF 首页的实词也要有一定比例落在目标标题里）与实词下限 4，
与 `fulltext.route_arxiv_title` 同一套判据。首页含作者/机构/摘要，故反向阈值比 fulltext 宽。

## 教训

**凡两处同义的判据，只留一份。** 本批为此把 `fetch_missing_pdfs.py` 的四条通道、三闸校验、
换宿主全部改成委托 `src/scholar/fulltext`。这一处因为入参形态不同（本地文件而非 URL）
没法直接委托，只能同步判据——那就更要在两边都写明「改一处要同步另一处」。
