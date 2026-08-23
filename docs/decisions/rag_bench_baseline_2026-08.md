# RAG 检索 benchmark 基线（2026-08-21，摘要喂厚前）

## 背景

此前「概念换述 @1 ~30%」「5 case 全 top-1」等数字全是会话内临时实测、case 不落盘，
改语料前后无法同口径对比。本批（摘要喂厚）起，唯一权威口径是：

- 评测脚本：`scripts/rag_bench.py`（子进程跑 `notes_search.py --json`，与真实调用路径一致）
- case 集：`test/data/rag_bench_cases.jsonl`（65 条，进 git）
- 命令：`python scripts/rag_bench.py --json`（默认 hybrid + level auto + limit 10）

case 四类：en_title 15（英文标题原文当查询）、zh_oneline 15（中文判词原文）、
paraphrase 30（人工写的概念换述，不用标题/判词原词）、legacy_5case 5
（短中文概念查询；08-16 的原 5 case 存在会话中无法找回，按同场景重建）。
gold 允许多 citekey（同主题同等合理的篇命中任一算对）；造集阶段对 6 条 case 做过一次
人工 gold 放宽（逐条读了候选论文的 title/one_line 确认同等合理，非机械反填检索结果），
此后 gold 冻结——只有发现内容性错误才允许改，改动必须在本文件追记。

## 基线数字（喂厚前，向量库 = title+one_line 两段式，bge-m3，2254 paper + 18412 highlight）

| type | n | @1 | @5 |
|---|---|---|---|
| en_title | 15 | 15 (100%) | 15 (100%) |
| zh_oneline | 15 | 13 (86.7%) | 15 (100%) |
| **paraphrase** | 30 | **14 (46.7%)** | 19 (63.3%) |
| legacy_5case（短查询） | 5 | 3 (60%) | 5 (100%) |
| 总计 | 65 | 45 (69.2%) | 54 (83.1%) |

与历史临时实测方向吻合：英文标题近满分、中文判词 ~87-93%、概念换述是硬伤
（历史 ~30%，本 case 集 46.7%——case 集不同数字不同属预期，以本表为准）。

## 本批验收线（roadmap v2）

- paraphrase @1：46.7% → **≥60%**（目标值）
- en_title @1 不得低于基线（15/15）
- zh_oneline / legacy_5case 不得明显退化

原始明细：跑基线时的完整 JSON 由复跑 `python scripts/rag_bench.py --json` 再现；
喂厚后的对照数字在本文件下方追记。

## 摘要来源覆盖 census（2026-08-21 审核 agent 全库实测，非抽样）

2256 主条目 = 1898 有 DOI + 63 arXiv-only + 295 无任何 id：

- OpenAlex（DOI 侧）：找到 1875/1898，带摘要 **1493**
- Crossref 补（OpenAlex 无摘要的 DOI）：再得 **30**
- PubMed 补：再得 **144**
- arXiv：**63/63** 全有摘要
- OpenAlex title.search（无 id 的 295 条）：命中 135，带摘要 **117**

联合估算覆盖 ≈ (1493+30+144+63+117)/2256 ≈ **81.8%**。
故 roadmap 的 85% 降为参考线，正式验收线 = 回填实测覆盖率 ≥ census 估算 × 0.95 ≈ 78%，
且失败键必须分类落盘可重试。

## 2026-08-21 喂厚落地追记（P0 结论）

**落地形态：一篇双向量**（三轮 A/B 后定案，过程数字如下）：
- `p:<citekey>` 瘦精准向量（title+判词，文本与喂厚前逐字节相同）
- `ab:<citekey>` 厚召回向量（title+判词+摘要[:800] 同文，判词作跨语桥）
- 检索侧瘦/厚 dense 各自排名 + BM25 三路 RRF（`notes_search --paper-lanes thin` 可精确复现喂厚前行为，A/B 长期可复跑）

**A/B 实验矩阵（30 条中文换述）**：三段混合拼进 paper 文本 → para@5 63%→73% 但 zh_oneline@1 87%→73%（稀释）；
纯英文摘要独立 chunk → 判词恢复但跨语命不中，para@5 回落；合并掩码同台比余弦 → 瘦 chunk 系统性挤掉厚命中。
最终两路 RRF：**全部档位相对喂厚前零退化**（en_title 100%、zh_oneline 86.7%、para 46.7%/63.3%、en_para 80%/90%）。

