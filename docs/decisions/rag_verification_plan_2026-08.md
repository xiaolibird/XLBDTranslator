# 札记库 RAG 链路引入验证机制：改造方案

日期：2026-08-27
状态：**P0 + P1（两层）已完成**，判据经三轮对抗审核定稿；**P2、P3 待决**
起因：精读 CRITIC-RAG（`li2026Criticrag`，IEEE JBHI 2026，DOI 10.1109/JBHI.2026.3687666）后评估其机制能否移植

## 进度

- 2026-08-27 阈值收编：`DEFAULT_GAP_EVIDENCE_MIN_SIM` 及其标定档案搬进
  `thresholds.QA_GAP_EVIDENCE_MIN_SIM`，防漂移锚见 `test_thresholds.py`。
  （下面「顺带记账」第 4 条原写有两个游离阈值，**其中一个是我记错了**——
  `DEFAULT_GAP_TOPIC_MIN_SIM` 早就挂在 `TOPICS_MIN_SIM` 上且有锚，已订正。）
- 2026-08-27 P0 完成：`scripts/gen_bench.py` + `test/test_gen_bench.py`。首版基线报
  「584 个数字 100% 接地」，**该数字已作废**——对抗审核在判据里找出四个洞，修后
  定稿基线（v4）为 **538/542 = 99.26%**（3 条报警，全部已判读）。
  **且只覆盖 31.2% 的论断**。详见基线档案。
- 2026-08-27 **P1 第一层完成**：判据抽成 `src/scholar/grounding.py`（bench 与生产
  链路共用同一个判据，不许各写一套），接进 `topics.validate_synthesis`（论断 + 分歧
  两侧）与 `qa.validate_qa`，四个计数进 `ValidationReport`、两个进 frontmatter。
  验收见 `test/test_grounding.py`（14 条）。端到端复跑 shadow-variable：生产侧
  `numbers_checked: 26`，bench 事后从 markdown 反算也是 26，交叉校验一致。
  **只记账不拦截**，理由见下。
- 2026-08-27 **P1 第二层完成**：`scripts/entail_audit.py` + `config/prompts/
  entailment_audit_prompt.md` + `src/scholar/page_parse.py`（页面解析，与 gen_bench
  共用），离线验收 `test/test_entail_audit.py`（22 条）。首轮标定 16 条报警 4 条、
  人工复核 **4/4 全部成立**，详见 `entail_audit_calibration_2026-08.md`。
  该标定暴露出一个**未决的真问题**：外推与证据共享同一个引用（见下）。
- 2026-08-27/28 **五路 + 三轮对抗审核**：判据共修 11 处（四个洞 + 两处反向误报 +
  五项收紧，见基线档案 v2/v3/v4 三节），标定的**定量结论全部推翻**、只保留定性
  结论，三份文档共订正 20 余处错误陈述（全部以 ⚠️ 就地标注，不重写掩盖）。
  过程中我自己制造并修掉了 3 个回归，其中 2 个是「凭直觉写字符类、没在真实语料上
  验证产出量」——已用金丝雀多重集 + 冻结页面 fixture 两道测试结构性堵住。

---

## 起因

CRITIC-RAG 把一个小规模指令微调 verifier 贯穿 RAG 全流程做三阶段验证（检索必要性 / 证据相关性 / 答案 groundedness）。问题是：这四个机制里哪些对我们的札记库真有用？

结论先行：**不能整套照搬**。其中一个对我们无意义，一个只值得抄一半，一个是我们的真缺口，还有一个我们缺得比论文更严重。

---

## 一处必须先纠正的认知错误

评估初期我判断「groundedness 这一环我们已经比论文强，因为有 `quote_verify.py` 的确定性 grep 回验」。**这个判断是错的。**

