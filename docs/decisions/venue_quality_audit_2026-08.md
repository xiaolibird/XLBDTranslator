# 发表源质量审计与白/黑名单（2026-08-24）

起因：用户问「除了 arXiv，库里是不是还混进了很多野鸡期刊/会议」，提议「公认野鸡或
断层之下的全部删出库」，后改为「直接写一个白名单和黑名单」「灰态直接联网搜索」。

## 结论

**库里的野鸡问题极小：黑名单命中 8 篇（0.4%），合计仅 6 条证据句，占全库 4234 条
可取证句的 0.14%。** 联网核实过的灰态刊里没有发现新的野鸡。

最终三态：

| 态 | 篇数 | 占比 |
|---|---|---|
| ✅ white（领域公认可信） | 778 | 34.1% |
| 🔴 black（公认掠夺性） | **8** | **0.4%** |
| ⚪ gray（未列入名单） | 1495 | 65.5% |

gray 的构成：预印本 483 + 无 DOI 无刊名的杂项 222 + 正经但未列名单的期刊/会议/丛书
790（涉及 525 个 venue，长尾）。**gray 是常态不是问题**，绝不能当 black 处置。

## 一、为什么用人工名单，不用算法判据

四种自动方案在动手前逐条被证伪：

| 方案 | 证伪 |
|---|---|
| 刊名正则 | 命中的 28 个「可疑刊名」绝大多数是正经甚至顶级刊——`International Journal of Computer Vision` 是 CV 顶刊 IJCV、`American Journal of Kidney Diseases` 是肾病顶刊 |
| h-index | 被发文量稀释。IJCA（公认水刊）h=113，因为它发了 28094 篇；IJCV h=291 但只发 3994 篇 |
| 刊级 IF | OpenAlex 不给会议 proceedings 算 `2yr_mean_citedness`，KDD / WWW 一律返 **0.0** —— 按 IF 断层删，**第一批被杀的是顶会** |
| 单篇被引 | 测的是「这篇论文影响力」不是「这个 venue 是不是野鸡」。实测把 3 篇真 AAAI/NeurIPS 论文判成可疑：`eiben2021Parameterized`（理论 CS，5 年 2 引）、`gupta2022Flexiblewindow`、`luo2023Dfrd` |

**所以「断层」不存在于数值维度，只存在于类型维度**（期刊/会议/预印本指标不可比）。
而本领域的 venue 就那么几十个，人工定一次比任何算法都准。

## 二、名单设计（`config/venue_lists.yaml`）

三态而非二态：**white / black / gray**，gray 既不加分也不减分。

匹配优先级 **issn > doi_prefix > name_regex**：

- **ISSN 最准**，不受刊名变体/大小写/缩写影响。⚠️ Crossref 的 ISSN 是**列表**（期刊
  常有 print + online 两个，JAMIA 是 `['1067-5027','1527-974X']`），必须**任一命中即算
  命中**——只取第一个会让白名单按另一个写的条目全部漏配，实测 JAMIA 37 篇里 24 篇因此
  掉进 gray。已加回归测试。
- **DOI 前缀**用于出版商级黑名单。⚠️ **不能按 Crossref 的 `publisher` 字段**：Hindawi
  被 Wiley 收购后 publisher 显示为 "Wiley"，按它判会漏掉全部 Hindawi 刊。
- **刊名正则**给会议用（会议大多无 ISSN）。

黑名单只收学界共识明确的（Hindawi / IJCA / SCIRP / OMICS / SPG / Zenodo 自助上传），
**MDPI(10.3390, 60 篇) 与 Frontiers(10.3389, 40 篇) 刻意不列**——它们是**争议**不是掠夺，
Frontiers in Medicine IF 2.46，一刀切会误伤 100 篇。

白名单初版 34 条 + 审计扩充 43 条 = 77 个 ISSN + 18 条会议正则，覆盖 778 篇。
扩充依据是「库内 ≥3 篇且判定确定可信」，拿不准的一律留 gray。

## 三、灰态处理

用户提议「灰态直接联网搜索可信度」。实际执行时先做了量级判断：

- 灰态 525 个 venue，其中 **484 个只出现 1 次**，全搜不合理
- ≥3 篇的原有 73 个，其中一大半是已知结论（Scientific Reports / PLOS ONE / IEEE
  Access / Frontiers / MDPI 系是「争议但非野鸡」，LNCS 是丛书）

所以只对**真正有疑问的**联网核实，结果两个都推翻了我的印象：

| venue | 我的怀疑 | 实查结论 |
|---|---|---|
| Computers, Materials & Continua (Tech Science Press) | 疑被 WoS 除名 | **仍在 WoS/Scopus，IF 2.4** —— 影响力一般，留 gray |
| Informatics in Medicine Unlocked (Elsevier) | 疑被 Scopus 除名 | **仍活跃、仍被索引** —— 留 gray |

其余高频灰态直接按领域知识扩进白名单，灰态 ≥3 篇的 venue 从 **73 个降到 30 个**，
剩下的 30 个全部是刻意留灰的争议刊，**没有漏网的野鸡**。

