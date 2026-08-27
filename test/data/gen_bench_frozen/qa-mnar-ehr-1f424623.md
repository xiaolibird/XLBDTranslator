---
qa: "qa-mnar-ehr-1f424623"
title: "MNAR 诊断在纵向 EHR 上到底能不能做？"
question: "MNAR 诊断在纵向 EHR 上到底能不能做？"
type: "qa"
qa_schema: 2
first_asked_at: "2026-08-17T19:01:04"
generated_at: "2026-08-17T21:18:43"
n_evidence: 28
n_papers: 15
n_points: 11
n_caveats: 8
n_gaps: 5
evidence_used: 20
dropped_claims: 0
invalid_refs: 0
stripped_cites: 0
tags:
  - "札记/问答归档"
---

<!-- BEGIN GENERATED v2 h=8e3177ab · 由 scripts/ask_notes.py 生成 · 此块会被重建覆盖，请写在下面的「我的批注」里 -->
# MNAR 诊断在纵向 EHR 上到底能不能做？

> 📎 **相关概念页**：[[mnar-diagnosis]] —— 它证据面更宽、随新论文**自动重合成**，概念级的问题先读那一页。

> MNAR无法仅凭观测到的纵向EHR数据本身被严格证实——诊断流程走到最后一步，NMAR的确认依然依赖外部数据与领域专长，而且独立性检验类诊断方法本身通常需要完整的Y，真实MNAR场景下Y缺失时该检验就无法执行。但可以做启发式的结构化诊断：先用统计检验排除MCAR，再结合「缺失由照护过程本身生成」这一临床先验去识别确定性或状态依赖的信息性缺失结构，辅以有界MNAR的敏感性分析兜底。这条路径对应causal hypothesis generation而非causal identification的定位。

## 依据

- 库内可参考的完整诊断流程：先做EDA+统计检验，跑Little's MCAR检验，若p>0.05确认MCAR；否则跑逻辑回归和卡方检验，缺失若可由观测因素解释则确认MAR（用MICE/贝叶斯/回归类方法处理）；否则确认NMAR——但NMAR这一步的确认仍需要外部数据与领域专长，无法仅凭手头观测数据完成。 [@wani2026Comprehensive]
- 结构化/确定性缺失可作为不完全依赖统计检验的诊断线索：某类检验一旦获得正常结果就不再复测，这种缺席本身携带临床决策信号；这也解释了为什么在良设树模型中额外的缺失指示符增益接近零——缺失已与观测值确定性绑定，指示符信息冗余。 [@tan2023Informative]
- 有一节被认为是哲学上分量最重的论述：缺失很少是良性的数据质量伪影，而是由照护过程本身生成——监测强度、检验开单、记录习惯与设备可用性都随患者状态与临床医生判断动态适配，产生混合的、状态依赖的、常常是信息性而非随机的缺失机制。 [@wani2026Comprehensive]
- MNAR此前主要在临床试验数据中被识别（与患者对治疗的依从性和脱落有关），已有研究在EHR数据中同样识别出MNAR（与个体疾病负担有关），说明EHR场景下的MNAR识别并非全新命题。 [@li2021Imputation]
- 一项3国、15个4CE站点、69,939名COVID住院患者的多站点描述性研究，用「一旦获得正常检验结果即不再复测同类检验」的机制坐实了结构化I-MNAR，该模式随站点、性别、疾病严重度（Severe:Non-severe比值0.06–2.23）、时间强烈异质，但全文无监督预测模型、无插补实验、无树与神经网络对照。 [@tan2023Informative]
- 已有论文把MAR假设正式形式化写出（令y、x、z、m分别代表标签、特征、驱动获取的临床因素、是否获取的指示，做出y,x⊥m|z的条件独立假设，并据此推出IPW估计量），说明形式化诊断/建模在方法论上可行，但形式化本身不等于该假设已被数据证实。 [@zhang2026Newonset]
- 有一套可复用的三段式稳健性评估实验设计：按缺失率做子组分析、在测试端注入缺失、在训练端注入缺失，并观察到「低缺失子组死亡率反而最高」这类信息性缺失现象，可复用于跨中心可迁移性论证的实验设计。 [@zeng2023Neural]
- 具体可用的统计诊断工具包括fluxplot（influx/outflux系数）、margin plot、对合并症矩阵做PCA后配合Welch非配对t检验的组合分析；缺失的操作性定义可界定为「未做检验」或「做了但值超出3个IQR」，且诊断范围通常限定在缺失比例<75%的变量。 [@li2021Imputation]
- 对MNAR的显式敏感性分析已有技术先例：借用边际敏感性模型（在权重上加乘性扰动上限）可覆盖「有界的MNAR」，但仅用于比较性主张而非给出绝对性能的上下界，扰动方向由最小化F1差距的对抗搜索决定而非由具体临床下单机制导出，「不可信」的判定也只靠FP/TP比14.33与8.15的非正式比较，没有E-value之类可跨研究比较的度量。 [@zhang2026Newonset]
- 独立性诊断依赖完整数据：F⊥R|Y检验用了全量Y，这在半合成场景下（Y全观测）才可执行；真实MNAR场景下Y本身缺失，这个诊断方法就不可执行，只能靠敏感性分析兜底，说明部署时实际可用的验证路径比方法展示的要弱。 [@chen2026Partial]
- 有方法论给出可观测性清单，作为监测缺失机制漂移的借鉴思路：系统级指标（延迟分布、吞吐量、错误率）、数据级指标（缺失模式、缺口长度分布）、输出诊断（不确定性膨胀、约束违反、弃权频率），并要求显式的错误预算——长缺口期间有限的弃权可接受，广泛弃权或不合理重建则指示系统失效。 [@wani2026Comprehensive]