`quote_verify.py` 属于书籍精读链路，校对的是引句是否逐字出现在原 PDF 中，与 topics/qa 的合成链路无关。合成链路的实际情况写在代码自己的注释里——[`src/scholar/qa.py:783-784`](../../src/scholar/qa.py#L783-L784)：

> 防线保证 citekey 与原句真实存在，**不保证转述没有失真**

即：一条论断只要挂了一个存在的 `E7`，哪怕内容与 E7 原句毫无关系甚至相反，[`validate_synthesis`](../../src/scholar/topics.py#L845) 与 [`validate_qa`](../../src/scholar/qa.py#L572) 全部放行。

**我们不是比论文强，是完全空的。** 编号回译防的是「模型编造文献」，防不了「模型曲解证据」。

---

## 现状盘点

链路（topics 与 qa 共用前半段）：

```
query → embed → VectorStore.search（暴力余弦 + 布尔 mask）
  → 硬过滤 level/role/exclude_sections（在 mask 里，检索前）
  → 阈值过滤 eff_min = max(min_sim, α·top1)   ← 唯一的质量闸门，纯分数
  → bucket_bonus 软加权（只改名次）
  → select_evidence（去重、单篇配额、重编号 E1..En）
  → render_evidence_block（只给 E 编号，抹掉 citekey）
  → prompt → LLM(json_mode, temp 0.2/0.3, 解析失败重试1次)
  → validate_*（编号回译 + 剥裸引用 + 丢弃无出处论断）
  → 渲染落盘 → [离线] audit_*_pages
```

**全仓 `grep -rn rerank` 零命中。**（2026-08-29 追记：notes_search CLI 层已加 bge-reranker 重排，该 grep 不再为零；但 topics/qa 生成侧链路仍无 rerank，本节论证不受影响。）检索之后到拼 prompt 之前，全部动作是「按分数排序 + 按 citekey 分配名额」——`select_evidence`（[`topics.py:247`](../../src/scholar/topics.py#L247)）关心的是多样性配额，不是相关性。

现有把关只有三类，全部是确定性的词面 / 分数 / 编号检查：

| 环节 | 检查什么 | 挡不住什么 |
|---|---|---|
| 阈值过滤 | 余弦分数 ≥ eff_min | 分数高但答非所问 |
| 编号回译 | 引用的 E 编号是否越界、有没有裸 `@key` | 论断内容与所引证据是否一致 |
| 离线 audit | 死键、逐字失锚、残留编号 | 同上，且是事后 |

---

## 逐机制取舍

| CRITIC-RAG 机制 | 论文自证的效力 | 对我们 | 理由 |
|---|---|---|---|
| Retrieve-on-Demand | 效率主张全文零数字 | **不抄** | 我们没有「用参数知识旁路检索」这条通道，论文那个二分类无处安放 |
| Evidence Filtering | 消融**降幅最大**（MedQA Acc -0.050、LiveQA BS -0.053）；Table IV 事实对齐度跃升 **+0.103 全在这一步** | **抄**（P2） | 我们检索后确实没有第二判据 |
| 聚类 + 自一致性 | MedQA -0.057，但需多路径生成 | **只抄一半** | 聚类去冗余可以；多路径生成对我们成本不划算 |
| Groundedness 验证 | LLM 五分制打分 | **抄，但改成双层**（P1） | 我们缺得比论文严重；纯 LLM 打分成本高，论文自陈 nontrivial |

⚠️ **订正（2026-08-27 对抗审核）——上表有一处双重标准，已改**：原先第一行拿
「消融仅 -0.003」当「不抄」的首要理由，而本文档末尾的「纪律」一节又说这篇论文
「-0.003 的差异也拿来给模块重要性排序……这个量级与运行噪声不可区分」。
**如果它与噪声不可区分，它就不能反过来当"这个模块没用"的证据**——它什么都不能
证明。同一张表在数字大时采信、数字小时判无效，而论文根本没给方差、文档也从未
说明「多大才算过噪声线」。已把该理由从表中删除，结论（不抄）靠另一条理由成立。

⚠️ **另一处订正**：不抄的理由不能写成「不存在该不该检索的判断点」——链路里
有同族的判断点，只是退化了：`topics.py` 在证据为 0 时才 skip，没有「证据太弱
不该合成」这一档；`qa.py:482` 的 `relative_alpha=0.0` 把自适应门槛整个关掉。
而本文档自己的标定数据显示证据最稀薄那页外推最多。**「证据充分性弃权」应当单独
立项**，它和 retrieve-on-demand 不是一回事，不该被这条决定连带否掉。

**明确不做训验证器。** 论文要用 GPT-4o 造约 8 万条伪标签去训 Llama-3.2-3B，是因为 PubMed 段落无标注。我们的证据是精读过的札记句子，本来就带 `role`（可引用证据/可反驳观点/方法论借鉴）、`section` 归属和页码锚——结构化信号现成。这条路线在库里另有实证支持：`zheng2025Miriad` 证明结构化 QA 替代原文段落做 RAG 能显著提质降幻觉。

---

## P0（前置）：补生成侧 bench —— ✅ 已完成 2026-08-27

> 落地结果与基线数字见 [`gen_bench_baseline_2026-08.md`](gen_bench_baseline_2026-08.md)。
> 实际做法与下面的设想有一处关键出入：**不重跑生成，只审计已落盘页面**，因而零
> LLM 成本、可反复跑。首跑还捞出两个真 bug（重复 citekey 时原句挂错文献、
> `4CE`/中文逗号的数字误抽），并暴露出主指标只覆盖 31.2% 的论断。

**为什么必须先做**：[`scripts/rag_bench.py`](../../scripts/rag_bench.py) 只对每条 query 起一个 `notes_search.py` 子进程测排序（hit@1 / hit@5 / nDCG@10 / MRR），**nDCG@10 之后链路上发生的一切都没有任何量化基线**。不先补这个，P1/P2 做完也无法证明有没有用。

跳过这一步就是复现这篇论文最大的方法学毛病：全文单点数字、无方差、无置信区间、无显著性检验，-0.003 的差异也拿来给模块重要性排序。

**做法**：新建平行 bench，case 集沿用 [`test/data/rag_bench_cases.jsonl`](../../test/data/rag_bench_cases.jsonl) 的 `{id, type, query, gold}` jsonl 口径。

**指标（三个都要确定性可算，不引入 LLM 裁判）**：

1. `numeric_grounding_rate` —— 论断中出现的数字/百分比/样本量，在其所引证据原句中能找到的比例
2. `claim_evidence_overlap` —— 论断与所引证据的词面重叠（作弱信号，单独不可用于判定，仅看分布漂移）
3. `dropped_claims` / `invalid_refs` / `stripped_cites` 的分布 —— 已由 `ValidationReport`（[`topics.py:799`](../../src/scholar/topics.py#L799)）产出，只是目前无人聚合

**验收标准**：在当前代码上跑出基线数字并存档到本目录（体例参照 `rag_bench_baseline_2026-08.md`）。P1/P2 每一步都必须给出前后对照，退化即回滚。

**改动面**：纯新增文件，不碰主链路。

---

## P1（最高收益）：groundedness 双层验证

**插入点**：[`validate_synthesis`](../../src/scholar/topics.py#L845) 与 [`validate_qa`](../../src/scholar/qa.py#L572) 之后、`render_*_block` 之前。

现成载体已经齐了：
- `ValidationReport`（[`topics.py:799`](../../src/scholar/topics.py#L799)）加字段即可，`build_frontmatter`（[`topics.py:1027`](../../src/scholar/topics.py#L1027)）会自动带进页面
- `is_archivable`（[`qa.py:643`](../../src/scholar/qa.py#L643)）是现成的「拒绝落盘」闸门，语义可扩展

### 第一层：确定性数字回对（零成本，先做这个）—— ✅ 已完成 2026-08-27

> 落地形态与设想一致，补充三点实际决定：
> 1. **判据抽成 `src/scholar/grounding.py`**，`gen_bench.py` 与 `validate_*` 共用。
>    两边各写一套的话，bench 报 100% 而生产链路报别的，谁也不知道该信哪个。
>    `test_gen_bench.py::test_bench_and_production_share_one_judge` 锁住这件事。
> 2. **分歧两侧也检查**（`position_a` / `position_b`），它们同样带数字同样会失真。
> 3. **交叉校验**：生产链路把计数写进 frontmatter，bench 事后从 markdown 反算，
>    两条独立路径不一致就是 bug，`gen_bench` 会直接报出来。

论断里出现的数字、百分比、样本量，必须在它所引证据的原句里出现。抄错量级（0.06 → 0.6）、张冠李戴（把 A 文献的 AUC 安到 B 文献头上）这两类错误纯 grep 就能抓，精度接近 100%，不花一分钱。

注意实现细节（已有踩坑记录可参照 `quote_verify` 那条链路）：软连字符、全角/半角、千分位逗号、百分号与小数的互换（0.488 vs 48.8%）都要归一化后再比。

**处置（已实施）**：数字对不上的论断进 `ValidationReport` 的四个新字段
（`numbers_checked` / `numbers_derived` / `ungrounded_numbers` / `ungrounded_claims`），
前两个中的 `numbers_checked` 与 `ungrounded_numbers` 落进 frontmatter，同时打
`logger.warning` 带上论断原文与引用。

**只记账不拦截，这是有意的决定**：v2 基线 539/542 接地、仅 2 条报警，样本仍不足以支撑
"直接拒绝落盘"。这一层的误报代价很实在——一旦开始拦，规则里任何一个没想到的合法
写法（专名里的数字、论断自己算的比值、页面元信息）都会**静默吃掉真论断**，而丢一条
真论断比留一条可疑数字更难发现。等积累几轮真实告警、确认误报率足够低，再考虑把
`ungrounded_numbers > 0` 接进 `is_archivable` 或 `topics/_lint.md`。

### 第二层：LLM 蕴含判定（只兜第一层的漏网）—— ✅ 已完成 2026-08-27

> 标定与未决问题见 [`entail_audit_calibration_2026-08.md`](entail_audit_calibration_2026-08.md)。
> 三点与设想的出入：
> 1. **做成独立脚本而非接进生成链路**。接进去会让每次生成都多烧一轮 LLM，而我们
>    的既定策略是"只记账不拦截"——那就没有理由同步跑。独立脚本可按需、可限量、
>    可只审某一页。
> 2. **四态而非二元**（supported / overreach / unsupported / contradicted）。
>    `overreach`（方向对但说过头）是最常见也最隐蔽的一档，二元判定会把它压成
>    supported 而漏掉。
> 3. **加了摘录回验**：LLM 每条非 supported 必须从证据逐字摘一段，脚本再验这段
>    是否真在证据里。首轮 16 条就抓到 1 条编造的摘录。

无数字的纯定性论断才送 LLM，且**一次 prompt 批量判多条**，不要每条一次调用。

成本约束是硬的：8 个概念页目前已经要分批跑防订阅触顶（见 `scholar-topic-pages` 记忆）。论文自己也承认用 GPT-4 做大规模打分成本 nontrivial——这正是把确定性那层放在前面的理由。

~~**先做第一层就能验证价值**，第二层视 P0 基线的结果再决定要不要上。~~
⚠️ **已被执行推翻**：两层都已完成。而且 P0 基线并没有、也不可能给出「要不要上
第二层」的依据——它测的是生成侧的数字接地，而第二层要覆盖的恰恰是**不含数字**的
那 68.8%。真正的依据是覆盖缺口本身，不是基线数值。

---

## P2：evidence filtering（检索后相关性第二判据）

**插入点**：[`topics.retrieve_evidence` 返回处（topics.py:366）](../../src/scholar/topics.py#L366) 与 [`qa.retrieve_qa_evidence` 返回处（qa.py:485）](../../src/scholar/qa.py#L485)——单一收口，两条链路都过这里，改动面最小。

这是论文消融里降幅最大的模块，且 Table IV 显示 LiveQA 的 NLI 对齐度从 Top-10 的 0.595 升到过滤后的 0.698（+0.103），而最终答案验证只再贡献 +0.011——**增益几乎全在过滤这一步**。

我们目前在这个位置的全部逻辑就是「分数够高就留下」。

⚠️ **本节未随新数据订正，现补**：蕴含审计的 16 条报警**没有一条**是「证据不该被
选进来」，所以 P2 的收益在那批样本上**未被观测到**。但注意这不等于「检索侧干净」
——审计器按定义看不见这类错误（见标定档案订正五）。**结论是「P2 的优先级缺乏依据，
不是「P2 该做」也不是「P2 不该做」**。要定夺它，需要一个测证据集本身的指标
（gold 召回、语义冗余率、来源覆盖数），而那正是 P0 没做到的那一半。

同时有一个**已观测到的**检索侧问题，比 P2 想解的更具体：跨论文语义冗余 33 个槽位、
32 个被引用（实测见「现状盘点」的订正）。它的解法是 `select_evidence` 里加一行
贪心 MMR/阈值抑制，向量已经在 `store.records` 里，零额外成本。

**待定**：用 cross-encoder rerank 还是 LLM 零样本相关性判定，取决于 P0 基线暴露出的失败模式。两者都要先在 bench 上验证再合入。（2026-08-29 追记：CLI 检索层已选 cross-encoder 并上线，见 rerank_hyde_experiment_2026-08.md；生成侧证据链是否引入仍待 P0 基线。）

---

## P3（白捡）：topics 侧补 gaps 回查

QA 侧有 [`recheck_gaps`（qa.py:1065）](../../src/scholar/qa.py#L1065)：拿模型写的每条 gap（「库里没有 X」）剥掉否定脚手架后回查向量库，命中就当场把反例贴在旁边。

**topics 侧完全没做**——[`topics.py:912`](../../src/scholar/topics.py#L912) 的 gaps 原样落进页面。

`qa.py:755-762` 的注释记了这个坑犯过一次：把「本次召回缺口」印成了「对 2300 篇库的事实断言」，而反例就躺在同一页的证据表里。**同一个坑在概念页链路上至今没盖。**

这条不用抄论文，抄我们自己已有的实现即可。改动最小、收益确定、当天可完成。

注意阈值口径：qa 侧两档分别是 0.55（概念页通道）/ 0.65（句级通道），且 `qa.py:873-878` 明说句级通道实测精确率仅约四成——移植时要沿用这个已标定的口径，不要另起。

---

## 顺带记账：本次探查暴露的既有欠账

与本方案同属「质量把关」范畴，但不在 P0–P3 主线内，单独记着：

1. **`bucket_bonus` 是无标定档案的经验偏置**（[`topics.py:263`](../../src/scholar/topics.py#L263) `score + bucket_boost`），直接改名次却不进 `min_sim` 判定。原始余弦不回写是对的（[`topics.py:241-244`](../../src/scholar/topics.py#L241-L244)），但加成本身该有标定
2. **`NOTES_SEARCH_MIN_SCORE = 0.4` 至今无标定**（[`thresholds.py:19-20`](../../src/scholar/thresholds.py#L19-L20) 自认），而 skill 文档里手传的 0.62 是跨链路借用
3. **QA 侧的相对判据被整个关掉**（[`qa.py:482`](../../src/scholar/qa.py#L482) `relative_alpha=0.0`），注释自认「QA 的 query top1 分布未验证」，导致强问题与弱问题共用同一条绝对线 0.55
4. ~~**两个阈值没集中到 `thresholds.py`**~~ —— **已修，且原记账有一半是错的**：
   `DEFAULT_GAP_TOPIC_MIN_SIM` 早就挂在 `TOPICS_MIN_SIM` 上（`qa.py` → `topics.DEFAULT_MIN_SIM` → `thresholds`），
   `test_thresholds.py` 还有防漂移锚锁着，从来不是游离值。真正硬编码的只有
   `DEFAULT_GAP_EVIDENCE_MIN_SIM=0.65` 一个——它标定档案齐全，但值和档案都留在
   `qa.py`，绕开了 `thresholds.py` 的铁律，换 embedding 时扫不到。
   2026-08-27 连档案一起搬到 `thresholds.QA_GAP_EVIDENCE_MIN_SIM`，`qa.py` 侧留同名别名
5. **引用率只是仪表不是闸门**，且页面自己写着这个数字两个方向都会骗人（[`topics.py:974-977`](../../src/scholar/topics.py#L974-L977)：某页 98% 引用率里近三分之一是无关方法联想）
6. **`invalid_refs` / `dropped_claims` 突然变大不触发任何动作**，靠人看 frontmatter（[`topics.py:801-802`](../../src/scholar/topics.py#L801-L802) 自认）

---

## 纪律

任何一步合入前，必须在 P0 的 bench 上跑出前后对照。**不接受「看着更好了」这种判断**——那正是被精读的这篇论文的毛病：Table III 拿 -0.003 到 -0.015 量级的单点差异给模块重要性排序，既无重复实验也无显著性检验，而这个量级与运行噪声不可区分。

---

## 参考

- 精读全文：`科研札记_2026-08_手动精读.md`，citekey `li2026Criticrag`
- 相关文献：`zheng2025Miriad`（结构化 QA 替代原文段落做 RAG）、`zakka2024Almanac`（检索增强 + 可核验引用）、`yun2025Medprm`（检索证据逐步验证推理链）
- 既有标定档案：`topics_threshold_calibration_2026-08.md`、`digest_neighbors_calibration_2026-08.md`、`rag_bench_baseline_2026-08.md`
