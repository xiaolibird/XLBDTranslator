---
name: paper-audit
description: 对一篇论文做法证式诚信审查(独立于常规精读)——数字自洽、图文互证、引用回对、公式量纲、版本谱系与撤稿状态。当用户说"审查这篇/这篇有没有问题/法证精读/audit/查查这篇可不可信/撤稿风险"时使用。区别于 read-paper:那是为取证入库,这是为找矛盾定级。
---

> 真相源：本文件在仓库 `docs/skills/paper-audit/SKILL.md`；改完须跑
> `bash scripts/install_skills.sh` 同步到 `~/.claude/skills/`。

# 论文法证审查（paper-audit）

方法论出处：DynaGraph 撤稿案（npj Digit. Med. 2026;9:216）的独立复核——我们在不知道
撤稿声明细节的情况下核出了与官方五类问题一一对应的实锤，外加两处官方没点名的引用错配。
本 skill 把那套流程固化。完整案例档案：
`output/scholar_notes/撤稿案例_DynaGraph_npjDigitalMedicine.md`。

## 判定纪律（先读这个，违者整份审查作废）

1. **没读到最后一页不许断言编造**。曾只读到 31 页 PDF 的第 12 页就断言草稿引用的
   Table 15/21/24 是编的——那些表在附录里，每个数都对。附录恰是最容易被误判的地方。
2. **文本检索不命中 ≠ 论文里没有**。图内数字常是位图，`grep` 不到要渲染页面亲眼看
   （Zink 案例：52,059 等关键数字全在栅格图里，pdftotext 零命中）。
3. **分清页码体系**。印刷页码可与 PDF 页号不同（Annual Review 的 p.416 = PDF 第 24 页；
   JCST 印刷页码到 4 位数）。断言"引用了不存在的页码"前先确认作者用的哪套。
4. **二手元数据必须回源直验**。检索摘要模型会转述失真——实测编造过"LNCS 14836
   pp.353-367"这种卷号页码级的假出处，Crossref 直查即证伪。凡卷期页码/DOI/会议名，
   一律 Crossref/出版方一手验证后才可写进报告。
5. **只报事实与页码锚，不推断动机**。"Fig 4 四面板数值逐位相同且与正文冲突"是审查结论；
   "作者伪造了数据"不是——那是编辑部和机构的职权。
6. **必须写「公允节」**：哪些数字立得住要同样明说（DynaGraph 案例里 AUPRC +6–8% 四个
   数据集验算全部成立——不写这条，报告就成了检察官文书而非审查）。

## 执行路径（按序跑，每步有命令）

### 第 0 步：带页码锚抽全文

```bash
python3 - <<'PY'
import fitz, sys
doc = fitz.open("PAPER.pdf")
for i, page in enumerate(doc, 1):
    print(f"[p.{i}]"); print(page.get_text())
PY
```
（仓库内可直接用 `scripts/backfill_methods.py` 的 `pdf_text_with_pages`。）
同时记录总页数——第 2 步要用 Read 工具**逐页读完整本**，含附录。

### 第 1 步：版本谱系与撤稿状态（联网，5 分钟）

```bash
# Crossref：标题是否带 RETRACTED 前缀、是否有 update-to 关系
curl -s "https://api.crossref.org/works/<DOI>" | jq '.message | {title, "update-to", relation}'
# Crossref 按标题反查全部同名版本（**证伪二手检索给的假出处专用**）
curl -s "https://api.crossref.org/works?query.title=<Title>&rows=8" | jq -r '.message.items[] | "\(.DOI) | \(.title[0])"'
# 代码仓库是否还活着（404 = 已删，本身就是信号）
curl -s -o /dev/null -w "%{http_code}\n" https://api.github.com/repos/<owner>/<repo>
# ★ 匿名评审镜像：作者删得掉 GitHub，常忘了删这个（实测 DynaGraph 靠它拿到全部超参与代码）
curl -s "https://anonymous.4open.science/api/repo/<slug>/files/"
# arXiv 分身：版本历史、有无撤稿标注（撤稿**不跨版本传播**）
# WebFetch https://arxiv.org/abs/<id>
# PubPeer 对爬虫 403——给用户链接让其浏览器查，不要替它下结论
```

