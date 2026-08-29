# rerank / HyDE 检索增强试验（2026-08-29）

## 背景与动机

精读 gao2024Modular（Modular RAG 综述）后按其算子清单盘点本库缺口，选出三个候选
（依据：高引技术 × 本库适配度）：post-retrieval 的 model-base rerank、pre-retrieval
的 HyDE（884 引）、生成侧的 lost-in-the-middle 证据排序（4821 引）。全部过
`rag_bench.py` 87 case 这把尺裁决。

**先复跑当日基线**（库内容漂移实证：08-23 档案 hybrid 63@1/0.8102，当日已 66@1/0.8162
——库加了新篇。所有 A/B 必须同日同库，旧档案数字不能直接当对照）。

## 基线（2026-08-29，2426 papers，含当日新增 gao2024Modular）

| 模式 | @1 | @5 | nDCG@10 |
|---|---|---|---|
| hybrid（默认） | 66/87 | 76/87 | 0.8162 |
| dense | 65/87 | 78/87 | 0.8208 |

## E2 rerank：bge-reranker-v2-m3 重排 top-10 —— **压倒性胜出**

方法：纯离线重排。拿当日 bench 每条 query 的 top-10 citekey，doc 文本取向量库
`ab:<citekey>` chunk（title+判词+摘要[:800]），无摘要篇退回 `p:<citekey>`（title+判词）；
交叉编码器 BAAI/bge-reranker-v2-m3（transformers 直载，MPS），max_length 512。
检索本身不动，只重排序。

| 配置 | @1 | @5 | nDCG@10 | paraphrase@1 | acronym@1 |
|---|---|---|---|---|---|
| hybrid 基线 | 66 | 76 | 0.8162 | 16/30 | 10/12 |
| **hybrid+rerank** | **75** | 79 | 0.8642 | 21/30 | **11/12** |
| dense 基线 | 65 | 78 | 0.8208 | 18/30 | 8/12 |
| **dense+rerank** | **75** | **81** | **0.8805** | **25/30** | 8/12 |

- 逐 case 差分：dense+rerank 赢 15 / 输 5 / 平 67；hybrid+rerank 赢 13 / 输 7 / 平 67。
- **最硬的档翻身了**：paraphrase（中文换述→英文语料，08-21 结论「bge-m3 跨语对齐是
  硬上限、换 embedding 才能救」）dense+rerank 到 25/30 @1（nDCG 0.6214→0.8286）——
  **不换 embedding 模型，重排就把跨语档打上去了**。roadmap 里「换 embedding」那条
  主菜的必要性需要重估。
- 回归清单（rerank 伤了谁）：全部集中在短关键词/缩写 query——hybrid 侧 legacy-002
  （rank 1→5）、acr-007 与 oneline-010（1→2）；dense 侧 acr-001（2→4）、acr-002/
  acr-010/oneline-010/para-024（1→2 或 1→3）。交叉编码器对「EM algorithm」这类
  无上下文短查询判别力弱，属已知特性。
- 打分理智检查通过（相关对 +1.65 / 离题对 −11.0）；libomp 双载须
  `KMP_DUPLICATE_LIB_OK=TRUE OMP_NUM_THREADS=1`（计算在 MPS 上，OMP 不参与打分路径）。
- 延迟：模型载入 3.0s（一次性）+ 每 query 10 对 0.31s——CLI 单次调用可忍，批处理
  （digest 近邻、topics 证据）摊销后可忽略。

## E1 HyDE：LLM 假设摘要检索 —— **不采用**

方法：87 条 query 各生成一段 60-100 词英文假设摘要（claude CLI sonnet，生成缓存落盘
保证可复现），两变体走 `notes_search --mode dense`：hyde（纯假设文档）、qhyde
（原 query+假设文档拼接）。

| 配置 | @1 | @5 | nDCG@10 | paraphrase@1 |
|---|---|---|---|---|
| dense 基线 | 65 | 78 | 0.8208 | 18/30 |
| hyde | 54 | 66 | 0.6962 | 19/30 |
| qhyde | 62 | 72 | 0.7829 | 21/30 |

结论：HyDE 唯一的受益档是 paraphrase（qhyde 21/30，nDCG 0.7158→0.8112），其余
全档受损（en_title 15→14、zh_oneline 13→11、acronym 8→6、legacy @5 4→2）。
而它的受益档被 rerank 完全支配（25/30 > 21/30），且 rerank 零 LLM 成本、无逐查询
延迟与不确定性。**HyDE 在本库没有生态位**。附注：87 case 无一是问句式 query
（HyDE 主场），此结论限于现 case 集口径，若未来加问句档可重开。

## E3 lost-in-the-middle 证据排序 —— **不做（已覆盖 + 不可测）**

