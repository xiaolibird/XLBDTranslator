# 可信度加固：筛选召回闸门、证据不足拒答、札记模型留痕（设计，2026-10-13）

> 本文是**顶层设计**，不含实现代码。读者是没参与讨论的实现者：照第 9 节的工单逐个做即可，
> 每个工单都写了输入输出、可改文件、测试要求与完成定义。需要用户拍板的点集中在第 11 节。
> 文中所有「现状」数字都是 2026-10-13 对仓库与 `output/` 的只读核查结果（核查口径见附录 A）。

---

## 0. 一页摘要

| 项 | 一句话 | 主要产物 | 是否调 LLM |
|---|---|---|---|
| **项三 模型留痕**（前置） | 每次 LLM 调用记下**实际应答**的 provider 与模型（不是请求的模型），落到札记条目的 `decision_*` / `closeread_*` 字段；存量能回填的回填，回填不了的明确记「未知」 | `LLMClient.call_with_meta`、索引新字段、`_provenance/overlay.json`、按模型分组报表 | 否（只是记录） |
| **项一 筛选召回闸门** | 用**人的行为**（稿件引用、手动精读、盲标）构造金标准，测 filter-v3 的「不被丢弃率」与过度纳入率；配置（prompt 指纹 × 实际模型）没过认证时，运行时把 EXCLUDE 降成「待复核」而不是静默丢弃，CI 只核对指纹与认证记录 | `src/scholar/filter_eval.py`、`scripts/filter_eval.py`、`config/filter_eval/certification.json`、CI 测试 | 评测执行时调用；CI 不调用 |
| **项二 覆盖说明 + 证据不足拒答** | 概念页/问答页顶部印出确定性的「本页覆盖范围」；独立来源数不足阈值时**不调用 LLM**，落一页「库内证据不足」存根；单一来源论断加徽标 | `src/scholar/coverage.py`、`thresholds.py` 新节、topics/qa 镜像改造 | 否（拒答反而省调用） |

工单（详见第 9 节）：WO-1 调用元数据 → WO-2 札记来源字段 → WO-3 存量回填与分组报表；WO-4 证据充分性标定 → WO-5 覆盖说明与拒答（topics/qa 镜像）；WO-6 金标准构造 → WO-7 评测执行器 → WO-8 放行闸门。

---

## 1. 动机

npj Digital Medicine 上 Zhang & Fu (2026) 对 Quicker（LLM 循证指南系统）的 Matters Arising（doi:10.1038/s41746-026-03098-z）指出这类系统的三处结构性缺口：**检索/筛选环节的召回率从未对照人工金标准测过**、**证据不足时系统仍然给出看似完整的综合结论**、**产出物无法追溯到生成它的模型与版本**，因此其结论既不能复现也不能被审计。本仓库的札记链路在三处都有同构缺口：filter-v3 决定一篇论文能否进库，却从未对照人工判定测过召回；概念页与问答页只要召回到证据就硬写综合，没有回避机制；札记条目不记录是哪个模型写的，而项目先后用过 DeepSeek、Sonnet（claude-agent）、Gemini、Ollama 兜底，回退链还会在调用中途静默换人。本批借鉴这三条，目标是让「库说了什么」与「库没说什么」都可核查。

---

## 2. 现状核对：与需求描述的出入（以代码与数据为准）

| # | 需求描述中的说法 | 核查结果 | 影响 |
|---|---|---|---|
| D1 | 「粗查 schema 没有找到札记层面的模型字段」 | **字段其实存在，但只记了请求值，而且在落索引时被丢掉**。`FilterDecision.model` / `.prompt_version`（`src/scholar/schema.py:287-288`）与 `CloseReading.model`（`schema.py:342`）都有；但 `entry_from_segment`（`src/scholar/_citekey_utils.py:439`）构造索引条目时一个都没带，所以 sidecar 与 `literature_index.json` 里没有 | 项三不必新造 schema，主要是**改成记实际应答者 + 透传到索引** |
| D2 | （隐含）记下的 model 就是生成者 | **记的是请求的模型，不是实际应答的模型**。`workflow._filter_by_llm` 把 `settings.llm.filter_model or settings.llm.model` 直接写进每条裁决（`workflow.py:592`、`:778`）；`close_read` 写 `cr.model = model`（`closereading.py:651`），`model` 是调用参数，未设置时为 `None`。`LLMClient.call()`（`llm_client.py:415`）只返回文本，链上切到 deepseek/gemini/ollama 之后**调用方无从得知**。excluded sidecar 顶层的 `model` 也是请求值 | 按模型分组的漂移审计在回退发生过的批次上会**分错组**。这是项三的核心改动 |
| D3 | 「漂移审计一次升级了 199 篇旧 MAYBE→INCLUDE，说明漏判不少」 | 这 199 篇**没有被漏掉**：它们一直以 MAYBE 留在库里，变化的是排序档位；而且旧标签来自 filter-v1/v2，新标签来自 v3，所以这是**版本间分歧**，不是 v3 的召回缺口。真正会丢论文的是 EXCLUDE（EXCLUDE 不进索引），而 v3 的 EXCLUDE 从来没有被人工抽检过 | 项一要直接抽检**生产流里的 v3 EXCLUDE**，这是对「漏判率」最直接的估计 |
| D4 | （隐含）RAG 生产口径的审计结果可信 | **带 RAG 的审计存在自身泄漏**：`_library_neighbors`（`workflow.py:826`）没有排除论文自己，库内论文重审时最近邻就是它自己（sim≈1），而 prompt 规定「与已收文献纯重复 → 降为 MAYBE」。实测 `promotion_scan_20260808.json` 的 1215 条里至少 **247 条**（20%）被判 MAYBE，理由形如「与 athreya2021 重复，无增量」——说的正是它自己；`audit_filter_v3_drift_20260808_rag.json` 150 条里有 17 条同型 | 1) 评测必须做近邻自排除；2) 08-08 的升级扫描**少升了**一批，可另开工单重扫（第 7.3 节） |
| D5 | manual 系列是「用户亲手选的精读」，可作非循环信号 | **成立，而且比想象的更干净**：483 条 manual 全部**没有经过 filter-v3**。`pdf_ingest.build_segment`（`pdf_ingest.py:801-809`）直接构造 `decision="INCLUDE"`、`model=None` 的裁决。但它把 `stage` 写成了 `"llm_judge"`，**名不副实**，任何按 stage 统计「LLM 裁决」的脚本都会把 manual 算进去 | manual 层是干净的人类阳性；WO-2 顺手把新写入的 stage 改成 `manual_pick` |
| D6 | 可按批次排除「刻意超出画像的支线批次（达摩院、Chrome 标签页）」 | 达摩院批次有独立文件（`科研札记_2026-10-10-AI4LifeScience_手动精读.md`，6 篇）可以按文件排除；**Chrome 标签页批次的 19 篇已并入 7 个既有桶**，按文件名分不出来 | 支线分层不能只靠文件名，需要用户按批次加按篇预先登记（第 5.3 节） |
| D7 | 稿件引用文献是最强阳性 | 成立。`~/Desktop/Lab/P4/manuscript/arxiv/bibliography.bib` 57 条、`.../ieee/references.bib` 20 条。按 citekey/DOI/标题匹配，arxiv 版 43 条在库（38 manual + 5 auto），ieee 版 5 条在库，其余约 29 条不在库（含教科书、软件、数据集） | 库外的稿件引用**从未被过滤器看过**，是偏倚最小的阳性；库内且来自 auto 系列的那几条存在幸存者偏倚（第 5.4 节） |
| D8 | 「qa.py 与 topics.py 有同构的两份渲染代码」 | 成立：`topics.render_generated_block`（`topics.py:1181`）与 `qa.render_qa_block`（`qa.py:1202`）的证据表段逐行同型，注释里已互相点名「两侧必须同步」 | 项二把新逻辑放进共享模块 `coverage.py`，两边只各加一个调用点，再加镜像测试 |
| D9 | （隐含）概念页/问答没有任何不足提示 | 部分已有：qa 的「本次召回没覆盖到的」一节已经印出「不等于库里没有」（`qa.py` render 内 A1 注释）；`qa_synthesis_prompt.md` 第 40 行要求证据不足时 answer 直说答不上来。但这些**全靠 LLM 自觉**，没有确定性门槛；topics 的「证据缺口」一节没有这句免责 | 项二补的是确定性门槛与覆盖说明，不是从零开始 |
| D10 | 证据池 = 全库 | **不是**。2745 个 keeper 条目里只有 **1095 篇**有句级 highlight（向量库 `level='highlight'` 1095 个 citekey），概念页与问答只从这 1095 篇取证；另外 1650 篇（689 INCLUDE + 959 MAYBE + 2）只有题录或摘要，**永远不会出现在任何概念页的证据表里** | 覆盖说明必须把这个口径印出来（这是「库里没有 ≠ 文献里没有」之外的第二层缺口：「库里有 ≠ 证据池里有」） |
| D11 | 生产输入 = 标题 + 摘要 | 生产流里送审的 `original_abstract` 多数是 Google Scholar 邮件摘录：近 6 次运行 1233 条中位数 **407 字符**（P10 237、P90 715）；而 `abstracts.json` 缓存的完整摘要中位数 1595 字符。prompt 统一截到 800 | 拿完整摘要做评测会比生产**乐观**，评测要有「生产口径截断」模式（第 5.5 节） |
| D12 | （新发现）`ingest.classify_segments` 的手选覆盖 | `ingest.py:388-391` 用 `stage="manual_pick"` 构造 `FilterDecision`，但 `stage` 的 `Literal`（`schema.py:276`）里没有这个值，**这条分支一旦走到会抛 ValidationError**；`:392-397` 把 LLM 原判 EXCLUDE 就地改成 INCLUDE，**原判只进了日志**。被用户手选推翻的 EXCLUDE 恰恰是现成的「漏判」样本 | WO-2 把 `manual_pick` 加进 Literal，并新增 `llm_decision_before_override` 字段保留原判 |
| D13 | （新发现）auto 精读模型无处可查 | digest JSON（108 份、7680 段）里 `close_reading` 全为空；`backfill_*_progress.json` 与 `deepread_batch1_progress.jsonl` 都不记模型。**auto 系列的精读模型基本无法回填** | 存量 auto 精读只能记 `unknown-legacy`；本设计不做按时间线推测（第 4.3.4 节） |
| D14 | （新发现）manual 终稿的「模型」是自由文本 | 483 个 bundle 里 `close_reading_final.model` 有 375 个为空，其余是 `"opus-gen+sonnet-adversarial"`、`"gpt-6-sol independent reading and gpt-6-sol cross-check"` 之类的自由描述；`close_reading_script.model` 才是规范模型名（sonnet 251 / deepseek-v4-pro 99 / gemini 31 / 无脚本 102） | manual 要拆成两层记：脚本草稿模型 + 终稿署名（原样保留自由文本） |
| D15 | （新发现）带不带 RAG 两轮的一致率 | 08-08 两轮（无 RAG / 有 RAG）三态一致 110/150 = 73%（记忆里写 76%，分母口径不同）。注意两轮差的不只是随机性，还有 RAG 与上面的自身泄漏 | 评测必须做重复运行并报告翻转率，单次运行的数字不可当真 |

