# 对标 GitHub 成熟方案的入库+检索代码级审计（2026-08-23）

## 结论

把本库 RAG 的 15 个关键环节与 4 个高 star 成熟方案逐项对比（代码级，两侧均有 file:line），并对全部疑点做了真库实证：

**没有严重错误（❌ = 0）。** 2 条 ⚠️，4 条 🟡 合理偏离，9 条 ✅（其中 6 条我们的防线**超出**全部对标框架）。此前三轮对抗审计修过的方向（`_gate_sparse`、hybrid 排序语义、增量闸）与成熟框架逐一印证后全部站得住。

> **两条 ⚠️ 已于 2026-08-23 当日修复并回归**（用户裁决后追加执行，本文保留发现时的原始记录，修复详情见每条末尾的「修复」小节与 `rag_bench_baseline_2026-08.md` 同日追记）。修复后：全量 pytest **1662 passed / 0 failed**（+7 条新锚测试），bench 存量 75 条**逐 case 零回退**，新增 9 条 acronym 类 hybrid **9/9**。

判定纪律：框架默认值 ≠ 真理；只有当偏离在**本库真实语料上跑出可复现的精度损失**才升级 ⚠️/❌。每条 ⚠️ 附最小复现。

## 对标基线

| 项目 | commit / 版本 | 取材 |
|---|---|---|
| FlagOpen/FlagEmbedding（bge-m3 官方） | `292ad78`, 2026-08-14 | `inference/embedder/encoder_only/m3.py`、`research/BGE_M3/README.md` |
| langchain-ai/langchain (~125k★) | `339eaa6`, core 1.6.1 | `langchain_classic/retrievers/ensemble.py`、`langchain_core/indexing/api.py` |
| run-llama/llama_index (~46.5k★) | `d802122`, core 0.14.24 | `retrievers/fusion_retriever.py`、`ingestion/pipeline.py` |
| deepset-ai/haystack (~24k★) | `c7cb46c`, 3.2.0-rc0 | `document_stores/in_memory/document_store.py`、`joiners/document_joiner.py` |

## 逐项判定表

### A. 入库链路

| # | 环节 | 我们 | 对标 | 判定 |
|---|---|---|---|---|
| A1 | 切分粒度 | 三级结构化 chunk（`p:`/`ab:`/`h:`），零通用切块、零 overlap，`embed_store.py:190-291`；abstract 裁 800 字 | LangChain splitter 默认 4000 字符/overlap 200（`text_splitters/base.py:62`）；LlamaIndex SentenceSplitter 1024 token/200（`sentence.py:43`）；Haystack 200 词/0（`document_splitter.py:57`） | 🟡 语义单元代替滑窗；最长 chunk 2113 字符仍小于 LangChain 默认块。瘦/厚双向量有三轮 A/B 档案（`embed_store.py:196-201`）支撑，不是拍脑袋 |
| A2 | embedding 调用 | Ollama `/api/embed` 仅 `model+input`（`embeddings.py:136-139`），依赖默认 truncate=true | 官方 encode **默认 max_length=512**，8192 须显式传（`m3.py:294-295`, README:123-126） | ⚠️ **潜伏截断风险，见下文实证**（当前零截断） |
| A3 | 身份键 | 内容寻址 `h:<citekey>:<role>:<sha1_12>:<seq>`，携带结构语义 | LangChain `hash(content)+hash(meta)→uuid5`（`api.py:213-230`）；LlamaIndex `sha256(text+meta)`（`schema.py:801`）；Haystack `sha256(content+meta+embedding)`（`document.py:103`） | ✅ 同为内容寻址；我们的 id 还带 level/role 可回查 |
| A4 | 增量判重 | `sha256(model+text)[:16]` 三态 diff（`embed_store.py:294`, `:557-565`） | LangChain RecordManager 内容 hash **不含模型名**，换 embedding 模型须人工记得重建 | ✅ 反超：模型名织入哈希 + `model_matches` 硬闸（`:476-483`），LangChain 的坑我们天然免疫；元数据变更走独立 UPDATE 不重嵌，比 LangChain（meta 变即换 id 重嵌）省 |
| A5 | 更新/删除 | 期望集 diff：`to_delete = existing − expected`（`:565`），UPSERT 覆盖 | LangChain cleanup=`full`："Delete all documents that have not been returned by the loader during this run"（`api.py:351-364`）；LlamaIndex `UPSERTS_AND_DELETE` 同构（`pipeline.py:495-505`） | ✅ 与两家的声明式全集同步语义逐字等价。代价同样一致：合成产物（概念页/qa）不传就会被删——已知欠账的成因在两家框架同样存在 |
| A6 | 撤稿踢库 | `is_retracted → continue` 出期望集（`embed_store.py:218-225`），diff 自动删 | 框架靠显式 delete API；LangChain full 模式下与我们同构 | ✅ 43% 失效 bug 已修且有三条回归测试锚（`test_notes_index.py:1691/:1738/:1761`） |
| A7 | 一致性校验 | 8 道硬闸（空集/骤缩/ab:双闸/schema/模型/并发快照/inode）+ 读侧维度闸 + 新鲜度降级 | LangChain cleanup=full **loader 返回空集会把整库删光，无任何守卫**；Haystack 只在查询时报维度错（`document_store.py:877-905`）；LlamaIndex 无 | ✅ 全面反超。这是四家都没有的防线 |