⚠️ **撤稿不跨版本传播**：npj 版被撤，arXiv/会议版照样"干净"。审查对象有分身时逐个查，
并**逐格对照两版的结果表**——指标列被整列替换而代码无对应实现，是改稿造数的直接证据。

**拿到代码后的三条便宜检测**（实测全部命中过）：
1. `grep -io "auroc\|auprc\|roc_auc\|average_precision" *.ipynb` —— **论文主指标在代码里有实现吗**？
   没有 = 该指标的数字没有代码来源（DynaGraph npj 版的 AUROC/AUPRC 即如此）；
2. README 引用的脚本/目录是否真的存在于仓库（Evaluate.ipynb、models/ 缺失）；
3. `requirements.txt` 里有没有 Python 标准库（`re==2.2.1`、`csv==1.0`、`json==2.0.9`）——
   有 = `pip install -r` 第一步就失败 = 该文件从未被第三方视角执行过。

### 第 2 步：数字自洽（离线，全文亲读时同步做）

对每个总量宣称做加法；对每个"提升 X%"做**双口径复算**：

```python
pp  = a - b            # 百分点
rel = (a - b) / b      # 相对百分比——正文写的到底是哪个？两个都算，对号入座
```

- 子样本加总 == 总数？
- "至少发生其一"的比例 ≥ 任一单项比例？
- 同一参数全篇一致？（把该参数的所有提法 grep 出来对照：`grep -n "hour\|window\|interval"`）
- 摘要数字 == 正文数字 == 表格数字？（三方对账，任何一角脱钩都记录）

### 第 3 步：图文互证（必须渲染页面看图，不能只靠文本层）

- 正文点名的每个**变量**，图/表里真的存在吗？（逐图核对节点/坐标轴/图例清单）
- 正文引述的每个**图内数值**，与图上标注一致吗？
- 多面板图的数值**是否雷同得不自然**？（不同数据集逐位相同 = 强信号）
- 小节标题、该节首句、该节结论三者同向吗？
- 表格**加粗审计**：每列自己求 argmax/argmin，与加粗位逐一比。

### 第 4 步：引用回对（抽查 5–10 条支撑关键论断的引用）

1. 文中说"[n] 做了 X"→ 翻到参考文献表看 [n] 的真实标题；
2. 标题与 X 相符吗？不符 = 错配；
3. 对可疑条目再向外验证一步（Crossref 按标题查真实主题）；
4. 特别盯：同一编号在两处用途不同、被引论文标题自带的方法词与文中转述矛盾
   （"标题写 transformer 却被说成 neural ODE"级别的矛盾在 PDF 内部即可证伪）。

### 第 5 步：公式与符号

- 每个公式做量纲/维度走查（矩阵乘法两侧形状写出来）；
- 符号表：每个符号只有一处定义？跨式撞名？总损失里的系数与分项损失里的系数重复使用？
- 同一更新式是否重复出现（冗余本身无害，但常伴随拼装痕迹）；
- **对第三方方法的机理转述，逐句对照其原论文的自我定位**——实测把 gcForest（原文卖点就是
  non-NN、无梯度、非可微）描述成「用反向传播优化卷积滤波器与森林参数」，一眼即穿；
- 「收敛性证明」这类附录要真读：把收敛定义成 `lim‖W_t−W_{t−1}‖=0` 而假设里已有 `η_t→0`，
  对任何更新序列都平凡成立，等于没证。

### 第 6 步：效应量与统计

- 核心模块的消融增益 vs 该指标的折间标准差——增益落在一个标准差之内且无检验 = 记为存疑；
- 显著性检验的单位（种子方差 ≠ 患者抽样不确定性）；
- 有没有只给单点值无方差的关键对比；
- 每个性能宣称（如推理时延）有没有对应实验支撑。

### 第 7 步：代码与数据可得性

**专门去参考文献前后找 Code/Data availability 声明**——它排版上离方法节最远，实测
连续三篇的脚本草稿全漏了它（其中两篇明明有公开仓库）。有链接就 `git ls-remote` 验活性。

## 输出格式