---

## 3. 总体设计与依赖关系

```
项三 WO-1 调用元数据 ──► WO-2 札记来源字段 ──► WO-3 存量回填/分组报表
        │                                          
        ├──────────────► WO-7 评测执行器（按实际模型记账）──► WO-8 放行闸门
        │                       ▲
        │               WO-6 金标准构造（无 LLM）
        │
        └──────────────► WO-5 覆盖说明 + 拒答（页面记合成模型）
                                ▲
                        WO-4 证据充分性标定（无 LLM）
```

- **项三先做**：项一的闸门以「实际应答模型」为键，回退链换人时必须能识别出来；没有 WO-1，闸门只能看请求值，等于没装。
- **workflow.py 会被 WO-2、WO-7、WO-8 三个工单改动**，必须串行（WO-2 → WO-7 → WO-8），不能并行派发。topics.py/qa.py 只由 WO-5 改动。
- 第一批可并行派发的工单：WO-1、WO-4、WO-6（三者改的文件互不相交）。

---

## 4. 项三：札记模型与版本留痕

### 4.1 现状

- 见第 2 节 D1、D2、D13、D14。数据面：
  - auto 筛选裁决：digest JSON 里有 `filter_decision.model`（`deepseek-v4-flash` 2196 段、`sonnet` 525 段、空 4959 段，空的是早期关键词/v0 时代）与 `prompt_version`（`filter-v2@02c6656b` 2170、`filter-v3@08540624` 541 等），但都是请求值，且没进索引。
  - auto 精读：无任何持久化记录。
  - manual：bundle 里有脚本草稿模型（请求值）与终稿自由文本署名；`cross_check_report.reviewer_label` 18 个 bundle 有值。
  - 概念页/问答页：frontmatter 不记合成模型、审计模型（`build_frontmatter` `topics.py:1371`、`build_qa_frontmatter` `qa.py:1337`）。
- 索引共 2889 条；有 per-file sidecar 的札记 78 个文件，**43 个 auto 月份没有 sidecar**（`notes_index.py:855` 注释），这些月份走 md 降级解析，md 里不印模型信息。

### 4.2 目标

1. 每次 LLM 调用都能拿到「实际是谁答的」：`provider`（链上名字）、`served_model`（应答方自报的模型 id，拿不到时退回实际发出的模型名）、是否发生过回退、是否是降级半截正文。
2. 每个新写入的札记条目带上**裁决来源**与**精读来源**两组字段，进 sidecar 与 `literature_index.json`。
3. 存量条目：能从现有档案确定的回填，确定不了的**明确记未知**，绝不推测。
4. 一条命令按模型分组统计（供漂移审计用），例如「filter-v3 下 sonnet 与 deepseek-v4-flash 裁出的 INCLUDE 比例」「各精读模型产出的可引证句数」。
5. 概念页/问答页 frontmatter 记录合成模型与审计模型（顺手做，复用同一个元数据）。

### 4.3 方案

#### 4.3.1 调用元数据 `CallMeta`（WO-1）

在 `src/scholar/llm_client.py` 新增：

```
@dataclass(frozen=True)
class CallMeta:
    provider: str            # 链上名字：deepseek / claude-agent / gemini / ollama / openai-compatible
    requested_model: str|None   # 调用方传入的 model 参数（原样）
    sent_model: str          # 实际发出请求时用的模型名（_model_for 解析后的值）
    served_model: str|None   # 应答方自报的模型 id；拿不到为 None
    chain_index: int         # 应答时的链位，0 = 主 provider
    fell_back: bool          # 本次调用期间是否推进过链位（含内容拒答的临时切换）
    degraded_partial: bool   # 是否为「重试耗尽后交还的半截正文」
    at: str                  # ISO 时间（秒）
```

- 新方法 `call_with_meta(prompt, **同 call 的参数) -> Tuple[str, CallMeta]`；`call()` 改为调用它并丢弃 meta，**签名与行为不变**（所有既有调用点零改动）。
- 不用「最后一次调用」这种实例级可变状态：精读是多线程的（`_conn_lock` 注释），实例级 last_meta 会串线。
- `served_model` 的来源（实现者须用真实响应结构核对，写进代码注释）：
  - openai-compatible（deepseek/ollama）：响应体顶层 `model` 字段；
  - gemini：响应对象的 `model_version`（拿不到则 None）；
  - claude-agent：CLI `--output-format json` 信封里的 `modelUsage`（以模型 id 为键的字典），取输出 token 最多的那个键；信封里没有就为 None。**不要凭记忆写键名**：实现者先用一个已有的信封样本（测试里若有固定样本就用，没有就让主会话跑一次 `claude -p --output-format json` 抓一份存成测试夹具）确认结构。
- `degraded_partial`：沿 `_call_once` 现有「重试耗尽交还 partial」那条路径置真。

#### 4.3.2 字段定义（WO-2）

**Schema 层**（`src/scholar/schema.py`，全部 Optional、默认 None，老 JSON 反序列化不受影响）：

| 模型 | 新字段 | 含义 |
|---|---|---|
| `FilterDecision` | `provider` | 实际应答的 provider |
| | `served_model` | 应答方自报模型 id |
| | `fell_back` | 该批调用是否发生过回退 |
| | `llm_decision_before_override` | 被手选覆盖前的 LLM 原判（INCLUDE/MAYBE/EXCLUDE），只在覆盖时写 |
| | `gate` | 项一闸门的处置标记（见 5.7），本工单只加字段不用 |
| | `stage` 的 Literal | 增加 `"manual_pick"` |
| `CloseReading` | `provider`、`served_model`、`fell_back` | 同上；分块深读取**汇总步**那次调用的 meta，另记 `chunk_models: List[str]`（各块调用的 served_model 去重后列表，块间换人时可见） |
| | `prompt_version` | 精读 prompt 的指纹：`"{label}@{md5前8位}"`，label 用 `closeread-single` / `closeread-chunked` / `manual-synth`，md5 对模板原文取（与 `workflow._get_filter_prompt_version` 同法） |
| | `final_by` | manual 终稿署名（自由文本原样保留）：优先 `close_reading_final.model`，其次 `cross_check_report.reviewer_label`，都没有则 None |

**原有 `model` 字段保留原语义（请求值）不改**，避免老数据含义漂移；新增字段承载实际值。

**索引条目层**（`entry_from_segment` 增加，平铺键，与 `fulltext_*` 同风格；「缺失 = 未知」口径与 `fulltext_truncated` 相同，SCHEMA_VERSION 不动）：

| 键 | 来源 |
|---|---|
| `decision_source` | `digest` / `manual` / `manual_pick` / `keyword_fallback` / `promotion-scan-2026-08-08`（回填）/ `unknown-legacy` |
| `decision_provider` | `fd.provider` |
| `decision_model` | `fd.served_model or fd.model`（并记 `decision_model_verified: bool` = served_model 是否非空） |
| `decision_prompt_version` | `fd.prompt_version` |
| `decision_at` | `fd.decided_at`（ISO 日期） |
| `closeread_provider` / `closeread_model` / `closeread_model_verified` / `closeread_prompt_version` / `closeread_at` | `CloseReading` 对应字段 |
| `closeread_final_by` | `cr.final_by`（仅 manual） |
| `provenance_origin` | `native`（本批之后新写入）/ `backfill-digest-json` / `backfill-bundle` / `backfill-promotion-scan` / `unknown-legacy` |

**不写进 md 正文**。理由：这些字段是**来源**而不是**裁决**，不需要人读；写进 md 就会进入「md + sidecar + 人读表格」三处同步的雷区（见记忆 scholar-filter-v3-drift-audit）。代价是 43 个无 sidecar 的月份只能靠 overlay（下节）承载。

#### 4.3.3 写入点（WO-2）

| 写入点 | 改法 |
|---|---|
| `workflow._filter_by_llm` / `_apply_filter_verdicts` / `_build_filter_decision` | 把 `_call_llm` 换成走 `call_with_meta` 的版本，meta 随批传进 `_apply_filter_verdicts`，写入每条 `FilterDecision`。首轮与末尾重试轮各自用各自那次调用的 meta（重试轮很可能是另一家）。excluded sidecar 顶层新增 `served_models: {"claude-agent:claude-sonnet-x": 批数, ...}` 汇总，原 `model` 键保留 |
| `closereading.close_read` / `deep_close_read` | 同上，写 `CloseReading` 新字段与 `prompt_version` |
| `pdf_ingest` 汇总步（`pdf_ingest.py:769-789`） | 同上；`build_segment` 的 `stage` 改为 `"manual_pick"` |
| `pdf_ingest.segment_from_bundle` | 用 bundle 的 `close_reading_final.model` / `cross_check_report.reviewer_label` 填 `final_by`；并把 `close_reading_script` 的 model/provider 透传到索引字段（终稿覆盖精读后，脚本草稿的模型信息不能丢） |
| `ingest.classify_segments`（force_include 分支） | 覆盖前把原判写入 `llm_decision_before_override`；修 D12 的 Literal 问题 |
| `_citekey_utils.entry_from_segment` | 生成上表索引键 |
| `notes_index._rename_citekey_in_note` 等改写 sidecar 的路径 | **只需验证**它们原样保留未知键（加测试），不改逻辑 |

