---
name: scholar-search
description: 对话式临时检索 arXiv/PubMed 学术文献（不入库、只报结果，命中自动标注是否已在本机札记库）。当用户说"搜文献/查论文/search papers/arXiv 上找/PubMed 搜/有没有关于 X 的新论文"等临时检索需求时使用。区别于月度 digest 流水线（自动筛选入库）与 scholar-notes（只查已入库札记）。
---

> 真相源：本文件在仓库 `docs/skills/scholar-search/SKILL.md`；改完须跑
> `bash scripts/install_skills.sh` 同步到 `~/.claude/skills/`。

# 临时文献检索（arXiv + PubMed，不入库）

复用 XLBDTranslator 流水线的检索客户端，公开 API、无需密钥。命中结果会对照
`literature_index.json` 自动标注「📚 已在札记库（citekey）」。

## 用法

```bash
cd /Users/xiaolibird/Documents/GitHub/XLBDTranslator-dev
PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/search_pubs.py "<检索式>" [选项]
```

选项：
- `--source {arxiv,pubmed,all}` 默认 all（两库都搜、不去重）
- `--max N` 每源最大条数，默认 15，**上限 100**（超过会被收敛到 100；PubMed 走 GET 拼 PMID，护栏防 URL 过长）
- `--days N` 只要最近 N 天；**或** `--from YYYY-MM-DD --to YYYY-MM-DD` 历史区间。两者**互斥**，
  `--from`/`--to` 必须成对出现
- `--abstract` 打印完整摘要（默认截 240 字符）
- `--json` 结构化输出（你要做二次加工/筛选时用这个）。输出是对象
  `{"results": [...], "failures": ["arXiv"|"PubMed"]}`：`results` 是命中数组（`published`
  序列化为 `YYYY-MM-DD` 字符串），`failures` 非空表示该源检索失败、结果**可能不完整**
  （退出码仍为 0，务必检查这个字段，别把半份结果当全部）

退出码 / 失败区分（重要）：
- 有结果或"真的没命中"→ 退出 0（无命中时打印 `没有命中。`）。
- 请求的**全部源检索失败**（网络/代理/限流）→ 退出码 **2**，stderr 打印失败原因；
  这不是空结果，别向用户汇报成"没有相关论文"。
- 部分源失败（如 arXiv 挂了但 PubMed 有结果）→ 退出 0；文本模式结尾行标注哪个源被跳过，
  `--json` 模式则看 `failures` 字段（退出码不体现部分失败）。

## 检索式怎么写（重要——两库都只按发表日期降序，无相关度排序；检索式松了会混进弱相关新文）

两库 API 都**没有**按相关度排序的能力（本脚本也不提供 `--sort`），只能靠收紧检索式提高信噪比。

- **arXiv**：用字段语法收紧。`ti:"graph transformer"`、`abs:"missing not at random"`、
  `cat:cs.LG AND all:"EHR"`。裸词组默认全字段 AND，容易漂。
- **PubMed**：短语加引号 + 字段标签。`"missing not at random"[tiab]`、
  `("MNAR"[tiab] OR "informative missingness"[tiab]) AND "electronic health records"[tiab]`。
  裸词会被 NCBI 自动 term mapping 扩得很宽。
- 用户给的是自然语言主题时，**你负责把它改写成上述紧检索式**再跑；
  首跑结果弱相关时主动收紧重试一次，不要把噪音直接呈给用户。

### 自然语言 → 紧检索式（照做示例）

1. 用户："有没有电子病历里缺失数据不是随机缺失（MNAR）的插补新方法"
   - PubMed：`("missing not at random"[tiab] OR "MNAR"[tiab]) AND ("imputation"[tiab] OR "missing data"[tiab]) AND ("electronic health record*"[tiab] OR "EHR"[tiab])`
   - arXiv：`abs:"missing not at random" AND (abs:imputation OR abs:"missing data") AND cat:stat.ME`
   - 跑：`... --source all --max 15`

2. 用户："图 transformer 做分子性质预测的最新工作"
   - arXiv（此题偏 CS/ML，PubMed 命中少）：`ti:"graph transformer" AND abs:"molecular propert*"`，或放宽 `abs:"graph transformer" AND abs:molecul*`
   - 跑：`'ti:"graph transformer" AND abs:molecul*' --source arxiv --days 180`
   - 若 <3 条，去掉 `ti:` 限定改 `abs:` 再跑一次。

## 跨源去重（`--source all` 时你负责合并）

脚本对 arXiv + PubMed 的结果**不去重**（多个源同时成功时结尾行会标注「未去重」；
单源检索不涉及）。同一篇预印本常同时
命中 arXiv 与 PubMed（发表后）。要给用户一份干净列表时，用 `--json` 拿结构化结果、自己合并：

- **首选按归一化 DOI 合并**：把 DOI 小写、去 `https://doi.org/`、去尾标点后比较（脚本内部
  `_in_library` 同一套规则）。两条 DOI 相同即同一篇，保留信息更全的一条（通常 PubMed 有期刊/
  PMID，arXiv 有 `arxiv_id` 与预印本链接——可合并两边的 id 一并呈现）。
