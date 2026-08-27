---
topic: "icu-benchmarks"
title: "多库 ICU 基准与数据集特性"
type: "topic"
topic_schema: 1
generated_at: "2026-08-27T11:59:34"
n_evidence: 60
n_papers: 60
n_claims: 47
n_disputes: 2
n_gaps: 4
evidence_used: 56
dropped_claims: 0
invalid_refs: 0
stripped_cites: 0
tags:
  - "札记/概念页"
  - "概念/icu-benchmarks"
---

<!-- BEGIN GENERATED v1 h=3ef7f6b6 · 由 scripts/build_topics.py 生成 · 此块会被重建覆盖，请写在下面的「我的批注」里 -->
# 多库 ICU 基准与数据集特性

> MIMIC-IV / eICU / AmsterdamUMCdb / HiRID 各自的缺失特性与可比性如何？跨库实验的常见口径陷阱是什么？

60条证据中仅E51/E52/E11/E60正面涉及多库对比,无一条同时覆盖MIMIC-IV/eICU/AmsterdamUMCdb/HiRID四库并给出特征级缺失率;同一数据库在不同研究间报告规模常相差7倍以上,单位口径(人次/患者数/住院次数/样本数)也常混用。库内缺失率呈明显梯度:结构性/自动化变量趋近0缺失,人工录入与化验类变量可达40%-99%。跨库陷阱集中于完整性筛选带来的选择偏倚、排除口径不透明、统计单位混用;E30/E33直接点出MNAR假设与建模缺口,但无证据触及『缺失条件依赖结构』的跨中心可迁移性,也无证据评估MA-GCT本身。

## 三库/四库层面的正面对比证据

- 三个ICU库(MIMIC-III/eICU/HiRID)在同一研究中被直接对比:MIMIC-III 28,344人/78个连续变量/20个离散变量(ICU死亡6.59%、院内死亡3.21%、30天再入院3.95%),eICU 99,015人/55/19(ICU死亡4.54%),HiRID 14,129人/50/39(ICU死亡8.96%),三库合计141,488名患者;HiRID的ICU死亡率约为eICU的1.97倍。 [@li2023Generating]
- 另一项覆盖四库(MIMIC-III/IV、eICU、HiRID、AmsterdamUMCdb)的研究报告合计334,812例ICU住院:MIMIC-III/IV约40k/73k,eICU约201k,HiRID约34k,AUMCdb约23k;eICU规模约为AUMCdb的8.7倍。 [@water2023Another]
- 本页证据中唯一给出AmsterdamUMCdb特征级缺失率的来源是一项三库(AmsterdamUMCdb/OSUWMC/MIMIC-III)对比:Lactate缺失率88%/90%/89%,PT为78%/92%/80%,Urineoutput为23%/39%/33%,Creatinine为75%/85%/80%,四个变量在三库间差异均在14个百分点以内(以PT差异最大);但OSUWMC不属于本页四库范围,且该证据不含eICU、HiRID的对应数据。 [@yin2024Sepsislab]
- MIMIC-III与eICU两个真实世界EHR数据集是常见的跨库配对组合,常用于ICU/eICU入院后24小时和48小时两个时间点的临床时间序列插补与院内死亡预测评估任务。 [@liu2023Contrastive]

## 同一数据库在不同研究中报告的规模并不固定

- 『MIMIC-IV』在不同研究中因纳排标准、时间窗与特征集不同,报告规模从57,212次(要求ICU住院时长≥12小时且≥5次化验医嘱,总死亡率约12%)到65,623次(训练集,50,354名患者,死亡率10.1%,6,659死)、92,938次(61特征:50个实验室值+5个生命体征+氧饱和度+5种血管加压药,412,365患者天)再到约43.1万次ICU就诊(用于模态缺失实验),最小与最大相差约7.5倍;另有研究以35,131例患者/1,686,288个时间点(该研究还纳入了非ICU的NSCLC肿瘤真实世界EHR数据与非ICU的阿尔茨海默病ADNI数据作对照,两者不应作为ICU库特性的直接证据)或18,835个样本(正负比约0.721)为统计单位,与上述『次数』口径不可直接比较。 [@ji2025Exosito] [@zeng2023Neural] [@xiang2026Learning] [@bhandari2026Comparative] [@makarov2025Large] [@xie2025Methodological]
- 『MIMIC-III』同样口径不一:患者数从13,998人(99种time series measurements,missing rate约90%,院内死亡阳性1,181例/阴性12,817例,约1:10.8)到28,344人不等;另有研究以19,714次入院(99个时间序列特征,仅入院后存活满48h的患者、只用前48h,死亡阳性1,716)为统计单位,还有研究从超6万例ICU住院中另取23,983例子集(42个数值特征)做缺失点实验——『患者数』『入院次数』与『实验子集』三种口径常被混用。 [@lee2021Multiview] [@li2023Generating] [@che2018Recurrent] [@bhandari2026Comparative]
- 『eICU』报告规模从41,026次就诊(3,093个诊断码、2,132个治疗码,平均7.70诊断/5.03治疗,该子集本身无结构信息)到超20万例ICU入院记录(纳入8个基线协变量)不等,另有研究给出99,015人、约201k、150,753次住院(外部测试集,126,804名患者,死亡率8.5%,12,878死)或42,842个样本(正负比约0.947)——就诊次数/入院记录数/人数/样本数/住院次数五种单位混用,不宜直接相除得出倍数;『无结构信息』这一说法本身也可能只对应特定版本或子集,与另一研究中含8个基线协变量的eICU是否同一版本尚不清楚。 [@choi2020Learning] [@cao2025Heterogeneityaware] [@li2023Generating] [@water2023Another] [@zeng2023Neural] [@xie2025Methodological]
- P12/PhysioNet Challenge 2012存在『全库规模』与『实验子集』的落差,且不同研究对该数据集总量的描述本身不一致:一项研究称P12全库11,988例ICU患者中仅取3,997例子集、36个特征用于缺失点实验;另一项研究称该数据集(作为『数据集二』)共含4,000条记录、覆盖48个临床变量;还有一项研究称总量为8,000份ICU记录、约48小时、33个变量,但因结局仅Training Set A公开而只使用该子集(死亡阳性554)——总记录数(4,000 vs 8,000)与变量数(48 vs 33)两处描述互相矛盾。 [@bhandari2026Comparative] [@rahman2021Interpretable] [@che2018Recurrent]

## 库内与模态内缺失率的高异质性:不存在单一的『ICU缺失率』

