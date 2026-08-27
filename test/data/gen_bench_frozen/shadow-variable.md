---
topic: "shadow-variable"
title: "影子变量与 MNAR 辅助识别"
type: "topic"
topic_schema: 1
generated_at: "2026-08-27T20:42:41"
n_evidence: 60
n_papers: 60
n_claims: 41
n_disputes: 2
n_gaps: 5
evidence_used: 57
dropped_claims: 0
invalid_refs: 0
stripped_cites: 0
numbers_checked: 26
ungrounded_numbers: 0
tags:
  - "札记/概念页"
  - "概念/shadow-variable"
---

<!-- BEGIN GENERATED v1 h=a7ffdbe7 · 由 scripts/build_topics.py 生成 · 此块会被重建覆盖，请写在下面的「我的批注」里 -->
# 影子变量与 MNAR 辅助识别

> 影子变量（shadow variable）在 MNAR 识别中承担什么角色？临床数据里哪些量可充当弱影子变量，其前提如何被检验？

本页问影子变量在MNAR识别中的角色,及临床候选弱信号如何被检验。60条证据中仅1条(域外,LLM对话场景)明确出现『弱影子变量』一词;临床相关证据只有辅助变量处方、I-MNAR定义、m-graph形式化等定义/结构层材料,无一条对具体候选变量做过排他性检验。应用文献主流默认MAR/MCAR或直接用缺失指示符入模,诊疗强度、站点身份等候选弱信号仅被点名『可能相关』,未见验证流程。

## 影子变量的定义与证据库中的稀缺性

- 60条证据中仅一条明确使用『弱影子变量』一词:做法是设计3×3提示矩阵交叉『视角』(用户数字孪生、一般用户、中立服务审计员)与『构念』(整体评分、目标完成度、处理质量),并加一支二元是/否目标完成提问分支;但该证据取自LLM对话文本抽取场景,属域外证据(非临床/EHR领域),尚未在临床数据上验证。 [@chen2026Partial]
- 临床相关文献中最接近影子变量核心逻辑的是一条一般性方法学处方:当缺失不受数据收集者控制时,收集对结局与缺失概率均有预测力的辅助变量,可使MAR假设『更可信』;但这只是一般性建议,不是针对具体候选变量的可操作检验流程。 [@little2020rubin]
- 一项研究正式提出informative missingness并归为MNAR的特例——informative-missing not at random(I-MNAR):某变量的缺失依赖其自身取值,也可能受其他(观测或未观测)变量影响;这仅是机制定义,未给出影子变量式的排他性识别条件。 [@tan2023Informative]
- 有一项形式化设定在结构上与影子变量框架相容:预测变量X1部分观测且可能存在信息性缺失,X2完全观测,U为未观测变量,并用二元指示变量M1表示X1的缺失情况;但未说明M1或X2是否满足『仅通过缺失倾向而非直接通过结局起作用』的排他性假设。 [@sisk2023Imputation]
- 缺失也可用因果DAG形式化为missingness graph(m-graph):G=(V,E),V=V_o∪V_m∪U∪V*∪R,V_o为完全观测变量、V_m为部分观测变量、U为潜变量;相关术语表还定义了selection indicator(R)、selection bias(条件于观测数据时因缺失过程充当选择机制而在变量间诱发非因果关联)与structural missingness(因设计而缺失)等概念。这些证据只给出图结构与词汇定义,均未涉及具体的排他性检验步骤。 [@ceriscioli2026Discovering] [@moreau2026Best]
- 另有工作处理了含信息的预测变量缺失(informative predictor missingness)并用惩罚回归做变量选择,停留在变量选择层面,未涉及影子变量式的排他性识别。 [@wiegand2022Development]
- 一项声称能学到有效影子变量表示的工作,其核心上界对照仅在一个合成设定(d_s=0.9d、α=1e-6、β=5)下做过,未跨设定重复,『能学到有效影子变量表示』这一核心主张的证据面偏窄。 [@li2024Learning]

## 缺失指示符/哑变量:常见但与影子变量框架不同的做法

- 缺失指示符方法常被直接作为预测协变量纳入模型:一项工作让所有医疗专科类别共用同一个缺失指示符,缺失值本身用固定值99替代后一并纳入多变量预测模型;另一项工作对SOFA、APACHE II缺失分量默认按『正常』处理,其余变量采用虚拟变量调整缺失(Dummy Variable Adjustment),并把指示变量一并纳入模型。 [@gao2026Comparing] [@murray2024Augmenting]
- 已有时序工作多仅使用missing indicator或time interval两者之一,并采用启发式的单调不增衰减函数,没有学习缺失性的显式表示;相应研究希望同时对observed values、missing indicators与time interval之间的关系做显式学习。 [@lee2021Multiview]
- 更粗糙的做法是仅用二分法插补(判定为非随机缺失的特征用固定值、其余用中位数),正文未说明如何判定某特征属于非随机缺失,也未做插补方式的敏感性分析,与缺失感知表征学习/图神经网络插补路线相比方法论明显滞后。 [@dam2025Readmission]
- 有一个自相矛盾的案例:某研究将指示变量的缺失一律置0,把『没记录』直接等同于『没有』,而这恰是该研究自身在别处讨论过的偏倚类型;另一研究简单中位数填补即便创建了缺失指示特征,仍可能未能充分捕捉信息性缺失,从而影响跨中心稳健性。 [@zink2026Access] [@dayanFederatedLearningPredicting2021b]
- 同一队列内不同类型变量的缺失处理机制也可能不统一:一项研究社会/类别变量用missing indicator method处理,实验室数值特征用均值填补,两者机制不同,引用不对称论点时需限定为lab数值特征层面。 [@bellamy2023Labrador]
- binary covariates以记录的有无表示,协变量本身不含缺失值;条件、药物或操作记录的缺失被处理为患者不具有该条件,而left censoring可能导致这类记录缺失——这是把缺失编码问题当分类问题处理,而非把缺失本身建模为信号。 [@anon2021Evaluating]
- 另一类做法是让不同模型族拿到不同的缺失处理:分类变量缺失被统一赋予独立的『unknown』类别以保留数据完整性信息;基于树的集成模型(可通过代理分裂等机制原生处理缺失)保留连续变量缺失,而需要完整数据的模型(如逻辑回归)则按分布偏态用中位数或均值填补——这提示树模型的原生缺失处理与显式影子变量/掩码建模是两条并行但未被比较过优劣的路线。 [@wu2026Artificial]

