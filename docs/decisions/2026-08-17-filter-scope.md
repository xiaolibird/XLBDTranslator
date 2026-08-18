# filter-v3 范围决策：结构同构机制不纳入

日期：2026-08-17
状态：**已决定**（决策一、决策二），台账持续有效
决策人：xiaolibird

---

## 起因

filter-v3 连续三次把「跨领域但方法可移植」的论文判 EXCLUDE：

1. 陈隆的 CV 博士论文（CSS 的 Gap Δ 捷径依赖度量、CI 指标确实可移植）
2. Long Chen × 况琨合作线 5 篇（含 *Learning Causal Transition Matrix for Instance-dependent Label Noise*，与 MNAR 结构同构）
3. 影子变量三部曲（ICML 2024）——**这批是人工顺合著网络扒出来的，根本没进 filter**

据此曾提议给 filter 新增「方法可移植性」维度 H。该提议经三轮对抗审核（4 个 subagent）被证伪，两个替代方案（修召回层、造回归集测现状）也依次被推翻。最后用一次 LLM 调用做实测探针，得到本文件的决定性数据。

---

## 决策一：不扩 filter 范围

### 探针结果（10 条历史 EXCLUDE 过现行 `filter-v3 + sonnet`，1 次 LLM 调用）

| | 结果 |
|---|---|
| 标「应保住」的 3 条 | **0/3 被保住** |
| 标「应排除」的 5 条 | 5/5 仍排除 |

原型案例 *Addressing Label Noise for Electronic Health Records: Insights from Computer Vision for Tabular Data*（Yang et al., medRxiv 2023）：

| | 裁决 |
|---|---|
| 当年 `filter-v2 + deepseek-flash` | EXCLUDE / X3 / conf 0.95 / 「EHR标签噪声，无缺失机制」 |
| 现行 `filter-v3 + sonnet` | EXCLUDE / **X4** / conf 0.70 / 「标签噪声EHR分类方法,未涉及缺失机制」 |

### 关键判断：filter 没有故障

它给的理由（「未涉及缺失机制」「非特征 MNAR 缺失」「无缺失/迁移定量证据」）都站得住。`config/research_profile.yaml` 写的主题是「EHR 缺失机制（MNAR）」，而这些论文是 label noise / censoring——**结构同构，主题不同**。这是范围问题，不是 bug。

同批探针中，一篇标题含 collider 的高能物理论文被正确识别（X1, conf 0.97），说明 filter 没有被字面词欺骗。

### 收益/代价（14641 条唯一排除记录，全部加词边界统计）

| | 条数 |
|---|---|
| 命中同构机制词（label noise / censoring / collider bias / MNAR / truncation / PU 等）〔口径见注〕 | 96 |
| └ 同时命中 表格/EHR —— **扩范围真正想要的** | **5** |
| └ 影像类（会误入） | 16 |
| └ NLP 类（会误入） | 4 |
| └ 通用 ML / 物理等（会误入） | 71 |

交集口径精确率上限 **5%**。那 5 条逐条核对，真正有价值的**只有 1 篇**（Yang et al.）；另 4 条是 COVID 队列、量表信效度、药物超敏病例挖掘、真实世界 PFS——探针中已判真阴性且 filter 排除正确。

〔口径注〕上表 96 条 = 机制词并集、匹配 title+abstract、按标题去重。另一口径（单用 `\blabel[- ]nois`）数出 61 条。两者结论方向一致（5 条交集 / 1 篇有价值），但**引用具体数字前须先对齐口径**。

**结论：三年买 1 篇。** 代价是改三份副本文本（`config/research_profile.yaml` / `src/scholar/research_profile.py` 的 `_DEFAULT_PROFILE` / `test/test_research_profile.py` 的 `FIXTURE_*`）+ 打红 5 条测试 + 变更 prompt md5 + 长期压制一个 95% 噪声的类目。**不做。**

### 重要限定：不要误读那个「5 条」

它是**下限不是真实率**。`src/scholar/settings.py:135-148` 的 `arxiv_query` / `pubmed_query` 是硬 AND 三段，第一段必须命中 `MNAR / missing not at random / informative missingness / missingness mechanism`——**label noise 与 collider bias 类论文从一开始就不会被抓取**。