## ⚠️ 用之前要知道的

- 即使完整走完诊断流程图，NMAR的最终「确认」这一步仍然依赖外部数据和领域专长，不是纯数据驱动、可自动化完成的判定——这直接限制了「MNAR诊断」在纵向EHR上能做到多彻底。 [@wani2026Comprehensive]
- 有方法假设先验数据与目标数据的缺失结构相似，但缺乏显式检测或标记缺失异质性的机制——这对「缺失条件依赖结构的跨中心可迁移性」这一命题构成直接挑战：若诊断方法本身默认结构不变，就难以先验地检测出结构本身会跨中心漂移。 [@li2025Transferlearning]
- 有研究者仅凭知识假设声称缺失机制至少为MAR而未实际检验，而常规采集数据的缺失常具非随机性——用一句话假设MAR却不检验、不讨论MNAR可能性，是需要警惕的常见做法。 [@zhang2024ardsb]
- 横截面调查类数据（单次就诊快照）无法追踪同一患者跨就诊的检验模式演变，也无法探究开单行为的纵向/序贯依赖结构——纵向EHR相对占优，但仍需专门设计才能真正利用这一优势去做MNAR诊断。 [@zhang2026Predicting]
- 缺失机制判定（MAR假设）若仅基于「缺失-开检验率」这一种关联的粗略比较，并未做正式的MNAR敏感性分析或用潜在未观测混杂变量（如分诊时的主观病情严重度判断）去检验缺失是否依赖未记录信息，这是留给后续研究的明显缺口，说明当前方法离「彻底诊断」还有距离。 [@zhang2026Predicting]
- 有论文承认「违背MAR假设可能源于EHR记录错误或其他未被记录的患者信息」——这实质上是承认存在未观测的下单决定因素，但作者未将其正式命名为MNAR，也未讨论偏倚的方向与量级，说明即使承认线索存在，也常止步于定性层面。 [@zhang2026Newonset]
- 同一批证据里，点级缺失实验覆盖MCAR/MAR/MNAR三种机制，但模态整体缺失实验全程只用MCAR模拟（其中一处明确承认该假设简化了现实中选择性检查医嘱、影像不可及等系统性模态缺失场景），说明「机制覆盖完整」的结论不能不加区分地外推到所有实验设置。 [@bhandari2026Comparative]
- 多站点COVID描述性证据内部存在方向不一致（严重度-时间变化率因指标截断效应在不同化验上方向相反，并非单向规律）和数字口径不一致（分项医院数相加为241，与摘要/正文明文的232不一致），引用具体数字或单向结论前需回原文核对。 [@tan2023Informative]

## 本次召回没覆盖到的

这是**本次这 28 条证据**没能回答的部分，**不等于库里没有**——召回是按单个问题从全库切出来的窄切片，召回不到 ≠ 不存在。决定要不要去补文献之前，先查 `topics/INDEX.md` 与 `notes_search.py`。