## 临床数据里可能的弱候选信号,及其未被检验的排他性假设

- 一份方法学梳理把因缺乏医学必要性而未做的化验、以及因高成本或高风险而难以获取的信息(如侵入性操作或放射检查)与患者严重程度、诊断相关的其他因素,标注为informative missingness的定义性例子,提示反映诊疗强度/检验开单行为的变量可能是弱候选,但未给出对这类候选变量做排他性检验的方法。 [@ghosheh2022Review]
- 关键变量在不同站点缺失严重且模式不同,将缺失值归为『_unknown』并排除的做法可能丢失信息,提示站点身份或站点特异性诊疗流程变量可能与缺失结构系统相关,是跨中心场景下的候选弱信号,但这一关联本身未被验证满足排他性约束。 [@adekkanattu2023Prediction]
- 协变量存在缺失的观测被整体剔除而未评估缺失机制或采用插补方法,若缺失并非完全随机(MCAR),该做法可能引入选择偏倚,且原文未报告具体缺失比例。 [@sathe2021Identification]
- 一个来自孟德尔随机化的类比证据(遗传学领域,非临床缺失场景,尚未在EHR/临床数据上验证)指出,工具变量所依赖的排他性假设(如无水平多效性)在复杂行为表型中难以完全满足,提示影子变量的排他性约束在复杂临床协变量结构中同样可能难以验证。 [@panizza2026Physical]
- 部分研究只挑选『本身没有缺失』的静态变量(如age、sex、admission unit)参与建模,时序变量用前向填补,理由是『临床医生在实践中也只参考最后一次记录的测量值』,把缺失问题从静态侧一刀切掉,回避而非利用潜在候选信号;另有做法直接放弃携带信息的变量,如算法未纳入用药剂量信息,原因是EHR中剂量数据缺失比例较高,同样是规避而非利用候选弱信号。 [@mesinovic2024Dysurv] [@sealock2024Crossehr]
- 『设计性缺失』(机构根本没采集某变量)与『测量性缺失』(变量测了但未记录)是两种不同的结构化缺失,前者不构成MNAR问题,但两者在跨中心建模时会碰到同一堵墙——特征维度不一致,提示候选信号的可用性本身可能因中心而系统性缺席。 [@xu2024Basic]

## MNAR/MAR前提如何被检验:现状普遍缺席

- 多项研究直接假设缺失为MAR或MCAR且未做实证检验:剩余缺失值中位数填充时假设MAR;缺失处理假设为MCAR即便经临床评估也可能过于严格、未探讨非随机缺失;中位数插补法可能无法完全解决非随机缺失数据带来的潜在偏倚;缺失数据小于5%时径直采用均值/中位数/众数简单填补;还有研究仅描述了缺失数据但完全未使用插补方法处理;另有研究对连续变量用均值插补、对药物用0/1编码并报告缺失比例,但同样未检验机制。 [@shanbehzadeh2022Predictive] [@li2026Causal] [@li2026Predictive] [@hu2026Dual] [@bauer2025Sepsis] [@huang2023Federated]
- 更弱的情形是完全不引入MCAR/MAR/MNAR的形式化区分:一项研究仅说缺失『systematically or randomly』发生,经全文与补充材料逐词检索确认无一处出现MNAR/MCAR/MAR字样,补充表中的相关指标也无一能区分模型是在MAR假设下填补还是真正处理了信息性缺失;另有研究未交代EHR缺失数据的类型(完全随机缺失/随机缺失/非随机缺失)及其处理策略,可能导致预测偏差。 [@loni2025Review] [@koornwinder2025Multimodal]
- 动机部分与方法部分表述脱节也是常见现象:一项研究动机部分提到EHR数据存在高维、噪声和严重缺失值,但方法部分仅以一句『缺失值填补』带过,未说明具体方法、缺失比例,也未讨论缺失是否随机分布;另一项研究以简短的『median imputation for structured variables including vital signs and demographics』带过,同样未涉及随机性讨论。 [@chang2025Machine] [@zhang2026Predicting]
- CausalFI这类方法明确将『缺失以随机方式发生(missing at random assumption)』列为处理缺失数据的前提,并未处理缺失机制本身依赖于缺失值自身或未观测因素的情形,作者也将其列为该方法的边界条件——是少数明确承认MAR局限、而非默认掩盖的例子。 [@vo2024Federated]
- 缺失的操作性定义本身也不统一:有研究把缺失定义为『未做检验』或『做了但值在三个IQR之外』,并把缺失分析限定在缺失比例小于75%的检验变量。 [@li2021Imputation]
- 缺失机制讨论的定性本身也可能需要交叉核验才能定准:一项研究经核对后,其缺失数据的定性须由『未涉及』改正为『主动回避』——该研究称其它表格数据源存在质量问题(含缺失值),因此只用预处理并归一化过的数据集,仅把Missing values计入工程元特征表,全文无任何缺失机制的讨论;另一篇论文全文唯一涉及缺失的标注只是图表脚注里把『?』定义为missing data(临床IHC注释缺失,非组学缺失),也没有任何处理办法,是缺失讨论几乎完全缺席的另一极端例子。 [@jomaa2021Dataset2vec] [@li2022Mogcn]
- 观察性数据库容易受偏倚、结局测量不佳及缺失影响,可能掩盖真实的治疗效应异质性或制造出并不存在的假象——但相应作者并未就缺失机制本身做任何区分或建模,只是一笔带过。 [@rekkas2023Standardized]
- 更常规的MICE/链式方程插补在多项研究中被采用(含链式方程多重插补、scikit-learn包实现、Python Autoimpute包实现),均未明确声明其建立的MAR假设本身是否被检验过。 [@wan2025Exploring] [@zhang2026Newonset] [@zargoush2021Impact]
- 对缺失的混杂因素,方法论文献建议按全可用协变量评估期,在完全案例分析、末次观测结转、缺失模式法、多重插补、逆缺失概率加权之间择优,提示缺失处理方法选择高度依赖场景;不同插补方法在不同缺失场景下表现差异较大,无单一方法普遍适用。 [@butler2023Noninterventional] [@zhou2023Missing]

