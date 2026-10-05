# ask_notes v3：让问答页的「转述失真」可见、可拦（设计，2026-10-04）

## 1. 动机：2026-10-03 实测暴露的四类问题

问题集（验收要用）：

| 案例 | 问题 | 对口论文与原文结论 |
|---|---|---|
| **C1 定理类** | 「信息性缺失下，共形预测的覆盖保证能做到什么程度？」 | zaffran2024Predictive：无分布假设下按缺失模式的条件覆盖（MCV）**不可达**（Thm 2.3）；只限 MAR 或只限 MCAR **也不行**（Prop 2.6）；仅 MCAR 且 Y⊥M\|X 可证，水平 **1−2α**（Thm 4.3）；边际有效在 MNAR 下也成立（Thm 4.2） |
| **C2 数字类** | 「不同医院之间，同一项检验或测量的缺失率差异有多大？有没有实证数字？」 | patel2026Unmeasured Table S5：PaCO2 46.42%/69.01%、PaO2 46.41%/68.70%、GCS 0.46%/27.57%、白蛋白 74.03%/57.92% |

暴露的问题（按危害排序）：

1. **综合阶段把方向弄反**（C1 第 3 轮）：Prop 2.6 是硬度结果「只限 MAR 或 MCAR，同一不可能下界依然成立」，页面写成「CP-MDA 类理论结果被限定证明于 P_MAR 或 P_MCAR 分布族内」。`--verify` 全绿——现有防线只保证 citekey 与原句存在。
2. **把实验观察写成理论保证**（C1 第 1 轮）：「CP-MDA 对各种缺失模式维持不低于 1−α 的覆盖（与 Theorem 4.3 一致）」——来自 Fig. 3a 实验句，而 Thm 4.3 的水平是 1−2α。
3. **定理句召回不到**：公式密集的句子与自然语言问题相似度偏低（实测 0.54 < 默认 min_sim 0.55）；必须手动降阈值、写定理关键词才召回。
4. **角色过滤静默挡掉硬数字**（C2）：「PaCO2 46.42% vs 69.01%」在精读里标成「可反驳观点」，`--role citable` 把它整批挡掉，用户毫无察觉。（`--role all`、参数回显、`--show N` 已于 10-03 补上，本次不重复。）

另：数字接地（`topics._check_grounding`）**已经在跑**，但只写日志与 frontmatter 计数，**页面上看不到是哪一条、哪个数字**，读者无从下手。

## 2. 设计总览

| 层 | 做什么 | 确定性？ | 解决 |
|---|---|---|---|
| A1 逐条数字接地徽标 | 每条论断下标出「所引证据中找不到的数字」 | 是 | 让已有检查可见 |
| A2 理论类论断识别 + 原句并排 | 识别涉及定理/保证/界/可识别/不可能的论断，**在它下面直接贴出所引证据的原句** | 是 | 读者一眼对比方向（问题 1、2） |
| B 蕴含审计（第二层） | 一次额外 LLM 调用，逐条判「转述是否忠实于所引原句」：忠实 / 不忠实 / 无法判断 + 理由 | 否 | 拦住方向反转（问题 1、2） |
| C 理论模式召回扩展 | 问题是理论类时，对召回到的论文**按篇补捞**其定理/命题句，用放宽阈值 | 半 | 问题 3 |
| D 角色过滤可见化 | 统计被 `--role` 挡掉的高分句条数（其中含数字的条数），打印并写进 frontmatter | 是 | 问题 4 的可见性 |

**范围约束**：只改 QA 链路（`src/scholar/qa.py`、`scripts/ask_notes.py`、`config/prompts/qa_synthesis_prompt.md`、新增 `config/prompts/qa_audit_prompt.md`、测试）。**不改 `topics.py` 里的共用函数行为**（概念页链路不动；需要的能力在 qa.py 里包一层）。概念页同构渲染的镜像改造留作后续。

## 3. 各层细节

### A1 逐条数字接地徽标