**验收结论（诚实记账）**：
- 中文换述 @1 46.7%，未达 60% 目标值。根因不是语料而是查询语言：同概念英文换述 @1 80%，
  miss 的 11 条里 9 条 gold 有摘要——bge-m3 中文换述→英文语料的跨语对齐是硬上限。
  换 embedding 模型留下批（本批按既定优先级只做语料+阈值）。
- **实测净收益**：英文短术语查询召回面 +60%（"informative missingness" 命中 204→327，
  核心文献 sisk2020Informative 从榜外浮上 top-3——08-17「英文术语被饿死」问题的直接修复）；
  digest 近邻改查厚向量（收益在 P1b 标定量化）。
- 回填覆盖 80.0%（1804/2254）≥ 验收线 78%；正确性抽检 30/30；title.search 守门 sim=1.0 全数通过。
- 无震荡验收通过：任意入口二次增量 embedded=0、meta UPDATE=0。

**case 集追记**：造集后 gold 修正两次——6 条初始放宽（见上）；para-004/enpara-004 追加
gardner2023Benchmarking（TableShift 对「表格分布偏移基准」类 query 同等合理，厚路检索发现）。
新增 en_paraphrase 档 10 条（enpara-001..010）。现 75 case。

---

## 2026-08-23 对抗审计追记（S1/S2：`--min-score` 在 hybrid 下真正生效）

**改了什么**：`--min-score` 此前只约束 dense 泳道，BM25 单路命中无门槛地占 `--limit` 名额；
现在 hybrid 下 BM25 命中也回查余弦后过滤（`notes_search._gate_sparse`）。另：paper 侧展示分
改取**篇级最高余弦**（此前按 RRF 选代表，会丢掉更高余弦的兄弟 chunk）。

**基线复跑（分母 75 case，不是文档前半部分的 65）**：默认 `--min-score 0.4` 下**逐档全等**，
rank 变化 0/75：

| type | n | @1 | @5 |
|---|---|---|---|
| en_title | 15 | 15 | 15 |
| zh_oneline | 15 | 13 | 15 |
| paraphrase | 30 | 14 | 19 |
| en_paraphrase | 10 | 8 | 9 |
| legacy_5case | 5 | 3 | 5 |
| **总计** | **75** | **53** | **63** |

top-5 序列在 3 条上有变化（para-006 / para-008 / enpara-008），全是非 gold 位置互换，
指标不受影响。**追记这一条是因为提案方报的是"只在第 6~10 位重排"，实测最高够到第 3 位。**

**0.62 档（skill 判重口径，进程内同口径对照）**：@1 51→**54**、@5 73→**71**。
掉的两条是 para-006(`vaidya2026Nova`)、para-017(`wang2026Revisiting`)——它们的篇级真实最高
余弦分别是 0.5233 / 0.5786，**双双低于 0.62**，按 SKILL.md 的判据本来就不该报。
按"可执行召回"（gold 进前 N **且**展示为 cosine 且 ≥0.62）重算：@1 50→**54**、@5 71→**71**，
严格占优。这两条掉分是门槛语义的应有之义，不是退化。

**修复前的三个实测反例**（说明为什么这不是"文档写清楚就行"）：
- 纯离题探针 `"zzz量子纠错小麦施肥弱引力透镜" --min-score 0.95` → 返回 **108 篇**
  （而本目录 digest_neighbors_calibration 的标定档案写着硬离题探针 max sim 0.575）；修复后无命中。
- `--cite --min-score 0.7` 能对全库无一条 ≥0.7 的 query 吐出可粘贴引用串 → **假引用进稿子**。
- `--level paper --min-score 0.62` 报"语义命中 182 篇"，其中真正过门槛的只有 80 篇。

**未改也不建议现在改**：hybrid 仍按 RRF 名次排序，不按分数。SKILL.md 里"要按分数看排名请用
`--mode dense`"那条建议**一个字都不能放宽**——实测某 query 的全库最高余弦篇在修复前排第 10、
修复后仍排第 9。RRF 非分数序与 min_score 是正交问题。

**遗留课题（未做，需独立标定）**：对抗方实测 `--mode dense` 在本 case 集上是 **56@1 / 68@5**，
优于默认 hybrid 的 53/63，大类上 dense 全胜（paraphrase 17/25 vs 14/19、en_paraphrase 9/10 vs 8/9），
hybrid 只在 zh_oneline@5 与 legacy@5 各赢 1；"hybrid 召回 + 余弦排序"= 56@1/67@5 同样优于 RRF。
而本文档上半部分那张 A/B 矩阵比的是 **chunk 布局**（瘦/厚/合并掩码），**从未把 RRF 排序与余弦
排序做过对照**。默认模式可能选错了，但改默认要带自己的标定，不在本批范围内。