- 本次这批证据里没有一条给出在真实（非模拟）纵向EHR数据上用统计方法（Little's检验、条件独立检验等）实际诊断出MNAR、并经外部金标准（如病历审查、临床专家判断）验证诊断正确性的完整案例——相关方法都停留在流程/工具描述层面。
  - ⚠️ **但库里可能有**（回查这条空白时命中的；概念页那档实测很准，句级那档大约四成相关——**看标题与原句再决定**，尤其注意有些原句本身是否定句，说的是「那篇里没有」）：
    - `topics/mnar-diagnosis.md`（0.72）MNAR 与缺失机制诊断
    - `topics/shadow-variable.md`（0.59）影子变量与 MNAR 辅助识别
    - `topics/missingness-causal.md`（0.56）缺失与因果结构（m-graph 与可识别性）
    - `[@beaulieujones2017Characterizing]`（0.73）Characterizing and Managing Missing Structured Data in Electronic Health Records
      > 需要警惕的反向证据：本文的结论是「大部分检验可判为 largely MAR、常规填补够用」。若这一判断在多数 EHR 上成立，那么 MNAR 专门建模的必要性就需要更强的论证——我的 counter 
    - `[@anonJolt]`（0.73）JoLT: Joint Probabilistic Predictions on Tabular Data Using LLMs
      > 一个可借鉴的技术点：用文本表头和边信息驱动LLM识别什么缺失，或许可以移植到EHR缺失建模里，把变量说明或临床背景当提示辅助推断缺失机制是否为MNAR，但这仍停留在提示工程层面，不构成缺失机制的形式化
    - `[@ying2026Evaluation]`（0.72）Evaluation of extracorporeal membrane oxygenation in children with acute hypoxem
      > 该研究采用完全案例分析处理缺失EHR数据，为研究MNAR假设提供了临床场景，可探讨缺失值与预后关联造成的偏倚方向和程度。
    - `[@yun2025Medprm]`（0.72）Med-PRM: Medical Reasoning Models with Stepwise, Guideline-verified Process Rewa
      > 检索增强步骤验证的思路可直接迁移至 EHR 缺失机制推断：把'某变量缺失是否符合 MNAR'的推理链当作过程轨迹，逐步检索流行病学/统计缺失机制文献进行事实核验，而不是只看最终缺失机制分类是否正确。
- 本次证据没有专门系统讨论「MNAR/NMAR在观测数据上不可识别」这一统计学根本问题的形式化可识别性条件，唯一涉及敏感性分析的证据也仅限于比较性主张下的有界MNAR场景，没有给出绝对性能的上下界。
  - ⚠️ **但库里可能有**（回查这条空白时命中的；概念页那档实测很准，句级那档大约四成相关——**看标题与原句再决定**，尤其注意有些原句本身是否定句，说的是「那篇里没有」）：
    - `topics/mnar-diagnosis.md`（0.65）MNAR 与缺失机制诊断
    - `topics/missingness-causal.md`（0.56）缺失与因果结构（m-graph 与可识别性）
    - `[@yan2025Predicting]`（0.75）Predicting Partially Observed Long-Term Outcomes with Adversarial Positive-Unlab
      > 明确结论：不能把本文引作 MNAR 方法。它没有缺失机制假设、没有可识别性论证、没有敏感性分析（λ 敏感性是超参数敏感性，不是机制敏感性），也没有任何偏差量化。把它写进我的 related work 
    - `[@yan2022Observability]`（0.73）Observability and its impact on differential bias for clinical prediction models
      > 不可识别性论断的证据强度是本篇最该被质疑的地方：作者说的是 "The results suggested that this might not be possible"（结果「提示」这可能做不到），
    - `[@toye2025Benchmarking]`（0.73）Benchmarking Missing Data Imputation Methods for Time Series Using Real-World Te
      > 论文没有讨论缺失机制在观测数据中不可判定（identifiability）这一根本困难：全文 0 次出现 identifiability、ignorability、shadow variable、se
    - `[@chiang2025Learning]`（0.71）Learning Disease Progression Models That Capture Health Disparities
      > **Lemma 8 是可以拆出来复用的独立技术**：观测强度对潜在状态的依赖系数，可以从「人群平均观测率随时间的曲率」里识别出来，前提是潜在状态本身在漂移。这给了一条不依赖工具变量、不依赖敏感性参数的