#### 4.3.4 存量回填：overlay 而不是改 sidecar（WO-3）

- 新文件 `output/scholar_notes/_provenance/overlay.json`：`{"schema": 1, "generated_at": ..., "records": [{"dedup_key", "doi", "arxiv_id", "title", "fields": {...索引键...}}]}`。
- `notes_index.update_index` 在全部月份条目汇总之后、写盘之前合并 overlay：按**当前键梯重算**的 dedup_key 匹配（同 `_citekey_utils.recompute_entry_key`），认不到再按 DOI/arXiv/规范化标题；**只填缺失键，绝不覆盖** sidecar 已有的值。合并发生在月份级增量缓存之后，因此 overlay 变化不需要让月份缓存失效。
- 回填来源与规则（`scripts/backfill_provenance.py`，默认 `--dry-run`，只写 overlay，**不碰 md 与 sidecar**）：
  1. **auto 裁决**：扫 `output/scholar_digest/digest_*.json` 的 `segments[].filter_decision`，按 paper_id/DOI/标题对到索引条目，写 `decision_model`（`decision_model_verified=false`，因为是请求值）、`decision_prompt_version`、`decision_at`，`provenance_origin=backfill-digest-json`。同一条目在多份 digest 出现时取**最晚**一次（它才是当前 decision 的来源），并核对 decision 值一致，不一致则不回填并列入报告。
  2. **199 篇升级条目**：decision 已被 08-08 扫描改写，来源应是 `promotion_scan_20260808.json`（`new_decision=INCLUDE` 且当前索引 decision=INCLUDE、one_line 行尾带「复审升级 2026-08-08」）。该 JSON 不含模型字段；按记忆档案，那次是 `deepseek-v4-flash` + `filter-v3@08540624`，回填时 `decision_model_verified=false` 并在 `fields` 里加 `decision_model_note: "据 2026-08-08 审计记录"`。
  3. **manual**：从 bundle 回填 `closeread_model`（脚本草稿 model）、`closeread_final_by`、`decision_source=manual`，`provenance_origin=backfill-bundle`。
  4. **auto 精读模型**：无档案，全部记 `closeread_model` 缺失 + `provenance_origin=unknown-legacy`。**不按时间线推测**（回退链会在任意时刻换人，推测值进分组报表会制造假结论；用户若要推测，见第 11 节 Q7）。
- 分组报表：`scripts/provenance_report.py`（只读）：`--by decision_model|closeread_model|decision_prompt_version|provenance_origin`，输出各组条目数、INCLUDE/MAYBE 比例、平均可引证句数（`tag_counts`）、verified 比例；`--json` 机读输出。也可把分组能力加进现有 `scripts/notes_query.py`，二选一由实现者看那个脚本的参数结构决定，但只能加不能改既有参数。

#### 4.3.5 页面来源（并入 WO-5）

`synthesize_topic` / `synthesize_qa` / `run_audit` 改走 `call_with_meta`，frontmatter 新增受管键 `synth_provider`、`synth_model`、`synth_model_verified`、`audit_model`（qa 专有）。`QA_SCHEMA_VERSION` 3→4、`TOPIC_SCHEMA_VERSION` 1→2；`--verify` 与 `audit_*_pages` 对旧版本页照常通过。

### 4.4 需改动的文件

`src/scholar/llm_client.py`、`src/scholar/schema.py`、`src/scholar/workflow.py`、`src/scholar/closereading.py`、`src/scholar/pdf_ingest.py`、`src/scholar/ingest.py`、`src/scholar/_citekey_utils.py`、`src/scholar/notes_index.py`（overlay 合并）、新增 `scripts/backfill_provenance.py`、`scripts/provenance_report.py`、`docs/scholar_notes_AGENTS.md`（字段说明，索引重建会同步到 `output/scholar_notes/AGENTS.md`）、测试文件见工单。

### 4.5 验收标准

1. 假 provider 链测试：主 provider 抛可切换错误、第二家应答 → `CallMeta.provider` 为第二家、`fell_back=True`、`chain_index=1`；内容拒答临时切换后链位复原，meta 仍记录真实应答者。
2. 多线程测试：两个线程并发调用不同假 provider，各自拿到的 meta 不串线。
3. 筛选：首轮批次由 A 应答、重试批次由 B 应答 → 两批论文的 `FilterDecision.provider` 分别为 A、B。
4. `entry_from_segment` 输出含第 4.3.2 节全部键；无 meta 的老 segment 输出这些键为 None，**文章条目的其余键逐字节不变**（复用现有「书籍字段只在存在时写入」那条测试的思路）。
5. overlay 合并：只填缺失、不覆盖；改 citekey 后仍能按 dedup_key 认领；认不到的记录计数打印出来。
6. 回填 dry-run 在真实库上的报告（主会话执行，不是子代理）：auto 条目中 `decision_model` 可回填比例、manual 条目 `closeread_model` 可回填比例（预期 ≥ 79%，即 381/483 有脚本模型）、`unknown-legacy` 条目数。报告数字写进 WO-3 的完成说明。
7. `provenance_report.py --by decision_model` 在真实索引上能跑通且各组之和 = keeper 总数。
8. 全量 `pytest test/ -q` 通过，通过数不少于开工前基线。

### 4.6 不做

- 不按时间线/日志推测历史模型；不重跑任何历史精读来「补」模型信息。
- 不改 md 正文格式，不在人读表格里加列。
- 不把 provenance 进向量库 chunk 元数据（需要时从索引按 citekey 联表）。

---

## 5. 项一：筛选召回评测集 + 不达标不放行（fail-closed）

### 5.1 现状

- filter-v3（`config/prompts/whitelist_filter_prompt.md` + `config/research_profile.yaml`）由 `workflow._filter_by_llm` 执行，每批 20 篇（`FILTER_BATCH_SIZE`），温度 0.3（claude-agent CLI 不接受温度参数，实际不可控）。当前生产配置：`LLM__PROVIDER=claude-agent`、`LLM__MODEL=sonnet`、回退 `deepseek,claude-agent:opus,gemini`。
- prompt 指纹 `filter-v3@08540624` 已覆盖「模板 + 纳入维度 + 排除维度 + 研究方向」（`workflow.py:804-824`），但**不含模型**。
- 生产量级：近 8 次周跑每次 EXCLUDE 125–460 篇、纳入 26–135 篇，EXCLUDE 占八成以上；v3 时代累计 LLM EXCLUDE 2521 条，全部带送审时的原始摘要，存于 `output/scholar_digest/_archive/excluded/*.json`。
- 2521 条的置信度分布：0.5 有 4、0.6 有 54、0.7 有 127、0.8 有 1003、0.9 有 1153、1.0 有 180。
- 已有审计素材都是 LLM 标签（见 5.4），而且带 RAG 的那几份受 D4 自身泄漏污染。

### 5.2 目标

1. 一份**以人的行为为标签**的金标准，能估计：
   - **保留率**（主指标）：金标准阳性被判为 INCLUDE 或 MAYBE（即进库）的比例。EXCLUDE 是唯一真正丢论文的结果，所以召回按「不被丢弃」定义；
   - **INCLUDE 召回**（副指标）：阳性被判 INCLUDE 的比例（影响排序与能否进全文精读名额）；
   - **过度纳入率**：人工判定为无关的论文被判 INCLUDE 的比例（主），被判 INCLUDE 或 MAYBE 的比例（副）；
   - **生产漏判率**：生产流 EXCLUDE 中人工判定为「应收」的比例（直接回答「漏了多少」）；
   - **翻转率**：同一配置重复运行，三态结论不一致的比例。
2. 每个「prompt 指纹 × 实际应答模型」组合需要通过认证才算放行；没通过时运行时 **fail-closed**：不让未认证的配置静默丢论文。
3. CI 不调用 LLM，但能拦住「改了 prompt/画像却没有重新认证」。
4. 成本可控：一次完整认证约 40 次批调用，一次冒烟检查约 3 次。

### 5.3 金标准的构造口径

所有条目统一形状：`{item_id, title, abstract, abstract_source, stratum, label, split, dedup_key, citekey, label_source, labeled_at, notes}`。分层如下：

| 层 | 内容 | 标签 | 规模（预估） | 用途 |
|---|---|---|---|---|
| **P1-out** | 两份稿件 bib 中**不在库**的期刊/会议论文（剔除教科书、软件、数据集、网页，`entry_type` 不是 article/inproceedings/misc-arXiv 的一律剔除，剔除清单落盘供用户过目） | 阳性 | ~20 | **偏倚最小的阳性**：从未被过滤器看过 |
| **P1-in** | 两份稿件 bib 中在库的条目 | 阳性 | ~48（38 manual + 5 auto + ieee 若干，去重后） | 强阳性；其中来自 auto 系列的几条有幸存者偏倚，单列报告 |
| **M-core** | manual 系列中用户登记为「画像内」的批次/篇目，去掉已在 P1 的 | 阳性 | ~400 | 主门槛层；按 hash 固定切 dev 70% / test 30% |
| **M-side** | manual 系列中用户登记为「刻意的画像外支线」的批次/篇目 | 不打标签 | ~50–80 | **只报告不设门槛**：用来观察画像边界，不参与通过判定 |
| **F-override** | `ingest.classify_segments` 手选覆盖了 LLM EXCLUDE 的篇目（WO-2 起才有持久记录；之前的只能从日志 grep `手选覆盖裁决`，能找到就收） | 阳性 | 少量、逐步累积 | 直接的漏判样本；累积到 ≥10 条后进门槛 |
| **H-stream** | 用户**盲标**的生产流样本：从 v3 EXCLUDE 中按置信度分层抽 100 条（0.5–0.7 段全取后再按比例抽 0.8/0.9/1.0）、从 v3 INCLUDE 抽 40、MAYBE 抽 30，混洗后导出，**不显示 LLM 判定** | 用户标「应收 / 边缘 / 无关」 | 170 | 生产漏判率、过度纳入率；「边缘」不进门槛分母，单独报告 |
| **R-stable** | `audit_stable_excludes_20260808.json` 的 49 篇（两轮均拒） | LLM 标签 | 49 | **只做回归/稳定性观察，不当金标准** |