- 不完整生理监测数据据称影响ICU生命体征、实验室值与连续传感器流中15%至40%的观测,这一区间本身跨度已达约2.7倍,提示『ICU缺失率』这一说法在不同变量类别间差异巨大。 [@wani2026Comprehensive]
- 一份可引用的按变量类别汇总的缺失地图显示明显梯度:设备自动记录(心率、SpO₂、呼吸频率、动脉血压)缺失接近0,同类别内ST段分析/FiO₂约60-70%缺失、中心静脉压/肺动脉压/心输出量可得20-30%、主动脉内球囊反搏模式可得<5%;人工录入观察评分中心律/尿量近100%可得,氧输送装置/氧流量约80-90%可得,GCS/Ramsay镇静评分约50-60%可得,护理活动评分约20%可得;化验中血红蛋白/钠/钾/葡萄糖/血小板/肌酐近100%可得,白蛋白/ALAT/ASAT/乳酸60-75%可得,胆红素/TSH约40%可得;用药中丙泊酚约65%可得,吗啡/去甲肾上腺素40-50%可得,头孢噻肟/依诺昔酮仅10-15%可得。该地图未标注具体来源库,无法确认是否属于本页四库之一。 [@thoral2021Sharing]
- 另有队列报告极端缺失(pH缺失96.81%、胆红素92.82%、尿素86.63%、肌酐86.54%、环孢素99.92%、GCS昏迷评分97.16%、中性粒细胞减少85.20%、最高体温30.24%、导管总管腔数40.51%),与年龄、CICC等静态/结构性变量0%缺失并存(尿素/肌酐/胆红素/环孢素/pH的中位数(IQR)系对数变换后的值,非原始量纲);该证据未标明来源库。MIMIC-III队列99种time series measurements整体missing rate约90%的报告与上述极端缺失量级相印证,共同支持『结构性/静态变量几乎不缺、动态/侵入性检测变量高度缺失』这一梯度。 [@gao2026Comparing] [@lee2021Multiview]
- 与上述形成对照,也有队列报告总体缺失率仅3.8%,生命体征类变量(呼吸频率3.5%、心率2.7%、SpO2 4.1%、平均血压5.0%)全部低于5%,该缺失模式分析被用作后续设计三种掩码情景的依据。 [@poette2026Benchmarking]
- 另有队列(全体44,837例患者)报告BMI单变量缺失率高达41.20%(18,464/44,837例),而同一表中其他基线特征(性别、ICU专科分布)未见类似高缺失,提示同一队列内不同变量的缺失率可以跨越极大范围。 [@dam2025Readmission]
- 还有研究报告进入模型的全部数据约75.7%缺失(其中PRO子集约88.3%缺失,仅64名患者缺失率低于30%),以及一个EHR队列整体缺失率约80.37%——与3.8%的低缺失对照案例相比,不同队列定义(纳排标准、监测频率、特征子集选择)下缺失率绝对水平可相差超过20倍(88.3%对3.8%,约23倍)。 [@minoccheri2025Supervised] [@yang2021Multiseries] [@poette2026Benchmarking]
- 同一多中心研究内部,不同分中心的同一变量缺失率也可能显著不对齐:UNC与JHU联合队列(n=1,948,UNC 1,494+JHU 454)中约88%数据完整,但白蛋白缺失率UNC约14%、JHU约2%,相差7倍。 [@vazquez2026Federated]
- 训练集与外部验证集之间的缺失率也可能方向相反地错配:一项CBC研究中训练集缺失率为3.63%(多数参数)与20.85%(白细胞分类计数相关参数),而两个外部验证集本身样本几乎不缺失,唯独『Suspect』特征在外部验证集中反而高达52%(Desio)和53%(Bergamo)。 [@campagner2021External]
- 一项针对9个公开医疗机器学习数据集的调查显示,缺失值比例范围为0.2%至78.6%,用以说明缺失数据在真实数据集中普遍存在;该证据未逐一说明各数据集是否为ICU专属库,应作为医疗数据普遍存在缺失这一现象的旁证,而非四库的直接特性描述。 [@mohapatra2024Differentially]

## 跨库/跨中心实验的口径陷阱

- 以完整性为准入条件的筛选本身即选择偏倚:一项脓毒症队列研究从初始54,758例中排除6,822例(缺失数据>20% n=2,541、ICU<24h n=1,873、外院转入n=1,248、既往治疗限制n=1,160),最终分析队列47,936例,原始与筛选后队列在缺失模式上的差异未被报告。 [@zhang2026Machine]
- 排除标准若直接建立在『缺失即剔除』之上,保留率可能极低:一项研究因缺少转入住院日期、缺少分诊时间戳等原因从1,388,586次急诊记录筛到174,292次(保留率仅12.6%),被剔除者与保留者在缺失模式上的差异未被报告。 [@wu2026Artificial]
- 数据处理管道中的样本流失可能在报告中被静默吞掉:一项院内死亡率研究的CXR/结局配对管道从21,652条(ICU前48小时内摄片)→20,131条(AP位)→10,637条(去重、取最近一张)→最终划分训练4,485/验证488/测试1,242(阳性比例约14%-15%),三者合计仅6,215条,比上一级10,637条少4,422条(41.6%),论文对这一级流失未做任何说明。 [@elsharief2025Medmod] [@park2026Missingness]
- 排除口径的绝对数字差异巨大,若不按人群规模归一化会掩盖真实的跨中心差异:一项心律失常诊断研究(域外,非ICU EHR,尚未在ICU数据上验证)中,Center-A/B/C因『诊断标签缺失』分别剔除87,322/18,331/9,982例,因ECG信号损坏(20%以上幅值超5mV或20%采样点为0)分别剔除47,854/10,332/4,632例,因『已接受干预后采集』分别剔除110,202/21,095/11,361例,因同一个体波形已被纳入而重复剔除18,198/2,890/4,765例。 [@chen2024Congenital]
- 研究设计本身可通过选择纳入的特征子集来调节数据集整体缺失率:一项研究按数据可得性构造四个特征子集组合(A仅共病、B共病+实验室、C加吸烟、D加症状),整体缺失率从ABCD 39%、BCD 21%、CD 16%到D仅2%不等——『缺失率』这一指标本身依赖于研究者对特征集合的取舍。 [@daalen2024Bayesian]
- 多个缺失变量组合出现时,缺失模式种类会迅速增多:一项研究在14,474例完整病例之外,观察到18,713例缺失胆红素、424例缺失GCS、447例缺失呼吸频率、439例缺失收缩压,共观测到10种缺失模式(含完整病例),据此推导出17个独立的回归填补模型——跨库合并时若缺失模式定义不一致,插补策略难以直接迁移。 [@sisk2023Imputation]
- 排除高缺失指标可能损失统计功效:一项研究排除缺失率超过30%的指标(如CRP、ESR、HCY、UACR、PA、CysC)后,可能降低了检测两组在炎症、营养和肾损伤状态上差异的统计功效。 [@dong2026Objective]
- 样本划分口径本身也是一处易被忽视的跨库陷阱:一项研究要求ICU住院≥6小时并按患者ID随机8:1:1划分训练/验证/测试集,确保同一患者的记录不会跨集出现以防止数据泄漏,若跨库复现时未核对划分是否严格按患者ID隔离,可能引入数据泄漏。 [@im2025Labtopb]
- 跨中心二次分析中,两个非本页四库的独立前瞻性ICU队列规模同样不对齐(华盛顿大学Harborview医学中心n=768,范德堡大学VALID研究n=1,715,相差约2.2倍),提示队列规模不对齐是ICU多中心研究的普遍现象,不局限于四个标准基准库。 [@sathe2021Identification]
- MIMIC-IV公开版本仅覆盖全部383,220名住院/急诊患者中的50,048名ICU患者数据,且数据源自单一医疗系统(BIDMC的MetaVision),样本代表性和结论的外部有效性有限——这一限制在跨库泛化性讨论中容易被忽视。 [@meng2021Mimicif]
- 事件数有限时会连带限制缺失/混杂因素处理的空间:一项研究由于事件数有限(ICU期间和住院期间死亡分别为47例和72例),无法进行广泛的混杂因素筛选,提示小样本高缺失场景下统计功效与建模策略选择同时受限。 [@wan2025Exploring]