- 本次证据没有直接量化「缺失机制在不同医院/中心/时间点之间漂移程度」的实证研究，只有一条证据提出「机制假设先验/目标结构相似」这一前提本身可疑，没有给出漂移幅度的测量数据。
  - ⚠️ **但库里可能有**（回查这条空白时命中的；概念页那档实测很准，句级那档大约四成相关——**看标题与原句再决定**，尤其注意有些原句本身是否定句，说的是「那篇里没有」）：
    - `topics/cross-site-transportability.md`（0.62）跨中心迁移与外部验证
    - `topics/adversarial-evidence.md`（0.59）对抗性证据：与本文假设相反的实证
    - `[@sisk2020Informative]`（0.75）Informative presence and observation in routine health data: A review of methodo
      > 机制可迁移的假设检验设计，可以直接从 Table 3 的「假设」栏反着写。缺失指示假定「缺失是某未测量患者特征的代理」，汇总测量假定「观测过程是某未测量患者特征的代理」（p.161）——这两条假定的可
    - `[@hagag2026Association]`（0.74）Association of Arterial Partial Oxygen Pressure with Mortality in Critically Ill
      > 临床上不同中心对于血气分析（PaO₂）的采样指征和频率存在差异，导致「缺失条件依赖结构」在跨中心时可能发生漂移，这验证了我研究中探索“缺失机制跨中心可迁移性”的临床现实意义。
    - `[@xu2022Explainable]`（0.73）Explainable Dynamic Multimodal Variational Autoencoder for the Prediction of Pat
      > 论文未提供多中心数据或外部验证结果，模型在不同医疗环境下的可迁移性存疑。
    - `[@jia2026Derivation]`（0.72）Derivation and external validation of machine learning prediction for severe acu
      > 研究直接将源中心训练的模型应用于外部验证集，未考虑不同医疗中心由于临床路径和资源差异导致的‘缺失条件依赖结构’漂移，这限制了模型在更广泛异构中心的可迁移性。
- 本次证据里涉及的诊断工具（fluxplot、margin plot、PCA+Welch t检验、诊断流程图）均未给出所需样本量、检验功效或假阳性率方面的评估。
  - ⚠️ **但库里可能有**（回查这条空白时命中的；概念页那档实测很准，句级那档大约四成相关——**看标题与原句再决定**，尤其注意有些原句本身是否定句，说的是「那篇里没有」）：
    - `[@wang2026Cleo]`（0.67）CLEO closed loop framework for synthesizing medical privacy preserving tabular d
      > 未进行校准分析、公平性评估、机构分层鲁棒性测试和临床专家验证。
    - `[@yu2025Selfexplainingb]`（0.66）Self-Explaining Hypergraph Neural Networks for Diagnosis Prediction
      > 没有人工评估。所谓 clinical experts 的参与仅限一个患者的案例研究，没有多名评分者、没有评分量表、没有一致性统计；Faithfulness、Complexity、Distinctnes
- 本次证据没有讨论如何专门利用纵向EHR「跨就诊序贯依赖」这一结构本身去做MNAR诊断——只有一条证据指出横截面数据做不到，但没有给出纵向数据具体该怎么利用这一优势的方法。
  - ⚠️ **但库里可能有**（回查这条空白时命中的；概念页那档实测很准，句级那档大约四成相关——**看标题与原句再决定**，尤其注意有些原句本身是否定句，说的是「那篇里没有」）：
    - `topics/mnar-diagnosis.md`（0.68）MNAR 与缺失机制诊断
    - `topics/missingness-causal.md`（0.55）缺失与因果结构（m-graph 与可识别性）
    - `[@吴博图神经网络前沿进展与应用]`（0.72）图神经网络前沿进展与应用
      > 针对EHR中MNAR机制的异质性,可借鉴AGCN为单个样本定制拉普拉斯矩阵的思想,构建患者特异性的缺失条件依赖结构——这是我自己的延伸构想,论文本身并未讨论EHR或缺失机制。
    - `[@ying2026Evaluation]`（0.71）Evaluation of extracorporeal membrane oxygenation in children with acute hypoxem
      > 该研究采用完全案例分析处理缺失EHR数据，为研究MNAR假设提供了临床场景，可探讨缺失值与预后关联造成的偏倚方向和程度。
    - `[@yan2025Predicting]`（0.71）Predicting Partially Observed Long-Term Outcomes with Adversarial Positive-Unlab
      > 可借鉴的技术点只有一个，且要打折使用：partial alignment 的单向 KL 让目标未标注分布的支撑允许超出源负样本，即「只对确信同质的部分做对齐，对可能藏着异质信号的部分只施加单边约束」。
    - `[@metzcar2024Review]`（0.71）A review of mechanistic learning in mathematical oncology
      > 结合临床领域知识建模的思想可应用于EHR缺失数据：将已知MNAR机制作为先验嵌入到混合模型中，以提升跨中心可迁移性。

