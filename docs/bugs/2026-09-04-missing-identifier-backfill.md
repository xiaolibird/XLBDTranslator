# 存量条目缺 DOI 从未回补：36 篇连身份标识都没有，取全文与去重同时失效

日期：2026-09-04
状态：**未修复，推后（2026-09-04 台账批不做）**——第一步是联网只读盘点，属数据操作而非代码缺陷；回填须按「改身份键要扫派生物」流程走。原状态同。
严重度：中——这批篇目在库里等同「无法定位」，任何按标识找全文的通道都开不了门
发现者：xiaolibird / Claude（2026-09-03 索引层升格会话）

---

## 位置

- `src/scholar/crossref.py:111` `crossref_lookup()`——按标题查 Crossref 补规范元数据，
  **只在入库路径上跑**，存量条目没有回填入口
- `src/scholar/fulltext.py:85` `resolve_oa_pdf()`——`meta.arxiv_id` 与 `meta.doi` 皆空时直接返回
  closed，一个请求都不发
- 受影响条目的典型形态：`url` 是一条 Google Scholar 快讯跳转链接，`doi` 为空，
  `citekey` 形如 `anon2021Robust` / `anon2021Data`

## 报错情况

不报错，静默零产出：

```
[1/148] anon2021Robust … ROBUST COUNTERFACTUAL LEARNING FOR CLINICAL DECI
   ❌ 四条路都没走通，进人工清单
```

四条通道里有三条（EPMC / Semantic Scholar / OpenAlex）都以 DOI 为入参，无 DOI 时
连试都试不了；arXiv 标题检索是唯一还能动的，命中率有限。

## 证据

2026-09-03 的 125 篇待下载清单里，**36 篇（29%）连 DOI 都没有**。
补抓脚本已经加了「无 DOI 先按标题问一次 Crossref」这一步
（`scripts/fetch_missing_pdfs.py:backfill_doi()`），但这批命中率很低——
多为早期 Scholar 快讯条目，标题本身就不规范。

## 影响面

不止取全文。`dedup_key` 的身份判定依赖 DOI（见 `docs` 里「札记库身份键」一节），
无 DOI 条目走的是退化键，跨月去重、`all_references` 书目、向量库去重都受影响。

## 建议修法

分两步，**顺序不能反**：

1. 先做一个只读的**盘点**：对全库无 DOI 的 keeper 跑 Crossref/OpenAlex 标题检索，
   产出「能补上 / 补不上」两份名单，不写盘。先看命中率再决定值不值得。
2. 命中的再考虑回填，且**必须当成改身份键处理**——DOI 进 `dedup_key` 会连带影响
   citekey、向量库 chunk id、`all_references` 书目，得按既有的「改身份键要扫派生物」
   流程走，不能由一个补元数据脚本顺手做掉。

补抓脚本里那句 `backfill_doi()` 的注释已经写死了这条边界：查到的 DOI **只在内存里用于取全文，
绝不写回索引**。这个约束在真正做回填之前不要放开。

## 备注

这 36 篇里估计有相当比例本来就不值得补（预印本重复、poster、无法定位的条目）。
盘点那一步应当同时给出「建议放弃」的判据，别默认全都要救。
