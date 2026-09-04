# ingest 的 `degraded` 状态：全部块都成功也会触发，原因不落盘，且 skill 协议里没有这个状态

日期：2026-09-04
状态：**已修复（2026-09-04）**，见文末。原状态：未修复
严重度：中——不丢数据，丢的是**双轨核验的第二轨**；agent 若不看那一行会以为草稿只是"没写好"，实际是根本没生成
发现者：xiaolibird / Claude（2026-09-03「2026-09-02-插补扭曲」第二批精读入库会话）
所属仓库：XLBDTranslator-dev

---

## 起因

第二批 6 篇 PDF 批量 ingest，5 篇正常，**Hoogland 2020（17 页，本批最重要的方法学论文）** 这一篇打印：

```
📄 Handling missing predictor values when validating and applying a prediction model to new patients
   bundle       : output/scholar_notes/manual/2026-09-02-插补扭曲/51e91233bfe7b1dc.paper.json
   亲读范围      : 17 页 → 1 个 20 页窗口：1-17
   分块通读      : 27/27 块成功 | 脚本草稿 无 | draft_status=degraded
```

**27/27 块全部成功，却没有草稿。** 同批其余 5 篇均为 `draft_status=ok`。

---

## 三个独立的问题

| # | 问题 | 后果 |
|---|---|---|
| 1 | `degraded` 的**文档说明与实际触发条件不符** | 读文档的人会以为"有块失败了"，而实际是块全成功、汇总步失败 |
| 2 | **汇总步失败的原因从不落盘** | 无法判断是超预算、超时、还是 LLM 返回了不可解析的内容；不可复现、不可修 |
| 3 | **`read-paper` skill 的协议里没有 `degraded` 这个状态** | skill 只写了 `ok` 走正常协议、`api_error` 走 subagent 对抗回退；agent 遇到 `degraded` 无章可循 |

---

## 根因（代码位置）

`src/scholar/pdf_ingest.py:917`：

```python
elif n_ok == 0:
    draft_status, draft_note = "empty", "全文分块通读均失败，无脚本草稿"
else:
    draft_status, draft_note = "degraded", "汇总步失败但部分块可用"   # :917
```

这是 if/elif 链的 **else 兜底分支**——只要走到这里就是 `degraded`，**与"有多少块失败"无关**。而同文件 `:688` 的 docstring 写的是：

```
draft_status："ok"（有草稿）/ "api_error"（LLM 无额度/鉴权失败，应回退到 subagent 对抗生成）
              / "degraded"（部分块失败）/ "empty"（无可用块）
```

`degraded`（部分块失败）**描述的是一个这条分支并不检查的条件**。本例 `n_ok == 27`、`n_fail == 0`，仍然 `degraded`。

`draft_note` 是**硬编码字符串**"汇总步失败但部分块可用"，不携带任何来自汇总步的实际错误（异常类型、HTTP 状态、返回内容长度、是否超出 60000 字符预算）。

---

## 影响（这次的实际后果）

`read-paper` 协议的核心是**双轨交叉核验**——脚本草稿一轨、agent 亲读一轨，分歧回 PDF 定夺。这篇丢了脚本轨，等于**该论文的核验降级为单轨**，只能靠 agent 亲读自证。

Hoogland 恰是这批里方法学密度最高的一篇（Box 1 的逐方法部署成本表、Table 2 的九行 OOB C 统计量、八种缺失情景的生成式），也是后续 P4 研究方向调整最依赖的一篇。本轮是靠人工在 `cross_check_report` 里写明"本篇无脚本草稿、核验为单轨"补的记录，**但这依赖 agent 恰好注意到了那一行输出**。

---

## 建议修法

1. **拆状态**：`degraded` 只保留给"部分块失败"（`0 < n_ok < n_total`）；新增 `synth_failed`（块全成功但汇总失败）。本例应报 `synth_failed`。
2. **把失败原因写进 `draft_note`**：至少带上异常类型/消息前 200 字符、块笔记总长度、是否超 60000 预算。现在这三个信息全在汇总函数里、全部被丢弃。
3. **skill 补一条协议分支**：`docs/skills/read-paper/SKILL.md` 现在只有 `ok` 与 `api_error` 两条路。`synth_failed` / `degraded` / `empty` 三态都应当写明走法——最自然的是**复用已有的回退协议**（Opus 生成者 + Sonnet 对抗者各自独立亲读），因为丢的正是"第二轨"。
4. （可选）ingest 末尾的「⚠️ 需要注意」块现在只报"索引里已有同文/元数据不全/ingest 失败"三类，**不报 `draft_status != ok`**。这是本次差点漏看的直接原因，建议加进去。

---

## 未验证 / 需要复现的部分

汇总步**为什么**失败，这次没有留下任何线索，事后无法回溯。若要修 #2，需要先复现一次——建议对同一篇 PDF 加 `--force` 重跑并在汇总调用处临时打点，确认是超预算截断、超时、还是返回不可解析。**在拿到这个之前，#1 与 #3 可以先改（纯状态与文档），#2 需要一次复现才能动手。**

---

## 修复（2026-09-04 台账批）

四条建议全部落地（#2 不需复现——原因现在由汇总步自己写出来）：

1. **拆状态**：`ingest_pdf` 改为两个独立维度判定——有草稿 → `ok`（块全成功）/ `degraded`（**部分块失败**，note 写失败块号与「失败块页段须亲读补齐」）；
   无草稿 → `api_error` / `empty` / **`synth_failed`**（块有成功、汇总失败）。本条现场（27/27 块成功、汇总失败）现在报 `synth_failed`。
   注意 `degraded` 语义变化：此前「有草稿但部分块失败」报 `ok`，现在报 `degraded`（协议：仍走正常核验）。
2. **失败原因落盘**：`synthesize_deep_read` 把汇总失败原因写进 `budget_info["synth_error"]`——LLM 异常（类型 + 消息前 200 字符）或
   「返回不可解析（响应 N 字符；块笔记打包 M 字符 / 预算 66000；响应开头 …）」——进 `draft_note`。
3. **skill 协议**：`docs/skills/read-paper/SKILL.md` 第 1 步改为五态说明，回退协议标题改为「api_error / synth_failed / empty」。
4. **末尾块**：`read_pdf._print_attention` 新增「脚本草稿 <状态>：<标题> —— <note>」项；`cmd_ingest` 对无草稿三态都打印回退协议（此前只有 api_error）。
`write_bundle` docstring 改为五态权威说明。测试：`test_pdf_ingest.py::test_ingest_status_*`（5 例）/ `test_synthesize_records_failure_reason_*`；
`test_read_pdf_cli.py::test_attention_block_reports_non_ok_draft_status` / `test_cmd_ingest_prints_fallback_protocol_when_synth_failed`。