## 本页证据（28 条 · 15 篇）

每条可回溯到札记原文；未被引用的证据标 ○。**要把具体数字写进稿子，先点开原句核对**——防线保证 citekey 与原句真实存在，不保证转述没有失真。

- ● **E1** `[@wani2026Comprehensive]` 🟩方法论借鉴 · 图表与补充材料要点 · 科研札记_2026-06_手动精读.md:33
  <small>Comprehensive analysis of missing data imputation in clinical time-series: challenges, risks, and practical solutions</small>
  > Fig 2（缺失机制诊断流程图）：Start → 初步 EDA + 统计检验 → 跑 Little's MCAR 检验 → 若确认则 MCAR (p > 0.05)；若否则进入潜在 MAR/NMAR → 跑逻辑回归与卡方检验 → 若缺失可由观测因素解释则 MAR 确认（用 MICE、贝叶斯推断或回归类方法），否则 NMAR 确认（需要外部数据与领域专长）（流程图内容核对无误）。
- ● **E2** `[@zhang2024ardsb]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2025-01_全文精读.md:505
  <small>Machine Learning Models for the Early Real-Time Prediction of Deterioration in Intensive Care Units—A Novel Approach to the Early Identification of High-Risk …</small>
  > 作者声称缺失机制至少为随机缺失（MAR），但仅基于知识假设而未进行检验，实际常规数据缺失常具非随机性，可能威胁模型泛化性。
- ● **E3** `[@li2025Transferlearning]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2025-10_全文精读.md:94
  <small>Transfer-learning on federated observational healthcare data for prediction models using Bayesian sparse logistic regression with informed priors</small>
  > 方法假设先验与目标数据的缺失结构相似，但缺乏显式检测或标记缺失异质性的机制。
- ● **E4** `[@zhang2026Newonset]` 🟪可引用证据 · 方法与数据 · 科研札记_2026-08_手动精读.md:912
  <small>New-Onset Diabetes Assessment Using Artificial Intelligence-Enhanced Electrocardiography</small>
  > 缺失机制假设在 Appendix C.1 被形式化写出：令 y 为 HbA1c 二值标签、x 为 ECG、z 为「在已开 ECG 的前提下驱动 HbA1c 获取的临床因素」、m 为是否获取 HbA1c，作者做的 MAR 假设是 y, x ⊥ m | z，并由此推出 IPW 估计量 E[1(m=1) f(x,y) / p(m=1|z)]。
- ○ **E5** `[@saxena2024Beyond]` 🟪可引用证据 · 关键结论 · 科研札记_2024-12_全文精读.md:241
  <small>Beyond the Hype: A Review of Challenges in AI-based Medication Prediction and Future Prospects</small>
  > 需要关注缺失机制建模和跨中心可迁移性以推进临床应用。
- ○ **E6** `[@mohapatra2024Differentially]` 🟩方法论借鉴 · 方法与数据 · 科研札记_2023-10_全文精读.md:145
  <small>Differentially Private Data Generation with Missing Data</small>
  > 将缺失机制建模为一个概率采样过程，利用采样放大定理推导出更紧的隐私界，使得在不完整数据上训练的模型能提供对真实数据的隐私保证。
- ● **E7** `[@zeng2023Neural]` 🟩方法论借鉴 · 对本论文攻防的意义(复核后修正) · 科研札记_2026-07-17_手动精读.md:798
  <small>Neural networks based on attention architecture are robust to data missingness for early predicting hospital mortality in  intensive care unit patients</small>
  > 可借鉴的方法：其三段式稳健性评估协议(按缺失率子组分析+测试端注入缺失+训练端注入缺失)与'低缺失子组死亡率反而最高'的信息性缺失观测，都是研究者可复用于自己论证的实验设计与证据。