## 辅助信息与FMI:间接印证机制实质性,但不构成对具体候选变量的检验

- 无辅助信息时,缺失信息占比(FMI)近似等于缺失比例;加入辅助信息后FMI随之下降——这为『寻找好的辅助/影子变量能降低信息损失』提供量化动机,但FMI下降本身并不能证明某个辅助变量满足影子变量所需的排他性约束,两者是不同层次的问题。 [@madleydowd2019Proportion]
- 能用于检验更严格识别方法(如潜变量法)的软件基础设施本身分布不均:缺失指示、汇总测量、模式特异模型可轻易用常见统计软件实现,潜变量法与联合模型也有对应R/STATA工具,而似然法、相似度测量、HMM三类方法均标注『未提供代码』——这一落差可能是应用文献很少真正检验MNAR/影子变量前提、而多止步于默认假设的部分原因。 [@sisk2020Informative]
- 元分析层面的缺失SD值插补也依赖现成软件(CINeMA、ROB-MEN、STATA 17.0,以及Python sklearn的Iterative Imputer),说明检验或填补缺失的工具选择本身也受软件生态限制。 [@yadgarov2023Early]

## 与MA-GCT基础工作可能相关的直接/间接线索

- 有工作不对缺失值做均值/零值等数值填补,而是为每个输入变量引入一个单独的可学习缺失嵌入向量(token embedding)来显式表示该变量的缺失——这与MA-GCT『特征专属可学习掩码嵌入』的设计高度相似,是证据中与MA-GCT基础工作最直接对应的先例,但该证据本身未涉及影子变量或MNAR识别层面的讨论。 [@lee2025Mirrams]
- 静态变量缺失用MICEforest(基于LightGBM的多重插补,3次插补迭代、其余参数全默认)处理,是证据中被特别标注为『与主线最相关』的一条方法论借鉴,但同样未涉及缺失机制的形式化检验或影子变量式识别。 [@pang2026Featureinterpretable]
- 缺失性在某文献中具有双重身份:既是基础元特征组的成员,又是一个独立的固定控制变量feature_missing_fraction——这种把缺失既当描述性特征又当可控变量的设计,或可为MA-GCT把掩码嵌入既用作输入表征又用作可调节的先验约束提供参照。 [@herre2026Explaining]
- 一项关于decay式架构的作者自陈局限指出:若缺失完全不具信息性、或缺失模式与任务的相关性不明,模型『可能只获得有限提升甚至失败』,需要对应用领域有良好理解;该机制换到新领域需要显式重新设计——这提醒MA-GCT的Guide/Prior注意力约束能否跨中心迁移,同样可能依赖于对目标领域缺失机制的显式重新校准,而非默认可迁移。 [@che2018Recurrent]
- 一项仿真设计(领域未明确限定为临床/EHR)以2×2形式测试缺失机制有/无潜变量交叉线性/非线性变换,其中『有潜变量』一侧对相应方法无模型误设、『无潜变量』一侧则是刻意制造的误设,这类对照设计思路可为检验MA-GCT掩码嵌入在缺失机制误设下的鲁棒性提供借鉴。 [@xie2026Identifiable]
- 另有训练/评估协议(领域未明确限定为临床)区分block missing(连续缺失时间块大小2～24之间随机)与blackout(部分时间步所有变量同时缺失)两种模式,可为测试MA-GCT跨中心迁移下的缺失异质性提供参照,但同样未涉及影子变量或MNAR前提检验。 [@周立基于深度学习的不完整时序数据补全方法综述]
- 在成像模态(彩色多普勒图像)中,缺失病例用同标签病例的中位特征向量插补,而非在模型层面建模缺失,提示即便在临床数据的其他模态中,直接建模缺失机制(而非仅做插补)仍属少数做法;当原始预测变量因缺失不可得时,也有研究改用代理变量替代,但可能影响校准准确性——这类『用代理变量填补预测变量本身』的做法与『用影子变量识别另一变量的缺失机制』是两个不同问题,转述时需避免混淆两者。 [@yu2026Integrating] [@ogero2023Recalibrating]
- 早期方法学示例中,四个变量各有约20%的值被随机删除以人为制造缺失(MCAR式模拟),另一变量在原始数据集中本就有两例自然缺失——这类人为MCAR注入是缺失数据方法学中常见的基准构造方式,与临床数据中真实的信息性缺失机制形成对照。 [@little1988Test]

## MIMIC等基准任务中缺失被当作预处理杂项而非研究对象

- MIMIC相关临床任务(ICU死亡率、ICU住院时长)明确采用作者默认的缺失处理方法,全程未把缺失/插补/缺失指示符当作研究变量,论文全文无一处讨论sparsity-aware或缺失指示符相关机制——本论文关心的机制层完全未被这类基准触及。 [@gardner2023Benchmarking]

## ⚔️ 分歧与冲突

### 应用文献对缺失机制假设的强度差异:是默认MAR/MCAR且不检验,还是明确处理informative missingness/MNAR

- **一方**：多项研究直接假设缺失为MAR或MCAR,这一假设仅基于常规做法或临床评估惯例、未做实证检验,部分研究甚至完全不引入任何MCAR/MAR/MNAR的形式化区分 [@shanbehzadeh2022Predictive] [@li2026Causal] [@li2026Predictive] [@hu2026Dual] [@loni2025Review]
- **另一方**：另一些研究把informative missingness/MNAR作为明确的处理或形式化定义对象,在方法设计或问题定位阶段就纳入考虑,甚至将『缺失以随机方式发生』的假设明确列为边界条件而非默认前提 [@tan2023Informative] [@wiegand2022Development] [@sisk2023Imputation] [@vo2024Federated]
- *为何分歧*：反映应用文献对缺失机制假设的检验强度差异极大,也正是本页核心问题(影子变量前提如何被检验)证据稀薄的原因之一

