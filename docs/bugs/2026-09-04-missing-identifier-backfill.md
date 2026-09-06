# 存量条目缺 DOI 从未回补：36 篇连身份标识都没有，取全文与去重同时失效

日期：2026-09-04
状态：**第一步（只读盘点）脚本已落地并已开跑，但未跑完（2026-09-05）**：OpenAlex 改成按日额度计费（免费 ≈100 请求/日、UTC 零点重置），317 篇里 301 条还缺 OpenAlex 那一源，须每天 `--resume-from` 续跑约 3 天；当前报告 `_inventory/missing_ids_20260905T160744.json`。回填仍待人按跑完后的盘点结果决定，须按「改身份键要扫派生物」流程走。原状态：未修复，推后。
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

---

## 第一步落地（2026-09-05）

`scripts/inventory_missing_ids.py`：对无 DOI/arXiv/PMID 的 keeper（现库 317 篇：`title:` 263 /
`pmlr:` 44 / `openreview:` 10；INCLUDE 186 / MAYBE 131；另 `isbn:` 5 篇被 `select_targets` 排除、不查）
拿标题查 Crossref 与 OpenAlex
（后者按记忆里的教训用 `filter=title.search`），三分类 fillable / ambiguous / no_hit，再叠一层
「建议放弃」（PMLR/OpenReview 键本就有出版方稳定标识；MAYBE 裁决不值得动身份键）。
只写 `output/scholar_notes/_inventory/missing_ids_<时间戳>.{json,md}`，**不改索引**。
第二步回填仍须走「改身份键要扫派生物」流程。测试：`test/test_backfill_scripts_2026_09_05.py`。

## 生产实跑（2026-09-05）与 OpenAlex 计费变更

首次全量盘点跑完 317 篇，但**结论只有前 ~105 篇是两源的**：第 105 篇左右
`api.openalex.org` 连续 3 次终局失败被熔断，尾部 208 篇只问到了 Crossref。
探查真因不是限流，是 **OpenAlex 已改成按日额度计费**：

```
429 {"error":"Rate limit exceeded",
     "message":"Insufficient budget. This request costs $0.001 but you only have $0 remaining.
                Resets at midnight UTC. Need more? Add funds at https://openalex.org/pricing"}
```

每请求 $0.001、免费额度约 $0.1/日（≈100 请求）、UTC 零点重置，带不带 mailto 一样。
**放慢速度没有任何用**，全库一天跑不完是常态。

因此脚本补了一处与本批 R3-A1 同源的记账修正 + 续跑能力：

- 两个 `query_*` 改成返回 `(命中, 源是否答上)`；记录里存 `sources_ok` / `sources_failed`。
- `classify` 第四类 **`incomplete`**：没有像样命中**且有源没答上**时判它，不再判 `no_hit`。
  「问过了确实没有」和「压根没问成」不是一回事——后者记成结论就是把暂时性失败沉淀成永久判死，
  和额度耗尽被记成 `no_output` 是同一个病。
- `--resume-from <上次报告.json>`：只补问**还没答上的那一源**，其余原样结转、不再花额度。
  旧报告没有 `sources_ok` 时退化成「出过命中的源即算答过」——只会多问一次，不会误判成结论。

补这层记账时发现修法自己漏了一半：`crossref_lookup` 同样把 HTTP 失败与「查了没有」一律吞成
`None`（`crossref.py:137`），而包装函数却声称 Crossref 答上了。已改成在本脚本里自己发请求、
自己看状态码（命中解析仍复用 `best_match`，判据只留一份），顺带让 Crossref 也走 `polite_get`
的退避与按宿主熔断——此前它是裸 `c.get`。

**当前报告**（`_inventory/missing_ids_20260905T160744.{json,md}`，Crossref 单源）：
`fillable 21 / ambiguous 20 / no_hit 0 / incomplete 276`，70 篇带「建议放弃」；
301 篇 `sources_ok=[crossref] / sources_failed=[openalex]`，16 篇两源都没问
（标题实词 <4，故意不问，**不进重查集**，否则每次续跑都消不掉）。
`no_hit` 归零是这次修正的直接体现：OpenAlex 缺席时，"两源都说没有"一条都不成立。

续跑：`--resume-from output/scholar_notes/_inventory/missing_ids_20260905T160744.json`
（已验证：待重查 301 条、原样结转 16 条），按每日 ~100 次免费额度约需 3 个自然日。
第二步回填仍须走「改身份键要扫派生物」流程，与本次变更无关。

## 加两级直链源（2026-09-07）：本机 Zotero translation-server

用户问「为啥不用 docker 里的 zotero server」——**是对的，我漏了这条路**。

原方案只有「拿标题去 Crossref/OpenAlex 模糊检索」一条路，卡在 OpenAlex 的按日额度上要跑 3 天。
但这批条目**自己的 url 就指向那篇论文**，根本不必拿标题去猜：

| 路径 | 条数 | 性质 |
|---|---|---|
| ① url 里直接含标识（PMID / PMC / DOI / arXiv） | 41 | 零网络、零额度、精确 |
| ② 出版商落地页 → translation-server `/web` | 42 | 本地 docker、无外部额度、精确 |
| ③ 聚合站 / Scholar 跳转 / 无 url | 176 | 只能靠标题模糊检索 |

**质量比省时间更重要**：标题检索是「拿标题猜哪篇」，相似度承担**识别**职责；
直链是「顺着本条目自己的 url 走到出版商页」，相似度只承担**确认**职责。
这批标识要进 `dedup_key`，直链来源比模糊匹配可信得多。

### 阈值是量出来的，不是拍的

直链结果的相似度分布与标题检索**不是一回事**。实测 31 条真候选：29 条拿到 DOI，
相似度 min 0.75 / p25 0.80 / 中位 1.00，**0.70 以下一条都没有**。低分那几条逐条人工核过，
全是同一篇——差异来自排版（`Multi-Source`/`Multisource`、`non-iid`/`non-i.i.d.`、
标题带 `(January 202…` 后缀）。若沿用标题检索那档 `SIM_FILLABLE=0.92`，会把 9 条正确的直链命中
全判成 ambiguous。故新增 `SIM_DIRECT_LINK = 0.70`，只对直链来源生效；
年份与首作者两道闸照旧（`martin2019Predictingr` 库内年份 2019 而 DOI 是 2025 刊，
正是靠年份闸判进 ambiguous 交人看）。

另：url 里抽到的 PMID/PMC 一旦被 TS 用标题确认，**它本身就是可用身份**（`dedup_key` 有 `pmid:` 档）。
此前只认 DOI，会把这类已确认身份的条目报成 `no_hit`——「确认了却说没找到」。已一并修。

### 实跑结果（2026-09-07）

    fillable    21 → 67      其中 zotero_ts 贡献 39 条、crossref 20、openalex 8
    ambiguous   20 → 30
    no_hit       0 → 15
    incomplete 276 → 205     OpenAlex 当日额度再次耗尽

那 39 条是今天**靠 OpenAlex 根本拿不到**的（额度已尽），零成本拿到且来源更可信。
抽查相似度最低的 5 条，全部确认同一篇，零误判。

**顺带修掉一个记账 bug**：`sources_ok` 没结转上一轮已答上的源，导致下次续跑会把 crossref
对全部 301 条重问一遍——正好破坏 `--resume-from` 的全部意义。修后待重查 301 → **262**。

**剩余 262 条**仍要靠 OpenAlex 日额度续跑（约 2~3 天），或充值 $0.30 一次跑完。