**manual 支线的登记方式（防止事后找补）**：WO-6 先导出一张**批次级**登记表（30 个批次 × 篇数 × 批次名），用户在**第一次评测运行之前**把每个批次标成 `core` / `side` / `mixed`；`mixed` 批次再导出按篇的登记表。登记表连同登记时间写入 `gold` 版本号。之后若用户想改登记，必须新开 gold 版本，旧版本的评测结果不得与新版本混算。这样做的理由：如果先看评测结果再决定哪些 manual 篇算「支线」，用户会不自觉地把漏判的那几篇归为支线，召回率就被修饰过了。

**输入的来源**（必须与生产同口径：标题 + 英文原始摘要，截 800）：
- 库内 auto：`output/scholar_notes/abstracts.json`（键为 dedup_key 形式）；
- manual：bundle 的 `segment.original_abstract`；
- 库外 P1：用仓库已有的元数据模块（`crossref.py`、`title_lookup.py`、`academic_search.py`）按 DOI/标题取摘要，结果缓存进金标准文件，**只取一次**；取不到的标 `abstract_source=none`，仍入集（生产里也有只有标题的论文），但单列报告；
- H-stream：直接用 excluded sidecar / digest JSON 里**生产当时送审的那份** `original_abstract`。
- **不得**使用中文翻译摘要、精读正文、one_line 作为输入。

**文件位置**：`output/scholar_notes/_eval/filter/`（随 iCloud 周备份，`scripts/backup_snapshot.py` 已覆盖整个 `output/scholar_notes/`；不进 git，因为稿件引用清单属于未发表工作的信息）。其中：
- `gold_v{N}.jsonl`：金标准条目；
- `gold_v{N}.meta.json`：构造参数、各层计数、登记表时间戳、内容 sha256（前 8 位即 `gold_version`）；
- `registry_batches_v{N}.csv`、`registry_papers_v{N}.csv`：manual 登记表；
- `label_sheet_{date}.csv` / 回收后的 `labels_{date}.csv`：H-stream 盲标表（UTF-8 带 BOM，Numbers/Excel 直接打开）；
- `runs/{run_id}.json`：每次评测运行的逐条结果与汇总。

### 5.4 循环论证的规避

| 风险 | 规避措施 |
|---|---|
| 标签来自 LLM（drift/promotion/stable 三份审计） | 只用于 R 层的回归观察，**任何门槛不以 LLM 标签为分母** |
| 库内阳性曾被过滤器放行过（幸存者偏倚） | P1-out 与 manual 层天然无此偏倚（manual 从未过过滤器，见 D5）；P1-in 中来自 auto 的条目单列，不参与门槛 |
| RAG 近邻自身泄漏（D4） | 评测运行时近邻检索**排除与被测条目同 dedup_key 的条目，以及标题规范化后相同的条目**（预印本/正式版并存的情况）。另跑一个 RAG 关闭的对照臂，两臂差异写进报告 |
| 研究画像是看着库写的，画像本身对库内论文过拟合 | 主门槛放在 P1-out 与 M-core 的 test 切分上；P1-out 规模小，所以报告逐条列出每一篇被丢的论文，不只给比例 |
| 调 prompt 时盯着评测集调（过拟合评测集） | dev/test 固定切分（按 `sha256(item_id)` 取模，不随 gold 版本变）；`whitelist_filter_prompt.md` 中出现的 few-shot 标题若命中 test 切分或 P1，`filter_eval check` 报错。调 prompt 只许看 dev 的逐条结果，test 只看汇总 |
| 盲标被 LLM 判定影响 | 标注表不含 LLM 判定、置信度、bucket、近邻；条目顺序随机；标注完成前不得运行 `report --show-verdicts` |
| 生产输入是邮件摘录而评测用完整摘要（D11） | 评测默认用「生产口径」截断（见 5.5），完整 800 字符作对照臂 |

### 5.5 评测执行（WO-7）

- 入口 `scripts/filter_eval.py run --gold vN [--split test|dev|all] [--strata ...] [--repeats 2] [--rag on|off] [--abstract-mode prod|full] [--smoke]`。
- 复用生产路径：构造 `PaperSegment` 列表，调 `ScholarWorkflow._filter_by_llm`（不另写一套 prompt 组装，否则测的不是生产代码）。需要在 workflow 增加一个**仅评测使用**的近邻自排除钩子：`self._neighbor_exclude: Dict[int, Set[str]]`（segment_id → 要排除的 citekey 与 dedup_key 集合），`_library_neighbors` 归并时跳过；默认空字典，生产行为不变。
- `--abstract-mode prod`：把摘要截到前 400 字符（对齐 D11 测得的生产中位数；这个数作为模块常量写明出处）；`full`：截 800（与 prompt 一致）。
- 批次组成：阳性与阴性**混洗**后按 20 一批（与生产相同），随机种子写进 run 记录；不能整批全是阳性，否则批内对比效应会扭曲判定。
- 重复：`--repeats 2`（默认），两次使用不同的混洗种子。每条取**两次中更差的那次**计入保留率（保守口径），同时报告翻转率。
- 每批记录 `CallMeta`（来自 WO-1）。**一次运行内若出现多于一个 served_model**（回退发生），按 served_model 拆分统计；认证只对「全部批次都由同一 served_model 应答」的运行有效。
- 成本估算（claude-agent sonnet 订阅额度，按生产实测「12 批 10+ 分钟」）：
  - 认证运行：P1（~68）+ M-core test（~120）+ H-stream（170）≈ 360 条 × 2 次 ≈ 36 批调用，约 30–45 分钟；
  - 冒烟：每层固定抽 20 条共 60 条 × 1 次 = 3 批，约 3 分钟；
  - RAG 关闭对照臂与 full 对照臂各再加约 18 批，按需跑。
- 输出 `runs/{run_id}.json`：逐条 `{item_id, stratum, label, verdicts: [三态×repeats], confidence, served_model}` + 汇总指标 + 配置键（见 5.8）。

### 5.6 指标与门槛

指标全部给点估计 + Wilson 95% 区间（纯函数，`filter_eval.py` 内实现，有单测）。门槛常量放 `src/scholar/thresholds.py` 新节（理由见第 8 节），**初值是建议值，首次基线运行后由用户确认或修改**：

| 常量 | 初值 | 含义 |
|---|---|---|
| `FILTER_GATE_KEEP_P1_MIN` | 0.95 | P1（in+out 合并，auto 来源除外）保留率下限；另要求逐条列出被丢的论文 |
| `FILTER_GATE_KEEP_CORE_MIN` | 0.92 | M-core test 切分保留率点估计下限 |
| `FILTER_GATE_KEEP_CORE_WILSON_MIN` | 0.88 | M-core test 切分保留率 Wilson 下界下限（n≈120 时点估计 0.95 的下界约 0.90） |
| `FILTER_GATE_FALSE_INCLUDE_MAX` | 0.15 | H-stream 中人判「无关」者被判 INCLUDE 的比例上限 |
| `FILTER_GATE_STREAM_MISS_MAX` | 0.05 | H-stream 的 EXCLUDE 样本中人判「应收」的加权比例上限（按置信度分层抽样权重还原） |
| `FILTER_GATE_REGRESSION_MAX` | 0.02 | 与当前已认证配置在**同一批条目上**配对比较，保留率下降不得超过此值 |
| `FILTER_GATE_FLIP_MAX` | 0.15 | 翻转率上限（**只告警，不阻断**：随机性高本身不是丢论文，但会让上面的数字不可信） |
| `FILTER_UNCERTIFIED_HOLD_CONF_MAX` | 0.7 | 未认证期间，置信度 ≤ 此值的 EXCLUDE 转入待复核（见 5.7 方案 B） |

两个方向的错误都要计入：保留率管漏判，`FALSE_INCLUDE` 管过度纳入。过度纳入的代价是每篇多一次精读调用与用户注意力；漏判的代价是论文永久丢失、事后极难发现。因此门槛**不对称**：漏判门槛设硬下限，过度纳入门槛相对宽松。

### 5.7 放行闸门（WO-8）

**认证记录** `config/filter_eval/certification.json`（**进 git**；不含任何论文标题，只有指纹与指标）：

```
{
  "schema": 1,
  "gate_mode": "warn",                     // warn | enforce；首次认证通过后由用户改成 enforce
  "certifications": [
    {
      "prompt_fingerprint": "filter-v3@08540624",
      "provider": "claude-agent",
      "served_model": "<CLI 自报 id>",
      "requested_model": "sonnet",
      "rag_mode": "on-selfexcl",
      "abstract_mode": "prod",
      "batch_size": 20,
      "gold_version": "gold-v1@xxxxxxxx",
      "run_ids": ["..."],
      "certified_at": "2026-10-xx",
      "metrics": {...},                    // 5.6 表中各指标的点估计、区间与 n
      "thresholds_snapshot": {...},        // 认证当时的门槛常量值
      "passed": true
    }
  ],
  "waivers": [
    {"prompt_fingerprint": "...", "reason": "...", "expires": "YYYY-MM-DD"}   // 最长 14 天
  ]
}
```

**指纹函数提纯**：把 `workflow._get_filter_prompt_version` 的计算抽成纯函数 `filter_eval.filter_prompt_fingerprint(template, inclusion_dims, exclusion_dims, research_interests) -> str`，workflow 改为调用它（行为逐字节不变，加测试锁定 `filter-v3@08540624` 在当前仓库文件上的值不变——如果实现时发现不一致，说明工作树里画像或模板有未提交改动，先停下报告，不要改测试期望值）。

**CI 侧**（新测试 `test/test_filter_certification.py`，不调用 LLM、不读 `output/`）：
1. 用仓库文件计算当前指纹（模板 + `research_profile.yaml` 的维度与 interests；`config/scholar.env` 不在仓库，经核查其中没有 `PROCESSING__RESEARCH_INTERESTS` 覆盖，所以仓库文件就是生产输入）。
2. 断言：存在一条 `passed=true` 且指纹相同的认证，其 `metrics` 按**当前** `thresholds.py` 门槛重新判定仍通过；**或**存在指纹相同且未过期的 waiver。
3. 结果：改 prompt/画像而不重新认证 → CI 红；放宽门槛 → 需要改 `thresholds.py`，在 diff 里可见；收紧门槛 → 旧认证可能不再满足 → CI 红，提示重新认证。
4. waiver 过期判断依赖当天日期，这条测试**有意**随时间变红。