## 缺失机制证据:与读者MNAR/跨中心可迁移性主题直接相关

- 连续监测数据因患者活动、检查等经常中断,形成典型的临床缺失模式,提示缺失的触发本身可能与患者状态相关,而非纯随机过程。 [@kansal2025Mcmed]
- 机械通气相关亚组间的缺失率差异也可能系统性存在:一项研究中MI-during-IMV组全变量最大缺失率中位数为30.0%(IQR 21.4-52.0),对照组为28.5%(IQR 12.1-45.0),总体中位29.0%(IQR 13.3-45.0,范围1.4-98.4%)——亚组间缺失率的系统性差异本身可能携带与结局相关的信息。 [@awounvo2025Combining]
- 训练集与实际部署人群间的缺失率错配可能直接导致误分类:缺失主程序代码导致某模型预测长时间ICU停留和长时间机械通气的假阳性/假阴性增多,误分类案例中缺失率达38%-53%,而该模型训练集缺失率仅5%(另一模型训练集为36%),提示缺失结构的分布漂移(而非缺失率数值本身)可能是误分类的驱动因素。 [@dayanFederatedLearningPredicting2021b]
- 在人为注入缺失率对方法相对性能的影响上,以MIMIC-IV上RNN为例,缺失率提升至原始的70%时,某插补方法相对填补协议AUROC提升0.07%、AUPRC提升1.06%,缺失率升到90%时相对提升扩大到AUROC 11.03%、AUPRC 2.27%(原文括号内还各附一组未充分展开说明的数字10.96%、1.21%,引用时应留意)——方法间的相对差距会随人为注入的缺失率水平系统性变化。 [@liao2025Learnable]
- 有批评指出,声称贝叶斯网络『原生处理缺失数据』隐含MAR假设,但在现实ICU数据中缺失常为MNAR,此时常规边缘化可能产生偏倚,该批评文中未给出相关性能评估——这直接呼应了『缺失机制误设』这一问题,但未提供实证数据。 [@agard2025Improving]
- 有批评指出某研究完全未讨论缺失值处理:ICU时序天然不规则,正文只提到『同一小时内多次测量取平均』,对『某小时完全无测量』如何处理只字未提,而该研究在相关工作中自己引用了『EM式联合因果图学习与不规则时序缺失数据插补相结合』的算法却未采纳——提示因果图学习与缺失机制建模的结合在文献中已被提及但实践采纳率低。 [@xu2024Neural]

## 评估协议中人为设计的缺失率梯度

- 一项基准实验在9个数据集的观测值中置入缺失率10%-90%(其中PhysioNet2019数据集控制在81%-89%),缺失场景为MCAR-point。 [@周立基于深度学习的不完整时序数据补全方法综述]
- 另一项基准实验覆盖5个缺失率水平:10%、20%、40%、60%、80%。 [@pereira2024Imputation]
- 还有一项研究设定每个不完整变量各自50%缺失,跨所有场景『完整病例』比例均值为33%(范围10%-58%),具体每场景平均完整病例数(引用范围21-5769)见补充材料,该表本身不在正文内。 [@mathur2026Resurrecting]
- 一项方法论借鉴证据同时使用ICU数据(PhysioNet2012训练集A、MIMIC-III前48h数据)与域外、非临床的UCI Gesture合成时间序列(378条规则采样无缺失,人工构造4个缺失率相近但缺失-标签相关性不同的合成设定)作为基准,用于测试缺失机制(而非单纯缺失率)对下游标签的影响,后者结论尚未在真实ICU数据上验证。 [@che2018Recurrent]
- 有批评指出,某些用于缺失率基准评估的数据集规模与领域都窄:特征数仅6-9,均为工程/物理/生物领域的小型回归数据,没有EHR、没有临床数据、没有时间维度,提示把这类基准的结论外推到高维稀疏纵向的ICU数据需要额外论证。 [@you2020Handling]

## 缺失处理方法在文献中高度异质,本身构成跨库比较的障碍

- 完全案例分析直接剔除缺失病例是常见简化处理:一项三中心212例ICU队列研究直接剔除19例缺失病例(8.2%),未做缺失机制建模或插补。 [@eid2026Hybrid]
- 『取时间窗内最值+末次观测值结转+中位数填补其余缺失+min-max归一化连续特征+label encoding类别特征』是另一种常见组合流程。 [@wang2025Crisp]
- 连续变量均值插补、药物类别0/1编码是另一常见组合,且部分研究会同时报告缺失比例。 [@huang2023Federated]
- eICU相关研究中,纳入18-89岁、ICU住院12小时至10天的成年患者后,缺失率超过80%的变量被整列移除,其余缺失值以中位数或0填补。 [@liang2025Causal]
- 动态特征的缺失处理趋向『二元缺失指示列+同患者住院期间最近一次可得值向前填补+仅基于训练集计算的均值兜底』的组合,以避免数据泄漏。 [@tranchellini2026Evaluating]
- 血清肿瘤标志物类研究中,缺失处理阈值是研究者设定的:缺失超过20%整列指标被剔除,缺失低于20%用该指标中位数插补;原文这两条规则分别对应两篇不同方法学引文(剔除规则涉及样本量流失/排除方法学,插补规则涉及缺失数据插补方法学),不应笼统合并表述;该证据未明确是否来自ICU队列,可能来自非ICU的肿瘤标志物研究,引用时需留意领域边界。 [@yu2026Integrating]
- MIMIC-extract预处理流程产出的数据仍含缺失值,作者采用原作者提供的『默认方法』处理但未在正文说明细节,这类隐性默认处理进一步降低了跨研究可比性。 [@gardner2023Benchmarking]
- 也有一项对文献的系统调查显示:45%的研究(32篇)对缺失数据的处理信息缺失或表述不清,23%(16篇)采用完整病例分析,20%(14篇)采用临时方法(均值填补、缺失指示变量法、变量删除),11%(9篇)使用单次或多重随机填补但记录较差——缺失处理透明度本身在文献中普遍不足。 [@christodoulou2019Systematic]

## ⚔️ 分歧与冲突

### 缺失率高低本身与预测性能受损程度之间的关系方向

- **一方**：早期ICU再入院任务的敏感性分析显示,随机缺失率升至90%时AUROC降幅仍控制在约5%以内,且模型对高缺失率变量(如化验结果)相对鲁棒,对低缺失率变量(如生命体征)反而更敏感——提示单纯提高随机缺失率对模型性能的损害有限。 [@lim2025Multicenter]
- **另一方**：另一项研究中,某模型训练集缺失率仅5%(GNV)或36%(JAX),但在实际误分类(长时间ICU停留、长时间机械通气预测错误)案例中缺失率达到38%-53%——提示训练集与部署人群之间的缺失率/缺失结构错配会显著增加假阳性/假阴性。 [@dayanFederatedLearningPredicting2021b]
- *为何分歧*：两者衡量的并非同一维度:E16是对同一模型注入随机(可能MCAR式)缺失后的鲁棒性测试,E13是训练分布与部署分布之间缺失结构的分布漂移。二者合并看,提示『缺失率数值高低』本身可能不如『缺失结构是否与训练时的分布一致』更能预测模型受损程度,但两项证据都未直接检验这一合并假说。