正确表述：**扩 filter 范围买不到什么，因为该类论文大多没进池子。这类文献本身有价值，只是 filter 不是取它的工具。**

取它的工具是实际奏效的那个：**人工顺引文/合著网络扒**（已产出 6 篇入库，批次 `2026-08-17-影子变量与collider偏倚`）。自动化它的两条路已评估：

- **OpenAlex snowball —— 否决。** 已改计费额度制（实测 `x-ratelimit-limit-usd: 0.1`、预付余额 $0、`filter=title.search` 每次 10 credits、`filter=cites:` 1 credit）。按实际种子数 497（注意：`literature_index.json` 里 `keeper` 字段**全为 `None`**，keeper 身份靠 `duplicate_of == null` 表达）算，两跳需 ≈110,000 credits ≈ 110 天免费额度。且**结构上够不着** 3 个触发案例中的 2 个——instance-dependent label noise 与 VQA 的 CSS 与 MNAR 文献之间没有引文边。
- **第二检索式 —— 不做（2026-08-18 修正理由）。** ~~原因是「带回的垃圾给裁决层加负担」~~ ——实测该理由不成立：照生产口径写三段，每周仅新增约 5–6 篇（arXiv 0.38 + PubMed 5.3），折合 **+0~1 次 filter 调用/周**，离 `external_max_results=25` 的帽子差得远，这不是负担是噪音。
  **真正的理由是收益空**：查 2256 篇 keeper 与历史裁决，`censoring` / `selection bias` 的通过率是 **64% / 68%**（基线 INCLUDE 率约 27%），而其中 **94% 是从 Google Scholar 邮件臂进来的，不是检索式臂**——真正与 MNAR 同构的那批论文，标题摘要里本来就带 missing data（在库 5 篇 selection bias 里有 3 篇标题直接写 *"selection bias due to missing data in electronic health records"*），既命中现有检索式也命中纳入维度 A。边际增量只剩两类：不提 missing data 的 selection bias（探针实测 filter-v3 对这类 **0/3**）与 label noise（通过率 31% ≈ 基线）。**净收益估计 0–2 篇/年，且大部分邮件臂本来就会送。**

---

## 决策二：两个技术债作为独立 commit 处理

前置条件（lint / 撤稿处置那批改动提交）已于 `414d30d` 满足。

| # | 位置 | 内容 |
|---|---|---|
| T1 | `src/scholar/notes_index.py:98` | ~~建议改 `[A-Z]` + 负向先行断言~~ **2026-08-18 撤销，收益为 0**。原文举的失效场景写反了：`维度 X2` 在 `[A-G]` 下**本来就不匹配**（`X` ∉ `[A-G]`），不存在「被吃成 `["X"]`」。真实失效场景是 `维度 A2` / `维度 AB` → `["A"]`。而实测全库 **1493 条带维度的裁决行零个畸形、收尾字符 100% 是空格** → 只加断言严格 0 收益；加断言并放宽字符类等于引入一个决策一刚否决的行为。**不做** |
| T2 | `src/scholar/vault.py:680-684` vs `:762-771` | `_moc_link` 对任意 bucket 建归属链接，但 `build_moc_pages` 只遍历 `BUCKET_LABEL.items()` 建页 → 未知 bucket 产生**悬空 wikilink**，且 tag（`维度/X`）与 MOC 页名（`维度/X-X`）口径不一致，违反 `:257` 注释写死的契约。两侧均无测试覆盖 |

**两条都是卫生项，不是活 bug，且 2026-08-18 复核后均判定为「不做」。** T1 的失效场景当前不可达（`exclude_reason` 是独立 schema 字段，从不写进裁决行；83 份札记的裁决行全是 A–G 组合，2343 篇索引零个非 A-G 字母）。T2 的真实触发方向是从 `bucket_labels` **删**字母（如删 F 会让现存 33 篇 F 标注论文立刻悬空），而非新增。

**T1/T2 的共同根因（记录，当前不可达，不做）**：`src/scholar/workflow.py:618` 用 `_coerce_str_list(verdict.get('bucket'))` 直收 LLM 输出，**没有对 A-G 做白名单校验**。模型哪天吐 `bucket:["H"]` 或 `["A1"]`，会一路写进 md、索引、向量库、vault。真要治，在这一处加一道按 `BUCKET_LABEL` 键过滤 + warning 的闸，比改末端正则（T1）或改 vault 建链（T2）都正确——**一处堵死两个入口**。