**运行时**（`workflow._filter_by_llm` 收尾处，`_apply_filter_verdicts` 之后统一处置）：
- 判定每条裁决是否「已认证」：`(当前指纹, decision.provider, decision.served_model)` 命中某条 `passed=true` 的认证。served_model 为 None（拿不到自报 id）时视为未认证。
- 未认证且 `gate_mode=enforce` 时，对 EXCLUDE 按下面的方案处置（推荐 B，见第 11 节 Q2）：
  - **方案 A（全量）**：所有 EXCLUDE 改为 `verdict="undecided"`、保留 `decision="EXCLUDE"`、`gate="uncertified"`，进入 `undecided_segments` 待人工复核。按当前周量约 150 篇/周。
  - **方案 B（按置信度）**：只有 `confidence ≤ FILTER_UNCERTIFIED_HOLD_CONF_MAX`（0.7）或 confidence 缺失的 EXCLUDE 进待复核，其余照常排除但打 `gate="uncertified-pass"`。按 2521 条历史分布约 7.3%（约 11 篇/周）。H-stream 的分层抽检会告诉我们漏判是否集中在低置信段；若不集中，方案 B 就不成立，应退回 A。
  - **方案 C**：整步中止 digest。不推荐（一周断流的代价大于收益）。
- `gate_mode=warn`：不改判，只在日志、excluded sidecar（`gate_status` 顶层键）与 digest md 头部写一行「本次筛选配置未经召回认证」。
- **新增的待复核条目不影响 `ingest.py` 的现有约定**：`ingest.py:222-245` 本来就会跳过无裁决或未决的条目，不入库；它们的完整摘要都留在 excluded sidecar，认证完成后可以重放。
- `journal_screen.py` 通过 `ScholarWorkflow` 走同一条路径，自动受闸门约束；`ingest.classify_segments(force_include=True)` 是手选入库，闸门对它不起作用（被覆盖的原判照常写 `llm_decision_before_override`）。

**知识层 lint**（`src/scholar/lint_freshness.py` 增一个子项 `filter_cert`，遵守该模块「绝不抛异常、只走 stdout、不用 🚨」的纪律）：当前指纹未认证 → `🧭⚠ 筛选配置未认证`；认证依赖的 gold 版本早于 120 天且其后新增 manual 条目 ≥ 50 → `🧭 建议刷新金标准`。

### 5.8 何时必须重跑认证

认证的键 = `(prompt_fingerprint, provider, served_model, rag_mode, abstract_mode, batch_size)`，gold_version 只记录不进键。以下任一变化都使旧认证失效：
- 改 `whitelist_filter_prompt.md`、`research_profile.yaml` 的纳入/排除维度或 interests（指纹变）→ CI 红；
- 换主 provider 或模型、或别名背后的模型被厂商升级（served_model 变）→ 运行时识别为未认证；
- 回退链在某次运行中由未认证的下家应答 → 那几批的裁决按未认证处置；
- 改 `FILTER_BATCH_SIZE` → CI 红（新测试把它与认证记录里的 batch_size 对比）。
- **不触发**：gold 新增条目（记录为建议刷新，由 lint 提醒）；改 thresholds（由 CI 按新门槛重判旧指标）。

### 5.9 需改动/新增的文件

新增 `src/scholar/filter_eval.py`、`scripts/filter_eval.py`、`config/filter_eval/certification.json`（初始：`gate_mode=warn`、空认证列表）、`test/test_filter_eval_2026_10_13.py`、`test/test_filter_certification.py`；改动 `src/scholar/workflow.py`（指纹提纯、近邻自排除钩子、运行时闸门）、`src/scholar/thresholds.py`、`src/scholar/lint_freshness.py`、`scripts/lint_notes.py`（若需透传新子项）、`test/test_thresholds.py`（加接线断言）。

### 5.10 验收标准

1. 纯函数单测：Wilson 区间（对照教科书数值）、分层加权漏判率、配对回归比较、翻转率、dev/test 切分稳定性（同一 item_id 永远落在同一侧）。
2. `build-gold` 在 tmp 目录的构造数据上：剔除非论文 bib 条目、P1 与 manual 去重、登记表缺失时**拒绝**生成 gold（退出码非 0，提示先登记）。
3. 近邻自排除：假向量库里放入被测条目自身，开启钩子后近邻列表不含自身；关闭钩子（生产默认）行为与改动前一致。
4. 指纹提纯后 `ScholarWorkflow._get_filter_prompt_version()` 与纯函数结果相同。
5. 运行时闸门：`enforce` + 未认证 + 方案 B → 低置信 EXCLUDE 变 undecided 且 `gate="uncertified"`，高置信 EXCLUDE 照常排除；`warn` 下零改判；已认证时零改判。
6. CI 测试：指纹不匹配 → 失败且信息里打印「当前指纹 X，认证列表 [...]，请运行 scripts/filter_eval.py certify」；waiver 未过期 → 通过；过期 → 失败。
7. 首次基线（主会话执行，需用户先完成 manual 登记与 H-stream 盲标）：产出一份 `runs/` 记录与 `docs/decisions/filter_eval_baseline_<日期>.md`，后者列出各层指标、逐条被丢的 P1 论文、两条对照臂差异、翻转率。**用户据此确认 5.6 的门槛**，然后才把 `gate_mode` 改成 `enforce`。

### 5.11 不做

- 不在 CI 里调用 LLM，也不在 CI 里读任何 `output/` 数据。
- 不做自动调 prompt（评测只给数字，改 prompt 是人的决定）。
- 不对存量库做降级清理（沿用 08-08 用户决策「降级不做，只做升级」）。
- 不把 R-stable 或 promotion_scan 的 LLM 标签升格为金标准。

---

## 6. 项二：覆盖范围说明 + 证据不足时明确拒答

### 6.1 现状

- 概念页 9 页，每页 45–70 条证据、证据篇数 = 证据条数（一篇一条）；问答页 6 页，每页 28–36 条证据、16–21 篇。页面级篇数从来不少，**真正的单薄发生在论断级**：一条论断可能只挂一篇论文。
- 召回 0 条时 topics 返回 `skipped`、qa 不落盘（`is_archivable`，`qa.py:929`）；召回 1 条以上就进 LLM 合成。
- qa 页已有「本次召回没覆盖到的 … 不等于库里没有」（D9）；topics 页没有；两边都没有覆盖范围说明。
- 证据池只覆盖 1095/2745 篇（D10）。

### 6.2 目标

1. 每页（topic 与 qa）在生成块顶部印出**确定性生成**的「本页覆盖范围」，不经 LLM。
2. 页面级：独立来源数不足阈值时**不调用 LLM**，落一页「库内证据不足」存根（列出召回到的少量证据原句与覆盖说明，给出下一步建议）。
3. 论断级：只挂一篇独立来源的论断加「单一来源」徽标；qa 的 answer 所依据的 points 独立来源合计不足 2 篇时，answer 前加「库内证据不足」提示行。
4. 阈值显式、集中、可调，且有标定档案；过度回避与回避不足两个方向都要量化。

### 6.3 「独立来源」的定义

- 一条证据的来源 = 其 citekey 在索引中解析到的 **keeper**（`duplicate_of` 指向的那条；预印本与正式版若已被合并则天然算一篇）。
- 独立来源数 = 去重后的 keeper 数。
- 撤稿条目（`notes_index.is_retracted`）不计入（它们本来已被踢出向量库，这里是双保险）。
- **不做**按作者组/课题组合并（同一组的系列论文仍算多篇）。这会高估独立性，但合并规则需要作者消歧，成本高、误差大，列为后续（第 11 节 Q6）。

### 6.4 覆盖说明的内容（`src/scholar/coverage.py`）

```
@dataclass(frozen=True)
class CoverageStats:
    library_keepers: int          # keeper 条目总数（2745）
    library_include: int
    library_maybe: int
    evidence_pool_papers: int     # 有句级 highlight 的 keeper 数（1095）
    title_only_papers: int        # 只有题录/摘要、不进证据池的 keeper 数（1650）
    month_range: Tuple[str, str]  # 札记月份最早/最晚（取前 7 位）
    series_counts: Dict[str, int] # auto / manual / book
    filter_fingerprint: str       # 当前筛选 prompt 指纹
    filter_certified: Optional[bool]   # 读 certification.json；文件缺失/不可读 → None
    index_generated_at: str

def coverage_stats(index: dict, *, certification_path: Optional[Path]) -> CoverageStats
def page_evidence_profile(evidences, index) -> PageEvidenceProfile
    # n_independent、abstract_level_papers（证据来自 has_full_text_reading=False 的篇数）、
    # single_source_claims 等，供渲染与 frontmatter 用
def render_coverage_block(stats, profile, *, kind: Literal["topic","qa"]) -> List[str]
def render_insufficient_stub(...) -> List[str]
def claim_independent_sources(refs, ev_map, keeper_of) -> int
```

渲染文案（实现者可调措辞，但以下五点必须都在）：

> **本页覆盖范围**（截至 {index_generated_at}）
> - 本库是按研究画像筛选的方法学札记库（{library_keepers} 篇：INCLUDE {i} / MAYBE {m}；来源：Google Scholar 提醒 + arXiv/PubMed 抓取 {auto} 篇、手动精读 {manual} 篇、书籍 {book} 本；时间 {month_range}）。画像之外的主题**系统性缺席**。
> - 本页只能从有句级精读的 {evidence_pool_papers} 篇里取证；另有 {title_only_papers} 篇库内文献只有题录或摘要，不会出现在证据表里。
> - 本页证据来自 {n_independent} 篇独立来源，其中 {abstract_level_papers} 篇只读到摘要（付费墙或无全文）。
> - **库里没有 ≠ 文献里没有**。要确认「领域里有没有人做过」，请用 `scholar-search` 外部检索；追引用链时 OpenAlex 单路实测会漏约三分之一，须 Semantic Scholar + OpenAlex 双路。
> - 筛选配置 {filter_fingerprint}：{已通过召回认证（保留率 x，日期）| 尚未经召回认证}。

