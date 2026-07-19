---
name: scholar-search
description: 对话式临时检索 arXiv/PubMed 学术文献（不入库、只报结果，命中自动标注是否已在本机札记库）。当用户说"搜文献/查论文/search papers/arXiv 上找/PubMed 搜/有没有关于 X 的新论文"等临时检索需求时使用。区别于月度 digest 流水线（自动筛选入库）与 scholar-notes（只查已入库札记）。
---

# 临时文献检索（arXiv + PubMed，不入库）

复用 XLBDTranslator 流水线的检索客户端，公开 API、无需密钥。命中结果会对照
`literature_index.json` 自动标注「📚 已在札记库（citekey）」。

## 用法

```bash
cd /Users/xiaolibird/Documents/GitHub/XLBDTranslator-dev
PYTHONPATH=. python scripts/search_pubs.py "<检索式>" [选项]
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

脚本对 arXiv + PubMed 的结果**不去重**（结尾行会标注「未去重」）。同一篇预印本常同时
命中 arXiv 与 PubMed（发表后）。要给用户一份干净列表时，用 `--json` 拿结构化结果、自己合并：

- **首选按归一化 DOI 合并**：把 DOI 小写、去 `https://doi.org/`、去尾标点后比较（脚本内部
  `_in_library` 同一套规则）。两条 DOI 相同即同一篇，保留信息更全的一条（通常 PubMed 有期刊/
  PMID，arXiv 有 `arxiv_id` 与预印本链接——可合并两边的 id 一并呈现）。
- DOI 都缺时退而用 `arxiv_id`（去 `vN` 版本后缀）比较，再退到标题（小写、非字母数字折空格；
  纯 CJK 标题归一为空串、不可比，此时不合并）。
- 合并只在最终呈现层做；`in_library` 标记以任一条命中为准。

## 汇报格式

给用户：每条 标题（+📚 已收录标记）/ 来源+日期+期刊 / DOI 或 arXiv id / 链接 /
一句话摘要要点；结尾说明命中数。用户想深入某篇时的衔接：
- 「精读这篇」→ 让用户提供 PDF（或 OA 可得时先下载）走 `read-paper` skill；
- 「札记库里有没有相关的」→ 走 `scholar-notes` skill。

## 边界

- 本 skill **不写库**：不动 seen、不进索引、不触发筛选/翻译流水线。
- Gmail（Scholar Alert 邮件）不在本 skill 范围——那是月度流水线的输入源；
  临时查邮件用会话里的 Gmail 连接器。
- 检索失败常见原因：代理把 IPv6 黑洞（客户端已强制 IPv4 规避）；NCBI 限流（脚本已带礼貌间隔）。