---

## 已验证事实台账

以下全部为实测，非推断。做任何涉及 filter / 裁决 / 回测的改动前应先读这一节。

### A. 与 RAG 实现无关，可长期依赖

1. **MAYBE 不是复核队列，是静默入库。**
   `src/scholar/workflow.py:604` `included = decision_val in ("INCLUDE","MAYBE")` → `src/scholar/ingest.py:219` `if fd.verdict != "included": continue` → `scripts/ingest_notes.py:200` `--auto`（全部入库、无人值守），launchd 周一 09:30。
   **任何「降级到 MAYBE 作为人工缓冲」的设计都不成立。** 只有 `journal_screen` 的 candidates 文件是真人工勾选。

2. **`classify_segments` 的 `force_include` 默认为 `True`**（`src/scholar/ingest.py:307`），会就地 `fd.verdict, fd.decision = "included", "INCLUDE"`。
   **任何用它做测量的 harness 都是无效的**——召回率会测出 100%。测量须显式传 `force_include=False`（`src/scholar/journal_screen.py:327` 是正确用法），或自建 `ScholarWorkflow` + `wf.segments` + `_step_filter_papers()`。

3. **历史 EXCLUDE 的裁决来源**：97.7% 由 `filter-v2@02c6656b` 判、99.0% 由 `deepseek-v4-flash` 判。
   数据在 `output/scholar_digest/*_excluded.json`（83 份、16372 条 llm_judge 记录、去重约 14641 条），每条含 `metadata` + `original_abstract` + 完整 `decision`（含 model / prompt_version / exclude_reason / confidence）。回测样本充足。

4. **排除码缺一个槽位**：没有「结构同构但主题不符」这一类，模型只能挑最近的码硬套——X4（「LLM/foundation model 通用能力」）被套到一篇与 LLM 毫无关系的论文上；X1 已漂移成「无表格 EHR」的万能筐（2026-08-17 那次 run 用它标了高能物理、羽毛球视频问答、机器人 VLA）。
   **做任何基于 `exclude_reason` 的分层抽样时必须知道这点。**

5. **同一段文本有三份副本**：`inclusion_dims` / `exclusion_dims` / `bucket_labels` 除 `config/research_profile.yaml` 外，还在 `src/scholar/research_profile.py` 的 `_DEFAULT_PROFILE` 与 `test/test_research_profile.py` 的 `FIXTURE_*`。
   改任一处会红 5 条测试，且测试名叫 `..._matches_pre_refactor_text`——**看起来像「预期内、更新文本即可」，但把 fixture 复制成新文本会把这组测试从「防止 filter 行为突变的唯一锚点」降级成复读机**。
   正确改法：保留旧 fixture 不动，改成 `新文本 = 旧 fixture + 新增段` 的构造式断言。

6. **`prompt_version` 无任何消费方**：`src/` 与 `scripts/` 全库无读取方，无分支、无缓存键；测试只断言非空与两处一致（`test/test_llm_filter.py:130/138/373/380`），**无任何测试钉死具体哈希**。改 prompt 造成的「md5 断代」实际代价仅为分析上多一个桶。缓解：把 `whitelist_filter_prompt.md:1` 的人读标签升版（如 `filter-v3.1`），并把 prompt 改动与验证结果放进同一 commit。

7. **`bucket_labels` 是开放式合并键**：`src/scholar/research_profile.py:155` `_OPEN_ENDED_DICT_KEYS`，yaml 新增维度不会被 `_deep_merge_defaults` 丢掉。`test/test_research_profile.py:334` 已有一条拿 `"H"` 写的测试；`test/test_vault.py:257-266` 则拿 `bucket=["H"]` 当「未知值」测兜底——**若将来真新增 H 维度，这条测试语义会反转，需换一个真未知值。**

8. **launchd 无常驻进程**：`config/launchd/` 五份 plist 无一设 `KeepAlive`，全是 `RunAtLoad` / `WatchPaths` / `StartCalendarInterval` 一次性调用，每次新起进程、yaml 每次重读。
   （曾判断「`vault.py` 的 `BUCKET_LABEL` 模块级求值 + `lru_cache` 会导致同进程不刷新」构成技术债——**前提错误，已撤销**。唯一可观测场景是交互式 REPL。）

