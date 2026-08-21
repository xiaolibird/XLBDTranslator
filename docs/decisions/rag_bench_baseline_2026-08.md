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