- DOI 都缺时退而用 `arxiv_id`（去 `vN` 版本后缀）比较，再退到标题（小写、非字母数字折空格；
  纯 CJK 标题归一为空串、不可比，此时不合并）。
- 合并只在最终呈现层做；`in_library` 标记以任一条命中为准。
- **📚 标注的匹配键同上面这套三级回退**：DOI → `arxiv_id`（去 `vN`）→ 归一化标题。
  新预印本通常无 DOI，此时按 arxiv_id 精确匹配——所以「无 📚 标注 = 确实不在库」
  对 arXiv 结果同样可信（除非该文在库里只有 PubMed 侧身份且两边都没 DOI，极少见）。

## 汇报格式

给用户：每条 标题（+📚 已收录标记）/ 来源+日期+期刊 / DOI 或 arXiv id / 链接 /
一句话摘要要点；结尾说明命中数。用户想深入某篇时的衔接：
- 「精读这篇」→ 让用户提供 PDF（或 OA 可得时先下载）走 `read-paper` skill；
- 「札记库里有没有相关的」→ 走 `scholar-notes` skill。

判断"库里是否已有类似文献"（比自动的 📚 标注更模糊的相似判断）用语义检索。
**照抄这条命令**（XLBDTranslator-dev 仓库根目录跑，语义命中不要求原文用词一致）：

```bash
PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 \
  scripts/notes_search.py "<标题或摘要句>" --level paper --mode dense --min-score 0.62 --limit 5 --json
```

⚠️ **`--mode dense --min-score 0.62` 两个参数都不能省**，原因是默认模式有个会坑人的性质：
默认 `hybrid` 按 RRF **融合名次**排序，**不是按分数排序**，所以 `--limit N` 截出来的前 N 条
里可能根本没有分最高的那条（实测：某查询全库唯一一条 ≥0.62 的命中排在 hybrid 第 12 位，
`--limit 5` 完全看不到它，照着下结论就会漏报库内强近邻）。`--mode dense` 才按分数降序，
`--min-score` 直接施加阈值、把判据交给脚本而不是靠眼睛看列表。

**两轮用法（推荐）**：先 `--mode dense` 看分数判强弱，若无输出再跑一次 `--mode hybrid`
补召回——实测两种模式的失败集**完全不相交**（75-case bench 上两者都丢出 top-10 的 = 0 条，
并集 @10 = 75/75，而单跑最好的模式只有 70/75）。dense 强在概念换述、hybrid 强在纯关键词
与中文词面命中，任何一个单跑都会漏掉另一个的主场。

（2026-08-23 更新：`--min-score` 在 `hybrid` 下**已经能真正过滤**了——此前它只约束 dense
泳道，BM25 单路命中无门槛地占 `--limit` 名额，导致门槛越高结果越脏；离题探针配
`--min-score 0.95` 曾能返回 108 篇。**但排序仍是 RRF 名次**，所以上面 `--mode dense`
这条建议一个字都不能省：要按分数看排名，只有 dense。）
`--level paper` 默认走瘦+厚双路：既比标题+一句话判词，也比库内回填的原文摘要——
每条结果的 `match_source` 标注命中来自 `title` 还是 `abstract`（摘要命中判"疑似同篇"更可信）。

`--json` 输出结构：顶层 `{total, shown, truncated, query, mode, results}`，
`results[]` 每条含 `citekey / score / score_kind / score_from / match_level / match_source /
year / title / one_line / hits / note_file / note_line`。
**`score_from` 必看**：它说明 `score` 取自篇级（`"paper"`）还是句级（`"highlight"`）。
下面「≥0.62 判库内疑似同篇」那条判据**只对 `score_from == "paper"` 成立**——句级 0.71
只说明"库里有一句相关证据"，不等于"库里已有这篇"。`match_source` 回答的是另一个问题
（这篇有没有句级命中），两者不能互相替代。

**怎么读语义命中（勿误报）**：
- 语义命中 ≠ 已收录——📚（索引精确匹配）才是收录判据，notes_search 只说明库里有**近邻**；
- **命中数本身没有意义**（不加 `--min-score` 时门槛 0.4 极宽，随便一个查询都有几百条），
  只看分；上面那条命令已经把 0.4~0.62 的主题相关噪声滤掉了，**有输出才提，没输出就是没有**；
- 分数怎么读：≥0.62 **且 `score_from == "paper"`** 才值得向用户提示「库内疑似同篇/强近邻」
  （0.62 是 digest 近邻注入的标定线，本链路属**借用**、query 形状与那次标定不完全一致，
  见 thresholds.py 的记账）；
  **≥0.78 且 citekey 与某条结果的 📚 标注一致，多半是这篇自己**（已收录论文会自命中
  ~0.8），那不是"新发现的近邻"，先回头看该条有没有 📚；
- 句级证据检索是另一套 0.55 口径；三条链路阈值集中在仓库 `src/scholar/thresholds.py`。

## 边界

- 本 skill **不写库**：不动 seen、不进索引、不触发筛选/翻译流水线。
- Gmail（Scholar Alert 邮件）不在本 skill 范围——那是月度流水线的输入源；
  临时查邮件用会话里的 Gmail 连接器。
- 检索失败常见原因：代理把 IPv6 黑洞（客户端已强制 IPv4 规避）；NCBI 限流（脚本已带礼貌间隔）。