### B. 检索链路

| # | 环节 | 我们 | 对标 | 判定 |
|---|---|---|---|---|
| B1 | 查询编码一致性 | query/doc 同客户端同模型同归一化，零 instruction（`notes_search.py:380-392`） | 官方 README:75-76："bge-m3 **no longer requires adding instructions to the queries**"；Haystack 甚至不校验 query/doc embedder 同模型 | ✅ 与官方一致，校验反超 Haystack |
| B2 | 相似度 | L2 + matmul 点积 = cosine（`embeddings.py:128-130`, `embed_store.py:909`），暴力精确扫 | 官方归一化内积（`modeling.py:334-346`, `577-581`）；LlamaIndex 默认 cosine；22k 规模精确扫无 ANN 召回损失 | ✅ |
| B3 | 稀疏侧 | 自研 BM25 k1=1.2/b=0.75，Lucene 平滑 IDF；分词 `[a-z]{3,}`+领域停用词+中文 2-gram（`vault.py:543-552`） | Haystack 默认 BM25L k1=1.5/b=0.75，`\w+` **保留单字符 token、无停用词表**，但中文整串一个 token（`document_store.py:67-107` 实测退化）；IDF 用全库、打分只在过滤后候选（`:742-764`） | ⚠️ **分词器丢 2 字母缩写+领域停用词，见下文实证**。中文 2-gram 反超 Haystack；掩码子集现算 IDF 属可辩护偏离（子集即语料），记 🟡 |
| B4 | RRF | k=60、rank 从 1 起、等权、无 tie 处理、每路 top-200（`notes_search.py:200-291`） | LangChain c=60 rank 从 1 起（`ensemble.py:326-335`）；**LlamaIndex rank 从 0 起**（`fusion_retriever.py:128-135`，偏离 Cormack 原文，我们反而更忠实）；Haystack k=61 补偿 0-base（`misc.py:156-189`）；三家皆无 tie 处理 | ✅ 与全部对标一致；gate 后重排名次 vs 原始名次已做 75-case A/B：**hit@5/@10 两种取法完全相同**（见下文），差异只在同分段排序 |
| B5 | min-score 在 hybrid 下 | 两路统一按余弦过滤（`_gate_sparse`, `notes_search.py:157-172`），排序仍按 RRF 名次并在 `--help`/skill 文档写死此语义 | LangChain Ensemble **无任何阈值机制**；Haystack 核心仓 grep `score_threshold` 零命中；LlamaIndex SimilarityPostprocessor 在 **fusion 之后**对融合分生效（`retriever_query_engine.py:161-163`）——用 cosine 尺度的 cutoff 砍 RRF 分，正是我们 R1 修掉的那类量纲混用 | ✅ 反超：三家中没有一家把"融合分与余弦不可比"处理干净，LlamaIndex 的默认挂点还会踩进去 |
| B6 | 阈值体系 | thresholds.py 集中 4 常量，其中 3 个有标定档案；0.4 无标定已记账 | 官方 FAQ："what matters is the relative order... select an appropriate similarity threshold **based on the similarity distribution on your data**"（`baai_general_embedding/README.md:42-49`）；框架全部把阈值当用户自填超参 | ✅ 标定纪律正是官方建议的做法；qa.py 散落阈值各带标定表注释，并入与否属整洁问题非正确性问题 |
| B7 | top-k 管线 | 每路 200 → RRF → limit 10 | LlamaIndex 默认 similarity_top_k=**2**（`constants.py:12`）；LangChain 无截断；Haystack top_k 默认 None | ✅ 候选深度显著优于框架默认 |
| B8 | 下游参数分化 | digest 0.62（90 对人工标定+离题探针 0.575）/topics 0.55+α0.85（36 query 标定）/qa 关 α（top1 分布未验证，`qa.py:475-482`） | 无对标物；官方立场支持按数据分布分别标定 | ✅ 分化各有档案，qa 关 α 的理由成立 |

