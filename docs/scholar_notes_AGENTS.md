# 科研札记文献库 · Agent 使用说明

> 本文件由 XLBDTranslator-dev 的 `scripts/notes_index.py` 自动部署(源文件在仓库 `docs/scholar_notes_AGENTS.md`,勿直接改这份副本)。
> 面向对象:在**任何项目**(尤其论文写作项目)中工作、需要检索/引用文献的 Claude agent。

本目录是一个按月精选的文献库:Gmail Scholar 提醒 + PubMed + arXiv → LLM 方法学三态筛选 → 优先级排序 → top-5 强模型**全文精读**(句级角色标记 可引用证据/可反驳观点/方法论借鉴,聚合成 `highlights[]` 供工作流按用途调取,关联研究主线 MNAR / MA-GCT / EHR 缺失机制)。

## 目录结构

| 文件 | 内容 |
|---|---|
| `literature_index.json` | **机器可读总索引**(先查这个,再读原文) |
| `INDEX.md` | 人读索引(统计 + 按月表格) |
| `all_references.json` | **全局书目**(全库去重合并的 CSL-JSON,pandoc 直接挂;自动刷新,勿手改。只含 `duplicate_of==null` 的键——渲染月度 md 本身请仍用同月 references.json) |
| `科研札记_YYYY-MM_全文精读.md` | **自动**月度札记(`series:"auto"`):Gmail/检索 → 三态筛选 → top-5 全文精读 |
| `科研札记_YYYY-MM_手动精读.md` | **手动**深度精读(`series:"manual"`):人给 PDF,agent 亲读整本 + 脚本交叉核验,通读更彻底 |
| `科研札记_YYYY-MM_{全文,手动}精读.references.json` | 该札记 CSL-JSON 参考文献(pandoc 可直接用) |
| `科研札记_YYYY-MM_{全文,手动}精读.index.json` | 该札记结构化 sidecar(索引数据源,一般不用直接读) |
| `科研札记_YYYY-MM_{全文,手动}精读.docx` | 样式化 Word 版(人读,agent 勿解析) |
| `manual/YYYY-MM/*.paper.json` | 手动精读的中间 bundle(内部用,勿检索) |
| `topics/<slug>.md` | **概念页**(按概念横切全库的活综述,见下) |
| `topics/INDEX.md` | 概念页目录 |

## 概念页(topics/)——按概念找答案,不是按论文

上面所有文件都是 **paper-centric** 的:回答「某篇论文说了什么」。`topics/` 是另一个轴,
**concept-centric**:回答「关于某个概念,全库现在的共识、分歧与缺口是什么」。

问题形如「MNAR 诊断都有哪些方法、各自前提是什么」「跨中心迁移的证据怎么说」时,
**先读概念页再决定要不要下钻到单篇**——它已经把几十篇里的相关句子合成好了,比你自己
跑一轮检索再逐篇读快得多。

- 每条论断后面的 `[@citekey]` 均由**证据编号回译**产生(LLM 只能引用召回集合里的编号,
  越界即剔除;模型自己写出的引用标记——不论带不带方括号——也会被剥离并计入
  `stripped_cites`),citekey 因此不是模型现编的,而是来自召回集合。`scripts/build_topics.py
  --verify` 会扫描死键(索引里已不存在的旧 citekey)与残留裸引用(`bare_cites`)兜底,
  看到 ⚠️ 再去核实即可,没有异常时不必逐条复核;
- 页面底部「本页证据」列出全部召回证据及其 `note_file:note_line`,要核验哪句话出自哪里
  照着点进去即可;标 ○ 的是召回了但没被用上的;
- 「⚔️ 分歧与冲突」一节是**有意保留的矛盾**,不是编辑疏漏——写 critique / discussion 时
  这里是现成的靶子;
- frontmatter 的 `n_evidence` / `n_papers` 是这一页的证据基础厚度;`invalid_refs` 若明显
  大于 0 说明那一轮合成质量可疑,读时留个心。

**怎么读引用率(○/● 与 `evidence_used`)**:这个比例**不能单独当质量信号**。
低不一定是 `queries` 跑偏——也可能是证据池混进了「对我研究的联想」这类精读者主观批注
(2026-08-16 起默认从召回排除,见 `config/topics.yaml` 的 `exclude_sections`,但某页可能
显式覆盖了默认值);高也不代表论断都扎实——曾实测某页 98% 引用率里近三分之一是与核心
问题无关的「方法联想」杂谈被顺带引用"注水"出来的。判断某一页可不可信,看具体论断是否
带条件/数据集/效应量、「⚔️ 分歧与冲突」两侧是否确实来自不同文献,比看这两个比例数字可靠。

**已知局限:要把具体数字写进稿子,先点开证据表核对原句**。防线保证的是「citekey 与原句
真实存在」(编号回译 + `--verify`),**不保证转述没有失真**——从证据到论断这一步由 LLM 完成,
靠 prompt 铁律约束,不是程序强制。实测过的失真样式:同一组并列数字里最不利的那个被静默
丢掉(四个值 66.0/4.4/70.2/64.4 被写成范围「64.4–70.2」,真实范围 4.4–70.2,差 16 倍),
且**同一页的正文小节写对了、分歧区仍然写错**。所以:结论性判断可以直接读页面,
**具体效应量/百分比一律回证据表看原句**,尤其是「⚔️ 分歧与冲突」里的数字(那里最容易被
直接摘去写作,也最容易因为丢掉不利数字而让某一方显得更强)。

概念页由 `scripts/build_topics.py` 从 `config/topics.yaml` 定义的主题生成,新论文入库后
重跑即更新(周度/月度/手动 PDF 三条入库路径都会自动只重合成受影响的页;一页若连续多轮
没被新论文命中,超过 `--stale-after-days`(默认 30 天)也会被强制重合成一次兜底,不必
苦等"恰好有新论文挤进证据集")。**它是派生物**:内容全部来自 `highlights[]`,不构成
独立事实来源;有疑问时以月度札记原文为准。

⚠️ **撤稿论文的处置（2026-08-17 起改口径）**:**保留札记**——读过它、判断过它的记录
不该消失,删掉等于假装没读过。只做三件事:

```bash
# 1. 在该条的「裁决」行末尾加标记(md 是唯一真相源,跟着札记进 git)
#    **裁决**: `MAYBE` · conf 0.30 · ⚑ RETRACTED
# 2. 刷新索引(解析出 flags,并把它从 all_references.json 剔除)
PYTHONPATH=. python scripts/notes_index.py
# 3. 同步向量库(整篇踢出 RAG,概念页/问答/notes_search 从此都召不到它)
PYTHONPATH=. python scripts/notes_embed.py
```

