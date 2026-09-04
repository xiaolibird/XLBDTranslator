# Crossref 的 article-number 被丢弃：e-locator 期刊的书目条目没有定位号

日期：2026-09-04
状态：**代码已修（2026-09-04）；存量 36 条回填待跑**，见文末。原状态：未修
严重度：中——不影响检索与入库，影响**投稿书目的正确性**（36 条已确认可补回）
发现者：xiaolibird / Claude（2026-09-03 付费墙三篇补入会话）

---

## 现象

`ladobaleato2025Testing`（Stat Med 2025;44:e10308）入库后，`all_references.json` 里的
CSL 条目**没有 `page` 也没有任何定位号**：

```json
{"id":"ladobaleato2025Testing","type":"article-journal",
 "container-title":"Statistics in Medicine","volume":"44","issue":"3-4",
 "DOI":"10.1002/sim.10308","issued":{"date-parts":[[2025,1,24]]}}
```

pandoc 渲染出来就是「*Statistics in Medicine*, 44(3–4)」——读者拿不到 e10308，
按刊名卷期翻不到文章。

## 根因（一行）

`src/scholar/crossref.py:89` 只读 Crossref 的 `page` 字段：

```python
"pages": (item.get("page") or "").strip() or None,
```

而现代期刊（Nature 系、Frontiers、BMC、Sci Rep、Wiley 的 e-locator 刊…）**不发页码**，
只给 `article-number`。实测该 DOI 的 Crossref 应答：

```json
{"volume":"44","issue":"3-4","page":null,"article-number":"e10308"}
```

`page` 为空 → `PaperMetadata.pages` 为 None（`src/scholar/schema.py:68`）→
CSL 构造（`src/scholar/_citekey_utils.py:421`）自然不写 `page`。

## 影响面（实测，非估计）

对当前 `all_references.json`（2399 条）全量扫描 + 逐条回查 Crossref：

| 口径 | 条数 |
|---|---|
| article-journal 类型 | 2305 |
| 其中无 `page` | 699 |
| 其中同时有 DOI 与刊名（可回查的候选） | 123 |
| **Crossref 确实给了 `article-number`、被我们丢掉的** | **36** |
| Crossref 也没有（真无定位号，多为预印本/会议） | 83 |
| 回查失败（超时/404） | 4 |

命中的刊都是主流：Scientific Reports、Nature Communications、npj Digital Medicine、
Communications Medicine、BMC Med Res Methodol、Nucleic Acids Research、Cancer Medicine、
Intensive Care Medicine Experimental、Frontiers in Medicine、Journal of Intensive Care…

样例：`adekkanattu2023Prediction → 294`、`chen2024Congenital → 976`、
`kim2026Prediction → 311`、`henriksen2025Lung → e70458`、`duan2026Discovering → gkag386`。

## 建议修法

最小改动在 `crossref.py:89`：

```python
"pages": (item.get("page") or item.get("article-number") or "").strip() or None,
```

需要一并想清楚的两点：

1. **CSL 语义**：把 article-number 塞进 `page` 是 CSL 1.0.2 允许的常见做法（多数样式会
   直接输出），但更规范的是 CSL 的 `number` 字段。若下游 csl 样式（`config/*.csl`）
   对 `number` 不友好，就仍走 `page`——请以实际出稿样式为准，别只按规范选。
2. **回填存量 36 条**：改完 `crossref.py` 只对**新入库**生效。存量要么跑
   `scripts/repair_references.py --month <月> --force` 逐月重建 CSL，要么写个一次性
   回填脚本按 DOI 补 `page`。后者更省——36 条散在十几个月桶里，逐月 `--force`
   会顺带重算别的字段。

## 验证方式

修完后对上面那 36 个 citekey 重新生成书目，确认 `page` 非空；重点看
`ladobaleato2025Testing` 应为 `e10308`、`duan2026Discovering` 应为 `gkag386`
（后者证明这个字段不一定是纯数字，别用 int 校验）。

---

## 修复（2026-09-04 台账批）

`src/scholar/crossref.py::parse_crossref_work`：`pages` 取 `page`，为空再取 `article-number`（不做 int 校验——`gkag386`/`e10308` 都不是纯数字）。
`scripts/repair_references.py` 走同一个归一化 dict，随之生效。走 CSL `page` 而非 `number`：与本条第 1 点一致，以实际出稿样式为准。
测试：`test/test_crossref.py`（新增 article-number 用例）。

**存量回填未在本批执行**（改的是生产 references.json，且需联网逐条回查 Crossref）。建议命令，逐月对涉及的 36 个 citekey 所在月跑：
```bash
PYTHONPATH=. python3.12 scripts/repair_references.py --month <YYYY-MM> --force --email <邮箱> --dry-run   # 先看
PYTHONPATH=. python3.12 scripts/repair_references.py --month <YYYY-MM> --force --email <邮箱>             # 再落盘
PYTHONPATH=. python3.12 scripts/notes_index.py                                                          # 重建全局书目
```
验证 `ladobaleato2025Testing → e10308`、`duan2026Discovering → gkag386`。