### PhysioNet Challenge 2012数据集规模的口径不一致

- **一方**：一项研究称该数据集(其『数据集二』)含4,000条成人ICU患者多变量临床时间序列记录,覆盖入ICU前48小时的48个临床变量。 [@rahman2021Interpretable]
- **另一方**：另一项研究称该数据集总量为8,000份ICU记录、约48小时、33个变量,但因结局仅在Training Set A公开而只使用该子集(死亡阳性554)。 [@che2018Recurrent]
- *为何分歧*：两项研究对同一公开数据集的总记录数(4,000对8,000)与变量数(48对33)描述互相矛盾,提示直接转述『PhysioNet2012规模』时需先核对原始挑战赛文档,而非采信单篇论文的转述。

## 证据缺口

- 无证据直接给出HiRID或AmsterdamUMCdb的特征级/变量级缺失率分布——仅E11给出AmsterdamUMCdb部分lab变量的缺失率(且伴随非本页四库的OSUWMC),HiRID在本页证据中只有E51给出的ICU死亡率等结局指标,缺特征级缺失特性,需要专门报告这两个库缺失分布的原始文献。
- 无证据直接比较MIMIC-IV/eICU/AmsterdamUMCdb/HiRID四库两两之间『缺失条件依赖结构』(即某变量缺失与其他变量或结局之间的相关性模式)是否可跨中心迁移——这是读者论文的核心问题,当前最接近的是E30(BN原生缺失处理隐含MAR假设的批评)与E33(未采纳EM式因果图+缺失插补联合算法的批评),但两者均是对个别论文方法缺口的批评,不构成跨库结构可迁移性的直接实证。
- 无证据涉及MA-GCT(特征专属可学习掩码嵌入+Guide/Prior注意力约束)方法本身在任一ICU库上的评估结果,也无证据涉及任何可比的『可学习掩码嵌入』方法在多库间的对比实验——需要补充该类方法或其近亲方法的跨库实证数据。
- eICU『无结构信息』(E53)与另一研究中eICU纳入8个基线协变量(E14)并存,但两者是否指同一版本或子集的eICU数据尚不清楚,需要明确eICU不同版本/衍生数据集之间结构化程度差异的说明性证据。

## 本页证据（60 条 · 60 篇）

每条可回溯到札记原文；未被引用的证据标 ○。引用率高低不能单独当质量信号——判断这一页可不可信，看下面论断是否具体、分歧是否来自不同文献，比看这个比例可靠。

- ● **E1** `[@eid2026Hybrid]` 🟩方法论借鉴 · 方法与数据 · 科研札记_2026-01_全文精读.md:204
  <small>Hybrid phenotype-guided modeling across algorithm–feature regimes with application to ICU mortality prediction for Acinetobacter baumannii</small>
  > 使用三中心212例ICU患者的回顾性数据，采用完全案例分析直接剔除存在缺失的19例（8.2%），未进行缺失机制建模或填补。
- ● **E2** `[@wang2025Crisp]` 🟩方法论借鉴 · 方法与数据 · 科研札记_2025-04_全文精读.md:413
  <small>CRISP: A causal relationships-guided deep learning framework for advanced ICU mortality prediction</small>
  > ICU观测指标取入ICU后48小时内记录的最小值与最大值；缺失数据方面，时间序列采用末次观测值结转，其余缺失值用中位数填补；连续特征用min-max归一化，类别特征用label encoding。
- ● **E3** `[@bhandari2026Comparative]` 🟪可引用证据 · 方法与数据 · 科研札记_2026-07_手动精读.md:3409
  <small>A comparative evaluation of handling missing data points and modalities in electronic health records</small>
  > 研究基于MIMIC-III（超6万例ICU住院，缺失点实验取23,983例子集、42个数值特征）、MIMIC-IV（约43.1万次ICU就诊，用于模态缺失实验）和P12（11,988例ICU患者中取3,997例子集、36个特征）三个数据集设计两套实验。
- ● **E4** `[@minoccheri2025Supervised]` 🟪可引用证据 · 实验方法 · 科研札记_2025-06_全文精读.md:117
  <small>Supervised Coupled Matrix-Tensor Factorization (SCMTF) for Computational Phenotyping of Patient Reported Outcomes in Ulcerative Colitis</small>
  > 进入模型的全部数据约75.7%缺失，其中PRO子集约88.3%缺失；仅有64名患者缺失率低于30%。
- ● **E5** `[@huang2023Federated]` 🟩方法论借鉴 · 方法与数据 · 科研札记_2023-10_全文精读.md:310
  <small>Federated machine learning for predicting acute kidney injury in critically ill patients: a multicenter study in Taiwan</small>
  > 缺失数据采用均值插补（针对连续变量）或0/1编码（药物），并报告了缺失比例。
- ● **E6** `[@yang2021Multiseries]` 🟪可引用证据 · 结果与效应量 · 科研札记_2021-08_全文精读.md:314
  <small>Multi-series Time-aware Sequence Partitioning for Disease Progression Modeling</small>
  > 该EHR队列数据的整体缺失率约为80.37%。
- ● **E7** `[@vazquez2026Federated]` 🟪可引用证据 · 实验方法 · 科研札记_2026-08_手动精读.md:463
  <small>Federated Learning with Incomplete Data: When to Use Complete Cases and When to Weight</small>
  > 数据来自两个来源：UNC 主持的**23 家美英医学中心**回顾性多中心队列（2014–2020，IRB #23-2802）与 **5 家 JHU 医院**队列（2018–2025，IRB #00453058）；分析样本 **n=1,948**（UNC 1,494 + JHU 454），其中约 88%（n=1,710）数据完整；白蛋白缺失率 UNC 约 **14%**、JHU 约 **2%**（p.27）。
- ● **E8** `[@liang2025Causal]` 🟪可引用证据 · 实验方法 · 科研札记_2026-07_手动精读.md:522
  <small>Causal Representation Learning from Multimodal Clinical Records under Non-Random Modality Missingness</small>
  > 【队列与划分】eICU数据集中，纳入18–89岁、ICU住院时长在12小时至10天之间的成年患者，剔除缺失关键标识符或特征的入院记录，缺失率超过80%的变量被移除，其余缺失值以中位数或0填补(p.14)。
- ● **E9** `[@xie2025Methodological]` 🟪可引用证据 · 方法与数据 · 科研札记_2025-01_全文精读.md:827
  <small>Methodological development study: Dynamic mask attention graph neural network for mechanical ventilation in elderly intensive care unit patients.</small>
  > MIMIC-IV dataset: 18,835 samples, positive-negative ratio ≈ 0.721; eICU: 42,842 samples, ratio ≈ 0.947.