- ● **E8** `[@zhang2026Predicting]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2026-08_手动精读.md:6574
  <small>Predicting Laboratory Test Ordering in Emergency Departments Using Integrated Structured and Unstructured Electronic Health Records: Machine Learning Study</small>
  > 缺失机制判定（MAR假设）仅基于“缺失-开检验率”这一种关联的粗略比较，并未做正式的MNAR敏感性分析或用潜在未观测混杂变量（如分诊时的主观病情严重度判断）去检验缺失是否依赖于未被记录的信息，这是留给后续研究的明显缺口。
- ● **E9** `[@tan2023Informative]` 🟩方法论借鉴 · 研究问题与定位 · 科研札记_2026-07-17_手动精读.md:31
  <small>Informative missingness: What can we learn from patterns in missing laboratory data in the electronic health record?</small>
  > 关键机制描述经逐字核对:"once a normal laboratory result is obtained, no further assays of the same type are present in the record"——缺席本身携带临床决策信号,这正是研究者所称树可原生利用的确定性/结构化(bin)缺失机制。
- ● **E10** `[@li2021Imputation]` 🟩方法论借鉴 · 实验方法 · 科研札记_2026-06_手动精读.md:1389
  <small>Imputation of missing values for electronic health record laboratory data</small>
  > 【特征】缺失模式与机制通过fluxplot（influx/outflux系数）、margin plot以及对合并症矩阵做PCA后与Welch非配对t检验相结合的方式进行分析 (p.11, p.7)
- ○ **E11** `[@romdhane2024Predictstr]` 🟩方法论借鉴 · 方法与数据 · 科研札记_2024-06_全文精读.md:81
  <small>PredictStr: A Balanced Benchmark Dataset for Improve Stroke Prediction</small>
  > 对缺失值采用中位数填补（BMI）和众数填补（smoking_status），假设缺失机制为完全随机缺失。
- ○ **E12** `[@chen2026Partial]` 🟩方法论借鉴 · 实验方法 · 科研札记_2026-08_手动精读.md:4097
  <small>Partial Identification under Missing Data Using Weak Shadow Variables from Pretrained Models</small>
  > 【特征与预处理】缺失机制模拟方法：为每段对话随机抽取一个来自 Twin-2k-500（Toubia et al. 2025，含 2,000 个人格摘要）的 persona，让 LLM 扮演该人格输出留下评分的概率，再以该概率为均值从伯努利分布中抽取实际是否响应，从而使响应与真实结局相关 (p.25)。
- ● **E13** `[@li2021Imputation]` 🟩方法论借鉴 · 逐节通读要点 · 科研札记_2026-06_手动精读.md:1389
  <small>Imputation of missing values for electronic health record laboratory data</small>
  > Methods-Recognition of missingness:缺失定义为"未做检验"或"做了但值在三个IQR之外";缺失分析限定在缺失比例<75%的检验变量。
- ● **E14** `[@bhandari2026Comparative]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2026-07_手动精读.md:3386
  <small>A comparative evaluation of handling missing data points and modalities in electronic health records</small>
  > 缺失点实验覆盖MCAR/MAR/MNAR三种机制，但模态缺失实验（文本/测量整体缺失）全程只在MCAR假设下模拟，论文摘要与结论中'直接建模能更好保留临床模式'的表述并未区分两类实验的机制覆盖范围差异，存在结论外推的风险。
- ○ **E15** `[@aerts2021Quality]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2021-08_全文精读.md:484
  <small>Quality of Hospital Electronic Health Record (EHR) Data Based on the International Consortium for Health Outcomes Measurement (ICHOM) in Heart Failure: Pilot Data Quality Assessment Study.</small>
  > 作者提出引入机制区分缺失与真阴性、采用两级就诊结构等建议，但这些建议的可行性未经验证。
- ○ **E16** `[@anonLlmdr]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2026-08_手动精读.md:7681
  <small>LLMDR: Large language model driven framework for missing data recovery in mixed data under low resource regime</small>
  > 全文实验仅采用MAR模式注入缺失，未测试MCAR或MNAR情形，也未讨论框架在缺失机制系统性偏离随机（即MNAR，如“值越极端越容易缺失”“因病情不需要检测而系统性未记录”）时是否失效——这对论文声称的“对缺失比例保持稳健性能”构成实质性缺口（p1, p8）。