## ⚠️ 两条的实证记录

### ⚠️-1（A2）：Ollama 实际输入上限是 2048 token，不是 8192；"4 倍余量"推理不成立

实证（2026-08-23，`bge-m3:latest` @ Ollama 本机）：

- `ollama show bge-m3` 报 context 8192，Modelfile **无** `PARAMETER num_ctx`；但合成中文文本在 **2872 字符 = 2048 token** 处越过 `truncate=false` 报错线（`{"error":"the input length exceeds the context length"}`），二分定位 2872~2877 字符。真正卡脖子的是 llama.cpp 对 embedding 模型的 **`num_batch`（默认 2048）**：请求体加 `options={"num_batch": 8192}` 后同一 6034 字符文本全量吃进（prompt_eval_count=4176）。
- 真库排查：最长 chunk `h:lee2025Mirrams:refutable:234c9e30c51f:0`（2113 字符）实测 **1414 token**；对最长 60 条逐条 `truncate=false` 试探，**0 条超限——当前生产库零截断**。
- 勘误：`scholar_audit_2026-08-23.md` 证伪节"bge-m3 上下文 8192，4 倍余量"的**结论侥幸成立、论据错误**——真实余量是 2048/1414 ≈ **1.45 倍**，且 highlight 无长度上限，一条 ≳2900 中文字符的句子入库即被静默截断（生产默认 truncate=true，无任何告警）。

**修复建议**（一行改动，不触发重嵌——现库全部 chunk ≤1414 token，向量数值不变）：`src/scholar/embeddings.py:136-139` 请求体加 `"options": {"num_batch": 8192}`；更彻底的做法再加 `"truncate": false`，让未来 >8192 token 的输入直接抛 `EmbeddingError` 走既有 best-effort 通知链，符合本库"宁抛不静默"的既定哲学。

**修复（已完成，2026-08-23）**：两条都做了——`embeddings.py` 新增 `_NUM_BATCH = 8192` 常量与 `truncate: False`，并新增 `_status_error_message()` 在超长 400 时点名批内最长的那条文本（一批 64 条只要一条超长就整批失败，只报状态码等于让人瞎猜）。

- **不触发重嵌已实测**：新旧请求体对真库 3 条最长 chunk + 2 条典型 query 产出的向量**逐位相同**（max|Δ| = 0.000e+00），与库内已存向量 cos = 1.00000000 —— 22592 条向量继续有效，无需 `--full`。
- 超限行为已验证：16950 字符文本正确抛 `EmbeddingError`，信息含长度、首 60 字符与 num_batch 提示。
- 锚测试 3 条（`test_embed_store.py`）：请求体契约（truncate=False + num_batch≥8192）、超长错误必须点名最长文本、非超长错误不许被伪装成"文本太长"（否则会把人引去砍文本）。

### ⚠️-2（B3）：BM25 分词器丢 2 字母缩写 + 领域词停用，缩写类 query 的关键词路失明

`vault.tokenize`（`vault.py:543-552`）的 `[a-z]{3,}` 丢掉全部 2 字母 token，`_STOP` 含 data/model/method/result/study/paper 等领域词。对标：Haystack `\w+` 保留单字符 token 且无停用词表；LangChain/LlamaIndex 核心库不内置停用词。真库实证（2026-08-23）：