- ● **E10** `[@makarov2025Large]` 🟩方法论借鉴 · 方法与数据 · 科研札记_2026-07_手动精读.md:1506
  <small>Large language models forecast patient health trajectories enabling digital twins</small>
  > 三个数据集覆盖不同缺失/规模特征：NSCLC 来自 Flotron Health 真实世界 EHR（16,496 例，约 280 家肿瘤诊所、约 800 个站点，1991–2023，320 个变量、773,607 患者‑天，原始缺失率高达 94.4%），任务是预测确诊后最长 13 周的每周实验室指标；ICU 来自 MIMIC‑IV（35,131 例患者，1,686,288 个时间点，预处理后得到 300 个输入‑输出片段），任务是用首 24 小时数据预测未来 24 小时的血氧饱和度、呼吸频率和镁离子；阿尔茨海默病来自 ADNI（1,140 例），任务是用基线值预测未来 24 个月的 MMSE、CDR‑SB、ADAS11 三个认知量表。
- ● **E11** `[@yin2024Sepsislab]` 🟪可引用证据 · 结果与效应量 · 科研札记_2024-07_全文精读.md:328
  <small>SepsisLab: Early Sepsis Prediction with Uncertainty Quantification and Active Sensing</small>
  > Table 7报告了三个数据集中lab test变量的缺失率，例如Lactate在AmsterdamUMCdb/OSUWMC/MIMIC-III分别为88%/90%/89%，PT为78%/92%/80%，Urineoutput为23%/39%/33%，Creatinine为75%/85%/80%。
- ● **E12** `[@campagner2021External]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2021-10_全文精读.md:159
  <small>External validation of Machine Learning models for COVID-19 detection based on Complete Blood Count</small>
  > 训练集中CBC参数缺失率为3.63%(多数参数)、20.85%(白细胞分类计数相关参数)，而两个外部验证数据集本身样本几乎不缺失，唯独「Suspect」特征在外部验证集中缺失率高达52%(Desio)和53%(Bergamo)。
- ● **E13** `[@dayanFederatedLearningPredicting2021b]` 🟪可引用证据 · 结果与效应量 · 科研札记_2025-05_全文精读.md:769
  <small>Federated Learning for Predicting Major Postoperative Complications</small>
  > 缺失主程序代码导致 GNV 模型预测长时间 ICU 停留和长时间机械通气的假阳性/假阴性增多，缺失率在误分类案例中达 38%–53%，而 GNV 训练集缺失率仅 5%，JAX 为 36%。
- ● **E14** `[@cao2025Heterogeneityaware]` 🟩方法论借鉴 · 实验方法 · 科研札记_2025-10_全文精读.md:51
  <small>Heterogeneity-Aware Federated Causal Inference Leveraging Effect-Measure Transportability</small>
  > 真实数据:eICU Collaborative Research Database(eICU-CRD),多中心ICU数据库,涵盖美国2014-2015年超20万例ICU入院记录;处理变量为是否使用升压药(vasopressor),结局为院内死亡;纳入p=8个基线协变量(年龄、入院体重、体温、血糖、BUN、肌酐、白细胞计数、血小板)。
- ● **E15** `[@daalen2024Bayesian]` 🟩方法论借鉴 · 方法与数据 · 科研札记_2024-11_全文精读.md:972
  <small>A Bayesian Network Approach to Lung Cancer Screening: Assessing the Impact of Data Quantity, Quality, and the Combination of Data from Danish Electronic Health Records.</small>
  > 根据数据可得性将人群分为四个子集（A:仅共病, B:共病+实验室, C:加吸烟, D:加症状），并组合成不同缺失率的数据集：ABCD缺失39%，BCD缺失21%，CD缺失16%，D仅2%缺失。
- ● **E16** `[@lim2025Multicenter]` 🟪可引用证据 · 结果与效应量 · 科研札记_2026-07_手动精读.md:7268
  <small>Multicenter validation of a machine learning model to predict intensive care unit readmission within 48 hours after discharge</small>
  > 早期ICU再入院的缺失敏感性分析显示：随机缺失率升至90%时，AUROC降幅仍控制在约5%以内；模型对高缺失率变量(如化验结果)相对鲁棒，对低缺失率变量(如生命体征)反而更敏感。
- ○ **E17** `[@seo2021Data]` 🟩方法论借鉴 · 实验方法 · 科研札记_2021-05_全文精读.md:199
  <small>Study On ECG Data Dependency For Atrial Fibrillation Detection Based On Residual Networks</small>
  > MITDB：47名受试者的48条半小时双通道ECG记录，其中23条来自约60%住院/40%门诊的混合人群，另25条特意收录较少见但具临床意义的心律失常；采样率360Hz，11比特分辨率，10mV量程；数据同样采自波士顿贝斯以色列医院。
- ● **E18** `[@poette2026Benchmarking]` 🟪可引用证据 · 实验方法 · 科研札记_2026-07_手动精读.md:5600
  <small>Benchmarking imputation strategies for missing time-series data in critical care using real-world-inspired scenarios</small>
  > 【队列与划分】数据集中总体缺失比例为3.8%,各变量缺失率分别为呼吸频率3.5%、心率2.7%、SpO2 4.1%、平均血压5.0%,该缺失模式分析是后续设计三种掩码情景的依据(p.4)。
- ● **E19** `[@elsharief2025Medmod]` 🟪可引用证据 · 方法与数据 · 科研札记_2026-08_手动精读.md:1193
  <small>MedMod: Multimodal Benchmark for Medical Prediction Tasks with Electronic Health Records and Chest X-Ray Scans</small>
  > 院内死亡率：21,652 条（CXR 摄于 ICU 前 48 小时内）→ 20,131 条（AP）→ 10,637 条（去重、最近一张）→ 划分 4,485 / 488 / 1,242。注意三者合计仅 6,215，比上一级的 10,637 少了 4,422 条（41.6%），论文对这一级流失没有任何说明，Table 2 也照抄 4485/488/1242 而不解释缺口。
- ● **E20** `[@kansal2025Mcmed]` 🟪可引用证据 · 关键结论 · 科研札记_2025-07_全文精读.md:249
  <small>MC-MED, multimodal clinical monitoring in the emergency department</small>
  > 连续监测数据因患者活动、检查等经常中断，形成了典型的临床缺失模式，可用于研究真实世界数据缺失机制。
- ● **E21** `[@mohapatra2024Differentially]` 🟪可引用证据 · 研究问题 · 科研札记_2023-10_全文精读.md:202
  <small>Differentially Private Data Generation with Missing Data</small>
  > 一项针对9个公开医疗机器学习数据集的调查显示，缺失值比例范围为0.2%至78.6%[36]，作者以此说明缺失数据在真实数据集中普遍存在。
- ● **E22** `[@rahman2021Interpretable]` 🟩方法论借鉴 · 实验方法 · 科研札记_2021-09_全文精读.md:74
  <small>Interpretable Additive Recurrent Neural Networks For Multivariate Clinical Time Series</small>
  > 数据集二：PhysioNet Challenge 2012训练集A，含4000条成人ICU患者多变量临床时间序列记录，覆盖入ICU前48小时的48个临床变量，用于院内死亡率（in-hospital mortality）预测。
- ● **E23** `[@dam2025Readmission]` 🟪可引用证据 · 图表与补充材料要点 · 科研札记_2026-07_手动精读.md:7154
  <small>ICU readmission and mortality risk prediction: Generalizability of a multi-hospital model</small>
  > Table 1报告全体44,837例患者的基线特征：年龄分布、性别（女33.71%/男65.65%）、BMI（缺失率高达41.20%，18,464/44,837）、ICU专科分布（心脏外科占比41.85%）与结局发生率（7天内再入院或死亡2967例，6.62%）。
- ● **E24** `[@dong2026Objective]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2026-08-03_全文精读.md:193
  <small>Objective tongue phenotyping identifies phenotypic heterogeneity in diabetic kidney disease: a dual-center clustering analysis</small>
  > 排除缺失率超过30%的指标（如CRP、ESR、HCY、UACR、PA、CysC）可能降低了检测两组在炎症、营养和肾损伤状态上差异的统计功效。