- ● **E17** `[@tan2023Informative]` 🟦可反驳观点 · 对本论文攻防的净影响 · 科研札记_2026-07-17_手动精读.md:31
  <small>Informative missingness: What can we learn from patterns in missing laboratory data in the electronic health record?</small>
  > 【立场:相邻】相邻上游、弱支撑+一处需正面处理的蚕食点(Opus 判断基本成立,细节需修正)。本文用 3 国、15 个 4CE 站点、69,939 名 COVID 住院患者的多站点描述性证据,坐实 EHR 化验缺失是结构化 I-MNAR(核心机制原文直述:"once a normal laboratory result is obtained, no further assays of the same type are present in the record"——正是树原生 sparsity 能吃的确定性 bin 机制),且随站点(Fig.1)、性别(Fig.2A,受小样本站点驱动)、严重度(Fig.2B、Severe:Non-severe 比 0.06–2.23 极不均衡)、时间(Fig.3–5)强异质。全文自始至终无任何有监督预测模型、无插补实验、无树 vs 神经比较,因此不构成"比较不对称"攻击靶,只能作机制/位移证据征用。核心蚕食点确认:Discussion 第 3 点标题即为"Missingness patterns could be predictive of clinical outcomes",但正文原话仅是"the removal of one test's missing values from a model could potentially affect model performance"——纯思辨(could potentially),零 AUROC、零对照实验,这与研究者"良设树模型中缺失指示符增量增益接近零"的量化结论处于不同层级、不冲突。需要在 Opus 稿基础上补两处:(1)"严重度-时间变化率"方向在原文并非单向——开篇断言"missingness increases faster over time in non-severe patients"(适用 Leukocytes),但 Troponin/Ferritin 因 [0,1] 截断效应显示相反(非严重组更快触顶,故整体变化率反而更小、严重组更大),三例中方向不一,引用时不可当作单向规律;(2)Table 1 医院数逐行相加为 241,与摘要/正文明文的"232"不一致,引用具体机构规模数字时应加注谨慎(未影响 69,939 患者、15 站点、16 化验等核心数字的可信度)。
- ○ **E18** `[@mohapatra2024Differentially]` 🟪可引用证据 · 关键结论 · 科研札记_2023-10_全文精读.md:145
  <small>Differentially Private Data Generation with Missing Data</small>
  > 缺失机制中的随机性可以作为采样过程，通过隐私放大技术将针对不完整数据的隐私预算收紧为针对真实数据预算的0.1-0.65倍（在10%-50%缺失率下）。
- ● **E19** `[@wani2026Comprehensive]` 🟩方法论借鉴 · 逐节通读要点 · 科研札记_2026-06_手动精读.md:33
  <small>Comprehensive analysis of missing data imputation in clinical time-series: challenges, risks, and practical solutions</small>
  > 该节给出可观测性的具体清单：系统级指标（延迟分布、吞吐量、错误率）、数据级指标（缺失模式、缺口长度分布）、输出诊断（不确定性膨胀、约束违反、弃权频率），并要求显式的错误预算——长缺口期间有限的弃权可以接受，而广泛的弃权或不合理的重建则指示系统失效。
- ● **E20** `[@tan2023Informative]` 🟩方法论借鉴 · 对本论文攻防的意义 · 科研札记_2026-07-17_手动精读.md:31
  <small>Informative missingness: What can we learn from patterns in missing laboratory data in the electronic health record?</small>
  > "拿到一次正常值即停开同类检验"的机制(原文原话经核对无误),可被研究者用来解释"缺失指示符对树增益小":该缺失是与观测值确定性绑定的分裂点,树的原生 sparsity 已隐含吸收,额外指示符信息冗余。
- ● **E21** `[@wani2026Comprehensive]` 🟪可引用证据 · 逐节通读要点 · 科研札记_2026-06_手动精读.md:33
  <small>Comprehensive analysis of missing data imputation in clinical time-series: challenges, risks, and practical solutions</small>
  > Missingness Mechanisms and Epistemic Limits (p.9-11)：本文哲学上最有分量的一节，指出缺失很少是良性的数据质量伪影，而是由照护过程本身生成——监测强度、检验开单、记录习惯与设备可用性都随患者状态与临床医生判断动态适配，产生混合的、状态依赖的、常常是信息性而非随机的缺失机制。