- topic 页插在 `> {spec.question}` 之后、summary 之前；qa 页插在「相关概念页」提示之后、审计提示之前。**两边都只是一行 `lines += coverage.render_coverage_block(...)`**，逻辑不得在 topics.py/qa.py 内各写一份。
- topics 的「证据缺口」一节标题下加一行与 qa 同义的免责：「这是本页这批证据没覆盖到的部分，不等于库里没有」。
- frontmatter 新增受管键：`coverage_pool_papers`、`coverage_title_only`、`n_independent_sources`、`abstract_level_papers`、`single_source_claims`、`evidence_status`（`ok` / `insufficient` / `forced`）、`filter_certified`。

### 6.5 证据不足的处置

| 层级 | 判据 | 处置 |
|---|---|---|
| 页面（topic） | 召回后、合成前：`n_independent < TOPIC_MIN_INDEPENDENT_SOURCES`（计数只算 `score ≥ EVIDENCE_SUFFICIENCY_MIN_SIM` 的证据） | 不调用 LLM；走 `merge_topic_page` 写存根页（保留用户批注区），`status="insufficient"`。**例外**：若已有页面的 frontmatter `n_independent_sources ≥` 阈值（以前够、现在不够），不覆盖，返回 `failed` 并提示「证据数倒退，先查向量库/阈值」——这多半是故障而不是真实状态 |
| 页面（qa） | 同上，阈值 `QA_MIN_INDEPENDENT_SOURCES` | 不调用 LLM，归档存根页（它本身就是一个有价值的答案：「问过了，库里不够」，下次 `find_similar_questions` 能命中，避免重复问）；`ask_notes.py` 退出码 0，并打印一行原因 |
| answer（qa） | `points` 引用的独立来源合计 `< QA_ANSWER_MIN_SOURCES` | answer 前加 `> ⚠️ 库内证据不足：下面的回答只依据 {n} 篇文献。` |
| 论断（topic 与 qa） | 该论断 refs 的独立来源 = 1 | 论断行末加 `［单一来源］` 徽标；不删除 |
| 强制 | `--force-synthesize`（两个脚本都加） | 跳过页面级闸门照常合成，`evidence_status="forced"`，覆盖说明里印「本页在证据不足时被强制生成」 |

存根页内容：标题与问题、覆盖说明、`## 库内证据不足` 一节（说明阈值与实际数、召回到的证据原句全部列出）、`## 下一步` 一节（固定文案：scholar-search 外检、S2+OpenAlex 双路追引用、`notes_search.py --mode dense --min-score 0.62` 查库内近邻）。证据表照常渲染。

`BUILD_TOPICS_EXIT_CODES` 不新增码：`insufficient` 不算失败；`summarize_build_topics_run` 的摘要里单列 insufficient 的页数与 slug。

### 6.6 阈值与标定（WO-4）

`src/scholar/thresholds.py` 新节「证据充分性」：

| 常量 | 初值 | 说明 |
|---|---|---|
| `EVIDENCE_SUFFICIENCY_MIN_SIM` | 待标定，初值取 `TOPICS_MIN_SIM`（0.55） | 只有不低于它的证据才计入独立来源 |
| `TOPIC_MIN_INDEPENDENT_SOURCES` | 3 | |
| `QA_MIN_INDEPENDENT_SOURCES` | 3 | |
| `QA_ANSWER_MIN_SOURCES` | 2 | |

标定方法（无 LLM、只用 Ollama embedding 与现有召回函数；脚本 `scripts/calibrate_evidence_sufficiency.py`，只打印与写 docs，不写 `output/`）：

- 正例（应当**不**拒答）：`config/topics.yaml` 的全部 topic spec（用 `retrieve_evidence` 生产默认参数）；6 个已归档 qa 问题（用 `recall_qa_evidence` 生产默认参数，参数从页面 frontmatter 的 `ask_*` 键读）；再由用户补 5 个「冷门但库里确实有」的问题（例如影子变量、collider 偏倚、Rubin 1976 可忽略性条件）。
- 反例（应当拒答）：`thresholds.py` 中 QA gap 标定用过的 8 条「库里绝对没有」的话题改写成问句（AlphaFold 置信度校准、量子退火加速比、中世纪欧洲农业史、NICU 喂养耐受性量表、机器人抓取样本效率，以及标定档案里的其余 3 条），再加 5 条「画像外但医学相关」的问题（如影像分割的数据增强）。
- 对每个问题，在 sim 下限 ∈ {0.55, 0.58, 0.60, 0.62, 0.65} × 来源阈值 ∈ {2, 3, 4, 5} 的网格上，计算：**误拒率**（正例被拒的比例，即过度回避）与**漏拒率**（反例没被拒的比例，即回避不足）。
- 选择规则（写进标定档案）：先要求误拒率 = 0（概念页和用户问过的问题被拒是严重回归），再在其中取漏拒率最低的组合；若不存在误拒率 = 0 的组合，停下交用户决定，不得自行放宽。
- 已知风险：QA gap 标定时反例在 0.55 下有 5/8 能召回到东西（句级 top1 最高 0.641），所以 0.55 + 3 篇很可能漏拒不少；这正是要标定的原因。
- 档案写到 `docs/decisions/evidence_sufficiency_calibration_<日期>.md`，常量注释里写摘要（遵循 `thresholds.py` 的铁律）。

### 6.7 需改动的文件

新增 `src/scholar/coverage.py`、`scripts/calibrate_evidence_sufficiency.py`；改动 `src/scholar/topics.py`（`render_generated_block`、`build_frontmatter`、`build_topic`、`synthesize_topic` 走 meta）、`src/scholar/qa.py`（`render_qa_block`、`build_qa_frontmatter`、`synthesize_qa` / `run_audit` 走 meta、存根归档）、`scripts/build_topics.py`、`scripts/ask_notes.py`（`--force-synthesize`、退出码说明）、`src/scholar/thresholds.py`、`test/test_thresholds.py`、`docs/scholar_notes_AGENTS.md`（解释新键与存根页）、`docs/skills/ask-notes/SKILL.md`（存根页与强制参数的用法，若该 skill 文件描述了退出码则同步）。**不改** `config/topics.yaml`（工作树里有别的会话的未提交改动）与两个合成 prompt。

### 6.8 验收标准

1. 镜像测试（新文件 `test/test_trust_coverage_2026_10_13.py`）：同一组假证据分别走 `topics.render_generated_block` 与 `qa.render_qa_block`，从两份输出中抽出覆盖说明块，**断言逐字相同**（kind 相关的那一句除外，用参数化处理）；单一来源徽标在两边都出现且位置规则一致。
2. 独立来源计数：dup 合并（两个 citekey 指向同一 keeper 计 1）、撤稿不计、低于 sim 下限不计。
3. 页面闸门：假召回只给 2 篇 → topic 与 qa 都**不调用 LLM**（FakeLLM 被调用即测试失败）且写出存根页，frontmatter `evidence_status=insufficient`；`--force-synthesize` 时调用 LLM 且 `evidence_status=forced`。
4. 倒退保护：已有页 `n_independent_sources=10`、本次只召回 2 篇 → 不覆盖，返回 failed。
5. 旧页兼容：`--verify`、`audit_topic_pages`、`audit_qa_pages`、vault 同步对 v1 topic 页与 v3 qa 页照常工作。
6. 标定（主会话执行）：档案落盘，正例误拒率 = 0，报告反例漏拒率；把定下来的常量值写回 `thresholds.py`。
7. 端到端（主会话执行、需要 LLM）：重建 1 个概念页与重问 1 个已归档问题，页面出现覆盖说明、frontmatter 新键齐全、`synth_model` 有值；再问 1 个反例问题，得到存根页且日志显示未调用合成。

### 6.9 不做

- 不改 topic/qa 的合成 prompt（改 prompt 会让 9 个概念页全部重合成，代价与本批收益不相称）。
- 不在页面层判断「库外文献里有没有」（那是 scholar-search 的职责，本页只给指路文案）。
- 不按作者组合并独立来源。

---

## 7. 两条备选的判断

### 7.1 证据类型标注（定理 / 模拟 / 真实数据 / 基准）——**本批不做，排进下一批**

- 理由一：ask_notes v3 的 A2 已能识别理论类（`qa._THEORY_KEYWORD_RE`），但「模拟」「真实数据」「基准」在中文精读句里的措辞变化大（「在 MIMIC-IV 上」「合成数据实验」「半合成」），纯正则会有可观的误判，需要先标一批（约 200 句）测精度，否则又是一个没标定的分类器。
- 理由二：它依赖本批的「独立来源」口径才有意义（强度 = 独立来源数 × 证据类型）。
- 建议下批做法：确定性正则初分 + 人工抽检 200 句标定 + 只作为证据表里的标签显示，不进任何门槛。

### 7.2 「证据强度」与「转述忠实度」分开显示——**本批顺带做最小版本**

- 本批的「单一来源」徽标与覆盖说明已经是「强度」轴（多少独立来源支撑），ask_notes v3 的 ❌/❔ 审计标记是「忠实度」轴（转述是否忠于原句）。两者用**不同的符号与位置**呈现（强度：论断行末方括号徽标；忠实度：行首 ❌ 与下方审计行），本批在 AGENTS 文档里写清两轴的含义即可，不再另造显示层。
- 不做的部分：把两轴合成一个「可信度分」——合成分会把两种性质完全不同的问题混在一起，正是要避免的。

### 7.3 顺带发现、建议另开的工单（不在本批）

- **WO-X 用自排除重跑 2026-08-08 升级扫描**：D4 表明 1215 条旧 MAYBE 中至少 247 条因「和自己重复」被压在 MAYBE。WO-7 完成后，用带自排除的执行器只重跑这 247+ 条（约 13 批调用），升级候选交用户确认后按三处同步规则写回。

---

## 8. 阈值放在哪里

**推荐：全部放 `src/scholar/thresholds.py`，分两个新节（「证据充分性」与「筛选放行门槛」），并把模块 docstring 的范围从「检索阈值」扩成「检索与放行阈值」。**

