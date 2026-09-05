# 额度耗尽被记成 `no_output`：35 篇「全文已到手但没读成」混进了待下载清单

日期：2026-09-04
状态：**已修复（2026-09-04 台账批，见文末「修复」）；存量 35 篇已全部处置完毕**——32 篇随第 3 段 expand 自愈，剩 3 篇于 2026-09-05 用 `--redo` 重跑，成功 3 / 失败 0。⚠️ 全库**同型陈旧条目还剩 4 篇**（`liao2021Improving` / `li2023Prediction` / `jin2024Transferb` / `yoo2025Review`），要救回同法加 `--citekey`，见文末「后续」
严重度：中——不丢数据，但让账本失去区分力：真抓不到全文与额度挂掉长得一模一样
发现者：xiaolibird / Claude（2026-09-04 INCLUDE+mid 批次）

---

## 位置

- `src/scholar/closereading.py:248` `close_read()`——LLM 调用失败时 **catch 后打 warning 返回 None**，不向上抛
- `scripts/backfill_deepread.py:cmd_run`——只看 `done`/`cr` 是否为空，空就一律记 `reason: no_output`
- 我在同一处加的「按失败路径分流熔断」（`if not (expand and err is None)`）**在这里失灵**：额度耗尽走的是 catch 分支、`err` 恒为 None，被判成「干净的抓不到全文」不计连败

## 报错情况

```
⚠️ 通读块 3/8 失败: 429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message':
  'You exceeded your current quota ... limit: 20, model: gemini-3.5-flash' ...}}
⚠️ 精读 LLM 调用失败(255): 429 RESOURCE_EXHAUSTED ...
   ❌ 重读未产出，跳过（不写盘）
```

日志里看得见真因，**账本里看不见**——落盘的只有 `"reason": "no_output"`，与「Unpaywall/EPMC 都没有这篇」完全同形。

## 当天的回退链走位

| 时段 | provider | 事件 |
|---|---|---|
| 09:49–11:45 | claude-agent (sonnet) | 正常 |
| 11:45 起 | → deepseek | `You've hit your session limit · resets 1:10pm`，切换 58 次 |
| 傍晚 | → gemini | 切换 24 次 |
| 18:01 起 | gemini 也挂 | 免费层 20 次/天耗尽，88 次 `RESOURCE_EXHAUSTED` |

260 篇拆开：成功 69 / 真抓不到全文 156 / **拿到全文但 LLM 挂掉 35**。
那 35 篇的 PDF 或 OA 全文当时都已在手，纯粹是没模型可用。

## 影响面

1. 这 35 篇被写进 `backfill_expand_progress.json` 的 failed，而 expand 模式**重跑会跳过 failed**（那条设计本身是对的：抓不到全文是确定性的）。所以它们不会自动被重试，会安静地留在待下载清单里等一个根本不需要的 PDF。
2. 熔断失效：额度耗尽本该立刻停批（这正是熔断存在的理由），实际一路跑到底，把 65 篇烧成失败。

## 建议修法

让 `close_read()` 把「模型侧失败」与「没有正文可读」区分开——最小改动是让它返回/透出一个失败原因，或对配额类错误改为向上抛，由 `cmd_run` 记成 `reason: llm_unavailable` 并**计入连败**（这类正是熔断该拦的）。

配套：`--only-failed` 应支持按 reason 过滤，或让 `llm_unavailable` 不在「重跑跳过 failed」的跳过集里——它不是确定性失败。

## 受影响的 35 篇（额度恢复后直接重跑即可，不需要下载）

```
PYTHONPATH=. python scripts/backfill_deepread.py run --expand --apply \
    --citekey lee2023Selfsupervised \
    --citekey kapur2022Understanding \
    --citekey arpanahi2022Predicting \
    --citekey zhang2023Adversarial \
    --citekey ding2023Crosscenter \
    --citekey zhou2023Crossinstitutional \
    --citekey zhuang2024Normalization \
    --citekey lippl2025Fact \
    --citekey zhou2026Representation \
    --citekey kim2025Clientcentered \
    --citekey zhu2025Fedweight \
    --citekey amstel2025Clinical \
    --citekey chen2025Effective \
    --citekey stringer2025Three \
    --citekey ehrig2025Imputation \
    --citekey zhu2025Causal \
    --citekey lotspeich2026Large \
    --citekey ho2025Early \
    --citekey li2025Unsupervised \
    --citekey zhao2025Multimodal \
    --citekey deng2026Statisticalneural \
    --citekey li2026Postoperative \
    --citekey lian2026Subtyping \
    --citekey desman2026Contrastive \
    --citekey cao2026Integrativec \
    --citekey panizza2026Physical \
    --citekey gelbach2008Heart \
    --citekey zhang2026Localized \
    --citekey cui2026Agentgfm \
    --citekey helms2026Granulomonocytapheresis \
    --citekey lu2026Construction \
    --citekey wang2026Effects \
    --citekey farouji2026Outcomes \
    --citekey hong202624hour \
    --citekey czarnecki2026Large
```

