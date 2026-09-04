# 额度耗尽被记成 `no_output`：35 篇「全文已到手但没读成」混进了待下载清单

日期：2026-09-04
状态：**已修复（2026-09-04 台账批，见文末「修复」）**；受影响的 35 篇清单附在文末，额度恢复后可直接重跑
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