### 缺失指示符/哑变量是否足以处理信息性缺失

- **一方**：多项应用工作把缺失指示符或哑变量作为标准做法直接纳入预测模型,视为处理缺失信息的常规且充分的手段 [@gao2026Comparing] [@murray2024Augmenting] [@lee2021Multiview]
- **另一方**：另一些研究批评这类做法:把指示变量的缺失一律置0等于把『没记录』当成『没有』,是正在被讨论的偏倚本身;简单中位数填补即便配合缺失指示特征,仍可能未能充分捕捉信息性缺失、影响跨中心稳健性 [@zink2026Access] [@dayanFederatedLearningPredicting2021b] [@dam2025Readmission]
- *为何分歧*：两方并非直接互相反驳,而是不同论文对同一套技术手段的默认使用与事后批评并存,提示该手段的充分性本身尚无共识

## 证据缺口

- 缺乏任何一项在EHR/临床数据上对具体候选变量(如诊疗强度指标、检验开单行为、站点身份)正式执行排他性检验(exclusion restriction test)的研究——现有证据只在定义层(I-MNAR、m-graph)或域外场景(E5的LLM对话抽取)提及影子变量式结构
- 缺乏把诊疗强度/检验开单行为/站点身份等被点名的候选弱信号,实际代入某个MNAR识别或插补流程并报告效果的证据——这些候选目前只停留在『可能相关』的定性描述
- 缺乏检验MA-GCT的特征专属可学习掩码嵌入或Guide/Prior注意力约束,能否在跨中心场景下起到类似影子变量作用的直接证据——E48/E21/E50/E53/E38/E31只提供方法论上的间接类比或对照设计思路,均未直接针对MA-GCT本身或影子变量识别做过测试
- 缺乏比较『树模型原生缺失处理(代理分裂)』与『显式可学习缺失嵌入/影子变量建模』两条路线在同一临床数据集上的直接效果对比——E60与E48/E21分别代表两条路线,但无证据把二者放在一起评估
- 缺乏任何量化证据说明,用候选弱影子变量辅助MNAR识别相较于直接假设MAR/MCAR或使用缺失指示符,能带来多大的偏倚校正或性能收益——现有量化证据(如E13的FMI)只涉及辅助变量对信息损失的一般性影响,不针对具体临床候选变量

## 本页证据（60 条 · 60 篇）

每条可回溯到札记原文；未被引用的证据标 ○。引用率高低不能单独当质量信号——判断这一页可不可信，看下面论断是否具体、分歧是否来自不同文献，比看这个比例可靠。

- ● **E1** `[@butler2023Noninterventional]` 🟩方法论借鉴 · 方法与数据 · 科研札记_2023-01_全文精读.md:944
  <small>Noninterventional studies in the COVID-19 era: methodological considerations for study design and analysis.</small>
  > 对于缺失的混杂因素，建议使用全可用协变量评估期，并采用完全案例分析、末次观测结转、缺失模式法、多重插补和逆缺失概率加权。
- ● **E2** `[@wiegand2022Development]` 🟩方法论借鉴 · 方法与数据 · 科研札记_2022-09_全文精读.md:192
  <small>Development and validation of a dynamic 48-hour in-hospital mortality risk stratification for COVID-19 in a UK teaching hospital: a retrospective cohort study.</small>
  > 处理了含有信息的预测变量缺失（informative predictor missingness），并利用惩罚回归进行变量选择。
- ● **E3** `[@huang2023Federated]` 🟩方法论借鉴 · 方法与数据 · 科研札记_2023-10_全文精读.md:310
  <small>Federated machine learning for predicting acute kidney injury in critically ill patients: a multicenter study in Taiwan</small>
  > 缺失数据采用均值插补（针对连续变量）或0/1编码（药物），并报告了缺失比例。
- ● **E4** `[@little1988Test]` 🟪可引用证据 · 实验方法 · 科研札记_2026-07_手动精读.md:5827
  <small>A Test of Missing Completely at Random for Multivariate Data with Missing Values</small>
  > 【特征】在该示例中，后四个变量（birthpill、cholesterol、albumin、calcium）各有约20%的值被随机删除以人为制造缺失，weight变量则在原始数据集中即有两例缺失(p.2)。
- ● **E5** `[@chen2026Partial]` 🟩方法论借鉴 · 实验方法 · 科研札记_2026-08_手动精读.md:5498
  <small>Partial Identification under Missing Data Using Weak Shadow Variables from Pretrained Models</small>
  > 【特征与预处理】弱影子变量通过提示词从对话文本中提取，设计为 3×3 的提示矩阵，交叉「视角」（用户数字孪生、一般用户、中立服务审计员）与「构念」（整体评分、目标完成度、处理质量），并额外加入一支二元是/否的目标完成提问分支 (p.25-26)。
