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

## 语义检索(中文找英文表述、换述同义词用)

`jq`/`notes_query.py` 是精确子串匹配；查不到换述表达（如中文"缺失机制不可忽略"查不到英文
"informative missingness"）时改用 `PYTHONPATH=. python scripts/notes_search.py <查询...>
[--role ...] [--json]`(默认 `--mode hybrid`=向量+BM25 关键词融合)。句级证据同样只覆盖库内
480 篇精读文献,语义命中若标注"该篇无精读句级证据"是真的没有,不是没搜到。

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
- **向量库 `embeddings.sqlite3`**:chunk id 内嵌 citekey(`p:<citekey>`),`chunks` 表另有
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
  照抄 vault sync 的思路,`WatchPaths` 盯住 `literature_index.json`,索引一变就跑增量同步——
  索引是所有改动的公共下游,盯住它就一网打尽。

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