标记之后:札记原样留着、`literature_index.json` 里 `flags: ["RETRACTED"]`、
**不在向量库、不在全局书目**。有人手打 citekey 想引它,pandoc 会渲染成 `(key?)` 当场炸
——这正是想要的失败方式。`scripts/lint_notes.py` 认得出"已标记",不再每月报警;
只有**查出来但还没标记**的才是 🚨 硬信号(退出码 1)。

若它曾是某概念页的证据来源,标记后要重跑那几页:
```bash
grep -lF '[@<被撤稿的 citekey>]' output/scholar_notes/topics/*.md
PYTHONPATH=. python scripts/build_topics.py --topic <上一步列出的 slug>...
```
用 `--topic`(强制重合成)而不是 `--affected-by <该 citekey>`——后者判的是"这个 citekey
有没有挤进证据集",而它已经不在向量库里了,必然查无此键、永远判"不受影响"。

**撤稿现在会自己找上门,不必等你想起来查**:月度回填收尾会跑一遍知识层 lint,
撤稿命中时弹系统通知(标题「Scholar 库里有撤稿论文」)并在报告里点名"它正在给哪几页
概念页当地基"。见下一节。

## 知识层 lint(`topics/_lint.md`)——整个库摆在一起看有没有问题

`build_topics.py --verify` 问的是**格式**:这一页的每个引用还追得回去吗。
`scripts/lint_notes.py` 问的是另一类问题:**把整个库摆在一起看,知识本身有没有问题**。
产物是 `topics/_lint.md`(同步到 vault `02-主题/_lint.md`,citekey 转 wiki 链接可直接点开)。

```bash
PYTHONPATH=. python scripts/lint_notes.py                     # 四项全跑(对撞要调 LLM,约 8 分钟)
PYTHONPATH=. python scripts/lint_notes.py --skip-contradictions  # 只跑不花钱的三项(月度自动跑的形状)
PYTHONPATH=. python scripts/lint_notes.py --offline --skip-stale --skip-coverage  # 只补跑对撞
PYTHONPATH=. python scripts/lint_notes.py --offline --dry-run    # 什么都不调用,只看候选统计
PYTHONPATH=. python scripts/lint_notes.py --vault-dir ~/Documents/ScholarVault  # 显式指定 vault(见下「ack 写在哪」)
```

四项检查:

1. **☣️ 撤稿**(唯一联网项,~20 秒):全库 DOI 过一遍 OpenAlex 的 `is_retracted`,
   外加"标题以 RETRACTED/WITHDRAWN/撤稿/已撤回 开头"这个独立的第二信号(OpenAlex 的
   结构化撤稿记录有已知覆盖缺口)。**读结论前先读它上面那行分母**——本库 2256 篇 keeper
   里 358 篇压根没有 DOI,"0 篇撤稿"从来不等于"库是干净的"。命中时 CLI 退出码 1,
   **且这个 1 优先于报告落盘冲突的 2**(发现绝不能因为报告写不进磁盘而丢失)。
2. **⚔️ 跨文献对撞**(唯一调 LLM 的一项):全库 `citable ↔ refutable` 的跨论文语义近邻句对,
   由 LLM 分成 `结论冲突 / 单篇内部自相矛盾 / 方法学分歧 / 适用范围限定 / 同题但不构成张力`
   五档,只报前四档。**相似度只是候选生成器**——它找的是"在讲同一件事"而不是"互相矛盾",
   所以报告里每一条都附了两句原文,自己看过再用。裁决同样走**编号回译**(喂进去的候选块
   不含 citekey;`单篇内部自相矛盾` 那一档模型只写「甲」/「乙」,由程序回填 citekey)。
   **看到 🔁 那一档不要去跨两篇核对数字**——问题出在它点名的那一篇自己的原文里。
   **主视野只有 `结论冲突` 与 `单篇内部自相矛盾` 两档**;`方法学分歧`/`适用范围限定`
   默认折进 `<details>`,因为**那一档不是待办,是 discussion / related work 的写作素材**
   ——"两篇的 F1 阈值 / 插补策略 / 多重比较校正不同"是文献的**永久属性**,没有任何操作
   能让它下一轮消失。真实报告 13 条里 11 条是这一档,不折起来就把 2 条真待办埋掉了。

   **五档各自该怎么处置**(⚔️ 那一档此前是空的,补上):

   | 档 | 确认它是真的之后干什么 |
   |---|---|
   | ⚔️ 结论冲突 | 见下方「⚔️ 被确认之后」——**不会自动回流概念页**,要人动手 |
   | 🔁 单篇内部自相矛盾 | **别跨两篇核对**,去翻它点名那一篇自己的原文(如附录 I vs 附录 R) |
   | 🔀 方法学分歧 / 📐 适用范围限定 | 写 discussion / related work 时展开来抄,不是缺陷 |
   | 同题但不构成张力 | 只计数,不列出 |

   **⚔️ 被确认之后**(撤稿有明确的四处处置流程,这一档没有,所以要说清楚):
   概念页**不知道**这条冲突存在——`build_topics.py` 合成时读的是句级证据,不读 lint 报告,
   下次重合成也不会把它写进页面。所以确认了一条真冲突之后**至少做这一件**:
   把结论写进 `_lint.md` 的 `## 我的批注` 区(那里是你的,生成器永不触碰),写清楚
   "哪两篇、谁的做法在什么条件下更可信",写 discussion 时回来抄;
   然后写一行 `- ack: <id> …` 把它折起来,免得下轮再问一遍。
   想让概念页本身反映这条冲突,只能手动编辑对应页的「我的批注」区
   (**生成块不能手改**,会判 conflict)。"裁决自动回流概念页"是 roadmap 项,尚未做。