- ● **E25** `[@xiang2026Learning]` 🟪可引用证据 · 方法与数据 · 科研札记_2026-07_手动精读.md:4126
  <small>Learning Representations from Incomplete EHR Data with Dual-Masked Autoencoding</small>
  > 数据集一：MIMIC-IV ICU，选取最常采样的 50 个实验室值、5 个生命体征、氧饱和度与 5 种血管加压药，共 61 个特征（附录B.1明确写“61 features”，详见 Table 9），最终 92,938 次住院、412,365 患者天（正文核对无误）。
- ○ **E26** `[@hamar2021Covid19]` 🟪可引用证据 · 关键结论 · 科研札记_2024-05_全文精读.md:262
  <small>COVID-19 mortality prediction in Hungarian ICU settings implementing random forest algorithm</small>
  > 随机森林模型预测ICU死亡的准确率为81.42%（95% CI 73.01-88.11%），AUC达91.6%，最重要的预测变量为P/F比值、淋巴细胞计数和胸部CTSS。
- ● **E27** `[@zeng2023Neural]` 🟪可引用证据 · 研究问题与核心主张 · 科研札记_2026-07-17_手动精读.md:798
  <small>Neural networks based on attention architecture are robust to data missingness for early predicting hospital mortality in  intensive care unit patients</small>
  > 任务为用ICU入住前24h数据预测院内死亡：训练集MIMIC-IV(n=65,623，50,354名患者，死亡率10.1%，6,659死)，外部测试集eICU-CRD(n=150,753，126,804名患者，死亡率8.5%，12,878死)。
- ● **E28** `[@wan2025Exploring]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2025-09_全文精读.md:82
  <small>Exploring trajectories of acute kidney injury in the intensive care unit: a population-based cohort study</small>
  > 由于事件数有限（ICU期间和住院期间死亡分别为47例和72例），无法进行广泛的混杂因素筛选。
- ● **E29** `[@tranchellini2026Evaluating]` 🟩方法论借鉴 · 实验方法 · 科研札记_2026-07-27_手动精读.md:1855
  <small>Evaluating deep learning sepsis prediction models in ICUs under distribution shift: a multi-centre retrospective cohort study</small>
  > 【特征】缺失值处理：先为每个动态特征标记二元缺失指示列，再用同一患者ICU住院期间最近一次可得测量值向前填补；若无先前测量值，则用仅基于训练集计算的该特征均值填补，以避免数据泄漏(p.10)。
- ● **E30** `[@agard2025Improving]` 🟦可反驳观点 · 可质疑点 · 科研札记_2025-09_全文精读.md:691
  <small>Improving Sepsis Prediction in the ICU with Explainable Artificial Intelligence: The Promise of Bayesian Networks</small>
  > 声称 BN「原生处理缺失数据」隐含 MAR 假设，但在现实 ICU 数据中缺失常为 MNAR，此时常规边缘化可能产生偏倚，文中未给出相关性能评估。
- ● **E31** `[@im2025Labtopb]` 🟪可引用证据 · 实验方法 · 科研札记_2026-08_手动精读.md:9585
  <small>LabTOP: A Unified Model for Lab Test Outcome Prediction on Electronic Health Records</small>
  > 【队列与划分】纳入标准为ICU住院时长至少6小时的记录，并按患者ID随机划分为训练、验证、测试集，比例为8:1:1，确保同一患者的记录不会跨集出现以防止数据泄漏(p.6)(p.15)。
- ● **E32** `[@park2026Missingness]` 🟪可引用证据 · 实验方法 · 科研札记_2026-07_手动精读.md:8590
  <small>Missingness as Signal: Channel-Independent Spectrogram Learning for Clinical Time Series Prediction</small>
  > 【队列与划分】数据集共包含 6,215 例 ICU 住院记录，其中训练集 4,485 例、验证集 488 例、测试集 1,242 例，阳性（院内死亡）比例约为 14%–15% (p.4)。
- ● **E33** `[@xu2024Neural]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2026-07-27_手动精读.md:2123
  <small>Neural Granger Causal Discovery for Derangements in ICU-Acquired Acute Kidney Injury Patients</small>
  > **未讨论缺失值处理**。ICU 时序天然不规则，正文只提到「同一小时内多次测量取平均」，对「某小时完全无测量」如何处理只字未提。而相关工作里作者自己引用了「EM 式联合因果图学习与不规则时序缺失数据插补相结合」的算法（参考文献 15），却没有采纳。
- ● **E34** `[@chen2024Congenital]` 🟪可引用证据 · 方法与数据 · 科研札记_2026-06_手动精读.md:207
  <small>Congenital heart disease detection by pediatric electrocardiogram based deep learning integrated with human concepts</small>
  > 排除规则（Fig. 4）直接涉及缺失机制：Center-A/B/C 分别有 87,322、18,331、9,982 例因「诊断标签缺失」被剔除，另有 47,854、10,332、4,632 例因 ECG 信号损坏（20% 以上幅值超 5 mV 或 20% 采样点为 0）被剔除，还有 110,202、21,095、11,361 例因「已接受干预后采集」被剔除，以及 18,198、2,890、4,765 例因同一个体波形已被纳入而剔除。
- ● **E35** `[@thoral2021Sharing]` 🟪可引用证据 · 图表与补充材料要点 · 科研札记_2026-07-27_手动精读.md:373
  <small>Sharing ICU Patient Data Responsibly Under the Society of Critical Care Medicine/European Society of Intensive Care Medicine Joint Data Science Collaboration: The Amsterdam University Medical Centers Database (AmsterdamUMCdb) Example*</small>
  > **Fig 2 实际上是一张已发表的、可引用的「缺失地图」**，按四类分别给出各变量在全部入院中的可得比例：**A 设备自动记录**（心率、SpO₂、呼吸频率、动脉血压近 100% → ST 段分析、FiO₂ 约 60–70% → 中心静脉压、肺动脉压、心输出量 20–30% → 主动脉内球囊反搏模式 <5%）；**B 人工录入的观察与评分**（心律、尿量近 100% → 氧输送装置、氧流量约 80–90% → GCS、Ramsay 镇静评分约 50–60% → 护理活动评分约 20%）；**C 化验**（血红蛋白、钠、钾、葡萄糖、血小板、肌酐近 100% → 白蛋白、ALAT、ASAT、乳酸 60–75% → 胆红素、TSH 约 40%）；**D 用药**（丙泊酚约 65% → 吗啡、去甲肾上腺素 40–50% → 头孢噻肟、依诺昔酮 10–15%）。