- ● **E6** `[@adekkanattu2023Prediction]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2023-01_全文精读.md:630
  <small>Prediction of left ventricular ejection fraction changes in heart failure patients using machine learning and electronic health records: a multi-site study</small>
  > 关键变量在不同站点缺失严重且模式不同，模型性能可能受缺失率影响，且将缺失值归为“_unknown”并排除的做法可能丢失信息。
- ● **E7** `[@anon2021Evaluating]` 🟩方法论借鉴 · 方法与数据 · 科研札记_2021-02_全文精读.md:49
  <small>Evaluating the Impact of Covariate Lookback Times on Performance of Patient-Level Prediction Models</small>
  > binary covariates以记录的有无表示，因此协变量本身不包含缺失值；条件、药物或操作记录的缺失被处理为该患者不具有该条件、药物或操作，而left censoring可能导致这类记录缺失。
- ● **E8** `[@ghosheh2022Review]` 🟪可引用证据 · 方法与数据 · 科研札记_2026-07-27_手动精读.md:613
  <small>A review of Generative Adversarial Networks for Electronic Health Records: applications, evaluation measures and data sources</small>
  > **§4.3 缺失插补一节列出的缺失成因值得逐条记下**：数据记录错误与机器故障、不规则采样与就诊不一致、**因缺乏医学必要性而未做的化验**、以及**因高成本或高风险而难以获取的信息（如侵入性操作或放射检查）与其他与患者严重程度和诊断相关的因素**。后两条本身就是 informative missingness 的定义。
- ● **E9** `[@shanbehzadeh2022Predictive]` 🟩方法论借鉴 · 方法与数据 · 科研札记_2022-05_全文精读.md:321
  <small>Predictive modeling for COVID-19 readmission risk using machine learning algorithms</small>
  > 剩余缺失值采用中位数填充，假设数据缺失为随机缺失（MAR）。
- ● **E10** `[@bauer2025Sepsis]` 🟩方法论借鉴 · 方法与数据 · 科研札记_2025-12_全文精读.md:52
  <small>Sepsis and septic shock case identification from electronic health records: an open-source workflow and comparison of cohorts by criteria</small>
  > 缺失数据被描述，但研究未使用插补方法处理缺失。
- ● **E11** `[@gao2026Comparing]` 🟪可引用证据 · 实验方法 · 科研札记_2026-07_手动精读.md:3501
  <small>Comparing methods for handling missing data in electronic health records for dynamic risk prediction of central-line associated bloodstream infection</small>
  > 【特征/缺失处理】缺失指示符方法为每个变量新增取值0/1的哑变量表示该变量是否缺失，所有医疗专科类别共用同一个缺失指示符，缺失值本身用固定值99替代后一并纳入多变量预测模型(Table 2, p.5)。
- ● **E12** `[@ogero2023Recalibrating]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2023-02_全文精读.md:880
  <small>Recalibrating prognostic models to improve predictions of in-hospital child mortality in resource-limited settings.</small>
  > 缺少原始预测变量（如无意识）时使用代理变量，可能影响校准准确性。
- ● **E13** `[@madleydowd2019Proportion]` 🟪可引用证据 · 结果与效应量 · 科研札记_2026-07-27_手动精读.md:1318
  <small>The proportion of missing data should not be used to guide decisions on multiple imputation</small>
  > **无辅助信息时 FMI ≈ 缺失比例**；加入辅助信息后 FMI 随之下降。这解释了为何缺失比例在无辅助变量的特例下「看起来」有用——它只是 FMI 的一个退化情形。
- ● **E14** `[@yadgarov2023Early]` 🟩方法论借鉴 · 实验方法 · 科研札记_2024-10_全文精读.md:83
  <small>Early detection of sepsis using machine learning algorithms: a systematic review and network meta-analysis</small>
  > 所用软件/工具包括 CINeMA 软件、ROB-MEN 网页应用和 STATA 17.0；缺失SD值插补使用 Python sklearn 库的 Iterative Imputer（贝叶斯回归模型）。
- ● **E15** `[@jomaa2021Dataset2vec]` 🟦可反驳观点 · 交叉核验记录 · 科研札记_2026-07-28-TFM_手动精读.md:1670
  <small>Dataset2Vec: Learning Dataset Meta-Features</small>
  > 纠错（p.17）：缺失数据的定性须改正：不是「未涉及」而是被主动回避。第 17 页称 OpenML 等其它表格数据源「suffer from quality issues (missing values, require pre-processing, etc.)」，故只用预处理并归一化过的 UCI 数据集；另 Table 3（第 14 页）里 MF2 含 Missing values 这一工程元特征。全文无任何缺失机制的讨论。
- ● **E16** `[@zhou2023Missing]` 🟪可引用证据 · 关键结论 · 科研札记_2023-04_全文精读.md:195
  <small>Missing data matter: an empirical evaluation of the impacts of missing EHR data in comparative effectiveness research</small>
  > 不同插补方法在不同缺失场景下表现差异较大，无单一方法普遍适用。
- ● **E17** `[@vo2024Federated]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2023-08_全文精读.md:49
  <small>Federated Learning of Causal Effects from Incomplete Observational Data</small>
  > CausalFI 仅在“缺失以随机方式发生（missing at random assumption）”的前提下处理缺失数据，并未处理缺失机制本身依赖于缺失值自身或未观测因素的情形，作者也将其列为该方法的边界条件。
- ● **E18** `[@tan2023Informative]` 🟩方法论借鉴 · 研究问题与定位 · 科研札记_2026-07-17_手动精读.md:31
  <small>Informative missingness: What can we learn from patterns in missing laboratory data in the electronic health record?</small>
  > 作者提出"informative missingness"并将其归为 MNAR 的特例——informative-missing not at random(I-MNAR):某变量的缺失依赖其自身取值,也可能受其他(观测或未观测)变量影响。原文明确措辞经核对无误。
- ○ **E19** `[@liu2025Fedrecon]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2025-04_全文精读.md:202
  <small>FedRecon: Missing Modality Reconstruction in Distributed Heterogeneous Environments</small>
  > 文中一处关键效应量声明（“surpassing baseline methods by 3.06% and…”）在提供的文本块中被截断，缺失具体指标与数据集信息，无法作为可核实的证据引用。
- ● **E20** `[@lee2021Multiview]` 🟩方法论借鉴 · 方法与数据 · 科研札记_2021-01_全文精读.md:35
  <small>Multi-view Integration Learning for Irregularly-sampled Clinical Time Series</small>
  > 已有工作仅使用missing indicator或time interval两者之一,并采用heuristic decaying function(如单调不增函数),而没有学习missingness的表示;本文希望同时对observed values、missing indicators与time interval之间的关系做显式学习。
- ● **E21** `[@pang2026Featureinterpretable]` 🟪可引用证据 · 方法与数据 · 科研札记_2026-08-16_手动精读.md:22
  <small>Feature-interpretable disease prediction from tabular data via dynamic GCN and LLMs</small>
  > ⚠️【缺失处理，与我主线最相关】静态变量的缺失值用 **MICEforest**（基于 LightGBM 的多重插补）处理，**3 次插补迭代、其余参数全默认**(p.4)。