按三级定级，每条带页码锚 + 复算过程（算式写出来，让读者可独立重跑）：

- **实锤**：PDF 内部即可逐位复核的矛盾（数字对不上、图文冲突、引用错配）；
- **存疑**：需要补充材料/作者回应才能定的（超参缺失、单点值无方差）；
- **结构性**：即便无错也成立的方法学质疑（评估协议的系统性乐观、动机与基准错配）。

末尾两节必写：**公允节**（哪些立得住）+ **引用卫生建议**（哪些数字在什么限定下可用）。

## 之后（回写与归档——审查不落库等于没审）

审查报告写完不是终点；不做下面三件事，下次取证时库还是旧判断（实测缺口：本 skill 曾是
全链唯一没有下游的死胡同）。

1. **回写札记库**（仅当审查对象已入库——先 `jq` 按 DOI/citekey 查 `literature_index.json`）：
   - 证实**已撤稿**：在该条札记的「裁决」行加 `⚑ RETRACTED`（`notes_index` 的 flags 以
     札记 md 为真相源，月度 lint 也认这个标记；向量库随之剔除）；
   - 实锤级但未撤稿：在「裁决」行追加一句审查结论（含页码锚），情节重的打 `THREAT`；
   - 「引用卫生建议」里带限定的数字：把限定写进该条的精读节/highlights——
     `scholar-write` 取证时只看得见札记里的话，写在审查报告里它看不见。
   - 回写后重建索引**并同步向量库**（在 XLBDTranslator-dev 仓库根；只跑 notes_index
     的话撤稿文献在下一次 embed 同步前仍能被检索召回）：
     `PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/notes_index.py && PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/notes_embed.py`。
2. **报告归档**：落 `output/scholar_notes/`——撤稿定档用 `撤稿案例_<论文>_<venue>.md`
   （沿 DynaGraph 案既有先例），一般审查用 `审查报告_<论文>_<venue>.md`（本条新立的约定，
   此前无先例）。两种命名都不匹配札记索引的文件名正则，不会被误收进索引。不要只留在对话里。
3. **审的是自己要引的文献时**，提醒用户：该文献所在稿件若已引用它，走 `manuscript-selfcheck`
   的引用卫生轮复核相关句。

## Few-shot：真实审查案例（全部出自本库实录）