- 理由：该文件的铁律（标定值、每个常量注释即标定档案摘要、不做 config/env 化以防被静默覆盖）正好是这两类门槛需要的；`test/test_thresholds.py` 的接线测试模式可以直接复用（断言 coverage.py、filter_eval.py、workflow.py 引用的是常量而不是字面量）。
- 不放 `config/*.yaml`：yaml 改动不会触发 CI 的认证重判，而且会诱导「临时调低一下」。
- `gate_mode`（warn/enforce）不是阈值而是放行策略，放 `certification.json`，与认证记录一起版本化。
- 备选：新建 `src/scholar/gates.py` 专放放行门槛（若用户希望 thresholds.py 保持「只有余弦阈值」的纯度）。见第 11 节 Q4。

---

## 9. 工单

通用约定（每个工单都适用）：
- 开工先跑 `pytest test/ -q` 记录基线通过数；完工时通过数 ≥ 基线 + 新增测试数，零失败。
- 新测试文件放 `test/`，命名带日期后缀（沿用 `test_ask_notes_v3_2026_10_04.py` 的习惯）；**不修改既有测试的断言**，除非本文明确要求。
- 测试一律用假 LLM / 假 embedding / tmp 目录，不联网、不调用真实 LLM、不读写真实 `output/`。
- 只改工单列出的文件；需要改清单外文件时停下报告。禁止任何 git 写操作（由主会话统一验收与提交）。
- 「主会话执行」标注的步骤需要真实 LLM、Ollama 或用户参与，子代理不做。

### WO-1 LLM 调用元数据
- **输入**：本文 4.3.1。
- **输出**：`CallMeta`、`LLMClient.call_with_meta`；`call()` 行为不变。
- **改动文件**：`src/scholar/llm_client.py`；新测试 `test/test_llm_call_meta_2026_10_13.py`。
- **测试**：4.5 第 1、2 条；各 provider 分支 served_model 解析（用固定响应样本，claude-agent 信封样本须是真实结构，见 4.3.1）；降级半截正文时 `degraded_partial=True`；`call()` 的既有测试（`test_llm_client_switch.py` 等）全绿。
- **完成定义**：上述测试通过；在代码注释里写明每个 provider 的 served_model 取自哪个字段、拿不到时的退化行为。

### WO-2 札记来源字段（依赖 WO-1）
- **输入**：4.3.2、4.3.3；D5、D12。
- **输出**：schema 新字段、各写入点、索引新键；`manual_pick` 进 Literal；`llm_decision_before_override`。
- **改动文件**：`src/scholar/schema.py`、`src/scholar/workflow.py`（仅筛选调用与裁决构造处）、`src/scholar/closereading.py`、`src/scholar/pdf_ingest.py`、`src/scholar/ingest.py`、`src/scholar/_citekey_utils.py`、`docs/scholar_notes_AGENTS.md`；新测试 `test/test_provenance_fields_2026_10_13.py`。
- **测试**：4.5 第 3、4 条；manual bundle 复原后 `final_by` 与脚本模型都在；`classify_segments` 覆盖分支不再抛错且保留原判；改 citekey 的流程保留新键（构造 sidecar → 调 `_rename_citekey_in_note` → 断言键仍在）。
- **完成定义**：测试通过；老 digest JSON / 老 bundle 反序列化测试仍通过；AGENTS 文档新增字段表与「缺失 = 未知」说明。

### WO-3 存量回填与分组报表（依赖 WO-2）
- **输入**：4.3.4。
- **输出**：`scripts/backfill_provenance.py`（默认 dry-run，只写 overlay）、overlay 合并逻辑、`scripts/provenance_report.py`。
- **改动文件**：`src/scholar/notes_index.py`（overlay 合并，一个函数 + 一个调用点）、两个新脚本；新测试 `test/test_provenance_backfill_2026_10_13.py`。
- **测试**：4.5 第 5 条；回填规则 1 的「多份 digest 取最晚、decision 不一致不回填」；规则 2 的升级条目识别；overlay 文件缺失或损坏时索引照常构建（只告警）。
- **完成定义**：测试通过；主会话在真实库上跑 dry-run，把 4.5 第 6、7 条的数字贴进完成说明；用户确认后主会话再去掉 `--dry-run` 真写 overlay 并重建索引。

### WO-4 证据充分性标定（无 LLM，可与 WO-1 并行）
- **输入**：6.3、6.6。
- **输出**：`scripts/calibrate_evidence_sufficiency.py`；独立来源计数的纯函数（放进新建的 `src/scholar/coverage.py`，WO-5 继续扩充该文件）。
- **改动文件**：新建 `src/scholar/coverage.py`（仅 `claim_independent_sources`、`keeper_map(index)`、`count_independent(evidences, keeper_of, min_sim)` 等计数函数）、新脚本；新测试 `test/test_evidence_sufficiency_2026_10_13.py`。
- **测试**：6.8 第 2 条；网格计算在假召回结果上的误拒/漏拒率正确。
- **完成定义**：测试通过；主会话跑标定（需 Ollama），写出 `docs/decisions/evidence_sufficiency_calibration_<日期>.md`；用户补的 5 个冷门正例问题在档案里列明。

### WO-5 覆盖说明与证据不足拒答（依赖 WO-1、WO-4 的标定结论）
- **输入**：第 6 节全部；4.3.5。
- **输出**：`coverage.py` 补齐统计与渲染；topics/qa 两边各一个调用点；存根页；frontmatter 新键；`--force-synthesize`；页面 synth/audit 模型记录；thresholds 新节。
- **改动文件**：`src/scholar/coverage.py`、`src/scholar/topics.py`、`src/scholar/qa.py`、`scripts/build_topics.py`、`scripts/ask_notes.py`、`src/scholar/thresholds.py`、`test/test_thresholds.py`（只追加接线断言）、`docs/scholar_notes_AGENTS.md`、`docs/skills/ask-notes/SKILL.md`；新测试 `test/test_trust_coverage_2026_10_13.py`。
- **测试**：6.8 第 1–5 条；`certification.json` 缺失时覆盖说明显示「尚未经召回认证」而不是报错（WO-8 之前它就不存在）。
- **完成定义**：测试通过；主会话完成 6.8 第 7 条端到端验收。

### WO-6 金标准构造（无 LLM，可与 WO-1 并行）
- **输入**：5.3、5.4。
- **输出**：`src/scholar/filter_eval.py`（gold 构造、分层、切分、bib 解析、登记表导入导出、盲标表导入导出、指标纯函数）；`scripts/filter_eval.py` 的 `registry-export`、`registry-import`、`build-gold`、`label-export`、`label-import`、`stats` 子命令。
- **改动文件**：两个新文件；新测试 `test/test_filter_eval_2026_10_13.py`。
- **测试**：5.10 第 1、2 条；盲标表不含任何 LLM 判定字段（断言列名白名单）；H-stream 抽样按置信度分层且记录抽样权重；同一输入两次构造得到同一 sha256。
- **完成定义**：测试通过；主会话在真实数据上导出批次登记表与盲标表交给用户；用户填完后 `build-gold` 产出 `gold_v1.*`，`stats` 打印各层规模。库外 P1 摘要抓取是联网操作，由主会话执行并缓存。

### WO-7 评测执行器（依赖 WO-1、WO-2、WO-6）
- **输入**：5.5。
- **输出**：`scripts/filter_eval.py run/report`；workflow 近邻自排除钩子。
- **改动文件**：`src/scholar/filter_eval.py`、`scripts/filter_eval.py`、`src/scholar/workflow.py`（仅钩子）；测试追加到 `test/test_filter_eval_2026_10_13.py`。
- **测试**：5.10 第 3 条；假 LLM 下的完整 run：批次混洗、两次重复取更差、多 served_model 时拆分统计并标记「不可用于认证」、`--smoke` 抽样固定。
- **完成定义**：测试通过；主会话先跑 `--smoke` 确认链路，再跑基线（5.10 第 7 条）。

### WO-8 放行闸门（依赖 WO-7 与用户确认的基线门槛）
- **输入**：5.6–5.8。
- **输出**：指纹纯函数、`certify` 子命令、`certification.json`、CI 测试、运行时闸门、lint 子项、thresholds 新节。
- **改动文件**：`src/scholar/filter_eval.py`、`scripts/filter_eval.py`、`src/scholar/workflow.py`（指纹调用 + 闸门处置）、`config/filter_eval/certification.json`、`src/scholar/thresholds.py`、`test/test_thresholds.py`（只追加）、`src/scholar/lint_freshness.py`、`scripts/lint_notes.py`（如需）；新测试 `test/test_filter_certification.py` 与闸门行为测试（追加到 `test_filter_eval_2026_10_13.py`）。
- **测试**：5.10 第 4–6 条；lint 子项遵守 `lint_freshness` 输出纪律（复用 `test_lint_freshness.py` 的同类断言）。
- **完成定义**：测试与 CI 通过（初始 `certification.json` 必须包含当前指纹的认证或 waiver，否则 CI 一合入就红——主会话在合入前用基线运行结果执行 `certify`）；用户确认后把 `gate_mode` 改为 `enforce`。

---

## 10. 阶段划分与先后顺序

| 阶段 | 工单 | 人/机 | 产出检查点 |
|---|---|---|---|
| 阶段 1（并行） | WO-1、WO-4、WO-6 | 子代理 | 三个工单测试全绿；主会话核对每个工单的改动文件清单没有越界 |
| 阶段 1′（人） | 用户做 manual 批次登记（约 15 分钟）、H-stream 盲标（170 条，约 1.5 小时）、补 5 个冷门正例问题 | 用户 | `registry_*`、`labels_*` 回收 |
| 阶段 2 | WO-2（依赖 WO-1）→ WO-7（依赖 WO-2、WO-6）；WO-5（依赖 WO-1、WO-4 标定） | 子代理 + 主会话 | WO-4 标定档案；WO-7 冒烟通过 |
| 阶段 3 | WO-3（依赖 WO-2）；基线运行；WO-8 | 主会话 + 用户 | 基线文档、门槛确认、首次认证、`gate_mode` 切换 |

阶段 2 中 WO-2 与 WO-7 都改 `workflow.py`，必须串行；WO-5 与它们无共同文件，可以并行。

---

## 11. 风险与开放问题（需要用户拍板）