- ● **E22** `[@gardner2023Benchmarking]` 🟦可反驳观点 · 研究问题与范围界定(与本论文的分界) · 科研札记_2026-07-17_手动精读.md:575
  <small>Benchmarking Distribution Shift in Tabular Data with TableShift</small>
  > 全程未把缺失/插补/缺失指示符当作研究变量:MIMIC临床任务(ICU死亡率、ICU住院时长)明确"因预处理数据含缺失值,我们使用作者默认的缺失处理方法"(§B.11,p.23),缺失被当作预处理杂项一笔带过,本论文的机制层它完全未触及,论文全文也无一处讨论sparsity-aware/缺失指示符/impute-then-regress。
- ● **E23** `[@murray2024Augmenting]` 🟩方法论借鉴 · 方法与数据 · 科研札记_2024-04_全文精读.md:338
  <small>Augmenting mortality prediction with medication data and machine learning models</small>
  > SOFA 和 APACHE II 缺失分量默认按“正常”处理，其余变量采用虚拟变量调整缺失（Dummy Variable Adjustment），并将缺失指示变量一并纳入模型。
- ● **E24** `[@hu2026Dual]` 🟩方法论借鉴 · 方法与数据 · 科研札记_2026-02_全文精读.md:414
  <small>A novel dual elastography-based model for screening high-risk varices in hepatitis B virus-related cirrhosis.</small>
  > 缺失数据＜5%，采用均值/中位数/众数简单填补法。
- ● **E25** `[@little2020rubin]` 🟪可引用证据 · Ch.1 · pp.3-28 · 1 Introduction · 让 MAR 更可信的办法（本章对本项目最直接的一段） · 科研札记_2026-08-26-LittleRubin2020_书籍精读.md:20
  <small>Statistical Analysis with Missing Data</small>
  > 当缺失不受数据收集者控制时，本章给出的处方就是收集辅助变量："the MAR assumption is made more plausible by collecting data" Y_1,…,Y_{K−1} on respondents and nonrespondents that are predictive both of Y_K and the probability of missingness。
- ● **E26** `[@moreau2026Best]` 🟩方法论借鉴 · 方法综述与一条重要的划界 · 科研札记_2026-08_手动精读.md:8429
  <small>Best Practices in Handling Missing Data in Psychological Research</small>
  > 术语表还定义了 **selection indicator (R)**、**selection bias**（条件于观测数据因缺失过程充当选择机制而在变量间诱发非因果关联）、**structural missingness**（因设计而缺失）等（p.3–p.4）。
- ○ **E27** `[@vaidya2026Nova]` 🟪可引用证据 · 结果与效应量 · 科研札记_2026-08_手动精读.md:3470
  <small>NOVA: An Agentic Framework for Automated Histopathology Analysis and Discovery</small>
  > 自定义工具消融（表 J.1）：无自定义工具 0.326、RAG 检索开源库文档造工具 0.337、自定义工具 0.477——文档 RAG 远不足以替代精心手工设计的工具；且无工具时 DataQA 耗时从 2.76h 升到 4.20h 而分数从 0.777 跌到 0.537。
- ● **E28** `[@panizza2026Physical]` 🟦可反驳观点 · 可质疑点 · 科研札记_2026-07-27_全文精读.md:234
  <small>Physical Activity Behavior and Acute Myocardial Infarction, Stroke, and Sepsis Outcomes in Brazil: Insights from Targeted Eigenvector Centrality Networks</small>
  > 孟德尔随机化所依赖的工具变量假设（如无多效性）在复杂行为表型中难以完全满足，结论可能受水平多效性影响。
- ● **E29** `[@mesinovic2024Dysurv]` 🟩方法论借鉴 · 方法与数据 · 科研札记_2026-06_手动精读.md:1096
  <small>DySurv: dynamic deep learning model for survival analysis with conditional variational inference</small>
  > 缺失处理是作者明确交代过的一段：静态变量取 age、sex、admission unit 等「本身没有缺失」的变量，时序变量用前向填补（forward filling），理由是「临床医生在实践中也只会参考最后一次记录的测量值」，且前人在这些数据集上就是这么做的。
- ● **E30** `[@rekkas2023Standardized]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2026-08-npjDM-supplement_全文精读.md:216
  <small>A standardized framework for risk-based assessment of treatment effect heterogeneity in observational healthcare databases</small>
  > 观察性数据库容易受偏倚、结局测量不佳及缺失（missingness）影响，可能掩盖真实的治疗效应异质性或制造出并不存在的假象——但作者未就缺失机制本身做任何区分或建模，只是一笔带过。
- ● **E31** `[@周立基于深度学习的不完整时序数据补全方法综述]` 🟪可引用证据 · 实验方法 · 科研札记_2026-07_手动精读.md:7692
  <small>基于深度学习的不完整时序数据补全方法综述</small>
  > 【训练与评估协议】block missing模式下连续缺失时间块大小为2～24之间的随机数，blackout模式下部分时间步上所有变量同时缺失(p.14-15)。
- ● **E32** `[@chang2025Machine]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2025-12_全文精读.md:297
  <small>Machine Learning Approaches to Clinical Risk Prediction: Multi-Scale Temporal Alignment in Electronic Health Records</small>
  > 正文在动机部分提到EHR数据“存在高维、噪声和严重缺失值”，但方法部分仅以一句“缺失值填补”带过预处理环节，未说明具体填补方法、缺失比例，也未讨论缺失是否随机分布。
- ● **E33** `[@koornwinder2025Multimodal]` 🟪可引用证据 · 可质疑点 · 科研札记_2025-03_全文精读.md:75
  <small>Multimodal Artificial Intelligence Models Predicting Glaucoma Progression Using Electronic Health Records and Retinal Nerve Fiber Layer Scans</small>
  > 研究中未交代EHR缺失数据的类型（如完全随机缺失、随机缺失、非随机缺失）及其处理策略，可能导致预测偏差。