注：`run` 会先扣掉账本已完成的；这 35 篇现在挂在 failed 里，`--citekey` 指定时不受「跳过 failed」影响。跑之前先把它们从 failed 摘掉更稳妥。

| # | citekey | 失败原因 | 标题 |
|---|---|---|---|
| 60 | `lee2023Selfsupervised` | LLM调用失败 | Self-supervised predictive coding and multimodal fusion advanc |
| 62 | `kapur2022Understanding` | LLM调用失败 | Understanding the chronic kidney disease landscape using patie |
| 67 | `arpanahi2022Predicting` | LLM调用失败 | Predicting Risk of Mortality in COVID-19 Hospitalized Patients |
| 77 | `zhang2023Adversarial` | LLM调用失败 | Adversarial Style Augmentation for Domain Generalization |
| 79 | `ding2023Crosscenter` | LLM调用失败 | Cross-center Early Sepsis Recognition by Medical Knowledge Gui |
| 82 | `zhou2023Crossinstitutional` | LLM调用失败 | A Cross-institutional Evaluation on Breast Cancer Phenotyping  |
| 90 | `zhuang2024Normalization` | LLM调用失败 | Is Normalization Indispensable for Multi-domain Federated Lear |
| 94 | `lippl2025Fact` | LLM调用失败 | FACT: Federated Adversarial Cross Training |
| 162 | `zhou2026Representation` | LLM调用失败 | Representation Learning to Advance Multi-institutional Studies |
| 163 | `kim2025Clientcentered` | LLM调用失败 | Client-Centered Federated Learning for Heterogeneous EHRs: Use |
| 164 | `zhu2025Fedweight` | LLM调用失败 | FedWeight: Mitigating Covariate Shift of Federated Learning on |
| 165 | `amstel2025Clinical` | LLM调用失败 | Clinical subtypes in critically ill patients with sepsis: vali |
| 167 | `chen2025Effective` | LLM调用失败 | Effective Non-IID Degree Estimation for Robust Federated Learn |
| 169 | `stringer2025Three` | LLM调用失败 | Three hospitalized non-critical COVID-19 subphenotypes and cha |
| 170 | `ehrig2025Imputation` | LLM调用失败 | Imputation and Missing Indicators for Handling Missing Longitu |
| 197 | `zhu2025Causal` | gemini额度耗尽 | Causal Debiasing Medical Multimodal Representation Learning wi |
| 200 | `lotspeich2026Large` | gemini额度耗尽 | On Using Large Language Models to Enhance Clinically-Driven Mi |
| 204 | `ho2025Early` | gemini额度耗尽 | Early prediction of ADHD symptoms from perinatal characteristi |
| 206 | `li2025Unsupervised` | gemini额度耗尽 | Unsupervised clustering based on a graph attention network rev |
| 212 | `zhao2025Multimodal` | gemini额度耗尽 | A multimodal synergistic model for personalized neoadjuvant im |
| 216 | `deng2026Statisticalneural` | gemini额度耗尽 | Statistical-Neural Interaction Networks for Interpretable Mixe |
| 217 | `li2026Postoperative` | gemini额度耗尽 | Postoperative red cell distribution width to platelet ratio is |
| 218 | `lian2026Subtyping` | gemini额度耗尽 | Subtyping Alzheimer's disease and Parkinson's disease using lo |
| 220 | `desman2026Contrastive` | gemini额度耗尽 | Contrastive Transformer-Driven Discovery of Temporal Hemodynam |
| 221 | `cao2026Integrativec` | gemini额度耗尽 | Integrative and interpretable machine learning framework for e |
| 226 | `panizza2026Physical` | gemini额度耗尽 | Physical Activity Behavior and Acute Myocardial Infarction, St |
| 227 | `gelbach2008Heart` | gemini额度耗尽 | Heart Failure sub-phenotyping and in-hospital and 28-day morta |
| 231 | `zhang2026Localized` | gemini额度耗尽 | Localized surface plasmon resonance-based point-of-care testin |
| 232 | `cui2026Agentgfm` | gemini额度耗尽 | AgentGFM: A Graph Foundation Model with Node-Agent Information |
| 236 | `helms2026Granulomonocytapheresis` | gemini额度耗尽 | Granulomonocytapheresis for Sepsis: Mechanistic Rationale, The |
| 238 | `lu2026Construction` | gemini额度耗尽 | Construction and validation of a sepsis prediction model using |
| 240 | `wang2026Effects` | gemini额度耗尽 | Effects of early continuous renal replacement therapy in criti |
| 243 | `farouji2026Outcomes` | gemini额度耗尽 | Outcomes Associated With Continuous Renal Replacement Therapy  |
| 247 | `hong202624hour` | gemini额度耗尽 | A 24-hour landmark machine learning model for predicting new-o |
| 255 | `czarnecki2026Large` | gemini额度耗尽 | Why Large Language Models Fail at Tabular Prediction |