### 2026-08-23 R3 订正：默认模式之争的真实性质

R1 的追记把「默认 hybrid 打不过 dense」列为遗留课题，并暗示「hybrid 召回 + 余弦排序 =
56@1/67@5」是个两全其美的折中。**R3 逐 case 差分推翻了这个读法**：

| | @1 | @5 | en_title | zh_oneline | paraphrase | en_para | legacy |
|---|---|---|---|---|---|---|---|
| hybrid（现默认） | 53 | 63 | 15/15 | 13/15 | 14/19 | 8/9 | 3/5 |
| dense | 56 | 68 | 15/15 | 13/14 | 17/25 | 9/10 | 2/4 |
| hybrid 召回 + 余弦排序 | 56 | 68 | 15/15 | 13/14 | 17/25 | 9/10 | 2/4 |

- 「hybrid 召回 + 余弦排序」与**纯 dense 逐 case 完全等价**，救回 0/5，不是折中方案。
  那 5 条的 gold 余弦本来就低，任何以余弦为主序的排法都会把它们沉到 top-10 之外。
- 换 dense 丢 5 条（`legacy-001`「缺失指示符 插补」这类纯关键词 query 是 BM25 主场；
  `para-006/017/019/030` 靠中文 2-gram 词面命中捞回），捡回 9 条。
- **两种模式的失败集完全不相交**：两者都丢出 top-10 的 case = **0/75**；
  并集 @10 = **75/75**、@5 = 73/75、@1 = 64/75，而单模式最好只有 dense 的 70/75 @10。

所以这不是「改个默认值 + 复跑 bench」能收的账，也不是「需要独立标定阈值」——真实性质是
**在两种互补的失败模式之间二选一**，而现有 case 集无法裁决：75 条里没有一类是为隔离 BM25
价值设计的（无罕见缩写 / 作者名 / 数字 / citekey 形状），那 5 条丢失属于偶然采到。

**结论：不改默认**（改默认还要连带改 `_paper_side_hits` dense 分支那段「要泳道公平请用
hybrid」的注释、它的绑定测试、4 处文档，净收益 +4/75，不划算）。
**该做的是零代码的用法升级**：把 skill 里「要按分数看排名用 `--mode dense`」升级成
「**两轮：先 dense 看分数，没找到再 hybrid 补召回**」——捕获 100% 而非 93%，一行代码不用动。
若将来要重开这个课题，先给 case 集补一类「罕见英文术语/缩写」query，否则 bench 裁决不了。

## 2026-08-23 对标审计追记（acronym 类落地 + BM25 缩写失明修复）

R3 上面那句「先给 case 集补一类罕见英文术语/缩写 query」正是这次做的事，而补 case 的
过程直接撞出了一个真 bug（对标 4 家高 star 方案时发现，全文见
`oss_alignment_audit_2026-08.md` ⚠️-2）。

**改了什么**：`notes_search.bm25_tokenize` 取代直接调 `vault.tokenize` 做 BM25 分词，
补回恰好两字母的大写缩写（EM/MI/IV/RR）。`vault.tokenize` 的 `[a-z]{3,}` 此前把它们
整个丢掉——`EM algorithm` 退化成只查 `algorithm`，BM25 top10 全是算法选择类文献、与
dense top5 **零交集**，再经 RRF 把这批泛词命中送进 hybrid 默认结果。真库语料侧：含独立
EM/MI/IV 的 chunk 各 47/113/538 条，全是 IDF 最高、BM25 本该最出力的术语。
vault.tokenize 本体**未动**（近邻图与 qa 词面查重还挂在上面）。

**case 集**：75 → **84**，新增 `acronym` 类 9 条。故意混入歧义样本——MIMIC-IV 的 IV 是
版本号罗马数字、Microsoft 数据集也简称 MI——缩写召回不能靠放宽词面换来假阳性。

**修复前后（同口径，`--cases test/data/rag_bench_cases.jsonl`）**：

| 口径 | 修复前 | 修复后 |
|---|---|---|
| 原 75 条 hybrid | 53 @1 / 63 @5 | **53 / 63**（逐 case 一致，零回退） |
| 原 75 条 sparse | 47 @1 / 54 @5 | **47 / 54**（同上） |
| 原 75 条 dense | 56 @1 / 68 @5 | **56 / 68**（BM25 不参与，本就该不变） |
| 新 9 条 hybrid | 7 @1 / 7 @5 | **9 / 9** |
| 新 9 条 sparse | 6 @1 / 7 @5 | **8 / 9** |