- ● **E36** `[@liao2025Learnable]` 🟪可引用证据 · 图表与补充材料要点 · 科研札记_2026-07_手动精读.md:6119
  <small>Learnable Prompt as Pseudo-Imputation: Rethinking the Necessity of Traditional EHR Data Imputation in Downstream Clinical Prediction</small>
  > Fig 5/附录B.3(高缺失率):以MIMIC-IV上RNN为例,缺失率为原始70%时PAI相对填补协议AUROC提升0.07%、AUPRC提升1.06%;缺失率升到90%时,PAI相对提升AUROC 11.03%、AUPRC 2.27%。【补充】原文括号内还各附了一组数字(10.96%、1.21%),Opus稿未纳入,该组数字在正文中未充分展开说明其确切含义(可能是另一子设置或重复实验的对照值),读者引用时应留意主数字之外还有这组未展开的附注。
- ● **E37** `[@ji2025Exosito]` 🟪可引用证据 · 实验方法 · 科研札记_2026-08_手动精读.md:6385
  <small>ExOSITO: Explainable Off-Policy Learning with Side Information for Intensive Care Unit Blood Test Orders</small>
  > 【队列与划分】纳入标准要求ICU住院时长不少于12小时且住院期间至少下达5次化验医嘱，据此从MIMIC-IV原始数据中最终筛选出57,212次ICU住院，总死亡率约为12%。(p.17)
- ● **E38** `[@che2018Recurrent]` 🟩方法论借鉴 · 实验方法 · 科研札记_2026-08_手动精读.md:7899
  <small>Recurrent Neural Networks for Multivariate Time Series with Missing Values</small>
  > 数据集:Gesture(UCI,378 条规则采样无缺失时间序列,5 类手势;人工构造 4 个合成设定,缺失率相近、缺失率-标签相关不同)[p.6-7];PhysioNet Challenge 2012(8,000 份 ICU 记录、约 48h、33 变量;只用结局公开的 Training Set A,死亡阳性 554;任务=死亡率二分类 + 4 任务多任务)[p.6];MIMIC-III(Metavision 2008-2012,19,714 次入院、99 个时间序列特征,仅入院后存活满 48h 的患者、只用前 48h;死亡阳性 1,716;input/output/lab/prescription 四类事件)[p.6-7]。
- ● **E39** `[@周立基于深度学习的不完整时序数据补全方法综述]` 🟪可引用证据 · 实验方法 · 科研札记_2026-07_手动精读.md:7692
  <small>基于深度学习的不完整时序数据补全方法综述</small>
  > 【训练与评估协议】缺失率实验在9个数据集的观测值中置入缺失率为10%~90%（其中PhysioNet2019数据集的缺失率控制在81%~89%）、缺失场景为MCAR-point的缺失值(p.16)。
- ● **E40** `[@zhang2026Machine]` 🟪可引用证据 · 方法与数据 · 科研札记_2026-07_手动精读.md:7472
  <small>Machine learning predicts sepsis deterioration trajectories</small>
  > 多中心回顾性队列:纳入符合Sepsis-3标准的ICU患者,初始多中心数据库54,758例,排除6822例(缺失数据>20% n=2541、ICU<24h n=1873、外院转入n=1248、既往治疗限制n=1160),最终分析队列47,936例。
- ● **E41** `[@gardner2023Benchmarking]` 🟦可反驳观点 · 实验方法 · 科研札记_2026-07-17_手动精读.md:575
  <small>Benchmarking Distribution Shift in Tabular Data with TableShift</small>
  > 【特征与预处理】ICU Length of Stay与ICU Hospital Mortality任务使用MIMIC-extract预处理流程产出的数据；由于预处理后数据仍含缺失值，作者采用MIMIC-extract原作者提供的“默认方法”处理缺失数据，但未在正文中具体说明该方法的细节 (p.23)。
- ● **E42** `[@lee2021Multiview]` 🟪可引用证据 · 实验方法 · 科研札记_2021-01_全文精读.md:35
  <small>Multi-view Integration Learning for Irregularly-sampled Clinical Time Series</small>
  > MIMIC-III数据集包含13,998名患者、99种time series measurements,missing rate约90%,院内死亡阳性1,181例/阴性12,817例(约1:10.8),不规则观测时间点数量范围1–247(均值49.29±35.90)。
- ● **E43** `[@awounvo2025Combining]` 🟪可引用证据 · 结果与效应量 · 科研札记_2026-06_手动精读.md:1267
  <small>Combining multiple imputation with internal model validation in clinical prediction modeling: a systematic methodological review</small>
  > 缺失率上 MI-during-IMV 也更高：全变量最大缺失率中位数 30.0%（IQR 21.4–52.0）对 28.5%（IQR 12.1–45.0），总体中位 29.0%（IQR 13.3–45.0，范围 1.4–98.4%）。
- ● **E44** `[@gao2026Comparing]` 🟪可引用证据 · 图表与描述统计要点 · 科研札记_2026-07_手动精读.md:3501
  <small>Comparing methods for handling missing data in electronic health records for dynamic risk prediction of central-line associated bloodstream infection</small>
  > Table 1 显示极端缺失：pH 缺失 96.81%、胆红素 92.82%、尿素 86.63%、肌酐 86.54%、环孢素(ciclosporin) 99.92%、GCS 昏迷评分 97.16%、中性粒细胞减少 85.20%、最高体温 30.24%、导管总管腔数 40.51%，而年龄、CICC等静态/结构性变量缺失为0%（以上9个百分比均逐一核对Table 1原文数字，全部准确）。需注意尿素/肌酐/胆红素/环孢素/pH的Median(IQR)数值系对数变换后的值（原文脚注c），并非原始量纲。
- ● **E45** `[@pereira2024Imputation]` 🟪可引用证据 · 实验方法 · 科研札记_2026-07_手动精读.md:9111
  <small>Imputation of data Missing Not at Random: Artificial generation and benchmark analysis</small>
  > 【评估协议】基准实验覆盖5个缺失率水平：10%、20%、40%、60%、80% (p.5)。
- ● **E46** `[@sathe2021Identification]` 🟪可引用证据 · 方法与数据 · 科研札记_2021-09_全文精读.md:244
  <small>Identification of persistent and resolving subphenotypes of acute hypoxemic respiratory failure in two independent cohorts</small>
  > 本研究是对两个独立前瞻性ICU队列的二次分析：发现队列来自华盛顿大学Harborview医学中心（2006–2010年入组，n=768），验证队列来自范德堡大学VALID研究（2006–2020年入组，n=1715）。
- ○ **E47** `[@essay2022Validation]` 🟪可引用证据 · 结果与效应量 · 科研札记_2022-03_全文精读.md:347
  <small>Validation of an Electronic Phenotyping Algorithm for Patients With Acute Respiratory Failure</small>
  > NIPPV利用率为38%，与既往报告约40%的ICU患者利用率相近；HFNO总体ICU死亡率最高，为28.1%；HFNO失败与ICU死亡率相对增加47%相关；NIPPV失败与ICU死亡率相对增加72%相关；ICU死亡率与住院死亡率分别为60.4%和60.7%，两者相近。