---

## 修复（2026-09-04 台账批）

按本条建议修法落地：让精读链路**说出失败原因**，账本据此把「模型侧挂了」与「真没正文可读」分开。

- **判据**：`closereading.is_llm_unavailable(exc)` —— 复用 `pdf_ingest.is_credit_error`（401/402/403/quota
  等额度鉴权终局，整词匹配防误触）**再补限流一档**：`429` / `RESOURCE_EXHAUSTED` / `rate limit` /
  `session limit` / `overloaded`。gemini 免费层耗尽回的正是前者，claude-agent 会话上限回的是后者。
- **失败原因出参**：`close_read(..., diag=None)` / `deep_close_read(..., diag=None)` /
  `close_read_segment(..., diag=None)` / `close_read_segments(..., diag=None)`（后者按 `paper_id` 分键）。
  记 `no_body`（既无全文也无摘要）、`llm_error` + `llm_unavailable`、`parse_failed`，外加
  `from_full_text`（这次到底拿没拿到全文）。不传 `diag` 时行为与历史逐字节一致。
- **账本分流**：`backfill_deepread.classify_failure(cr_diag, err) -> (reason, 计不计连败)`——
  `llm_unavailable:<原因>` / `error:<异常类型>` / `no_output`，条目另存 `had_fulltext`。
  回执上多打一行 `⛔ 模型侧不可用（额度/限流/鉴权），**不是抓不到全文**`。
- **熔断**：`llm_unavailable` **一律计入连败**——熔断存在的全部理由就是拦它。此前 `expand and err is None`
  那条分流恰好把它漏掉（额度耗尽走 catch 分支、`err` 恒为 None），于是一路跑到底烧了 65 篇。
- **重跑不再跳过**：`deterministic_failures(led)` 只把**确定性**失败放进 expand 批的跳过集；
  `llm_unavailable` 不在其中，额度恢复后原样重跑即可（不必 `--citekey` 一个个点名）。
- 测试：`test/test_backfill_deepread.py` 末节 6 组（判据 9 例参数化、三种 diag 形态、
  `close_read_segments` 分键、`classify_failure` 四态、`deterministic_failures` 排除律）。

**存量那 35 篇**：账本里它们的 reason 仍是旧的 `no_output`（本次改的是往后的记账口径，没有回写历史账本）。
额度恢复后按文末命令 `--citekey` 点名重跑即可；或先把它们从 `backfill_expand_progress.json` 的 failed 里摘掉。

---

## 后续（2026-09-05 02:39）：那 35 篇已自愈 32 篇

链条第 3 段 `run --expand --decision INCLUDE --tier mid --only-failed --pdf-dir … --apply`
在额度恢复后跑（23:00→02:39），把 192 篇 mid 失败篇整体重试了一轮，**成功 45 篇**，
其中就包含本文清单里的 **32 篇**。仍未读成的只剩三篇：

`zhu2025Fedweight` / `zhao2025Multimodal` / `hong202624hour`

**上面那张 35 篇的表已过时**，保留是为了留下当时的证据；真正待处理的只有这三篇。

需要说明的是这属于**侥幸**：第 3 段本是为「补下了 PDF 的篇目」设计的，恰好把额度挂掉的
一起捞了。换个跑法（比如不接第 3 段、或只跑补抓）它们就会一直躺在 failed 里——
这正是本条缺陷的实际危害，也是「修复」一节里 `deterministic_failures()` 那条排除律的意义。

## 收尾（2026-09-05 13:42–13:59）：最后 3 篇已重跑

    scripts/backfill_deepread.py run --expand --apply --redo \
        --citekey zhu2025Fedweight --citekey zhao2025Multimodal --citekey hong202624hour