即：对存量 case 是**严格零影响**，对缩写类是从半瞎到全中。`acr-001`「EM algorithm」
hybrid 下 rank 7 → **@1**，sparse 下 rank 10 → @1；`acr-004`「MI vs RI」sparse 从
top-10 外 → @1。dense 在 `acr-001`/`acr-007` 上是 rank=None——缩写正是 BM25 主场，
这批 case 同时也给「dense 全面优于 hybrid」那个读法补了反例。

**全量 84 条现基线（hybrid，2026-08-23）**：@1 **62/84**、@5 **72/84**。

**同批还改了** `embeddings.py` 的请求体（⚠️-1）：显式 `truncate: false` +
`options.num_batch=8192`。llama.cpp 对 embedding 的 num_batch 默认 2048，与模型 8192 的
上下文无关，超出部分被静默截断。**该改动不触发重嵌**——实测新旧请求体对真库 chunk
产出的向量逐位相同（max|Δ|=0.000e+00，与库内已存向量 cos=1.0），22592 条向量继续有效。

---

## 2026-08-23 R1 复审追记：缩写修复只修对了一半，case 集 84 → 87

上面那版修复（只补 `[A-Z]{2}`）在同日的两轮复审里被真库打脸两次，**两条同源**：

**R1-D 小写查询整条失效**。`em algorithm`（不按 shift）的 hybrid top5 原封不动还是
修复前的病态结果——`kerschke2019Automated` / `tornede2022Algorithm` /
`kotthoff2014Algorithm` 那批 Algorithm Selection 泛词命中。而当时新加的 9 条 acronym
case **全是大写**，照样满分 9/9。这是"bench 测不出的东西会无声退化"**同一个坑连踩两次**：
R3 提醒过要补缩写类 case，补了；但没想到还要补大小写两侧。

**R1-C 全大写反而制造假阳性**。只补大写导致 df 畸变：`of` 只有在全大写标题
（"DESIDERATA FOR THE DEVELOPMENT OF…"）里才被收，df 被压到 12 → IDF **7.50**，
比真缩写 `em` 的 6.16 还高。后果是 `MODELS OF CARE` 比 `models of care` 凭空多召回
一篇靠 `of` 命中的无关文献（`chen2025Phenotypic` 挤进 sparse top2）。

**改法**：两字母词**不论大小写全收**，让 IDF 自己决定权重——这本来就是 BM25 的设计
哲学。硬删虚词反而要维护一张黑名单，而真库里 OR/US/AS/AT/IS/IF/AN 恰恰**都是真缩写**
（odds ratio、adversarial training、homomorphic encryption、inversion score…），
删了就是丢信号。

| token | df(仅大写) | IDF | df(全收) | IDF |
|---|---|---|---|---|
| em | 47 | 6.16 | 49 | **6.12** |
| iv | 538 | 3.74 | 541 | **3.73** |
| mi | 113 | 5.29 | 125 | **5.19** |
| of | 12 | **7.50** | 3011 | **2.02** |
| on | 1 | **9.62** | 1419 | **2.77** |
| by | 10 | 7.67 | 840 | 3.29 |

真缩写 IDF 几乎不动，虚词被自动压平。代价 doc_len 仅 +3.3%（60.7 → 62.7），
sparse 查询耗时 0.96s 不变。

**回归（87 case 前先按存量 84 条同口径对比）**：hybrid / sparse / dense 的 @1、@5
**全部不变**，逐 case 只有 5 处 top10 内名次微动，其中 `enpara-005` 从 miss → rank 10
是净改善。dense 逐 case **完全全等**（它不走 BM25，本就该如此）。

**case 集 84 → 87**：新增 `acr-010~012` 三条**全小写**哨兵（`em algorithm`、
`mi vs ri for handling missing data`、`test of mcar for multivariate data…`），
当前 rank 2 / 2 / 1。选它们的判据是"当前能过、一旦改回只认大写就会掉出 top5"——
死 case 和满分 case 都当不了哨兵。大小写等价性另有单测
`test_bm25_tokenize_is_case_insensitive` 直接钉死。

**全量 87 条现基线（2026-08-23）**：

| 模式 | @1 | @5 |
|---|---|---|
| hybrid | **63/87** | **75/87** |
| sparse | 58/87 | 66/87 |
| dense | 65/87 | 79/87 |

（存量 84 条部分仍是 hybrid 62/72，与上一节完全一致。）