3. **⏳ 陈旧论断**(纯计算):概念页论断中,支撑文献最新的一篇也已是 N 年前(默认 5)。
   **这不是判它错**——方法学论断本来就可能十年不变;它只回答"这条结论后来有没有被更新的
   工作推翻,我确认过吗"。展示**按撑着它的那篇老文献聚合**(不是按概念页平铺):
   39 条背后只有 24 篇老论文(`lee2021Multiview` 一篇撑 4 条),**可执行单位是那 24 篇**
   ——"给撑得最多的那几篇补一轮新文献"是一个下午能做完的事,"39 条论断"不是。
   报告里直接给出集中度("其中 N 篇各撑着 ≥2 条,合计 M 条(X%),先补这几篇")与
   **执行命令**:`PYTHONPATH=. python scripts/search_pubs.py "<该篇的主题词>" --days 1825`
   (arXiv/PubMed 临时检索,不入库,命中会标注是否已在札记库;即 skill `scholar-search`)。
   ⚠️ **这一节的量是日历驱动的**:阈值是 `当前年 - --stale-years`,所以每年 1 月它自己
   就会长一截(实测把年份往前推:2027 年 62 条/38 篇、2028 年 116 条、2031 年 353 条),
   而那次"新增"不是发现、是时钟走了一格——报告会自己在 delta 下面写一句
   「其中 N 条是因为陈旧阈值前移到 X 年才掉进来的,不是内容变化」。锚文献数上限
   `--stale-anchor-limit`(默认 20,`0` 不限),超出的收成一句"另有 N 篇"。
   已知局限:判据量的是论断的形状而不是风险(真正该问的是"这个概念库里 2022 年后有没有
   更新的候选证据"),那需要给 `find_stale_claims` 引入向量库依赖,尚未做。
4. **🕳 覆盖缺口**(纯计算):已精读且高优先级、却连任何一页的**证据池**都没进过的论文。
   **它们不等于"与这几个概念无关",更不等于"queries 漏了问题域"**——8 页当前全部触及
   各自 `max_evidence` 上限,所以"碰不到"的准确含义只是**没挤进任何一页的
   top-`max_evidence`**。名单分两档:最近 3 个月新入库的单列(多半只是还没排进前 N 名),
   其余那档才值得考虑要不要开新页。**入库月认 `YYYY-MM` 前缀**,所以
   `2026-08-10`/`2026-08-np`/`2026-07-28-TFM` 这类手动批次桶(全库 199 条)不会再被当成
   "解析不出→当老条目"塞进"该开新页"那一档。"该开新页"那一档**按年份正序(最老优先)**
   ——年份倒序会让 `walker2009Evaluation` 这批最久没人碰的老文献永远排不进名单,
   而那批恰恰更可能是真缺口;`year` 晚于明年的(如索引里的 2045)标「元数据可疑」并沉底,
   **只标不改,不动索引数据**。判断某一篇是不是真缺口:对它手动跑一次
   `scripts/notes_search.py <该篇主题>` 看它在全库语义排名里的位置。
   各页证据厚度那张表带 `max_evidence` 列:标"饱和"的不算薄(只是复述配置),标"**薄**"的
   才是真的"库里就这么多证据";**全部饱和时收成一句话不出表**(每月重印 8 行全标"饱和"
   的表是纯噪音)。已从 `config/topics.yaml` 下线的退役页单独标出,不再误报成
   "调用方没给 specs",也不算进"全部饱和"的分母。

**报告的读法有四条要紧的**:

- **先看顶部那行「本轮必须处理」**:只放硬信号(当前只有撤稿),没有就写"无"。
  它下面那行「本轮状态」一行两用——四项各自是 ✅ 本轮刚跑 还是 ⏸ 结转自 N 天前,
  **每项都带绝对日期和条数**(报告是磁盘上的静态文件,月度 launchd 挂了它也不会更新,
  "刚跑"是相对谁说的必须写出来)。**✅ 只认"本轮真跑过"这件事本身,不从时间戳反推**
  ——同日先全跑再 `--offline` 窄跑时,撤稿那项写的是 ⏸ 结转自今天早些时候,不是 ✅。
- 每一节都写着自己的扫描口径与分母,frontmatter 里对应计数是 `null` 而不是 `0`——
  **`null` 现在的语义是"从来没执行过"**(跑过一次就会一直结转下去)。
- **本轮跳过的一节会结转上一次的结果并标明时效**:标题下方那条 `⏸ 本轮未执行……以下是
  N 天前那次运行的结果,不代表当前状态` 就是结转来的。所以 `--offline` 这种窄跑不会再
  把撤稿那节清空,但**结转的结论也不是当前状态**(撤稿那节的"已被概念页引用:X、Y"
  同样是那一轮的页面快照,动手前先核实)。
- 距上次对撞超过 45 天(`--contradiction-reminder-days` 可调)时,报告顶部与 stdout 都会
  提醒补跑。月度自动化明确不跑对撞(成本),它完全依赖人自己想起来。
- **陈旧/孤儿两节每节开头有一行 delta**:「本轮新增 N · 与上轮相同 M · 已消失 K」。
  上一版没有可比对的 ID 时会明说"本轮不做增量比对",而不是把全部条目报成新增。

**「这条我看过了,不是问题」怎么表达**——三节都支持,ID 形态各不相同:

| 节 | ID | 从哪抄 |
|---|---|---|
| ⚔️ 跨文献对撞 | 8 位哈希 `#ab12cd34` | 每条张力的 `####` 标题末尾 |
| ⏳ 陈旧论断 | 8 位哈希 `#ab12cd34`(`sha1(slug+论断文本)`) | 每条论断行末尾 |
| 🕳 覆盖缺口(孤儿) | **就是 citekey**(如 `#walker2009Evaluation`) | 每条孤儿行末尾 |

写法:`- ack: <id> <说明>`。**反引号连着复制也认**(报告里 ID 就是 `` `#ab12cd34` ``
这样渲染的,在 Obsidian 源码模式/Vim 里复制大概率把反引号带上),有序列表前缀
(`1. ack: …`)、全角冒号、大小写混排也认。

**写在哪一份文件里**——这是最容易踩空的一处:

- `output/scholar_notes/topics/_lint.md`(权威产物),**以及**
- `~/Documents/ScholarVault/02-主题/_lint.md`(vault 副本,有它自己**独立**的批注区)

**两份都读、取并集**。vault 那份的路径由 `--vault-dir` / 环境变量 `SCHOLAR_VAULT_DIR`
给,都不给时探测 `~/Documents/ScholarVault`。报告顶部会把当轮真正生效的两个绝对路径
逐字印出来——以那两行为准。

ack 生效后那条会被折叠进 `<details>` 而不再占主视野,**不是不再显示**:内容一变 ID 就变、
会重新展开(内容变了就是新问题,该重问)。**折叠对结转来的那一节同样生效**——月度自动化
固定跑 `--skip-contradictions`,对撞那节年年都是结转的,首版的 ack 在那个节奏下永远不
生效(实测 12 个月一次都没折叠上)。ack 的 ID 若一条都没对上,报告顶部会列出来并说明
三种可能(抄错 / 原文变了 / 那一节本轮跳过且结转不到),不再静默丢弃。

**ack 让待办往前走,不只是不占版面**:孤儿那节的 `--orphan-limit`(默认 25)是在**分完
ack 之后**才截断的,所以把列出的 25 篇处理掉,下一轮后面 25 篇会顶上来;小标题写
「共 208 篇 · 已确认 25 · 待办 183 · 本节列出前 25 · 队列里还有 158 篇没列」,
顶部状态行也跟着写「缺口 · 208 篇 · 待办 183」——**数字会随你干活而变小**。

⚠️ **概念页一被重合成,那一页上的陈旧 ack 会全部失效并重新报成"新增"**。这是设计如此
不是 bug:陈旧论断的 ID 是 `sha1(概念页 slug + 论断文本)[:8]`,而月度 `--affected-by`
重合成会重写论断文本,文本一变 ID 就变。语义是对的("论断改了就是新论断,该重问"),
但你会看到"我明明 ack 过的一整页又全冒出来了"。对撞那节的 `pid` 同理(哈希两侧句子原文),
孤儿那节**不受影响**(ID 就是 citekey,除非改 citekey——那要扫派生物,见 `scholar-identity-keys`)。

**ID 可以是任意字符**(不含空白与反引号):库里 2343 个 citekey 有 40 个含非 ASCII 字符,
其中 `куксенко2024Аналіз`、`đorđević2025Optimization`、`周立基于深度学习的…` 首字符就不是
ASCII。ID 在文本里靠**反引号/空白定界**识别,不靠字符白名单——所以照抄报告里印的那一串
一定管用。代价:`- ack: 我确认过了` 这种没写 ID 的行会被当成 ID(库里真有纯中文 citekey,
分不开),它会出现在"这条 ack 没匹配上"那份名单里。

## 问答归档(`topics/qa/`)——问过的问题不要再问第二遍

### 先看该不该用它(三行路由)

| 这个问题是…… | 用什么 |
|---|---|
| 概念级、会反复用、希望**自动保鲜** | **概念页** `topics/<slug>.md`;还没有就往 `config/topics.yaml` 加一条,跑 `build_topics.py` |
| 临时的、具体的、一次性的 | **`scripts/ask_notes.py`**(本节) |
| 只想看有哪些句子、自己判断 | `notes_search.py`(语义) / `notes_query.py`(role 硬门槛);写稿取证走 `scholar-write` |

⚠️ **有概念页覆盖时不要用 ask_notes**。实测撞过一次:同一个 MNAR 诊断问题,问答页
40 条证据 / 45% 引用率 / 7 条论断,而 `mnar-diagnosis.md` 是 70 条 / 100% / 34 条论断
且新论文入库自动重合成,23/40 个 citekey 还是重合的——**那 90 秒不值**。
现在 `ask_notes.py` 会在提问前自动比一遍概念页并提示,但判断权在你。

### 怎么用

`notes_search.py` 把相关句子捞给你看,答案在你脑子里、用完即弃。
`scripts/ask_notes.py` 多走一步:把「问题 + 答案 + 每条论断的句级出处 + **这次召回没覆盖到的部分**」
落成 `topics/qa/<slug>.md`,让探索**复合**而不是每隔几个月重问一遍。

```bash
PYTHONPATH=. python scripts/ask_notes.py "MNAR 诊断在纵向 EHR 上到底能不能做？"
PYTHONPATH=. python scripts/ask_notes.py "..." -q "informative missingness" -q "缺失机制可检验性"
PYTHONPATH=. python scripts/ask_notes.py "..." --dry-run   # 只看召回了什么,不调 LLM、不落盘
PYTHONPATH=. python scripts/ask_notes.py --list            # 已归档的问答
PYTHONPATH=. python scripts/ask_notes.py --verify          # 自检引用是否仍可追溯(死键/失锚)
```

- **同一个问题再问一次是原地更新那一页**(slug 由问题文本决定;空白、标点、全半角、
  零宽字符全部归一化,不掺日期),不会堆出第二份答案。`first_asked_at` 会保留,
  `generated_at` 前进。少打一个问号不会另开一页。
- **问之前会先查重**:与已归档问题接近的会提示你,附 slug、路径与**直接可粘的更新命令**
  (`ask_notes.py "<那一页自己的问题>" --slug <slug>`（**换个问法覆盖那一页没有路径**：slug 占用检查按问题身份比对，新问法必然被拒退 2）)。查重走**语义**(embedding),Ollama 不可用时
  降级回词面重合**并明说降级了**——词面档实测漏的正是"三个月后换个说法再问一次"那一档
  (0.10~0.17 全漏),纯英文短问题(`AI vs ML?`)分词后为空、永远不响。`--no-dedup-check` 关掉。
- **`--slug` 不会静默覆盖别人**:那个 slug 上已经躺着另一个问题时直接报错退 2,
  既不覆盖那一页,也不会把它的答案当"上一版"喂给这次的 prompt。
- `-q/--query` 是**额外检索词**,不改变问题本身:证据召回是每个 query 各检索一次后合并,
  不是拼成一句(拼接会把几个概念平均成一个语义中点,谁也召不准)。关键术语的中英两种
  写法都值得给——中文查询能命中英文原文,但概念换述的召回率实测只有 ~30%。
- 防幻觉与概念页同一套**证据编号回译**,外加一条本模块特有的:**正文里模型顺手写的
  `E31` 这类编号也会被回译成 `[@citekey]`**(越界的直接删掉并计数)。理由是编号只是
  本次召回的内部序号,页面被摘去写稿后就完全失去意义。
- 证据召回是**窄而深**(默认 28 条 / 单篇最多 3 条),不是概念页那种横扫全库:
  一个具体问题要的是把最相关那几篇挖到第 2、第 3 句。首版照抄概念页的 40/4,
  实测退化成「40 篇各 1 句」,把答案真正依据的那篇原始文献漏在了 ○ 里。
- ⚠️ **归档问答不在向量库里**,`notes_search.py` 搜不到它们。**概念页也不在**——
  这是 P1 就欠下的一笔已知欠账(`embed_store.sync_store` 的删除语义要求**每一个**调用方
  都把这类内容传进去,漏传的那个会在下次同步时整批删掉),不是问答这一层单独的取舍。
  找旧问答的三条路:`topics/qa/INDEX.md` 目录页、Obsidian 的 `02-主题/问答/`、
  或者直接再问一次(查重会把旧的那页指给你)。
- ⚠️ **Obsidian 那份不会立刻出现**:`com.xlbd.scholar-vault` 只盯 `literature_index.json`,
  而归档问答不动索引,所以要等下一次索引重建(周度/月度)。急用手动跑一次:
  ```bash
  PYTHONPATH=. python scripts/sync_vault.py --vault-dir ~/Documents/ScholarVault --force
  ```
- 「**本次召回没覆盖到的**」那一节**不是"库里没有"**。它说的只是这一次召回的
  几十条证据没能回答的部分——召回是按单个问题从 2300+ 篇里切出来的窄切片,
  **召回不到 ≠ 库里没有**。脚本会拿每条空白的文本回查一次向量库与概念页,
  命中就在旁边标「⚠️ 但库里可能有」;没有标记也别当结论,先查 `topics/INDEX.md`
  与 `notes_search.py` 再决定要不要去补文献。
  (首版这一节写的是「库里现在没有证据」,于是产出过一条「库内证据显示这类讨论
  几乎完全缺失」——而库里有一整页 300 行的综述在讲那件事。**答错了还能核对,
  指错方向不会去核对**。)
- `--verify` 除了死键/失锚,还报**残留证据编号**与**防线版本**:归档页是"生成它那一刻
  的代码"的快照,低于当前版本的页面会标 ⚠️ 并给出重跑命令(纯本地扫描,不花 LLM)。

## 语义检索(中文找英文表述、换述同义词用)

`jq`/`notes_query.py` 是精确子串匹配；查不到换述表达（如中文"缺失机制不可忽略"查不到英文
"informative missingness"）时改用 `PYTHONPATH=. python scripts/notes_search.py <查询...>
[--role ...] [--json]`(默认 `--mode hybrid`=向量+BM25 关键词融合)。句级证据同样只覆盖库内
约三成精读文献(668/2254,截至 2026-08-21;实时数以 literature_index.json 为准),语义命中若
标注"该篇无精读句级证据"是真的没有,不是没搜到。

## 检索流程(四步法)

1. **先查索引**:`jq` 过滤 `literature_index.json` 的 `papers[]`,拿到 citekey / note_file / note_line;
2. **再读原文**:按 `note_file` 打开对应月札记,`grep -nF '[@<citekey>]'` 定位到该篇小节,读裁决、摘要与「全文精读」节(`〔可引用证据〕〔可反驳观点〕〔方法论借鉴〕` 是句级角色标记;历史札记可能仍是旧标记 `〔方法学创新〕〔重要发现〕〔研究背景〕`)。**句级取证不必打开 md**——直接查条目的 `highlights[]`(见下);
3. **引用**:正文用 pandoc 语法 `[@citekey]`;
4. **配书目**:直接挂全局书目 `all_references.json`(已全库去重合并,不必自己拼月度文件;配方见下)。

## 索引 schema(`papers[]` 每条)

`citekey, citekey_source("zotero"|"fallback"|"unknown"|"missing"——missing=占位键勿引用), series("auto"自动流水线|"manual"手动深读), doi, arxiv_id, title, title_zh, authors[], year, month("YYYY-MM"), journal, url, priority_tier("high"|"mid"|"low"), priority_rank, priority_score, decision("INCLUDE"|"MAYBE"), one_line(一句话用处), bucket[], role(筛选角色,非句级), confidence, flags[], has_full_text_reading, reading_source, tag_counts{role计数:citable/refutable/method}, highlights[](句级可调取,见下), note_file, note_line, note_heading, references_json, dedup_key, duplicate_of(非 null=重复条目,检索时应过滤), duplicate_months[]`

**`highlights[]`——句级取证的核心**:每项 `{role, tag, section, text}`。`role` 是按**对后续工作流的用途**的三分:
`citable`(可引用证据:含数字/效应量/可溯源结果)、`refutable`(可反驳观点:作者主张/可质疑处,写 critique 的靶子;手动精读还含对抗核验的纠错条)、`method`(方法论借鉴:可迁移的方法思路)。`tag` 是对应的中文原标记,`section` 是精读分节名(溯源用)。工作流按 role 跨全库直取句子,无需打开 md。历史条目的 role 由旧标记规则近似映射(方法学创新→method、重要发现→citable、研究背景→丢弃),新精读由 LLM/subagent 直接精确产出。

顶层还有 `months{}`(按**文件 stem** 键,含 month/series)、`citekey_collisions[]`(撞键警告,见下)与
`title_near_duplicates[]`(疑似同文待人工确认,见下)。

**keeper 规则**(依次比较):`series:"manual"`(手动深读)恒为 keeper(即使月份晚于自动版)→ **书目更全者**
(authors/doi/journal 三项中非空的更多)→ 月份更早 → 优先级更高。所以按 `duplicate_of == null` 过滤后,
你读到的就是**最彻底的那版精读**,且不会是个作者/DOI 全空的残缺条目。

> 第二顺位是 2026-08-14 补的:此前只按月份,预印本被解析成 `anon*` 无作者条目时会因月份更早
> 压过带作者的正刊记录(实测 76 个重复簇里 6 组踩中,如 Research Square 版压过 Nature Medicine 版),
> 合并等于把完好记录换成残条。series 仍是第一顺位——正文内容比元数据完整度重要。

**判重三层**(并查集合成簇,保证传递性;每簇按 keeper 规则选权威):
1. 精确身份键:`dedup_key`(**doi: > arxiv: > 场地原生 id > 规范标题 > id:兜底**)+ 规范化标题二级键;
2. 人工确认对:`dedup_overrides.json` 的 `merge[[keyA,keyB],…]`,无条件合并;
3. 标题相似度:IDF 加权余弦 ≥ **0.70** 且无身份冲突(双方都有 DOI/arXiv id 而不同、
   首作者姓氏不同、年份差 >3 → 一律不合并)时自动合并,捕获**改写题名**的漏网重复
   (预印本→正刊常改标题,两侧又都无 DOI 时精确键判不出)。

### 场地原生 id(无 DOI 的会议论文,2026-08-15 加)

`pmlr:v287/elsharief25a`、`openreview:x4UK4GadLd` —— 从 `url` 抽出(id 恰好也长在 PDF 文件名与
官方 BibTeX key 上)。它由**出版方分配**、不可变、全局唯一;而标题键是派生量,标题被解析截断
(库里出现过 "Healthcare Analytics"、"ICU ADMISSION PREDICTION" 这种残条)身份就跟着变。

⚠️ **大小写**:PMLR slug 本身全小写,归一化无害;OpenReview 的 forum id 是**区分大小写**的
base62 串,`.lower()` 会把两个仅大小写不同的 id 折成同一个键 → 并查集判成同一篇 → 落败方
标 `duplicate_of` 后被下游一律过滤 = **静默吞篇**。故 PMLR 小写归一、OpenReview 原样保留
(2026-08-15 修正;`dedup_overrides.json` 里 3 个 `openreview:` 键同步改回原始大小写)。

url 变体由 `urlsplit + parse_qs` 拆开后匹配,不依赖参数顺序或路径形状:`?x=1` 无扩展名、
`?noteId=…&id=…` 换序、`attachment?id=`、`references/pdf?id=`、附件 `xxx-supp.pdf`、
以及 PMLR 官方 GitHub 镜像 `raw.githubusercontent.com/mlresearch/vNNN/…` 全部认得。
抽不出时静默退回 `title:` 键——正好回到这一层要解决的问题,所以宁可放宽也别漏。

为什么非要有这一层:实测本库 44 篇无 DOI 的 PMLR 论文,OpenAlex 只认出 33 篇且**全部经由
arXiv/PubMed**(0 篇有 PMLR 落点);剩下 8 篇在任何外部库里都不存在,**PMLR slug 是它们唯一的
正经标识符**。排在 arXiv id 之后:预印本记录没有 PMLR url、正刊记录没有 arXiv id,两者本就
拿不到同一个键,那是人工裁决层的职责。

⚠️ **改键梯规则后必须 `--full` 重建**。增量重建按 md 的 mtime/size 判断是否重解析,**不看代码
版本**,规则改了却不动文件的月份会沿用旧键。

sidecar 里的 `dedup_key` 是落盘时的快照,**磁盘上的值永不回写**——两条读取路径一律经
`_citekey_utils.recompute_entry_key()` 按当前规则重算(落到 `id:` 时才保留原值):
- 索引侧 `notes_index.build_month_entries`;
- 整篇覆盖守卫 `ingest._existing_note_dedup_keys`。

两处**必须共用同一个函数**。曾经只有索引侧重算、守卫侧直读冻结键,于是键梯一升级同一篇论文
两边拿到两个键,守卫把「本批已覆盖」算成「本批未覆盖」,同 label 重跑被误报「会丢数据」而拒写,
提示换 `--label`——而换 label 会真的产出重复札记(实测库内 43 条 sidecar 处于该状态)。

### 出版日期的精度(`date_precision`,2026-08-15 加)

⚠️ 先分清两个名字:CSL 的 **`issued` = 出版日期**、**`issue` = 期号**,毫无关系。

`PaperMetadata.publication_date` 是 `date` 类型,**必须凑齐年月日**;而 Crossref 常只给
`[[2026]]`、PubMed 常只给 Year+Month、pdf-llm 只抽得到年 —— 各来源一律补 1。于是参考文献
会渲染出论文并不存在的月份(`2026 (January)`)。

解法**不是**让 `publication_date` 可空(那会连累 `_fallback_citekey` 取年与索引的 `year` 字段),
而是新增 `date_precision: "day"|"month"|"year"|null` 如实记录精度,**只在产出层截断**:
- CSL 的 `issued` → `date_parts()`:day/month/year 各出三/二/一段;
- Zotero 的 `date` → `date_string()`:`2026-05-05` / `2026-05` / `2026`(该字段是自由文本,接受部分日期)。

`null` = 存量条目精度未知,产出层用启发式倒推(`infer_date_precision`):`(m,d)==(1,1)`→年、
`d==1`→年月、其余→日。**存量归一脚本与 regen 重写共用同一启发式**,故 finalize 不会冲掉归一成果。
已知误伤:真实"某月 1 日出版"降为月精度、真实元旦降为年精度(全库量级约 29 条与 2-3 条),
只丢渲染用的月/日,**年份永不丢**,检索与身份键完全不受影响。

一次性存量迁移:`scripts/normalize_pub_dates.py`(有 DOI 的回查 Crossref 拿真实精度,其余走启发式;
默认 dry-run,`--apply` 需带 `--i-know-this-is-a-one-shot-migration`)。**跑完别再跑**——
此后新入库的论文带真实 precision,其中不乏确实是某月 1 日出版的,重跑会误截。

### citekey 不是身份

`citekey` 是**给 pandoc 渲染引用的人读标签**,不是标识符:它由「首作者姓+年+标题首个实词」现算,
三项都可能错(实测 44 篇 PMLR 里 23 篇与官方 slug 不符——复姓被截断、首作者解析错、
论文集出版年与会议年差一年);它会撞(库里 38 个 citekey 出现多次);而且 `--fix-collisions`
会主动改写它。跨系统对账一律用 `dedup_key`。

**改过的 citekey 不会被 regen 顶回**(2026-08-15 起):`read_pdf.py` 的 `_reuse_citekeys` 在整月重建前
读上一版 sidecar,按 **dedup_key** 把现有 citekey 沿用回来;bundle 里根本没有 citekey 字段
(它是每次现算的派生量),此前 finalize/regen 会用 bundle 的旧元数据重算,把改过的键全部还原。
两侧撞键都拒绝沿用(旧 sidecar 同 dedup_key 多条 / 本批多份 bundle 同 dedup_key),退回 fallback 让
`used` 集合去消歧——否则两篇会拿到同一个显式键,而显式键不进消歧循环 = 静默撞键。
沿用的键在 sidecar 里仍标 `citekey_source: "fallback"`(`write_notes(explicit_citekey_source=...)`),
不冒充 Zotero 权威键。

校验工具:`PYTHONPATH=. python3 scripts/audit_citekeys_vs_pmlr.py`,拿 slug 当权威源反查首作者姓
与年份。改键前**先跑 `--apply --dry-run`** 看一遍将要生成的新键,确认后再去掉 `--dry-run`。
三道闸(任何一道不过就跳过并点名,绝不拼垃圾键、绝不留下半改状态):
1. 原 citekey 必须能拆出「姓+4 位年」,否则拿不到标题实词尾巴 —— 库里真实存在 23 个拆不出的
   (`molaeiFederated` 无年、`куксенко2024Аналіз` 西里尔),而无年 citekey 正是 PMLR 常态
   (`publication_date` 缺失时兜底键的 year 为空);
2. 新键必须过 pandoc 合法字符校验(否则引用会在非法字符处被截断/解析不到 = 静默失效);
3. `_rename_citekey_in_note` 对 md + references.json + sidecar **先全量预检、再全量写盘,
   写盘中途失败按已写成功的逆序回滚**——要么三处都改、要么一处不动。
   返回值是**三态字符串,不是 bool**(`if ret:` 会把 `"refused"` 当成成功):
   - `"ok"` 三处都改到位;
   - `"refused"` 磁盘零改动(预检不过,或写盘失败但已回滚干净);
   - `"partial"` 写盘失败**且回滚也失败** → 磁盘半改,单列清单、优先展示、必须人工核对三处。
   两个调用方(`fix_citekey_collisions` / `audit_citekeys_vs_pmlr.py --apply`)按三态分流:
   `refused` 与 `partial` 都非零退出/告警,但只有 `refused` 能说「磁盘未改动」;
   `partial` 时新键**已落在 md 上**,`fix_citekey_collisions` 必须把它计入 `all_keys`,
   否则同组下一条会拿到同一个新键 —— 修撞键的工具反而在磁盘上新造一个撞键。
   (曾经是 md 先写、后两处异常吞成 warning 仍返回 True:refs 没同步 → pandoc 解析不到条目;
   sidecar 没同步 → 下次重建把 md 改回旧值,撞键永远修不掉。加预检后这个状态一度只是从
   预检阶段挪到了写盘阶段,故补了回滚与三态。)

⚠️ **改键还有两个不会自己跟上的派生物**——三处写完不等于全库一致:
- **向量库 `embeddings.sqlite3`**:三级 chunk 的 id 都内嵌 citekey(`p:<citekey>` /
  `ab:<citekey>` / `h:<citekey>:<role>:<hash12>`),`chunks` 表另有
  `citekey` / `year` 两列,是语义检索结果回传给写作侧的**唯一身份来源**。库一旧,
  `notes_search --cite` 就输出磁盘上已不存在的键(pandoc 渲染成 `(key?)`),而索引驱动的
  `notes_query` 输出的是新键——同一篇论文,两条取证 CLI 互相矛盾,粘进同一篇稿子就有一半
  引用挂不上书目。digest 的语义近邻注入吃的也是这两列,陈旧库会把旧键旧年份喂进 LLM 裁决。
- **已渲染的 docx**:人读/传阅成品,正文里写死了 citekey,读者据此回查会查不到。
  注意手动精读那份**不能靠 `read_pdf.py regen` 重渲染**——它从 `manual/<month>/*.bundle`
  重算兜底键,而 bundle 没经过改键,重跑会把新键顶回旧键。只能定点改或人工确认。

两条改键路径(`fix_citekey_collisions` / `audit_citekeys_vs_pmlr.py --apply`)收尾都会调
`notes_index.announce_rekey_side_effects()`:列出受影响月份的过期 docx,并 best-effort
刷索引 + 跑 `sync_store`(失败只 warning 并打出 `PYTHONPATH=. python scripts/notes_embed.py`,
绝不改变改键本身的成败)。判据是「新键是否落到磁盘上」(OK **与** PARTIAL 都算),不是 renamed 计数。

向量库的兜底自愈有两层,别只依赖其中一层:
- `scripts/ingest_notes.py` 周度入库收尾会同步。它在「本周无新论文」时**也会走**——
  「本批没有新内容要嵌」不蕴含「向量库不需要动」,改键/改元数据同样让它变旧
  (只有 `--list` / `--dry-run` / `--no-index` 这三个只读入口才跳过);
- `config/launchd/com.xlbd.scholar-embed.plist`(需人工装:`bash scripts/install_embed_sync.sh`)
  照抄 vault sync 的思路,`WatchPaths` 盯 `literature_index.json` 与 `abstracts.json`
  **两个**文件,任一变动就跑增量同步。必须两个都盯:`abstracts.json` 由
  `backfill_abstracts.py` 直写 sidecar、**完全不经过索引**,只盯索引的话摘要回填完
  `ab:` 厚 chunk 要悬空到下一次无关的索引变动才生效。反过来也一样——回填摘要后
  **不需要**手动刷索引来触发重嵌,sidecar 一落盘 watcher 自己就跑。

落在 **0.45–0.70** 的对**不合并**,列进 `title_near_duplicates[]` 等人工确认——该区间实测混有
大量不同论文(短标题/截断标题尤其容易假阳性),而**误合并会静默吞掉一篇**(下游一律按
`duplicate_of` 过滤),代价高于漏合并。

裁决结果两侧都要写回 `dedup_overrides.json`,**判为不同也要写**:
- 判为同文 → 写进 `merge[[keyA,keyB],…]`,无条件合并,不受阈值变动影响;
- 判为不同 → 写进 `distinct[[keyA,keyB],…]`,此后不再出现在 `title_near_duplicates[]`。

`distinct` 只压制"请人看一眼",不影响合并(这些对本就没被合并);同一对同时出现在 merge 与
distinct 时以 merge 为准。

⚠️ **两侧的键都会被漂移检查盯着**:重建索引时 `merge` 与 `distinct` 的每个键都要能在库中找到,
找不到就逐条 WARNING「未生效」并区分「缺前者/缺后者/两侧都缺」。裁决文件从两处取并集
(仓库 `config/` + `notes_dir/`),**正常输出里未生效应恒为 0**——出现就说明某个键漂了
(元数据变动/键梯升级/札记已删),那条裁决已经悄悄失效,必须立刻核对更新。**没有这一半人工复核通道就永不收敛**:假阳性每次重建索引都原样再报,
看过多少遍都不减少,真正的新增待确认项被淹没。2026-08-14 一次性裁决 16 对(2 合并 14 判异),
`title_near_duplicates[]` 归零。

## 精读分节里的「实验方法」(2026-08-16 起全库都有)

分节 `heading` 是自由文本,渲染/解析/索引/嵌入/查询全链路无白名单,所以加节不影响任何存量条目。
新增的 **「实验方法」** 节按"他人照着能复现"整理:数据集/队列与划分(比例、分层、防泄漏)、预处理、
模型配置与超参(优化器/学习率/batch/epoch/随机种子/硬件)、评估协议与指标、基线及其配置、
代码与数据可得性(仓库链接、许可)。**原文未报告的项显式写「原文未报告:X」**——与出版日期那条同一
原则:空就说空,不省略、不推测填补。可移植做法打〔方法论借鉴〕,具体数字/配置打〔可引用证据〕。

**存量已于 2026-08-16 全部回填**(223 篇,`scripts/backfill_methods.py`)——原先"存量不回填"的
方针已废止,手动精读现在**每一篇都有这一节**,可以放心按它取方法学证据。

回填是重读 PDF 的独立链路,不是 `regen`(regen 只按现有 bundle 重建札记、不重读原文):

```bash
PYTHONPATH=. python scripts/backfill_methods.py scan                  # 看还剩多少
PYTHONPATH=. python scripts/backfill_methods.py repair-paths --apply  # PDF 被挪走后修 pdf_path
PYTHONPATH=. python scripts/backfill_methods.py run --month 2026-07   # 回填(断点续跑)
PYTHONPATH=. python scripts/read_pdf.py regen --month 2026-07         # 必须跟这一步才进 md/索引/向量库
```

进度记在 `output/scholar_notes/manual/backfill_methods_progress.json`,每篇一存,中断不丢。
`--force` 是**替换**已有那节(不是并排插两节)。产出过不了校验(少于 3 句 / 无页码锚 /
缺「原文未报告」/ 缺「代码与数据可得性」)就不写盘,留给下轮重跑——宁可空着也不写半成品。

⚠️ **回填件与手写件质量不同源**:回填是 sonnet 读全文一次成稿,没有 agent 亲读交叉核验那一轨。
实测抽检一篇逐条比对 PDF(28 条断言、0 编造),但取证到关键数字时仍建议回原文复核页码。
非实证论文(综述/观点/撤稿声明)本节只有三五句、明说"本文无实验",这是正确行为不是漏读。

只有摘要的降级篇(`has_full_text_reading == false`)本节通常很短或整节都是「原文未报告」,
这是正确行为——单跳 prompt 明确要求"仅在可得文本确有报告时"才写,防止照着摘要编造超参。

## 阅读深度量尺 `reading_depth`(⚠️ 库里并存两代精读,取证前先看这个)

条目上还有一把阅读深度量尺:`reading_depth, fulltext_chars(真正喂进 LLM 的正文字符数),
fulltext_chars_raw(抽取到的原始正文长度), fulltext_truncated`。

`reading_depth` **四态**(与仓库 `src/scholar/schema.py` 的字段注释逐字一致,全库只此一份定义):

| 值 | 含义 |
|---|---|
| `chunked` | manual 全部 + 开关打开后的 auto |
| `single-call` | auto 单跳 |
| `unknown-legacy` | 仅 auto 存量条目(由回填写入) |
| 键缺失 / null | 只可能出现在 `has_full_text_reading == false` 的非精读条目上 |

**下游(`notes_query` / skill `scholar-write` / Obsidian vault)必须这样用**:

- `unknown-legacy` = **深度未知**,可能只覆盖正文前 40k 字符、且集中在靠前的几页(方法/结果常被砍掉)。
  这批条目一律**不重跑**(重跑要数千次 LLM 调用并改写全部历史 md/references/vault,爆炸半径远超收益);
  真要引用其中某篇时,走 skill `read-paper` 对那一篇**手动重读**——个案实测能从十来条句级标记涨到 57 条,
  是效果最好的补救。
- **别按 `highlights` 条数横向排序取证**:新老两代精读的产出密度天差地别,按条数排会系统性偏向新札记。
  要比"读得深不深"请看 `reading_depth`,不要拿 `tag_counts` 当代理指标。
- `fulltext_truncated`:**缺失 = 未知**,`false` = 确认未截断,二者禁止混同(`fulltext_chars` /
  `fulltext_chars_raw` 同理,存量回填一律留缺失,不猜不填)。

```bash
# 取证前先分层:看这批候选各是什么深度
jq -r '.papers[] | select(.duplicate_of == null and .has_full_text_reading)
       | [(.reading_depth // "MISSING"), .series, .citekey] | @tsv' literature_index.json | sort | uniq -c

# 只要读得最彻底的(manual 深读 + 开关打开后的 auto 分块精读)
jq -r '.papers[] | select(.duplicate_of == null and .reading_depth == "chunked")
       | [.citekey, .month, .title] | @tsv' literature_index.json
```

## 查询配方(在本目录下执行)

```bash
# 关键词检索(标题+一句话用处),排除重复条目 —— 最常用
jq -r '.papers[] | select(.duplicate_of == null)
       | select((.title + " " + .one_line) | test("MNAR|missing|缺失"; "i"))
       | [.citekey, .month, .priority_tier, .title] | @tsv' literature_index.json

# 只要 INCLUDE 且有全文精读的高优先级文献
jq -r '.papers[] | select(.duplicate_of == null and .decision == "INCLUDE"
       and .has_full_text_reading and .priority_tier == "high")
       | [.citekey, .note_file] | @tsv' literature_index.json

# 按年份区间 + 按方法论借鉴标记数排序
jq -r '.papers[] | select(.duplicate_of == null and .month >= "2025-01" and .month <= "2025-12")
       | [(.tag_counts.method // 0), .citekey, .title] | @tsv' literature_index.json | sort -rn

# 【句级调取】某主题下所有"可引用证据"(带出处 citekey+section),写作直接取证
jq -r '.papers[] | select(.duplicate_of == null)
       | select((.title + " " + .one_line) | test("MNAR|缺失"; "i"))
       | . as $p | .highlights[] | select(.role == "citable")
       | [$p.citekey, .section, .text] | @tsv' literature_index.json

# 【句级调取】某篇的所有"可反驳靶子"(写 critique 用)
jq -r '.papers[] | select(.citekey == "mesinovic2026Retracted")
       | .highlights[] | select(.role == "refutable") | .text' literature_index.json

# 【句级调取】全库"方法论借鉴"灵感库
jq -r '.papers[] | select(.duplicate_of == null)
       | . as $p | .highlights[] | select(.role == "method")
       | [$p.citekey, .text] | @tsv' literature_index.json

# 定位并阅读某篇的精读原文
grep -nF '[@xu2026Development]' 科研札记_2026-05_全文精读.md   # 拿行号后 Read 该节

# 配书目:直接用全局书目(已全库去重合并,含全文精读+手动精读两系列)
jq -r '.citekey_collisions' literature_index.json               # 必须为 [] 才能安全引用
pandoc draft.md --citeproc --bibliography=all_references.json -o draft.docx
# (按 role 取证 → 写稿 → 出稿的完整写作流:skill `scholar-write`;检索 CLI:scripts/notes_query.py)

# 体检:索引是否落后于札记(数量不一致→先跑 scripts/notes_index.py)
# 注意口径要对齐:months 按文件 stem 键,含 auto+manual 两系列,不能直接对 wc -l 全文精读
ls 科研札记_*_全文精读.md | wc -l; jq '[.months[] | select(.series=="auto")] | length' literature_index.json
```

## ⚠️ citekey 注意事项(重要)

- 多数 citekey 是 **headless 兜底键**(`作者姓+年+标题词`,`citekey_source: "fallback"`),**不是** Zotero/Better BibTeX 权威键。跨系统对账(Zotero、他人书目)一律以 **DOI / dedup_key** 为论文身份,citekey 只在「本索引 + 对应月 references.json」闭包内有效。
- `citekey_collisions` 非空 = 不同论文共用同一键,合并书目时同键**只保留 keeper 那篇**(另一篇引不到);在 XLBDTranslator-dev 仓库跑 `python scripts/notes_index.py --fix-collisions` 自动改键(保最早月不动,后出现者加 b/c 后缀,md+references.json 同步改)后再合并。
- **升级为权威键的路径**(人在时做):按索引 DOI 批量导入 Zotero → BBT 生成正式 citekey → 论文 md 里 `sed` 替换旧键 → bibliography 换 BBT 自动导出。

## 可拷贝到论文项目 CLAUDE.md 的片段

```markdown
## 文献库
精选文献札记库(按月,含全文精读)在:
`/Users/xiaolibird/Documents/GitHub/XLBDTranslator-dev/output/scholar_notes/`
找文献四步法:1) jq 查该目录 literature_index.json(过滤 duplicate_of==null),
或按 role 取证 `python scripts/notes_query.py <关键词> --role citable|refutable|method`;
2) 按 note_file+citekey grep 定位札记原文精读节;3) 正文引用 [@citekey];
4) 书目挂该目录 all_references.json(全库已去重合并;先确认 citekey_collisions 为空)。
要读原文 PDF:`python scripts/locate_pdf.py <citekey|DOI|arXiv号|标题>` 直接给出本地路径
(Zotero→札记索引→Spotlight 三级回退;退出码 1=本地没有,需去下载)。
详细配方读该目录的 AGENTS.md。⚠️ citekey 是兜底键,跨系统对账以 DOI 为准。
```