- ○ **E48** `[@hu2014Dynamic]` 🟦可反驳观点 · 结果与效应量 · 科研札记_2022-10_全文精读.md:274
  <small>Dynamic prediction of life-threatening events for patients in intensive care unit</small>
  > 文中主张：本研究证明机器学习方法与ICU大规模高质量临床数据相结合，可以准确预测ICU患者的生命威胁事件以支持早期干预。
- ● **E49** `[@yu2026Integrating]` 🟪可引用证据 · 方法与数据 · 科研札记_2026-07_手动精读.md:7797
  <small>Integrating Ultrasound-CT-MR for Preoperative Multi-Task Prediction in Ovarian Cancer: Achieving Diagnostic Parity with Multidisciplinary Team Consensus</small>
  > 对缺失数据的处理是保守统计式：血清肿瘤标志物若缺失超过20%则整列指标剔除，缺失低于20%则用该指标中位数插补；核对原文发现这两条规则分别对应两篇不同引文——剔除规则引的是Schulz & Grimes(关于随机对照试验样本量流失/排除)，插补规则才引Little & Rubin，Opus稿笼统归为"引Little & Rubin"略不精确，已澄清。
- ● **E50** `[@wani2026Comprehensive]` 🟪可引用证据 · 研究问题 · 科研札记_2026-06_手动精读.md:33
  <small>Comprehensive analysis of missing data imputation in clinical time-series: challenges, risks, and practical solutions</small>
  > 论文开门见山给出问题规模：不完整的生理监测数据影响 ICU 生命体征、实验室值与连续传感器流中 15–40% 的观测（p.1 引言首句，核对无误）。
- ● **E51** `[@li2023Generating]` 🟪可引用证据 · 方法与数据 · 科研札记_2026-07-27_手动精读.md:2329
  <small>Generating synthetic mixed-type longitudinal electronic health records for artificial intelligent applications</small>
  > **三个 ICU 库、合计 141,488 名患者**（Table 7）：MIMIC-III **28,344** 人 / 78 个连续变量 / 20 个离散变量（ICU 死亡 1,870 [6.59%]、院内死亡 911 [3.21%]、30 天再入院 1,122 [3.95%]）；eICU **99,015** 人 / 55 / 19（ICU 死亡 4,500 [4.54%]）；HiRID **14,129** 人 / 50 / 39（ICU 死亡 1,266 [8.96%]）。
- ● **E52** `[@water2023Another]` 🟪可引用证据 · 实验方法 · 科研札记_2026-07_手动精读.md:411
  <small>Yet Another ICU Benchmark: A Flexible Multi-Center Framework for Clinical ML</small>
  > 【队列与划分】本研究使用四个开放获取ICU数据集——MIMIC-III/IV、eICU、HiRID、AUMCdb，合计�covers 334,812例ICU住院（p.2），Table 13进一步给出各数据集的具体版本与住院人次（MIMIC-III/IV约40k/73k，eICU约201k，HiRID约34k，AUMCdb约23k）（p.24）。
- ● **E53** `[@choi2020Learning]` 🟪可引用证据 · 方法与数据 · 科研札记_2026-07-27_手动精读.md:145
  <small>Learning the Graphical Structure of Electronic Health Records with Graph Convolutional Transformer</small>
  > 真实数据：eICU（Pollard 2018），美国多中心 ICU，2014–2015。**41,026 次就诊、3,093 诊断码、2,132 治疗码**，平均 7.70 诊断/5.03 治疗。eICU 无结构信息。
- ● **E54** `[@sisk2023Imputation]` 🟪可引用证据 · 实验方法 · 科研札记_2026-06_手动精读.md:1815
  <small>Imputation and missing indicators for handling missing data in the development and deployment of clinical prediction models: A simulation study</small>
  > 【特征与预处理】在开发集中有 14,474 例患者各预测变量完整；18,713 例缺失胆红素，424 例缺失 GCS，447 例缺失呼吸频率，439 例缺失收缩压，共观测到 10 种缺失模式（含完整病例），据此推导出 17 个（回归）填补模型 (p.12)。
- ● **E55** `[@mathur2026Resurrecting]` 🟪可引用证据 · 实验方法 · 科研札记_2026-08_手动精读.md:5216
  <small>Resurrecting complete-case analysis: a defense</small>
  > 缺失机制：每个不完整变量各自 50% 缺失；跨所有场景「完整病例」比例均值为 33%（范围 10%–58%），具体每场景平均完整病例数见补充材料 Table S1（原文引用范围 21–5769，该表本身不在本 PDF 正文内，原文未附）（p.1772）。
- ● **E56** `[@meng2021Mimicif]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2021-02_全文精读.md:203
  <small>MIMIC-IF: Interpretability and Fairness Evaluation of Deep Learning Models on MIMIC-IV Dataset</small>
  > MIMIC-IV公开版本仅覆盖全部383,220名住院/急诊患者中的50,048名ICU患者数据，且数据源自单一医疗系统（BIDMC的MetaVision），样本代表性和结论的外部有效性有限。
- ● **E57** `[@you2020Handling]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2026-07-27_手动精读.md:40
  <small>Handling Missing Data with Graph Representation Learning</small>
  > 规模与领域都窄：正文九个数据集特征数在 6–9 之间，全是工程/物理/生物的小型回归数据，**没有 EHR、没有临床数据、没有时间维度**。把结论外推到高维稀疏纵向的 ICU 数据需要额外论证。
- ● **E58** `[@christodoulou2019Systematic]` 🟦可反驳观点 · 实验方法 · 科研札记_2026-06_手动精读.md:943
  <small>A systematic review shows no performance benefit of machine learning over logistic regression for clinical prediction models</small>
  > 【特征与预处理】45%的研究（32篇）对缺失数据的处理信息缺失或表述不清；23%（16篇）采用完整病例分析（complete case analysis），20%（14篇）采用临时方法（均值填补、缺失指示变量法、变量删除），11%（9篇）使用单次或多重随机填补但记录较差 (p.4)
- ● **E59** `[@wu2026Artificial]` 🟦可反驳观点 · 局限与可质疑点 · 科研札记_2026-07-27_手动精读.md:839
  <small>Artificial Intelligence-powered tiered early warning framework addressing high false alarm rates for in-hospital mortality prediction</small>
  > **排除标准本身建立在「缺失即剔除」之上**：缺少转入住院日期、缺少分诊时间戳的记录在分析前被剔除。从 1,388,586 次急诊筛到 174,292 次（**保留率仅 12.6%**），而被剔除者与保留者在缺失模式上的差异未被报告——这本身可能引入选择偏倚。
- ● **E60** `[@liu2023Contrastive]` 🟩方法论借鉴 · 实验方法 · 科研札记_2023-08_全文精读.md:306
  <small>Contrastive Learning-based Imputation-Prediction Networks for In-hospital Mortality Risk Modeling using EHRs</small>
  > 实验数据集为 MIMIC-III 和 eICU 两个真实世界 EHR 数据集，评估任务包括 ICU/eICU 入院后 24 小时和 48 小时两个时间点的 clinical time series imputation 与 in-hospital mortality prediction。
<!-- END GENERATED · 以下内容属于你，生成器永不触碰 -->

## 我的批注