- ● **E34** `[@loni2025Review]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2026-06_手动精读.md:1684
  <small>A review on generative AI models for synthetic medical text, time series, and longitudinal data</small>
  > 关于「Data imputation」的处理是全文对缺失机制最薄弱的地方：作者只说缺失「systematically or randomly」发生，从未引入 MCAR/MAR/MNAR 的形式化区分，也没有任何一条指标（补充表2 的 LPL/MPL 也不例外）能区分模型是在 MAR 假设下填补还是真的处理了信息性缺失——这一判断经全文与补充材料逐词检索确认，无一处出现 MNAR/MCAR/MAR 字样。
- ○ **E35** `[@mukherjee2024Causal]` 🟦可反驳观点 · 方法与数据 · 科研札记_2026-08_手动精读.md:7021
  <small>Causal considerations can determine the utility of machine learning assisted GWAS</small>
  > 重要方法学缺口：论文正文自述“模拟所用的具体参数可在共享代码中找到，但我们的主要观察预期与具体参数选择无关”，因此正文中没有给出任何模拟的样本量 N、变异数 J、因果变异个数或效应量分布的具体数值——所有定量结论仅以热图呈现，无法脱离代码复现。代码地址 https://github.com/insitro/causal_considerations_ml_assisted_gwas 。
- ● **E36** `[@zhang2026Predicting]` 🟪可引用证据 · 实验方法 · 科研札记_2026-08_手动精读.md:9503
  <small>Predicting Laboratory Test Ordering in Emergency Departments Using Integrated Structured and Unstructured Electronic Health Records: Machine Learning Study</small>
  > 【特征与预处理】Missing data handled via median imputation for structured variables including vital signs and demographics (p.3).
- ● **E37** `[@sisk2023Imputation]` 🟪可引用证据 · 实验方法 · 科研札记_2026-06_手动精读.md:1815
  <small>Imputation and missing indicators for handling missing data in the development and deployment of clinical prediction models: A simulation study</small>
  > 【特征与预处理】预测变量 X1 部分观测且可能存在信息性缺失，X2 完全观测，U 为未观测变量；以二元指示变量 M1 表示 X1 的缺失情况 (p.3)。
- ● **E38** `[@xie2026Identifiable]` 🟩方法论借鉴 · 实验方法 · 科研札记_2026-08_手动精读.md:7352
  <small>Identifiable Deep Latent Variable Models for MNAR Data</small>
  > 仿真 1（3 维，Table 1, p.15）：2×2 设计——缺失机制**有/无潜变量** × 变换**线性/非线性**。「有潜变量」一侧对本方法无模型误设，「无潜变量」一侧则是刻意制造的误设。
- ● **E39** `[@li2026Causal]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2026-08-npjDM_全文精读.md:336
  <small>A Causal and interpretable machine learning framework for postcranioplasty risk prediction and surgical decision support</small>
  > 缺失数据处理假设为完全随机缺失（MCAR），即便经临床评估也可能过于严格，未探讨非随机缺失的可能性。
- ● **E40** `[@dayanFederatedLearningPredicting2021b]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2025-05_全文精读.md:769
  <small>Federated Learning for Predicting Major Postoperative Complications</small>
  > 缺失值处理采用简单中位数填补，虽创建了缺失指示特征，但可能未能充分捕捉信息性缺失，影响跨中心稳健性。
- ● **E41** `[@xu2024Basic]` 🟩方法论借鉴 · 研究问题 · 科研札记_2026-07-27_手动精读.md:1567
  <small>From Basic to Extra Features: Hypergraph Transformer Pretrain-then-Finetuning for Balanced Clinical Predictions on EHR</small>
  > **这本质上是「设计性缺失」而非「测量性缺失」**：不是变量测了没记，而是该机构根本没采集这个变量。它与我关心的 MNAR 是两种不同的结构化缺失，但在建模上会碰到同一堵墙——特征维度不一致。
- ● **E42** `[@dam2025Readmission]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2026-07_手动精读.md:7154
  <small>ICU readmission and mortality risk prediction: Generalizability of a multi-hospital model</small>
  > 缺失值处理相当粗糙：仅用'非随机缺失特征用固定值、其余用中位数'的二分法插补，正文未说明如何判定某特征属于非随机缺失、也未做插补方式的敏感性分析，与当前缺失感知表征学习/图神经网络插补路线相比方法论明显滞后。
- ● **E43** `[@sathe2021Identification]` 🟦可反驳观点 · 可质疑点 · 科研札记_2021-09_全文精读.md:244
  <small>Identification of persistent and resolving subphenotypes of acute hypoxemic respiratory failure in two independent cohorts</small>
  > 协变量存在缺失的观测被整体剔除而未评估缺失机制或采用插补方法，若缺失并非完全随机（MCAR），该做法可能引入选择偏倚，且原文未报告具体缺失比例。
- ● **E44** `[@zink2026Access]` 🟦可反驳观点 · 实验方法 · 科研札记_2026-08_手动精读.md:572
  <small>Access to care affects electronic health record reliability and AI-driven disease prediction</small>
  > 【缺失处理】**指示变量的缺失一律置 0**，年龄无缺失；zip 层 SES 特征用均值插补；连续变量标准化（Methods p.13）。⚠️ 「缺失置 0」把「没记录」直接当成「没有」，正是本文自己在讨论的偏倚，却在建模时照做不误。