- 语料侧：含独立 EM/MI/IV 的 chunk 各 **47/113/538** 条（正是高 IDF、BM25 本该最出力的词）。
- `"EM"` sparse 检索 0 命中；`"EM algorithm"` 与 `"expectation maximization"` 的 sparse top-3 **完全不相交**——前者实际退化为只查 `algorithm`，BM25 top10 全是算法选择文献（tornede2022Algorithm 等，与 EM 无关）。
- 反事实（monkeypatch 保留大写缩写、不改仓库）：同一 query 的 BM25 top1 变为 EM 因果论文 li2025Causal——因果链坐实。
- 波及默认模式：hybrid 下 `"EM algorithm"` top-5（kurstjens2022/yang2023/gao2025…）与 dense top-5（sui2022Find/little1988Test/morvan2020Neumiss…）完全不相交，泛词 BM25 命中挤占了语义命中。这也给 rag_bench 里 "dense 56@1 优于 hybrid 53" 补了一个机制解释。

**修复建议**：在 notes_search 侧做 BM25 专用分词包装（原版 tokenize + 原文中 `[A-Z]{2}` 独立缩写小写加标记），**不动 vault 语义图共用的 tokenize**；`_STOP` 里的领域词逐个复核。改后必须跑 rag_bench 并补缩写类 case（现 75 条 case 无一含 2 字母缩写，这类失明 bench 测不出来）。

**修复（已完成，2026-08-23）**：新增 `notes_search.bm25_tokenize()`，文档侧与查询侧共用（只改一侧等于白补）。只补**恰好两个**大写字母：单字母噪音太大，三字母及以上（EHR/MNAR）本就被 `[a-z]{3,}` 收着、再补一份等于给它们双倍词频。`vault.tokenize` 本体未动（近邻图与 qa 词面查重还挂在上面）。

- 效果：`EM` 从 0 命中 → 5 篇相关文献；hybrid 的 `EM algorithm` top5 与 dense top5 从**零交集**变为 4/5 重合。
- bench：存量 75 条 hybrid/sparse/dense **逐 case 零回退**（53/63、47/54、56/68 全部不变）；新增 `acronym` 类 9 条，hybrid 7→**9/9**、sparse 6→**8/9**，`acr-001`「EM algorithm」rank 7→@1。
- 锚测试 4 条（`test_notes_search.py`）：保留两字母缩写、拒绝小写与单字母、**必须是 vault.tokenize 的超集**（只做加法）、端到端缩写篇压过泛词篇。
- **`_STOP` 领域词复核未做**：BM25 的 IDF 本就会自动降权高频词，停用词表硬删属双重惩罚，但改它影响 vault 近邻图与 qa 查重两处已标定分布，需要独立实验 + 各自回归，不宜与本次修复混在一批。留作遗留课题。

## B4 名次取法 A/B 实验存档

疑点：`_gate_sparse` 过滤后对 BM25 幸存者**重排名次**（现状）会抬高其 RRF 贡献，标准 RRF 应保留原始名次。忠实复刻双泳道 hybrid（瘦/厚 dense + BM25 三路、RRF k=60、跨级按 citekey 取 max）在 rag_bench 全部 75 条 query 上对比：

```
min_score=0.4:  现状 hit@5=63 @10=66 | 原始名次 hit@5=63 @10=66 | top10 序有差异 13 条
min_score=0.62: 现状 hit@5=71 @10=71 | 原始名次 hit@5=71 @10=71 | top10 序有差异 44 条
```

命中指标零差异，差异全部发生在命中集内部的同分段互换。判定 ✅ 维持现状（Haystack 的 RRF 同样只认名次；LangChain 对过滤后列表取名次的行为与我们一致）。注：此处 hit 数字是实验内部 A/B 口径（双泳道+max 聚合，无展示层），与 rag_bench 官方基线（56/68）口径不同，不可互比。

## 反超项（对标框架没有、我们有）

1. 模型名织入 text_hash + `model_matches` 增量硬闸（A4）——LangChain 换模型需人工重建，无守卫。
2. 空期望集/骤缩闸/ab: 专项双闸（A7）——LangChain cleanup=full 在 loader 返回空集时会静默删光整库。
3. 并发快照复核 + inode 比对 + WAL 换库纪律（A7）——四家均无。
4. 新鲜度降级（24h 拒用近邻、5min 路由陈旧）——四家均无。
5. hybrid 融合分与余弦量纲的显式隔离（B5）——LlamaIndex 默认挂点会把 cosine 尺度 cutoff 施加在 RRF 分上。
6. 阈值标定档案化（B6）——与 bge 官方"按自家分布标定"的建议一致，框架只提供用户自填超参。

## 遗留课题