- 在 `qa.py` 里新写一个返回结果的版本（如 `_grounding_result(text, refs, ev_map, n_evidence) -> List[str]`），内部复用 `grounding.build_pool` / `grounding.check_numbers`，**不改** `topics._check_grounding`。`validate_qa` 照常累计 `ValidationReport`，同时把每条论断的 `ungrounded: [..]` 存进 item。
- 渲染：论断行末尾不动；**下一行**缩进加 `> ⚠️ 数字未在所引证据中找到：X、Y（可能是转述失真，也可能是自行换算——回原句核对）`。
- 注意 C2 的实情：PaO2 的错数字当时**来自精读句本身**，所以接地会判「已接地」。这是对的——A1 只判「转述 vs 证据」，不判「证据 vs 原文」。文档要写清这个边界。

### A2 理论类论断识别 + 原句并排

- 判定 = **LLM 自报 ∪ 关键词兜底**（不单信模型）：
  - prompt 里给每条 points/caveats 增加字段 `"kind": "实证" | "理论" | "方法" | "解读"`；
  - 关键词兜底（对论断文本**或**其所引证据原句任一命中即算理论类）：`定理|命题|引理|推论|保证|下界|上界|可识别|不可识别|不可能|不可达|硬度|充分条件|必要条件|渐近|一致性|Theorem|Proposition|Lemma|Corollary|guarantee|bound|identif|impossib|hardness|asymptot|1−α|1-α|1−2α`。正则放 qa.py 模块级常量，写注释说明来源（10-03 案例）。
- 渲染：理论类论断下方，逐条贴出**所引证据原句**（全文，不截断；每条前标 `[@citekey, p.X]`），标题为 `> 📌 原句（理论类论断，请对比方向与限定条件）：`。
- 代价：页面变长。只对理论类做；实证类不贴。

### B 蕴含审计（第二层）

- 新 prompt `config/prompts/qa_audit_prompt.md`。输入：若干条「论断文本 + 它所引的证据原句（含 role/section）」；输出 JSON：`{"audits": [{"id": "P1", "verdict": "忠实|不忠实|无法判断", "reason": "一句话，指出哪个词/方向/限定条件不符"}]}`。
- prompt 要点（写进铁律）：
  1. 只对比「论断」与「给出的原句」，不用外部知识；
  2. 重点查：**方向/极性**（可证 vs 不可能、增加 vs 减少、成立 vs 不成立）、**限定条件**（MCAR 且 Y⊥M\|X、在某数据集上）、**数值与水平**（1−α vs 1−2α）、**证据类型**（实验观察被写成理论保证 → 不忠实）、**语气强度**（「可能」写成「证明」）；
  3. 原句信息不足以判断时给「无法判断」，不要猜；
  4. 附两个 few-shot：用 C1 的 Prop 2.6 反转（不忠实）与 Fig.3a→「理论保证」（不忠实）各一例；再给一个忠实例（用 C2 的 GCS 0.46%/27.57%）。
- 审计对象：默认 **理论类论断（A2 判定）全部审**；`--audit all` 审全部 points+caveats；`--audit off` 关闭。一页一次批量调用。模型默认与合成相同，`--audit-model` 可覆盖。
- 处置（默认「标而不删」）：
  - 「不忠实」：论断前加 `❌`，下方加 `> ❌ 审计：<reason>`；并在 `answer` 前插一行 `> ⚠️ 本页有 N 条论断审计判为不忠实（已标 ❌），answer 可能受其影响。`
  - 「无法判断」：加 `> ❔ 审计：<reason>`。
  - `--strict`：直接丢弃「不忠实」论断（计入 `ValidationReport` 新字段），并在页面审计记录里列出被丢的原文。
  - 审计调用失败（LLM 报错/解析失败）：**不阻断归档**，页面顶部明说「本页未经蕴含审计（原因：…）」，frontmatter 记 `audit_status: failed`。
- frontmatter 新字段：`audit_status`（ok/failed/off）、`audited_claims`、`unfaithful_claims`、`uncertain_claims`、`theory_claims`、`ungrounded_claims_listed`。`QA_SCHEMA_VERSION` 2→3。
- `--verify` 对旧页（v2，没有这些字段）必须照常通过；可额外报告「未经审计」页数。