- ● **E22** `[@bhandari2026Comparative]` 🟦可反驳观点 · 实验方法 · 科研札记_2026-07_手动精读.md:3386
  <small>A comparative evaluation of handling missing data points and modalities in electronic health records</small>
  > 【评估协议】missing modality实验中作者选用MCAR作为受控基线以分离“模态整体缺失”这一效应,并明确承认该假设简化了现实中模态系统性缺失(如选择性检查医嘱、影像不可及等临床场景)的复杂性(p.5)。
- ● **E23** `[@li2021Imputation]` 🟪可引用证据 · 逐节通读要点 · 科研札记_2026-06_手动精读.md:1389
  <small>Imputation of missing values for electronic health record laboratory data</small>
  > Discussion强调MNAR此前主要在临床试验数据中被识别(与患者对治疗的依从性和脱落有关),本研究在EHR数据中同样识别出MNAR(与个体疾病负担有关)。
- ● **E24** `[@zhang2026Newonset]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2026-08_手动精读.md:912
  <small>New-Onset Diabetes Assessment Using Artificial Intelligence-Enhanced Electrocardiography</small>
  > 论文第 5 页确实承认「违背该假设可能源于 EHR 记录错误或其他未被记录的患者信息」，这在实质上就是承认存在未观测的下单决定因素，但作者既未把它命名为 MNAR，也未讨论偏倚的方向与量级。
- ● **E25** `[@zhang2026Predicting]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2026-08_手动精读.md:6574
  <small>Predicting Laboratory Test Ordering in Emergency Departments Using Integrated Structured and Unstructured Electronic Health Records: Machine Learning Study</small>
  > NHAMCS-ED是横截面调查（单次就诊快照），无法追踪同一患者跨就诊的检验模式演变，也无法探究开单行为的纵向/序贯依赖结构。
- ● **E26** `[@chen2026Partial]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2026-08_手动精读.md:4097
  <small>Partial Identification under Missing Data Using Weak Shadow Variables from Pretrained Models</small>
  > 独立性诊断依赖完整数据：图 3 的 F⊥R|Y 检验用了全量 Y（半合成里 Y 全观测才可检验）；真实 MNAR 场景下 Y 缺失，这个诊断本身不可执行，只能靠敏感性分析兜底——部署时的验证路径比论文展示的弱。
- ○ **E27** `[@bhandari2026Comparative]` 🟪可引用证据 · 实验方法 · 科研札记_2026-07_手动精读.md:3386
  <small>A comparative evaluation of handling missing data points and modalities in electronic health records</small>
  > 【特征】missing modality实验中文本先转为小写并去除特殊字符,再用Pyampute以10%、25%、50%三档比例在MCAR机制下随机遮蔽整个模态(测试缺文本-全测量值、缺测量值-全文本两种场景),每种设置以5个随机种子模拟,所有实验按种子独立运行,结果报告为均值±标准差(p.5)。
- ● **E28** `[@zhang2026Newonset]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2026-08_手动精读.md:912
  <small>New-Onset Diabetes Assessment Using Artificial Intelligence-Enhanced Electrocardiography</small>
  > Appendix C.2 借用 Zhao 等 2019 的边际敏感性模型（在权重上加乘性扰动上限）在技术上确实覆盖了「有界的 MNAR」，但它有三重局限：只用于比较性主张（ECG 优于两个基线是否被推翻），未给绝对性能估计的上下界；扰动方向由「最小化 F1 差距」的对抗搜索决定，而非由临床上具体的下单机制导出；「不可信」的判定仅靠 FP/TP 比 14.33 与 8.15 的非正式比较，没有 E-value 之类可跨研究比较的度量。

**本次未纳入的近邻论文**（分数就差在门槛下，不是引用，想挖深就调大 `--max-evidence`）：
    - `little1988Test` A Test of Missing Completely at Random for Multivariate Data with Missing Values
    - `royle2021Development` The development and validation of prognostic models for overall survival in the 
    - `toye2025Benchmarking` Benchmarking Missing Data Imputation Methods for Time Series Using Real-World Te
    - `mehryar2026Knowledge` Knowledge graph embedding and alignment of incomplete electronic health Records 
    - `xu2024Basic` From Basic to Extra Features: Hypergraph Transformer Pretrain-then-Finetuning fo
<!-- END GENERATED · 以下内容属于你，生成器永不触碰 -->

## 我的批注