| 检查项 | 案例（实锤） |
|---|---|
| 样本量加总 | DynaGraph：摘要称四数据集共 40,856 人，实际 17,279+1,433+33,000+2,378=54,090，两种口径都还原不出 |
| 比例逻辑 | DynaGraph：「至少发生一种并发症 16.00%」，而单是房颤就 28.49% |
| 参数全篇一致 | DynaGraph：「3 小时间隔」两见 vs「4 小时窗×6」四见以上，互不相容 |
| 多面板雷同 | DynaGraph：Fig 4 消融掉幅在三个数据集面板**逐位相同**（AUG −0.048/FOC −0.035/…），而正文明写四数据集各不相同（0.048/0.046/0.042/0.044） |
| 图文变量互证 | DynaGraph：正文三处大谈 creatinine（含「creatinine-haemoglobin 耦合领先 AKI 8–10 小时」），但 Fig 2/3 的节点清单里**没有 creatinine**，且十个结局里**没有 AKI** |
| 引用错配 | DynaGraph：CLOCS[32] 实际指向一篇胸片放射组学论文；STraTS[33] 被说成 neural ODE 而其标题白纸黑字写 transformer——后者无需外部资料即可证伪 |
| 公式量纲 | DynaGraph：Eq 1 的 Θᵀ·Ψ 得不到 d×d；Eq 20 漏列 α 且 λ/μ 与分项损失撞名 |
| 无支撑宣称 | DynaGraph：13ms 推理时延出现两次，全文无任何时延实验 |
| 加粗审计 | DySurv (JAMIA)：eICU 表把 DySurv 的 IBLL 0.319 加粗为最优，而 CoxTime 是 0.310；另一表整行加粗掩盖单项失利 |
| 双口径复算 | DySurv：摘要「12% more accurate、22% more sensitive」实为百分点被写成百分比；PRIME (JCST)：「超过 26.0%」复算 25.9%（高报），「6.1%」复算 6.5%（低报，方向相反=个别失误非口径问题） |
| 标题-正文同向 | PRIME：小节标题「Better on **Lower** Observation Rate」vs 正文「improvements with **higher** observation rates」 |
| 基线计数 | FITEL (Pattern Recognition)：正文两处写「six baselines」，实际列举与表格均为 7 个 |
| 消融增益 vs 方差 | FITEL：核心模块 DFI 增益 0.83pp < 自身折间标准差 ±1.42，且无显著性检验 |
| 消融臂复用 | FITEL：消融表某行与主表 FT-Transformer 行六项数值完全相同——该臂是复用基线而非独立实验 |
| 摘要强度 vs 正文 | TRIDENT (BRACIS)：摘要「consistently outperforms all baselines」，实际 0%/20% 两档输给 LightGBM，正文自己写的是「from 40% onward」 |
| 文表矛盾 | TRIDENT：§5.2 说 0% 时预训练版更好，同页表格显示无预训练版 0.8533 > 0.8426，加粗的正是前者 |
| 动机-基准错配 | TRIDENT：全文以「缺失带信息量」立论，基准却按 MCAR 均匀注入——MCAR 下缺失不含信息（结构性质疑的范例） |
| 数据集计数 | TRIDENT：引言 10 个数据集 vs 正文/表/结论 9 个 |
| 公允节示范 | DynaGraph：AUPRC 相对提升 6–8% 四数据集验算**全部成立**；Table 1 六十个单元格内部自洽——审查报告必须同样写明 |
| 好论文也有内伤 | Zink (Nature Health)：Fig 1b 写 80,610，正文/表格全是 93,593——内部矛盾不等于全文可疑，定级要克制 |
| **版本考古** | DynaGraph：npj 版被撤，arXiv 前身至今在线无标注。逐格对照发现 F1/Sens 八组数值两版**逐位相同**（同批实验），而 BA 列被替换成 AUROC/AUPRC 全新数字，代码里零实现——**失败发生在改稿阶段，不是原始实验层** |
| **消融图形态变更** | DynaGraph：arXiv 版是无数值箱线图、四面板形态各异；npj 改成带数值柱状图后才出现三面板逐位相同——比对两版可定位错误引入的时点 |
| **流程图残留** | XMI-ICU (Sci Rep)：Fig.5b 的 MIMIC-IV 流程图末端写「915 train / 228 test」，而正文四处声称该库全量作外部测试、从未训练——**示意图/流程图也必须进图文互证，不能只查数据图** |
| **同组跨论文复制** | XMI-ICU 的 Table 4 表注与 DynaGraph 补充表注逐字相同；DynaGraph 因此声明了一个自己根本没做的 MIMIC-IV 实验——同组素材复用会留下「引用了本文不存在实验」的指纹 |
| **计数与百分比互检** | XMI-ICU：正文 MIMIC-IV 死亡「131, or 11.5%」（自洽），Table 4 却写「105 (11.5%)」——105/1143=9.2%，计数与自身百分比对不上 |
| **表格行标签复制** | XMI-ICU：Table 4 的「Ethnicity (male)」行数值与上一行「Sex (male)」逐位相同 |
| **发表版代码链接失效** | XMI-ICU：Code availability 给的是匿名投稿账号 `anony10subm/XMI-ICU`（404），作者真实账号下同名仓库其实存在却没链——**发表前必须把匿名链接换成正式仓库** |
| **划分口径打架** | Patient Forest (Sensors)：§3.4 与表题写 75%:25%，§4 首段写 70%:30% |
| **基线清单 vs 表格** | Patient Forest：清单列 CART 而表中无、表中有 XGB 而清单无；XGB 的引用 [96] 实为一篇 K-NN 论文 |
| **公允节示范二** | 同批五篇中 Chauhan (BMC) 与 Molaei (AISTATS) 经全面审查**未发现系统性问题**——加粗诚实、超参全公开、CodeOcean 验证代码、甚至主动自曝负结果。审查一个课题组时必须给出这种质量分层，不能一竿子打翻 |