- ● **E45** `[@li2024Learning]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2026-08-17-影子变量与collider偏倚_手动精读.md:138
  <small>Learning Shadow Variable Representation for Treatment Effect Estimation under Collider Bias</small>
  > Table 6 那个上界对照只在**一个**合成设定（d_s=0.9d、α=1e-6、β=5）下做了，未across 设定重复，「能学到有效影子变量表示」这一核心主张的证据面偏窄。
- ● **E46** `[@wan2025Exploring]` 🟩方法论借鉴 · 方法与数据 · 科研札记_2025-09_全文精读.md:82
  <small>Exploring trajectories of acute kidney injury in the intensive care unit: a population-based cohort study</small>
  > 缺失数据采用链式方程多重插补（multiple imputation by chained equations）处理。
- ● **E47** `[@sisk2020Informative]` 🟪可引用证据 · 结果与效应量 · 科研札记_2026-08_手动精读.md:11188
  <small>Informative presence and observation in routine health data: A review of methodology for clinical risk prediction</small>
  > 软件可得性极不均衡（Table 3, p.161）：缺失指示、汇总测量、模式特异模型"easily applied in common statistical software"；隐变量有 Coley & Hubbard 提供的 R 代码；联合模型有 R 的 frailtypack、WinBUGS、STATA 的 merlin；而似然法、相似度测量、HMM 三类均标注 "None provided"。
- ● **E48** `[@lee2025Mirrams]` 🟩方法论借鉴 · 实验方法 · 科研札记_2026-07-17_手动精读.md:464
  <small>MIRRAMS: Learning Robust Tabular Models under Unseen Missingness Shifts</small>
  > 【特征与预处理】缺失值不做均值/零值等数值填补，而是为每个输入变量引入一个单独的可学习缺失嵌入向量（token embedding）来显式表示该变量的缺失(p.3, p.12)。
- ● **E49** `[@zhang2026Newonset]` 🟩方法论借鉴 · 实验方法 · 科研札记_2026-08_手动精读.md:1622
  <small>New-Onset Diabetes Assessment Using Artificial Intelligence-Enhanced Electrocardiography</small>
  > 【特征与预处理】变量缺失值使用scikit-learn包中的链式方程多重插补(MICE)方法进行插补(p.4)。
- ● **E50** `[@herre2026Explaining]` 🟪可引用证据 · 方法与数据 · 科研札记_2026-07-28-TFM_手动精读.md:375
  <small>Explaining Tabular Foundation Model Differences Through Meta-Features</small>
  > 缺失性在本文中有两重身份：既是基础元特征组 (a) 的成员（第2页明写 missingness），又是五个固定控制变量之一 feature_missing_fraction（第3页）——因此并非「仅作 control」。
- ● **E51** `[@bellamy2023Labrador]` 🟦可反驳观点 · 方法与数据:各方法拿到什么缺失处理(对称性核验通过) · 科研札记_2026-07-17_手动精读.md:359
  <small>Labrador: Exploring the Limits of Masked Language Modeling for Laboratory Data</small>
  > 补充细节(F.3.3):cancer队列的8个社会/类别变量(age、gender、smoking status、diabetes status等)缺失用的是'missing indicator method',而非lab数值特征所用的均值填补——两者机制不同,引用不对称论点时应限定为'lab数值特征'层面。
- ● **E52** `[@li2022Mogcn]` 🟦可反驳观点 · 图表与补充材料要点 · 科研札记_2026-08_手动精读.md:2003
  <small>MoGCN: A Multi-Omics Integration Method Based on Graph Convolutional Network for Cancer Subtype Analysis</small>
  > Figure 5B 表注里「?」被定义为「missing data」（BH-A209 的 HER2 一栏），这是全文唯一一处涉及缺失的标注，且只是临床 IHC 注释缺失，不是组学缺失，也没有任何处理办法。
- ● **E53** `[@che2018Recurrent]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2026-08_手动精读.md:7899
  <small>Recurrent Neural Networks for Multivariate Time Series with Missing Values</small>
  > 作者自认:若缺失完全不 informative、或缺失模式与任务的相关性不明,『模型可能只获得有限提升甚至失败』,需要对应用领域的良好理解;decay 机制换领域需显式重新设计 [p.10]。
- ● **E54** `[@ceriscioli2026Discovering]` 🟩方法论借鉴 · 方法与数据 · 科研札记_2026-08_手动精读.md:7251
  <small>Discovering Linear Non-Gaussian Models for All Categories of Missing Data (Student Abstract)</small>
  > 缺失用 missingness graph（m-graph, Mohan/Pearl/Tian 2013）表示：一个因果 DAG G=(V,E)，V = V_o ∪ V_m ∪ U ∪ V* ∪ R，V_o 为完全观测变量、V_m 为部分观测变量、U 为潜变量（p.1）。
- ● **E55** `[@li2026Predictive]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2026-08-03_全文精读.md:135
  <small>Predictive value of complete blood cell count-based inflammatory markers for peritonitis risk in peritoneal dialysis patients: A multicenter cohort study</small>
  > 研究采用的中位数插补法处理缺失值，可能无法完全解决非随机缺失数据（non-random missing data）带来的潜在偏倚。
- ● **E56** `[@li2021Imputation]` 🟪可引用证据 · 逐节通读要点 · 科研札记_2026-06_手动精读.md:1389
  <small>Imputation of missing values for electronic health record laboratory data</small>
  > Methods-Recognition of missingness:缺失定义为"未做检验"或"做了但值在三个IQR之外";缺失分析限定在缺失比例<75%的检验变量。
- ● **E57** `[@yu2026Integrating]` 🟩方法论借鉴 · 方法与数据 · 科研札记_2026-07_手动精读.md:7797
  <small>Integrating Ultrasound-CT-MR for Preoperative Multi-Task Prediction in Ovarian Cancer: Achieving Diagnostic Parity with Multidisciplinary Team Consensus</small>
  > 对149例缺失彩色多普勒图像的病例，用同标签病例的中位特征向量插补(median feature imputation)，而非在模型层面建模缺失。
- ● **E58** `[@sealock2024Crossehr]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2024-09_全文精读.md:155
  <small>Cross-EHR validation of antidepressant response algorithm and links with genetics of psychiatric traits</small>
  > 算法未纳入用药剂量信息，原因是EHR中剂量数据缺失比例较高。
- ● **E59** `[@zargoush2021Impact]` 🟩方法论借鉴 · 方法与数据 · 科研札记_2021-10_全文精读.md:251
  <small>The impact of recency and adequacy of historical information on sepsis predictions using machine learning</small>
  > 缺失值使用Python Autoimpute包中的多重链式方程插补算法(MICE)进行插补。
- ● **E60** `[@wu2026Artificial]` 🟪可引用证据 · 实验方法 · 科研札记_2026-07-27_手动精读.md:839
  <small>Artificial Intelligence-powered tiered early warning framework addressing high false alarm rates for in-hospital mortality prediction</small>
  > 【特征】分类变量缺失值被统一赋予一个独立的“unknown”类别以保留数据完整性信息；连续变量的缺失值在基于树的集成模型(可通过代理分裂等机制原生处理缺失)中予以保留，而对于需要完整数据的模型(如逻辑回归)，缺失值按分布偏态情况用中位数或均值填补(p.14)。
<!-- END GENERATED · 以下内容属于你，生成器永不触碰 -->

## 我的批注