1. ~~⚠️-1 修复~~、~~⚠️-2 修复~~、~~bench 补缩写 case~~ —— **均已于 2026-08-23 当日完成**（见上），
   其中 ⚠️-2 的首版修复在同日 R1 复审中被推翻重做（见下节）。
2. **`_STOP` 领域停用词复核**（未做）：`data/model/method/result/study/paper` 等词在本库（全是方法学论文）被当停用词删掉，而 BM25 的 IDF 本就会自动降权高频词，硬删属双重惩罚，且会让 `missing data` 这类术语退化成只查 `missing`。但 `_STOP` 由 vault 近邻图与 qa 词面查重共用，改它要同时重标定两处分布，需独立一批：先量停用词对三条链路各自的影响，再决定是否给 BM25 单独一份词表。
3. **概念页/qa 合成产物仍不进向量库**（A5 对比时确认这是声明式全集同步的固有代价，LangChain/LlamaIndex 同款）——原欠账不变，折中方案仍是独立小向量库。

## 复现

探测脚本三份（probe_ollama_ctx{,2,3}.py、probe_bm25_acronym.py、probe_rrf_rank_basis.py）在会话 scratchpad，关键方法与数字已全部内联本文；重跑要点：truncate=false 二分定边界、真库最长 60 条逐条试探、monkeypatch 分词反事实、75-case 双泳道 RRF A/B。

---

# 2026-08-23 R1 / R2 复审（对上面这批修复再迭代两轮）

对标审计交付后又跑两轮，**换攻击面而不是重读同一段代码**：R1 打刚提交的修复及其
直接邻域，R2 打真库端到端一致性。结论：**R1 挖出 2 条真问题（同源，已修）**，
**R2 全绿零发现**。

## R1：修复自身的对抗复审

| # | 检查 | 判定 | 依据 |
|---|---|---|---|
| R1-A | `num_batch=8192` 的运行时代价 | ✅ | 最坏批（64 条 × 真库最长 2113 字符）16.9s vs 旧 14.7s、940MB vs 664MB；真库随机 64 条混合批 3.3s vs 3.3s **零差异**。48GB 机器上可忽略 |
| R1-B | 入库中途 embed 失败的原子性 | ✅ | `sync_store` 是"全部 embed 完成后单事务落盘"，`EmbeddingError` 抛在写事务之前，**库一个字节不变**。符合"宁可失败不要坏索引"的立场 |
| R1-C | 只补大写 → df 畸变造假阳性 | ⚠️ **已修** | 见下 |
| R1-D | 只补大写 → 小写查询整条失效 | ⚠️ **已修** | 见下 |
| R1-E | 超长 query 的错误语义 | 🟡 记录不修 | `truncate=false` 后超长 query 抛 `EmbeddingError`，notes_search 归到退出码 3（语义是"起 Ollama"）。但触发门槛是 **>11500 中文字符**的 query，且下游 embed 输入（qa question / digest one_line / topics 证据）实测全是短文本。错误正文本身点名了"文本超出上限、最长 N 字符"，够诊断 |
| R1-F | `[A-Z]{2}` 在真库上的误报率 | ⚠️ 并入 R1-C | 补出 370 个 token，top40 全是真缩写（iv/ai/ml/ci/or/em/mi…），**但**全大写标题把 `BY/IN/OF/ON` 灌了进来（df 10-12），`NO2`/`SO2`/`PM2.5` 被切成 `no`/`so`/`pm` |
| R1-G | Ollama 不认 `num_batch` 时的降级 | ✅ | 退回 2048 上限；因 `truncate=false` 同在，超长会**报错**而不是静默截断。失败模式是安全的 |
| R1-H | 分词性能 | ✅ | sparse 查询 0.96s（三次一致），与改动前无差异 |

**R1-C + R1-D 同源，是一条根因**：只补大写 → 语料里 `of` 仅在全大写标题中被收 →
df 被压到 12 → IDF **7.50 比真缩写 `em` 的 6.16 还高**。于是同时得到两个坏结果：

- `MODELS OF CARE` 比 `models of care` 凭空多召一篇靠 `of` 命中的无关文献；
- `em algorithm`（小写）的 hybrid top5 **原封不动还是修复前的病态结果**——修复只对
  按了 shift 的用户生效，而当时新加的 9 条 acronym case 全是大写、照样满分 9/9。