`topics.py` 拼 prompt 的证据已按分数降序（`picked.sort(key=-score)`），最强在头部，
lost-in-the-middle 的主要收益已在手；剩余增量（尾部也放强证据）无法裁决——
gen_bench 只审已落盘页面不重跑生成（零 LLM 成本是它的设计原则），重跑生成做 A/B
既烧订阅额度又被生成随机性混淆。不为不可测的改动动代码。

## 落地建议（2026-08-29 已落地，实现与建议的差异见节末追记）

1. **notes_search 加 `--rerank` 可选 flag**：取 top-K 候选（K=2×limit 或 20）重排后
   输出。默认关——短关键词/缩写查询会小幅受损，且 3s 载入对交互有感。
2. **digest 近邻注入与 topics 证据召回保持不动**：那两条链路靠 cosine 阈值门槛
   （0.62/0.55 标定值），reranker 分数是另一个不可比的尺度，**只能用于排序，不能
   用于门槛**——不要把 reranker 分拿去和任何 min_sim 比。
3. 若采纳，环境依赖记录：transformers 4.57.3 已在 env002_reader，模型经
   `HF_ENDPOINT=https://hf-mirror.com` 下到 HF 缓存（~2.3GB），libomp workaround 见上。
4. roadmap 的「换 embedding 模型」主菜建议重估优先级：其最大论据（中文换述跨语
   硬上限）已被 rerank 以更小代价解决大半。

> **落地追记（2026-08-29）**：已实现为 `notes_search --rerank`（`src/scholar/reranker.py`
> + notes_search.py 重排层，rag_bench.py 加 `--rerank {auto,on,off}` 透传）。与本节
> 建议的两处差异：①默认不是「关」而是 **auto——hybrid 开、dense/sparse 关**（dense
> 保「按余弦分看排名」契约，供 scholar-search 判重依赖；sparse 未做 bench 验证）；
> ②重排对象不是 top-2×limit 候选，而是 **`--limit` 截断后的最终展示集**（`--limit 0`
> 时封顶 RERANK_CAP=100）——集合成员由 RRF/余弦决定，rerank 只动展示顺序，与本试验
> 验证的口径逐字一致。铁律第 2 条（rerank 分与 min_sim 不可比）已写入 reranker.py
> 模块铁律并在 thresholds.py 铁律段互引。模型加载走「snapshot_download(local_files_only)
> 解析本地目录→from_pretrained(目录)」两步法——直接 from_pretrained(repo_id,
> local_files_only=True) 会被 transformers 4.57.3 的 _patch_mistral_regex→is_base_mistral
> 无视 local_files_only 打 huggingface.co（上游 bug，被墙网络下每次调用挂十几秒）。

## 上线后三臂验证（2026-08-29，5-subagent 审计批，87 case 在线实测）

| 臂 | @1 | @5 | nDCG@10 | MRR | 判定 |
|---|---|---|---|---|---|
| hybrid `--rerank off` | 66 | 76 | 0.8162 | 0.8090 | 与改造前基线**逐 case 全等**（零行为变化） |
| **hybrid auto（新默认）** | **75** | **79** | **0.8642** | **0.8816** | 与离线试验**87/87 全序逐条相同** |
| dense auto | 65 | 78 | 0.8208 | 0.8153 | 与 dense 基线逐 case 全等（默认不重排得证） |

臂2 按 gold rank 的差分：赢 13 / 输 3 / 平 71（输的是 legacy-002 1→5、acr-007 与
oneline-010 1→2）；正文「输 7」是 nDCG 口径（另 4 条是多 gold 的次位 gold 滑动，
首位 gold 仍 rank1）。重排臂全程无降级警告。

同批审计裁决与修复：打分期非 RerankUnavailable 异常由 exit 4 改为 traceback+降级
（对抗审计 F1，铁律 3 字面执行）；补打分数量错配炸响、NaN 整批弃用、CLI 短进程
边界声明。回归 1864 passed（含 15 个重排专属测试、3 处变异测试全红）。

⚠️ 流程教训：变异测试（故意改坏生产码验证测试会红）与真机 CLI 测试**不能对同一
工作树并行跑**——本批一个故障注入 agent 的 24 连跑撞上 sort 方向反转的变异窗口，
产出过一个「4% 排序反转 flake」假象（分数映射全同、仅方向反转，正是该变异的签名；
树稳定后 12+20 连跑零复现）；首轮 bench 臂2 同样被污染重跑。

## 复现

```bash
# 基线（当日）
python scripts/rag_bench.py --json                 # hybrid
python scripts/rag_bench.py --json --mode dense
# rerank/HyDE 试验脚本与结果 JSON 在会话 scratchpad（会话结束即失效），
# 方法如上文，重写成本 ~100 行；HyDE 生成缓存不可复现（LLM），数字以本文为准。
```