| # | 问题 | 选项 | 推荐 |
|---|---|---|---|
| **Q1** | 金标准与盲标表存哪里？ | (a) `output/scholar_notes/_eval/filter/`，不进 git、随 iCloud 备份；(b) 进 git | **(a)**。稿件引用清单属于未发表工作的信息，分支是推送到远端的；进 git 的只有不含标题的 `certification.json` |
| **Q2** | 未认证期间 fail-closed 的力度 | A 全部 EXCLUDE 转待复核（约 150 篇/周）；B 只有置信度 ≤0.7 的转待复核（约 11 篇/周）；C 中止 digest | **B**，但以 H-stream 抽检结果为前提：若人判「应收」的 EXCLUDE 并不集中在低置信段，改用 A |
| **Q3** | 门槛初值（5.6 表） | 照表 / 基线后再定 | **基线后再定**：表中数字作为基线运行的对照，基线若低于下限，先改 prompt/画像，而不是先降门槛 |
| **Q4** | 阈值文件 | thresholds.py 新增两节 / 新建 gates.py | **thresholds.py**（第 8 节理由） |
| **Q5** | 评测主口径用「生产截断 400 字符」还是「完整 800 字符」 | prod / full | **prod 作为认证口径**，full 作对照臂；理由见 D11，取保守口径 |
| **Q6** | 独立来源是否按作者组合并 | 合并 / 不合并 | **本批不合并**，在覆盖说明里不声称「独立课题组」；下批随证据类型标注一起评估 |
| **Q7** | 存量 auto 精读模型是否按时间线推测 | 推测并标 inferred / 记未知 | **记未知**。回退链会在任意时刻换人，推测值一旦进分组报表就会产生看似有据的错误结论 |
| **Q8** | 页面级拒答阈值（3 篇） | 视标定结果 | **以标定为准**，选择规则是「正例误拒为 0 的前提下漏拒最少」；如果做不到误拒为 0，宁可放过反例（回避不足，靠覆盖说明兜底），也不误拒概念页 |
| **Q9** | H-stream 盲标的规模 | 170 / 100 | **170**。少于 100 条时 EXCLUDE 样本的漏判率区间太宽（3% 点估计在 n=100 时 Wilson 上界约 8.5%），门槛判不出来 |
| **Q10** | 是否另开 WO-X 重跑 08-08 升级扫描（7.3） | 做 / 不做 | **做**，排在 WO-7 之后；成本约 13 批调用 |

**其他风险**：
- claude-agent 信封里能否稳定拿到实际模型 id，需要 WO-1 用真实样本确认；拿不到时 served_model 为 None，闸门会把所有裁决视为未认证。缓解：认证记录允许 `served_model` 填「requested alias + CLI 版本」作为弱键，但必须由用户明确同意，并在 `certification.json` 里标 `weak_key: true`。
- 翻转率可能很高（D15 的两轮一致率只有 73%）。若基线翻转率超过 0.15，认证的数字本身不稳定，应先考虑把 `FILTER_BATCH_SIZE` 降到 10（记忆中 deepseek flash 批 10 明显更稳），这会改变认证键，需重跑。
- `waiver` 的日期判断让 CI 测试随时间变化，可能在无人改代码时变红；这是有意的提醒，不是缺陷。
- 存根页会让少数冷门概念页从「有内容」变成「证据不足」——倒退保护只拦住「以前够、现在不够」的情况，新建的冷门概念页会直接得到存根，这符合设计意图，但用户需要知道。

---

## 附录 A：本文数字的核查口径（全部只读）

- 索引：`output/scholar_notes/literature_index.json`（generated_at 2026-10-13T13:05:22），`papers` 2889 条；keeper（`duplicate_of` 为空）2745 条。
- 证据池：`embeddings.sqlite3` 只读查询 `select level, count(distinct citekey) from chunks group by level`，highlight 层 1095 个 citekey（has_full_text=1 的 1028 + 0 的 67）。
- 筛选历史：`output/scholar_digest/digest_*.json`（108 份、7680 段）的 `segments[].filter_decision.model/prompt_version`；`output/scholar_digest/_archive/excluded/*.json`（94 份）中 `stage=llm_judge` 且 `prompt_version` 以 `filter-v3` 开头的 2521 条及其 `confidence`。
- 自身泄漏：`promotion_scan_20260808.json` 中 `new_one_line` 同时含「重复」与被测条目 citekey 的作者年份前缀的条目数（下界，措辞不同的漏计）。
- manual：`output/scholar_notes/manual/*/*.paper.json` 483 个 bundle 的 `segment.filter_decision`、`close_reading_script.model`、`close_reading_final.model`、`cross_check_report.reviewer_label`。
- 稿件引用：两份 bib 按 citekey → DOI → 规范化标题依次匹配索引。
- 摘要长度：最近 6 份 excluded sidecar 的 `original_abstract` 长度（1233 条）；`abstracts.json` 1798 条。

---

## 12. 修订（2026-10-21）：WO-8 由「放行闸门」改为「入库确认 + 持续标注 + 漂移提醒」

**起因**：基线评测（run_20261021_125541）与新画像复跑（run_20261021_163254）之后，用户指出：(1) 自己的标注也不是金标准；(2) 研究兴趣在漂移，固定门槛会过时并挡住新方向。决定：**不写死放行门槛，改为每次入库都弹窗请用户确认**，确认结果即标签。原第 5.6/9 节 WO-8（certification.json、fail-closed、运行时未认证 EXCLUDE 转待复核）作废。

用户已拍板（2026-10-21）：

| 问题 | 决定 |
|---|---|
| 弹窗列哪些 | 筛选器判 INCLUDE/MAYBE 的（默认勾选）+ 置信 ≤0.85 的 EXCLUDE（默认不勾）+ 随机 3–5 条高置信 EXCLUDE 抽查（默认不勾） |
| 无人应答 | 挂起：不入库、不精读，进待确认队列；系统通知提醒；用户方便时一条命令再弹 |
| 漂移提醒 | 每月一次，只在分歧成簇时提 |

### 12.1 WO-8a 入库确认弹窗

- **插入点**：weekly digest（筛选完成）之后、weekly ingest / 精读之前。实现者先读 `scripts/run_weekly_digest.sh`、`scholar_main.py digest`、`scripts/ingest_notes.py` 与两个 launchd plist（`com.xlbd.scholar-digest` 09:00、`com.xlbd.scholar-weekly-ingest` 09:30），确认现有流程中精读发生在哪一步，把确认点放在**任何入库/精读之前**。
- **待确认队列**：digest 结束写 `output/scholar_notes/_pending/confirm_<run_date>.json`（每条：dedup_key、标题、来源、筛选器 decision/confidence/bucket/exclude_reason/one_line、抽样类别 include|maybe|lowconf_exclude|audit、served_model、prompt 指纹）。ingest 与精读只处理**已确认**条目；未确认的保持挂起，下次 ingest 再看。
- **弹窗**：macOS 原生 `osascript` 的 `choose from list ... with multiple selections allowed`，`default items` = 筛选器想收的。每项显示 `[类别] 标题（截 110 字）`。另提供一个「看摘要」按钮/入口：生成一份 md（标题 + 中文 one_line + 摘要前 400 字）用系统默认程序打开；**不做 web/TUI**（用户明确不要）。条目过多时分页（每页 ≤40）。
- **入口**：`python scripts/confirm_ingest.py [--date ...]`。launchd 跑完 digest 后先发 `display notification`；若检测到 GUI 会话可用则直接弹窗，`giving up after` 超时（如 10 分钟）即视为无人应答 → 挂起。挂起时每天提醒一次（可复用现有 launchd 或在 ingest job 里检查），直到确认。
- **确认结果落盘**：`output/scholar_notes/_eval/filter/stream_labels.jsonl` 追加，每条含 confirmed_at、user_decision（keep/drop）、筛选器原判与置信、抽样类别、指纹、served_model。用户勾上筛选器原判 EXCLUDE 的 → 视同手选覆盖（复用 WO-2 的 `llm_decision_before_override` / `stage=manual_pick` 语义）。
- **兼容**：`--no-confirm`（或环境变量）跳过确认，按旧行为直接入库，供补跑/回填/测试使用；回填与 `manual` 系列不走确认。

### 12.2 WO-8b 持续评测（只报告，不拦截）

- `filter_eval.py` 增加把 `stream_labels.jsonl` 并入金标准新层 `S-stream` 的能力（按确认日期滚动，可设窗口，默认近 12 周）；抽样类别里的 lowconf_exclude/audit 带回加权，用于还原漏判率。
- 改 prompt / 画像 / 换模型后照常 `run`，`report --vs <上一版>` 出配对对比；**不阻断**。CI 不调 LLM，只在当前指纹没有任何评测记录时打一条告警（不失败）。

### 12.3 WO-8c 月度漂移提醒

- 新脚本 `scripts/profile_drift.py`：读近 4 周 `stream_labels.jsonl`，统计
  - 用户补勾（筛选器 EXCLUDE → 用户 keep）按 exclude_reason、按向量库语义聚类成簇；
  - 用户取消（筛选器 INCLUDE/MAYBE → 用户 drop）按 bucket 聚类成簇。
- 只有成簇（同一原因/簇 ≥ 阈值，阈值进 thresholds.py，初值 5 篇且占该类 ≥ 50%）才提醒：`display notification` + 生成 `output/scholar_notes/_eval/filter/drift_<YYYY-MM>.md`，列出簇内论文与建议的画像改动文字（不自动改画像）。
- 挂到现有每月 1 日的 launchd（`com.xlbd.scholar-monthly-backfill`）之后跑，或新增 job；实现者择一并说明。

### 12.4 不做

- 不自动改画像；不设固定放行门槛；不为确认 UI 做网页或终端界面。

### 12.5 补丁（2026-10-21 真机测试后）

用户首次真机测试「点错了」（159 条 33 秒内按默认按钮连续提交）。改为：各页「确认本页」只暂存（草稿 `_pending/_draft.json`，不算确认）；全部点完后弹汇总核对框（默认按钮「返回修改」），「确认提交」后才落盘与放行入库；任一页取消/超时则整场不生效（取消原 partial 语义，上文 12.1「每页确认即落盘」作废）。相对默认未改动的条目在 stream_labels 标 `user_decision_source=default_accepted`，S-stream 与漂移分析默认不计。新增 `confirm_ingest.py --reopen YYYY-MM-DD`（未入库才可重开，写撤销行）。修正 confirmed_at 记成会话开始时刻的 bug，另记 committed_at。