9. **`original_abstract` 大多不是摘要**：94% 来自 Google Scholar 邮件臂，中位数 **669 字符**、**32.5% 以「…」结尾**，内容是「标题+作者+期刊+被砍断的正文」；pubmed 臂中位数 1955。`src/scholar/workflow.py:812` 再截到 `[:800]`。
   **因此任何需要读方法节才能判的维度（如「假设可操作化」「结构化掩码消融」）在这条链上无定义——不是难，是不可能。**

10. **裁决噪声约 10%**：同一篇论文在相同 model + 相同 prompt_version 下的重复裁决共 2017 对，308 对（15.3%）改变，其中 EXCLUDE↔MAYBE 209 对（10.4%）、INCLUDE↔MAYBE 约 2.2%。
    且 `src/scholar/llm_client.py:368-369` 的 claude-agent 分支**整个丢弃 `temp` 参数**，无法设 temperature 或 seed。
    **任何回测必须 k-of-n 重复取多数票，阈值须相对噪声定义，不能用绝对百分比**（例：「翻转率 ≤15%」这条线整个落在底噪里，测不出东西；「现有 INCLUDE 零降级」在 2.2% 噪声下抽 100 篇必然失败的概率约 89%）。

11. **批内容影响裁决**：`FILTER_BATCH_SIZE = 20`（`src/scholar/workflow.py:32`），整批序列化进同一个 `{{PAPERS_JSON}}`、一次 completion 判完，**同批互为上下文**。测量时不得按期望标签排序；重复跑应换洗牌而非原样重跑；不得为凑一批而调大 `FILTER_BATCH_SIZE`（那不是生产条件）。

12. **整批 LLM 失败会静默降级**为关键词裁决（`src/scholar/workflow.py:539-541` `_fallback_partition`，`stage="keyword_fallback"`）。**测量必须逐条断言 `stage == "llm_judge"`**，否则会拿到一个贴着 filter-v3 标签的白名单召回率。
    **生产已发生**：`digest_20260817_090002` 的 `fallback_count = 40`——整整两个 batch（`FILTER_BATCH_SIZE=20`）的 LLM 调用整批失败、静默降级，只留一行 warning。

19. **`output/` 全目录零 git 跟踪。** `.gitignore:44` 是裸 `output/`，实测 `git ls-files output/ | wc -l` → **0**。2343 篇札记 md、83 份 sidecar、`literature_index.json`、`embeddings.sqlite3` **全部无 git 兜底**。
    注意 `src/scholar/notes_index.py:104` 的注释写「md 是人写的、进 git、跟着札记走」——**在本仓库是错的**，它进不了 git。
    **推论：代码侧的 commit 边界是这条流水线仅剩的回滚单位。** 凡是会重写札记/索引的动作，`git checkout` 都救不回来。

20. **`literature_index.json` 是两个 launchd job 的 WatchPaths 触发源。** `config/launchd/com.xlbd.scholar-embed.plist`（ThrottleInterval 600）与 `com.xlbd.scholar-vault.plist`（120），两者均 `RunAtLoad true`。
    **碰一下索引 = 自动重嵌向量库 + 自动重建 vault + vault 侧自动 git commit，全程无人值守。** 这是「静默传播」的实际发动机。
    副作用之一：第 15 条那个 24h 陈旧陷阱因此会自愈——**前提是 Ollama 起着**；Ollama 挂了 `notes_embed.py` 退出码 3，时钟就一直上着膛。
    好消息：`~/Documents/ScholarVault` 自成干净 git 库、每次同步一个 commit，所以 vault 侧改动的回滚是全流水线最便宜的（`git -C ~/Documents/ScholarVault reset --hard`）。

### B. 依赖 RAG 实现，改动后需重测

13. **「现行 filter-v3 + sonnet 仍排除那 10 条」**（决策一的核心数据）是**带 RAG 近邻注入**跑出来的——其中一条判词为「与库内 kerdabadi2023Contrastive 同文重复」，证明注入生效。`414d30d` 起 `embed_store.chunks_from_index` 会把撤稿论文踢出向量库，**向量库内容已变化**。

