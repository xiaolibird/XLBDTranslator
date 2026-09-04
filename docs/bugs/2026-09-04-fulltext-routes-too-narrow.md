# 取全文只走两条路：月度流水线系统性漏掉本来拿得到的全文

日期：2026-09-04
状态：**代码已下沉进主链路，但开关默认关（2026-09-04）**——因此**月度链路的漏判率一点没变**，
这条缺陷的本体（每月都在少读）在打开开关之前不算解决。见文末「修复」与「怎么打开」。原状态：未修复
严重度：高——不报错、不丢数据，但每月都在少读；受影响的篇目会被当成「没全文」永久停在索引层
发现者：xiaolibird / Claude（2026-09-03 索引层升格会话）

---

## 位置

- `src/scholar/fulltext.py:85` `resolve_oa_pdf()`——只试两条：arXiv 直链（要 `meta.arxiv_id`）、
  Unpaywall（要 DOI + email）
- `src/scholar/fulltext.py:232` `europepmc_fulltext()`——只取 **JATS 全文 XML**
- 消费方：`src/scholar/closereading.py:close_read_segments()` / `close_read()`，
  两条都断时打 `⚠️ 无全文也无摘要，跳过精读`（`closereading.py:229`）后零产出

## 报错情况

不抛异常。表现为日志里成片的：

```
⚠️ Europe PMC 全文返回 404（PMC8661408）
⚠️ 无全文也无摘要，跳过精读(2): A high-throughput phenotyping algorithm is portabl
  全文精读 0/1 篇（其中真全文 0，top-1）
```

调用方只看到「这篇没全文」，无从区分「真没有」与「没去找」。

## 证据

2026-09-03 把 172 篇 INCLUDE+high 索引层篇目升格，主链路判死 148 篇。对这 148 篇另跑四条
公开通道（`scripts/fetch_missing_pdfs.py`），**捞回 20 篇**：

| 通道 | 捞回 | 主链路是否试过 |
|---|---|---|
| Europe PMC `?pdf=render` | 7 | ❌ 没试过 |
| arXiv（按标题检索副本） | 7 | ❌ 只认现成的 `arxiv_id` |
| OpenAlex `best_oa_location` | 5 | ❌ 没试过 |
| Semantic Scholar `openAccessPdf` | 1 | ❌ 没试过 |

即主链路的漏判率约 **13.5%**（20/148）。Unpaywall 一篇都没多给——它能拿的早拿了，
多出来的全在它覆盖不到的地方。

### 子问题 A：EPMC 的全文 XML 404 ≠ 没有 PDF

`europepmc_fulltext()` 走 `?format=xml` 的全文接口，而 `europepmc.org/articles/<PMCID>?pdf=render`
是**另一套**渲染版 PDF。实测 `geva2021Highthroughput`（PMC8661408）：XML 返回 404，
render PDF 返回 200 / 274KB / 5 页。当前代码把前者的 404 当成「这篇没全文」。

### 子问题 B：NCBI PMC 对脚本 403，EPMC 镜像同一份却不挡

OpenAlex 给出的 `pmc.ncbi.nlm.nih.gov/articles/PMC…` 链接下载一律 403。同一个 PMCID 换成
`europepmc.org/articles/PMC…?pdf=render` 就能下。实测有 3 篇因此白丢
（`fetch_missing_pdfs.rewrite_url()` 里已有这条改写）。

## 影响面

不止这次的存量批。**月度 digest 每次跑都按同样口径挑「能拿到全文」的篇目**
（`close_read_segments` 的 `prefer_full_text` 分支用 `resolve_oa_pdf` 预解析候选），
所以每个月都在按这个比例：

1. 把本来能全文精读的篇目降级成摘要或跳过；
2. 更隐蔽的一层——`has_ft` 名单短了，**择优挑 top-N 时会挑错人**，
   把名额让给真正拿不到全文的篇目。

## 建议修法

把 `scripts/fetch_missing_pdfs.py` 里已验证的四条路下沉进 `resolve_oa_pdf`，顺序按实测命中率：
arXiv（含标题检索）→ EPMC render → OpenAlex → S2。注意三点：

- **URL 改写要一并带上**（NCBI PMC → EPMC），否则 OpenAlex 那一路的收益打对折；
- **校验不能省**：反爬页也回 200，必须按 `%PDF` magic + 体积 + 可解析页数三道闸卡，
  否则坏文件会流到 `_pdf_text_with_stats` 炸成空文本，然后被误诊成「精读质量差」；
- 加路 = 每篇多打 2~3 个 API。`close_read_segments` 的候选集是 `candidate_factor*top_n`
  篇，月度跑会放大成几十次请求，**要节流**（补抓脚本用的是篇间 1.5s + 403/429 指数退避三轮）。

## 不该怎么修

- ❌ **不要去绕出版商反爬**。sciencedirect / mdpi / academic.oup / dl.acm / thelancet
  这些对脚本回 403 的，实测换 UA、加 Referer、走机构 IP（复旦 CERNET 202.120.79.143）
  都不解决——那是 Cloudflare 认「你不像浏览器」，不是订阅问题。这类篇目应进人工清单
  （`output/scholar_notes/需下载全文清单.md` 的 A 档），不该在代码里想办法。

---

## 修复（2026-09-04 台账批）

四路从 `scripts/fetch_missing_pdfs.py` 下沉到 `src/scholar/fulltext.py`：`route_arxiv_title`（词面重合 ≥0.75）→ `route_epmc_render` → `route_openalex`
（含 NCBI PMC → EPMC 换宿主 `rewrite_pmc_url`）→ `route_s2`，由 `extra_route_candidates` 串起来（路间 `route_delay` 节流、单路异常跳过）。
`resolve_oa_pdf(..., extra_routes=False, route_delay=0.0)`：**默认关时调用形状与结果逐字节不变**；开着时只有 arXiv 直链 / Unpaywall 都没给 pdf_url 才试四路，
命中则 `pdf_url` 取第一个、全部候选进新字段 `OAResult.candidates`。下载侧 `closereading.download_pdf(..., validate=True)` 加三闸
（`%PDF` magic 出现在前 1024 字节内 / 体积 ≥1KB / pypdf 可解析页数 ≥2；对抗审计后从 20KB 放宽——2 页纯文本短文只有 ~1.2KB，20KB 会误杀）+ PDF Accept 头 + 403/429 退避（`polite_get`），`close_read_segment` 对候选逐个试到一个过校验为止。

**开关**：`settings.processing.fulltext_extra_routes`（默认 False）、`fulltext_route_delay`（1.5s），五处 `close_read_segments` 调用方透传
（workflow / cli / ingest / backfill_notes / backfill_deepread）。关着时月度链路零变化；补深度/expand 批可在 `config/scholar.env` 里
`PROCESSING__FULLTEXT_EXTRA_ROUTES=true` 打开。真网络下的限流表现未验证——这是默认关的原因；`fetch_missing_pdfs.py` 保留为独立补抓入口。
「不该怎么修」一节的边界原样保留：不做任何绕出版商反爬的改写。
测试：`test/test_fulltext_extra_routes.py`（全部假 client，不发网络）。`scripts/fetch_missing_pdfs.py` 已改为**委托**这套实现，不再自持副本——第 2 轮审计实测那份副本没跟上「arXiv 标题检索改双向重合」的修复。
