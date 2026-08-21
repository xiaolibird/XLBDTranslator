# topics 相对判据标定（2026-08-21，P1a）

背景：topics.py 的证据召回此前用全局绝对阈值 DEFAULT_MIN_SIM=0.55，与查询强度耦合
（强 query 下 top-200 全数过线、阈值形同虚设；冷门 query 个位数召回是诚实信号）。
改为 per-query 相对判据 `eff_min = max(floor=0.55, α × top1)`，α 用 config/topics.yaml
全部 36 条真实 query 在当前 highlight 库（18412 条，bge-m3）上标定。
复跑：`python scripts/calibrate_topics_threshold.py`。

## 选型结论：α = 0.85

- α=0.80 几乎不生效（36 条 query 里仅 4 条门槛离开 floor）
- **α=0.85：只对 top1≥0.647 的强 query 生效（0.85×0.647≈0.55），冷门 query 完全退回 floor**
- α=0.90 过狠：informative missingness 只剩 3 条、mask embedding 只剩 5 条，0.60-0.65 段真命中被误杀

floor 保持 0.55（08-16 分段抽查：≥0.55 全题内 / 0.50-0.55 半漂移 的档位结论不变）。
落地：topics.py DEFAULT_RELATIVE_ALPHA=0.85，retrieve_evidence(relative_alpha=...)，传 0 关闭。

**生效范围：仅概念页（config/topics.yaml 的短 query）。** QA 问答归档
（qa.retrieve_qa_evidence）显式传 relative_alpha=0 维持纯绝对阈值 0.55——本表的
36 条样本全是概念页短 query，QA 的整句问题 + extra_queries 的 top1 分布未标定，
不能被动继承（强问题下 0.55~α×top1 段会被整段误砍）。要在 QA 侧启用须先用
topics/qa/ 归档的真实问题集复跑同款标定并另行落档。

## 标定表（top-200 掩码内召回；n=200 即打满上限）