14. **近邻自命中：机制属实、生产已发生，但「自我排除」是错的处方（2026-08-18 修正）。**

    **现象（生产实证）**：`_library_neighbors`（`src/scholar/workflow.py:673-790`）检索全部 `level=='paper'` 向量，`top_k=3`、`min_sim=0.65`，无自我排除。而 Google Scholar 邮件臂每周重复推送已入库论文，`seen` 去重又发生在 filter **之后**（`scripts/ingest_notes.py:279-294`），所以裁决时必然检索到论文自己。
    `digest_20260817_090002` 实测：**21 处标题完全相同的自命中**（sim 0.744–0.900），其中 **9 处进了 LLM**；**2 条判词直接点自己的名**（`maeng2026Interpretable` / `lu2026Construction` 判成「与已收 X 重复」，那个 X 就是它自己）；**1 条判词是自己 `one_line` 的近似复述**（`medina2026Aligning`）。当周 47 篇入选里 16 篇的标题在上一周入选列表中逐字出现。
    注：08-03 / 08-10 两轮 `library_neighbors` 全空（向量库陈旧被静默降级，即第 15 条），所以泄漏只有 3 周历史。已实现损害被 ingest 层的 `dedup_key` 去重兜住，未产生存活的重复札记。

    **原处方（自我排除）是错的，已作废**。三条否决理由：
    - prompt 本来就要模型用近邻查重（`workflow.py:47-49`：「若与已收文献纯重复、增量微小，降为 MAYBE，并在 one_line 中点名重复的 citekey」）——判词点名是规则**正确生效**；
    - `dedup_key` 在 preprint→期刊版（DOI 变了）会失手，正是近邻层兜住的那一类；
    - **`src/scholar/journal_screen.py:378-380` 的 `in_library` 依赖自命中**且走同一个 `_library_neighbors`，无条件排除会让那一整列静默变 `false`，而它就在「唯一真人工勾选」的候选表上，无任何测试会红。

    **实际采用的修法（2026-08-18 落地）**：保留自命中本体，只把 `one_line` 替换为标记文本，`citekey` / `year` / `sim` 原样保留。`in_library` 只读 `sim`，故零影响；自身识别用 `_citekey_utils._norm_title` 比对 paper chunk 文本首行（该首行即标题），无需 DOI——Scholar 臂的 `doi`/`arxiv_id` 均为 `None`。测试见 `test/test_embed_store.py::test_library_neighbors_self_hit_strips_one_line` 与 `..._keeps_in_library_signal`。

15. **向量库过期会静默降级**：超过 `STALE_DEGRADE_SECONDS`（24h，`src/scholar/embed_store.py:43`）会抛异常，把每一条降级成零近邻（`workflow.py:738-744`），**只留一行 warning**。
    且 `output/scholar_notes/literature_index.json` 与 `embeddings.sqlite3` **都被 gitignore，`git status` 永远不会警告**。任何测量前须快照这两个文件并把 `notes_dir` 指向副本。

### C. 已撤销的错误陈述

16. **Long Chen 那 5 篇的裁决从未落盘。** 当时用临时脚本跑的，没写盘；扫全部 83 份 sidecar 确认找不到（唯一的 165 条 sonnet 裁决全属 `digest_20260817_090002`，内容是高能物理 / 机器人 VLA / 扩散语言模型）。**不得作为证据引用**；若要用须以 `force_include=False` 重跑并写盘。

17. **「四大排除类中含分布偏移词的有 843 条（5.8%）」是子串 bug。** `ood` 未加词边界，`blood` 349 + `good` 199 + `neighborhood` 115 + `childhood` 104 + `food` 88 + `understood` 81 …加 `\bood\b` 后 **841 → 47 条（0.3%）**，虚高约 18 倍。
    **本文件所有统计已改为带词边界。做同类统计务必加 `\b`。**

18. **撤稿处置口径已于 `414d30d` 变更**：从「一律移出札记库 + 库外独立留档」改为「**保留札记**（读过它、判断过它的记录不该消失），只打标记 + 踢出向量库与书目」。判据是札记 md 裁决行上的 `⚑ RETRACTED`（`src/scholar/notes_index.py` 的 `RETRACTED_FLAG` / `is_retracted()`）。