**成功 3 / 失败 0**，三篇都拿到真全文：`zhu2025Fedweight`（37 条带 tag）、
`zhao2025Multimodal`（34 条，源 europepmc——出版商 PDF 403 后由四通道兜回）、
`hong202624hour`（35 条，源 unpaywall，6 块）。备份 `.backfill_deepread_backup/20260905T134222/`。
索引已重解析（三篇 `has_full_text_reading` 全部翻成 True），向量库经 WatchPaths job 自动同步
（复核：store 内三篇各 37/34/35 条 highlight chunk，与精读产出条数一致）。

**`--redo` 是必需的**：这 3 篇在 `backfill_expand_progress.json` 里是旧口径记的
`reason: "no_output"` + `had_fulltext: true`，不加会被 `deterministic_failures()` 当确定性
失败静默跳过——白跑且无任何提示。

**⚠️ 全库同型陈旧条目还剩 4 篇未处理**：`liao2021Improving` / `li2023Prediction` /
`jin2024Transferb` / `yoo2025Review`。它们同样是旧口径记的 `no_output` + `had_fulltext: true`，
要救回同法加 `--citekey`（或迁移其 reason）。本批未做——用户点名的是那 3 篇。

## 归因订正（2026-09-05 对抗审计 Lens C）：那 4 条不是旧口径存量，是新代码还在写

先前把「`no_output` + `had_fulltext: true` 的 4 条」判成了**旧口径遗留、补个迁移即可**。
**错了，而且错在对己方有利的方向**：

    021b33e（引入 classify_failure 与 had_fulltext）提交时间  2026-09-04 22:32:37
    那 4 条的 at                                          2026-09-04 23:00:23

`had_fulltext` 这个键**只有新代码会写**，所以它们是修完之后判出来的。根因是
`classify_failure` 落盘了 `from_full_text` 却从不拿它当判据：只看 `llm_unavailable` /
`err` / `routes_suppressed`，三者不中就一律 `no_output`——而 `no_output` 的 docstring
写着「真没正文可读（无 DOI / 闭源 / 四条通道全断）」，`had_fulltext: true` 直接证伪它。

**这是活缺陷，不是存量。** 修法：新增第五类 `empty_with_fulltext`（全文已到手却零产出：
模型返回不可解析、抽取零命中），归入 `TRANSIENT_REASON_PREFIXES`——不进永久跳过集、
重跑不跳过、不计连败。实证支持：同型 3 篇 2026-09-05 加 `--redo` 重跑，成功 3 / 失败 0。

附带记账缺口（未修，已知）：`backfill_expand_progress.json` 无 `schema_version` 也无
`generated_at`；269 条 failed 里 **127 条根本没有 `had_fulltext` 键**（写于 021b33e 之前，
正是额度耗尽那段时间），新口径无法回答「它们当时到底拿没拿到全文」。
09-04 捞回的 35 篇是靠翻日志找出来的，不是靠账本。

## 账本硬化（2026-09-05 压测）

- **并发会把账本写成垃圾**：`.tmp` 是固定名，两个进程共用同一路径——B 的 `"w"` 截断 A 写到
  一半的内容再 `replace` 成正式账本，而 A 的 fd 仍指着那个 inode 继续在高偏移写，
  于是账本变成带 NUL 空洞的 5.4MB 不可解析文件、**A 整批进度静默蒸发**、下次 load 直接
  「拒绝继续」，人只能删账本——十几小时的 LLM 花费一起没。已改成 `.tmp-<pid>`。
- **畸形账本 6 例裸栈**：`failed` 是 list / 是 str / value 是 str 或 list，都会在
  `legacy_skipped` 里抛 `AttributeError`（那两个调用在 try 之外）。已把形状校验搬进 try，
  统一走「拒绝继续」。`{"done": null}` 此前靠 `setdefault` 静默漏过、到下游才炸，也已归一。
- **`schema_version: 3.0` 会静默过版本闸**（Python 里 `3.0 == 3`，而 jq/pandas 过一遍就会
  把 3 变成 3.0）。改成按 int 严格比较。
- **`Infinity`/`NaN` 原样写出的不是合法 JSON**，账本会变成只有 Python 能读。已 `allow_nan=False`。

压测确认没问题的：10 万条规模 load/save 各 0.2s、`legacy_skipped` 与 `deterministic_failures`
都是 O(n)；kill -9 十一次不同时点**零半截 json**；20 轮 load→save 无膨胀无漂移；
`led = dict(led)` 不污染调用方。**已知未修**：`save_ledger` 无 fsync（防进程死不防断电）；
ENOSPC 时 OSError 从 `cmd_run` 直接冒出，此时 md/sidecar 已改写完而账本没记上。