## 四、黑名单命中的 8 篇

| citekey | 裁决 | role | 证据句 | 刊 |
|---|---|---|---|---|
| li2022Prediction | MAYBE | — | 4 | Computational Intelligence and Neuroscience |
| wang2022Covid19 | MAYBE | BACKGROUND | 2 | Computational and Mathematical Methods in Medicine |
| wang2021Fusing | **INCLUDE** | **CITE_SUPPORT** | **0** | Wireless Communications and Mobile Computing |
| wang2021Multitask | MAYBE | — | 0 | Computational Intelligence and Neuroscience |
| zhao2021Early | INCLUDE | BACKGROUND | 0 | Computational Intelligence and Neuroscience |
| maheshwari2021Nanotechnologybased | MAYBE | BACKGROUND | 0 | Journal of Nanomaterials |
| alshammari2024Implementation | MAYBE | — | 0 | International Journal of Computer Applications |
| （另 1 篇 Zenodo 自助上传） | — | — | 0 | — |

前 6 篇是 Hindawi（10.1155）。**5 篇是 MAYBE**（本就不是核心文献），唯一那篇
`CITE_SUPPORT` 的 `wang2021Fusing` **有 0 条证据**——从没被精读，只有题录级信息，
检索时根本捞不到句子。

## 五、处置建议

**推荐：手工给这 8 篇打 `⚑ LOW_VENUE`，不建自动机制。**

flags 无白名单/枚举，md 的裁决行写 `⚑ LOW_VENUE` 就会自动流进索引（多 flag 写成
`⚑ A/B` 不带空格）。要让它踢出向量库，在 `src/scholar/notes_index.py:113-118` 旁加
`is_low_venue()`、抽 `is_suppressed() = is_retracted() or is_low_venue()`，只改
`src/scholar/embed_store.py:218` 一处调用点。

**注意：不要照抄撤稿的「同时踢出书目」。** 撤稿能从 `all_references.json` 剔除，是因为
「引用一篇撤稿论文当场炸掉」是正确行为；低 venue 不同，剔除会让你**故意**引用的那篇
渲染成 `(key?)`，那是 bug 不是 feature。

**但更实际的判断是：连这个都可以不做。** 6 条证据句、0.14% 的污染面，且唯一
`CITE_SUPPORT` 的那篇本身 0 条证据。真等它出现在检索结果里再处理不迟。

名单本身的价值反而更长期：它是一份可维护的资产，将来若要给检索排序加 venue 权重
（见下节），判据现成。

## 六、附带发现：`priority_tier` 是假的质量信号

（用户已定：本次只记录，不动代码。）

`priority_tier` 是**按月批次内的排名三分位**（`src/scholar/_citekey_utils.py:58-66`），
实测 high 730 / mid 775 / low 775 —— 精确三等分，绝对质量信息为零。一篇发在水刊但当月
同批最好的论文，`tier=high`。

但它正被当质量用：

- `scripts/notes_query.py:135` 把它作为写作取证的**第一排序键**
- `docs/skills/scholar-write/SKILL.md:29` 在教用户用 `--tier high` 筛质量

**为什么不现在改**：真正该替换它的是可信的质量信号，而本次审计的结论是 venue 质量的
区分度不足以支撑（black 只有 8 篇、gray 占 65.5%）。现在把排序键改成别的会留下中间态，
等真要接 venue 权重时又得再改一次。留待一并处理。

若将来要做，三个真挂点按收益排序：`notes_query.py:135` 主排序键 →
`topics.select_evidence._sort_key`（`topics.py:262`，概念页 + 问答共用，`Evidence.tier`
管道已铺好）→ `notes_search.py --cite`（最短的检索→稿子路径，零人工复核）。
**不该做的两处**：`workflow._library_neighbors`（判重不需要质量信号）、
`build_all_references` 排除（语义不对，见上节）。

## 复现

```
PYTHONPATH=. python scripts/audit_venue_quality.py --json out.json --md report.md
PYTHONPATH=. python scripts/audit_venue_quality.py --offline    # 只用缓存不发请求
```

脚本**只读**：不改 literature_index.json、不改任何 md、不碰向量库。

数据源：**Crossref**（免费无配额，`type` 字段直接给期刊/会议/预印本/丛书四分类，
外加被引数、年份、ISSN 列表）。全库 1913 篇有 DOI，命中 1698 / 查无 202 / 请求失败 0。
**OpenAlex 2026-08 起改预算制**（$0.001/请求、每日 UTC 午夜重置），一次全量 652 源就
打光当日额度，脚本有 `RateLimited` 熔断，仅作可选补充。SJR 全量 CSV 被 Cloudflare
挡住（403），脚本下不了，需浏览器手动下。

缓存：`output/scholar_notes/crossref_works_cache.json`（篇级，按 DOI）、
`venue_quality_cache.json`（OpenAlex 刊级，按刊名）。