| topic | query | top1 | n@0.55 | α.75 门槛/n | α.80 | α.85 | α.90 |
|---|---|---|---|---|---|---|---|
| mnar-diagnosis | MNAR missing not at random 诊断 | 0.716 | 200 | 0.550/200 | 0.573/183 | 0.609/82 | 0.645/22 |
| mnar-diagnosis | missingness mechanism diagnosis test | 0.61 | 69 | 0.550/69 | 0.550/69 | 0.550/69 | 0.550/69 |
| mnar-diagnosis | Little's MCAR test 检验 | 0.648 | 21 | 0.550/21 | 0.550/21 | 0.551/21 | 0.583/9 |
| mnar-diagnosis | informative missingness 信息性缺失 | 0.755 | 200 | 0.566/132 | 0.604/31 | 0.642/7 | 0.679/3 |
| mnar-diagnosis | 缺失机制不可忽略 identifiability | 0.69 | 200 | 0.550/200 | 0.552/200 | 0.587/200 | 0.621/61 |
| missingness-aware-modeling | missingness-aware architecture mask embedd | 0.632 | 19 | 0.550/19 | 0.550/19 | 0.550/19 | 0.569/5 |
| missingness-aware-modeling | learnable mask token 掩码嵌入 | 0.696 | 87 | 0.550/87 | 0.557/70 | 0.592/12 | 0.627/5 |
| missingness-aware-modeling | attention with missing input 注意力 缺失 | 0.715 | 200 | 0.550/200 | 0.572/144 | 0.608/28 | 0.643/9 |
| missingness-aware-modeling | graph learning attribute missing feature p | 0.642 | 100 | 0.550/100 | 0.550/100 | 0.550/100 | 0.578/9 |
| missingness-aware-modeling | 无插补建模 imputation-free | 0.665 | 89 | 0.550/89 | 0.550/89 | 0.565/50 | 0.598/15 |
| missingness-causal | missingness graph m-graph causal | 0.622 | 30 | 0.550/30 | 0.550/30 | 0.550/30 | 0.560/17 |
| missingness-causal | MNAR identifiability 可识别性 | 0.662 | 158 | 0.550/158 | 0.550/158 | 0.562/90 | 0.595/27 |
| missingness-causal | causal discovery under missing data | 0.684 | 200 | 0.550/200 | 0.550/200 | 0.581/83 | 0.616/12 |
| missingness-causal | test-order measurement confounding 测量混杂 | 0.662 | 167 | 0.550/167 | 0.550/167 | 0.562/85 | 0.595/17 |
| missingness-causal | missingness indicator DAG node | 0.666 | 30 | 0.550/30 | 0.550/30 | 0.566/14 | 0.599/2 |
| cross-site-transportability | transportability cross-site external valid | 0.638 | 35 | 0.550/35 | 0.550/35 | 0.550/35 | 0.575/13 |
| cross-site-transportability | domain shift dataset shift EHR 分布偏移 | 0.655 | 77 | 0.550/77 | 0.550/77 | 0.556/59 | 0.589/15 |
| cross-site-transportability | LODO leave-one-dataset-out 多中心 | 0.574 | 13 | 0.550/13 | 0.550/13 | 0.550/13 | 0.550/13 |
| cross-site-transportability | site heterogeneity 站点异质性 | 0.659 | 56 | 0.550/56 | 0.550/56 | 0.560/41 | 0.593/10 |
| cross-site-transportability | 外部验证 性能衰减 generalization gap | 0.689 | 200 | 0.550/200 | 0.551/200 | 0.586/62 | 0.620/10 |
| imputation-pitfalls | imputation distortion bias 插补偏倚 | 0.747 | 200 | 0.560/200 | 0.597/200 | 0.635/45 | 0.672/4 |
| imputation-pitfalls | imputation downstream task performance 下游 | 0.663 | 45 | 0.550/45 | 0.550/45 | 0.564/20 | 0.597/4 |
| imputation-pitfalls | multiple imputation assumption violation | 0.601 | 8 | 0.550/8 | 0.550/8 | 0.550/8 | 0.550/8 |
| imputation-pitfalls | RMSE 重建误差 误导 evaluation metric | 0.677 | 200 | 0.550/200 | 0.550/200 | 0.576/111 | 0.610/29 |
| adversarial-evidence | causal features do not improve generalizat | 0.621 | 71 | 0.550/71 | 0.550/71 | 0.550/71 | 0.559/43 |
| adversarial-evidence | attention is not explanation 注意力 不等于 因果 | 0.63 | 47 | 0.550/47 | 0.550/47 | 0.550/47 | 0.567/30 |
| adversarial-evidence | 可解释性权重 可靠性质疑 saturation | 0.647 | 200 | 0.550/200 | 0.550/200 | 0.550/200 | 0.582/111 |
| adversarial-evidence | negative result 反例 未能复现 | 0.643 | 130 | 0.550/130 | 0.550/130 | 0.550/130 | 0.579/28 |
| icu-benchmarks | MIMIC-IV eICU AmsterdamUMCdb HiRID benchma | 0.638 | 87 | 0.550/87 | 0.550/87 | 0.550/87 | 0.574/46 |
| icu-benchmarks | ICU 数据集 缺失率 采样频率 | 0.719 | 200 | 0.550/200 | 0.575/200 | 0.611/172 | 0.647/34 |
| icu-benchmarks | cohort definition harmonization 队列口径 | 0.619 | 2 | 0.550/2 | 0.550/2 | 0.550/2 | 0.557/2 |
| icu-benchmarks | multi-database validation ICU mortality | 0.662 | 200 | 0.550/200 | 0.550/200 | 0.563/196 | 0.596/59 |
| shadow-variable | shadow variable MNAR identification | 0.593 | 4 | 0.550/4 | 0.550/4 | 0.550/4 | 0.550/4 |
| shadow-variable | instrumental variable missing data 工具变量 缺失 | 0.658 | 200 | 0.550/200 | 0.550/200 | 0.559/200 | 0.592/91 |
| shadow-variable | auxiliary variable nonignorable missingnes | 0.617 | 27 | 0.550/27 | 0.550/27 | 0.550/27 | 0.555/22 |
| shadow-variable | 放射报告 检查开单 弱影子变量 | 0.655 | 21 | 0.550/21 | 0.550/21 | 0.557/14 | 0.589/4 |