**改法**：两字母词不论大小写全收，让 IDF 自己决定权重（这本来就是 BM25 的设计哲学；
硬删虚词要维护黑名单，而真库里 OR/US/AS/AT/IS/IF/AN 恰恰都是真缩写）。修复后
`MODELS OF CARE` 与 `models of care` 结果**完全一致**，`em algorithm` 召回真 EM 文献。
IDF 对照表、逐 case 回归、新基线见 `rag_bench_baseline_2026-08.md` 同日追记。

## R2：真库端到端一致性（全绿）

| # | 检查 | 口径 | 结果 |
|---|---|---|---|
| R2-1 | 期望集 ↔ 库内 diff | `notes_embed.py --dry-run` | 期望 22592 = 库内 22592，**待嵌 0、待删 0** |
| R2-2 | chunk id 集双向 diff | `chunks_from_index` 重算 vs 库内全量 | 孤儿 **0**、缺失 **0**（最严口径，不是按 citekey 粗比） |
| R2-3 | 向量维度 | 全量 22592 条 | 异常 **0**，全为 1024 |
| R2-4 | L2 归一化 | 全量 | 范数 min=max=**1.000000**，偏离 >1e-5 的 **0** 条 |
| R2-5 | 全零 / NaN / Inf 向量 | 全量 | **0 / 0 / 0** |
| R2-6 | `text_hash` 与文本是否对得上 | 全量重算 `sha256(model+"\x00"+text)[:16]` | 不匹配 **0**（22592/22592） |
| R2-7 | chunk id 重复 | 全量 | **0** |
| R2-8 | **向量真的对应它的文本吗** | 分层抽样 200 条（各 level 各 60 + 最长 20）重新 embed | 与库内向量余弦 **全部 = 1.0**，min=0.99999988，<0.9999 的 **0** 条 |
| R2-9 | 撤稿踢库 | 真库 | 2 篇撤稿（mishra2024Knowledge / farnoosh2025Diabetesxpertnet）**均不在库**，札记保留 |
| R2-10 | 门槛体系 | 真库探针 | 硬离题探针（"高压锅炖牛肉"/"世界杯点球"）在 0.62 下 **0 篇**（无门槛时 top1 余弦仅 0.3297）；`--min-score 0.95` 也是 0 篇——R1 审计修的"门槛越高越脏"无回归；正常 query 0.62→124 篇 / 0.55→200 篇，单调正确 |
| R2-11 | 阈值标定是否被 BM25 改动动摇 | 推理 + 探针 | 否。`--min-score` 语义是余弦下限，`_gate_sparse` 回查的也是余弦，分词改动进不了这条量纲 |
| R2-12 | 下游消费方影响面 | grep | `bm25_tokenize` 仅在 `notes_search.py` 内部使用；digest 近邻 / topics / qa 全走 dense，**零影响**（dense 逐 case 回归也确认全等） |
| R2-13 | 极端输入健壮性 | 28 种 junk 输入 | 空串/单字/纯数字/emoji/全角/西里尔/换行全部返回 `[]` 不抛；`EM's`/`(EM)`/`EM-algorithm`/`用EM法` 都能切出 `em`，而 `AImodel` 里的 AI 粘在词内**不切** |

R2-8 是这轮最本质的一条：它同时证明了(a)库里没有"向量与文本错位"这类致命 bug，
(b)改过请求体（`truncate:false` + `num_batch=8192`）之后产出的向量与库内**逐位一致**，
22592 条继续有效，不需要 `--full` 重建。

## 这两轮新增的回归保护

- `test_bm25_tokenize_is_case_insensitive` —— 钉死 R1-C，改回只认大写立刻挂。
- `test_bm25_tokenize_lowercase_acronym_query_works` —— 钉死 R1-D 的端到端行为。
- `test_bm25_tokenize_boundaries` —— 钉死"独立成词才收"（`AImodel` 不切、连字符/括号/
  撇号/中文都算边界）与 junk 输入不抛。
- `rag_bench` `acr-010~012` 三条**全小写**哨兵 case（当前 rank 2/2/1）。

选哨兵的判据值得单记：**死 case 和满分 case 都当不了哨兵**，要选"当前能过、一旦退化
就会掉出 top5"的。同理，"bench 测不出的东西会无声退化"这个坑在本项目里已经踩了两次
（第一次是缩写类整体缺失，第二次是有了缩写类但全是大写），补 case 时要连**对称维度**
一起补，不能只补一个方向。
