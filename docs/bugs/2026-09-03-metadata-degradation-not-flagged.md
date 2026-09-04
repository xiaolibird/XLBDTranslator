# 元数据退化到 pdf-llm、DOI 全空，但 ingest 回执的「⚠️ 需要注意」块不报

日期：2026-09-03（写于 2026-09-04）
状态：**已修复（2026-09-04）**，见文末。原状态：未修
严重度：中——书目缺 DOI/卷期会污染 `all_references.json` 与下游 pandoc 出稿；且**回执是绿的**，不看日志中段就发现不了
发现者：xiaolibird / Claude（2026-09-03 景观地基精读入库会话）

---

## 速查

| 项 | 内容 |
|---|---|
| 触发命令 | `PYTHONPATH=. python3.12 scripts/read_pdf.py ingest <arXiv 来源的 PDF>`（网络故障时） |
| 具体位置 | `scripts/read_pdf.py:186-188` `_print_attention` 的 `thin` 判据；退化点在 `src/scholar/pdf_ingest.py:245` `resolve_metadata` |
| 报错情况 | 日志中段有 `⚠️ arXiv 精确查失败 … [SSL: UNEXPECTED_EOF_WHILE_READING]`，但**末尾回执不打「⚠️ 需要注意」块**；bundle 落盘 `doi/journal/volume/issue/pages` 全为 None，作者却齐全 |
| 影响 | 书目缺 DOI 与卷期页 → `all_references.json` 该条渲染残缺、`dedup_key` 退化成非 DOI 键、跨系统对账失效 |
| 复现测试 | 无。手工复现见文末 |

---

## 现象

批量 ingest 四篇，其中 `lavenant2024_trajectory_inference_theory.pdf` 的 arXiv 精确查询
遇 SSL 中断，退化为 `pdf-llm` 抽取，DOI / 期刊 / 卷 / 期 / 页**全空**：

```
2026-09-03 11:12:14 | WARNING | pdf_ingest:resolve_metadata:245 -
  ⚠️ arXiv 精确查失败（2102.09204v2）: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1010)
2026-09-03 11:12:54 | INFO | pdf_ingest:ingest_pdf:860 -
  元数据来源: pdf-llm | Towards a Mathematical Theory of Trajectory Infere | DOI=None
```

而跑完后的汇总回执里，**没有出现「⚠️ 需要注意」块**（四篇全被判为无需注意）。
那条 SSL WARNING 落在 24 块通读日志的中段，批量场景下必被淹没——正是
`_print_attention` 的 docstring 自己说要解决的那个问题。

bundle 落盘后的实际字段：

```
journal=None volume=None issue=None pages=None doi=None
arxiv_id='2102.09204v2'  authors=['Hugo Lavenant','Stephen Zhang','Young-Heon Kim','Geoffrey Schiebinger']
```

## 根因（具体位置）

`scripts/read_pdf.py:186-188`，`_print_attention` 的 thin 判据：

```python
thin = [r for r in outs if not r.get("skipped")
        and (r.get("meta_source") == "pdf-only" or not r.get("authors_n"))]
```

两个条件本篇都不满足，故不报：

| 条件 | 本篇实际 | 是否触发 |
|---|---|---|
| `meta_source == "pdf-only"` | `"pdf-llm"` | ❌ |
| `not authors_n` | 4（LLM 把四位作者全抽对了） | ❌ |

**判据错位**：它检查的是「抽取通道」与「作者数」，而真正决定书目可用性的是
**有没有拿到 DOI / 卷期页**。一条「作者齐全但 DOI 为空」的 `pdf-llm` 记录因此完全静默通过。

`doi` 字段本来就在回执 dict 里（`src/scholar/pdf_ingest.py:875-876` 与
成功分支同构），改判据不需要新增数据。

## 影响

- `all_references.json` 会多一条无 DOI、无卷期页的书目；用 `scholar-write` 走 pandoc 出稿时
  这一条渲染出来是残的。
- `dedup_key` 会退化成非 DOI 键，跨系统对账（本库以 DOI 为论文身份）失效。
- 本轮是靠人工发现的：我在写精读时注意到 bundle 里 journal 为空，才回查 Crossref
  手工补成 `10.1214/23-aap1969` / Ann Appl Probab 34(1A) (2024)。若不手补，这篇会以
  「无 DOI 的 arXiv 预印本」身份进库，而它 2024 年就已正式发表。

## 建议修法

1. **扩 thin 判据**（一行改动，最小修复）：

   ```python
   thin = [r for r in outs if not r.get("skipped")
           and (r.get("meta_source") in ("pdf-only", "pdf-llm")
                or not r.get("authors_n")
                or not (r.get("doi") or r.get("arxiv_id")))]
   ```

   注意别把「合法地没有 DOI」的条目（部分预印本、书籍章节）也报成问题——所以用
   `doi or arxiv_id` 兜底，并在提示语里区分「查询失败导致的空」与「本来就没有」。

2. **把查询失败本身记进回执**：`resolve_metadata` 里那条 SSL WARNING 目前只进日志。
   建议在返回值里带一个 `meta_degraded=True/原因`，由 `_print_attention` 统一报，
   并给出可直接照抄的补救命令（`ingest --title "精确标题"` 或手工 Crossref 补正）。

3. **可选：网络查询加重试**。SSL `UNEXPECTED_EOF` 属瞬时故障，一次退避重试大概率就能拿到，
   比事后补正便宜。但重试不能替代第 1、2 条——退化仍需被看见。

## 复现

```bash
# 断网或阻断 export.arxiv.org 后对一篇纯 arXiv PDF 跑 ingest
PYTHONPATH=. python3.12 scripts/read_pdf.py ingest <某篇 arXiv PDF>
# 观察：日志中段有「⚠️ arXiv 精确查失败」，末尾回执无「⚠️ 需要注意」块
# 检查 bundle：segment.metadata.doi is None 而 authors 齐全
```

## 相关

- `docs/skills/read-paper/SKILL.md` 第 1 步已提示「⚠️ 需要注意块必看」，但对这一类
  （作者齐、DOI 空）它根本不打印，提示本身失效。

---

## 修复（2026-09-04 台账批）

按建议修法 1+2 落地（3 的重试未做）：

- `pdf_ingest.resolve_metadata(..., diag=None)` 新增出参：权威链每一级为什么没走通（DOI 直查未命中 / arXiv 精确查失败或无结果 / LLM 抽取失败）
  都追加到 `diag["degraded"]`；`ingest_pdf` 回执带 `meta_degraded`。
- `read_pdf._is_thin_metadata`：判据从「抽取通道 + 作者数」改为**书目可用性**——来源 `pdf-llm`/`pdf-only`、或零作者、或 DOI 与 arXiv 皆空，任一命中即报。
  本条现场（pdf-llm、4 位作者、DOI 空、arXiv 有）现在必进「⚠️ 需要注意」块，并打印「原因：arXiv 精确查失败（…）: [SSL: …]」行与补救命令，
  区分「查询失败导致的空」与「本来就没有」。
- 测试：`test_read_pdf_cli.py::test_thin_metadata_predicate`（4 → 8 例，含本条现场）/ `test_thin_metadata_prints_degradation_reason_and_identifiers`；
  `test_pdf_ingest.py::test_resolve_metadata_records_arxiv_failure_in_diag` 等。
- SKILL.md 第 1 步「需要注意块」说明同步更新。