### C 理论模式召回扩展

- 触发：问题文本或 `-q` 命中 A2 关键词，或 `--theory` 显式开启（`--no-theory` 关闭）。
- 做法：正常召回之后，取候选池里**得分最高的前 K 篇论文**（K=6），对每篇把它的定理/命题类句子（文本命中 A2 关键词正则中的「定理|命题|引理|推论|Theorem|Proposition|Lemma|Corollary|Table 2 给出全文结论矩阵」之类）**以放宽阈值 `theory_min_sim`（默认 0.45）**补进候选，再走 `select_qa_evidence`。
- 实现优先走向量库（要有 note_file/note_line 才能渲染证据表）。先查清 `embed_store` / `topics.retrieve_evidence` 能否按 citekey 过滤或拿到某篇的全部 highlight chunk；若只能全库查，可以「对该篇各句单独打分」：用 embed_client 把问题向量与该篇候选句向量做余弦。**不要从 literature_index 的 highlights 构造 Evidence**——那里没有 note_file/note_line，证据表会断锚。
- 每篇补捞上限 3 句；补进来的证据在证据表里标 `（理论补捞）`。
- 打印一行：`📐 理论模式：从 K 篇补捞 N 条定理/命题句（阈值 0.45）`。

### D 角色过滤可见化

- `retrieve_qa_evidence` 在 roles 非空时，额外统计「相似度 ≥ min_sim 但因角色不符被挡掉」的条数，以及其中**文本含数字**的条数。
- 实现提示：拿一次 roles=[] 的候选池做差集即可（召回本身已经是「宽池再截断」的结构，见 `_WIDE_MAX_EVIDENCE`）；注意别让召回耗时翻倍——能复用一次向量查询就复用。
- 输出：`⚠️ 角色过滤（citable）挡掉 12 条高分证据，其中 5 条含数字；问数字/事实时考虑 --role all`。frontmatter 记 `role_filtered_out`、`role_filtered_numeric`。

## 4. 测试与验收

### 单元测试（新文件 `test/test_ask_notes_v3_2026_10_04.py`，不改旧测试的断言）

- A1：构造论断含证据里没有的数字 → item 的 `ungrounded` 非空且渲染出 ⚠️ 行；全部接地 → 不渲染。
- A2：关键词兜底在「LLM 未标 kind」时仍判为理论类；理论类渲染出原句块，实证类不渲染。
- B：用假 LLM（返回固定 JSON）测三种 verdict 的渲染、`--strict` 丢弃、审计调用抛错时的降级（不阻断、页面明说、`audit_status: failed`）、解析失败同上。
- B prompt：断言 few-shot 三例都在 `qa_audit_prompt.md` 里（防止日后被删）。
- C：用假 store/embed 测按篇补捞上限 3、阈值生效、补捞证据带标记；问题无理论关键词时不触发。
- D：roles=[citable] 时统计数正确。
- `--verify` 对 v2 旧页照常通过（用现有 4 页之一的副本做 fixture，或构造最小 v2 页）。

### 回归

- `PYTHONPATH=. python -m pytest -q` 全量通过（基线 2669 passed, 2 skipped）。

### 端到端验收（需要 Ollama + LLM；由主会话执行，不是子代理）

- **C1**：用**默认参数**（不手动降阈值、不写定理关键词）重问 → 证据里出现 zaffran 的 Thm 2.3/Prop 2.6/Thm 4.3 中至少两条（理论补捞生效）；页面若出现方向反转或「实验写成保证」的论断，必须被审计标 ❌。
- **C2**：数字徽标与 Table S5 截图一致；无新增误报。
- 两页 `--verify` 通过。

## 5. 不做的（明确排除）

- 不改概念页（`topics.py` 的合成、渲染、校验）。
- 不改向量库结构、不重建索引。
- 不自动修改精读 bundle（「证据 vs 原文」的错要人去修源头）。
- 不 commit；由主会话验收后与用户确认再提交。
