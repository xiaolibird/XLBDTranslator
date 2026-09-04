# `fulltext_truncated` 混用两种成因，覆盖率百分比是误导性指标

日期：2026-09-04
状态：**部分修复（2026-09-04 台账批）**——索引字段仍未拆（那要五处联动，另开一轮），
但**日志里已经把两种成因分开说了**：`close_read_segments` 的「精读偏薄」警告现在打
「已截断·总上限截尾，抬 closeread_max_chars 有用」或「已截断·单页上限削页，抬总上限无用」。
这一条就够挡住本文要防的那类误判（按覆盖率排序挑「最该重跑的几篇」）。
守卫：`test/test_closereading.py::test_thin_reading_warning_names_the_truncation_cause`。
严重度：轻——不丢数据，但会让任何基于覆盖率的判断跑偏，包括「该不该重跑」的决策
发现者：xiaolibird / Claude（2026-09-03 会话，三个独立 subagent 中两个各自算出同一结论）

---

## 现象

索引里 `fulltext_truncated=True` 的 17 篇 chunked 深读，按 `fulltext_chars / fulltext_chars_raw`
算出的「覆盖率」跨度从 46% 到 99%。但这个百分比**量的不是同一件事**：

```
   80,138 / 137,025  xie2025Unlocking         ← 单页上限削页
   88,691 /  89,945  liu2022Predicting        ← 单页上限削页
   92,720 /  93,193  athreya2021Prediction    ← 单页上限削页
  110,987 / 125,214  duan2023Fedtda           ← 单页上限削页
  120,000 / 256,992  jin2024Transfer          ← 总上限截尾
  120,000 / 167,539  meng2021Mimicif          ← 总上限截尾
  ...（其余 12 篇 fulltext_chars 恰为 120,000，全是总上限截尾）
```

判据很简单：`fulltext_chars` 恰好等于 `closeread_max_chars` 的，是**总上限截尾**；
小于它的，是 `AUTO_PAGE_MAX_CHARS` 削页。

---

## 根因

两个上限，共用一个布尔标志：

- `src/scholar/closereading.py:34` `AUTO_MAX_CHARS = 40000`
- `src/scholar/closereading.py:35` `AUTO_PAGE_MAX_CHARS = 20000` ← **单页**上限
- `src/scholar/settings.py` `closeread_max_chars` ← **总**上限
- `src/scholar/closereading.py:335`
  `_pdf_text_with_stats(pdf, max_chars=eff_max_chars, page_max_chars=AUTO_PAGE_MAX_CHARS)`

`truncated` 的定义是 `body_chars_raw > body_chars`，两种成因都会让它为真。但后果完全不同：

| 成因 | 丢的是什么 | 提高 `closeread_max_chars` 有用吗 |
|---|---|---|
| 总上限截尾 | 正文**尾部**连续一段 | 有用 |
| 单页上限削页 | 某张**病态长页**的后半（如 76,887 字符的单页矢量图） | **完全无用（空操作）** |

所以「把上限提到 N 就能修好这 17 篇」这个判断，对其中 4 篇是**确定错的**。任何按覆盖率
排序挑「最该重跑的几篇」的做法，都会把 `xie2025Unlocking`（58%）排在很前面，而它恰恰
是提高总上限唯一救不了的那类。

---

## 这条为什么不紧急

2026-09-03 直接查过这 18 篇札记的实际内容：**全部都有「局限与可质疑点」节，每篇 4–11 句**，
没有一篇是空的。连覆盖率 46% 的 `jin2024Transfer` 都有 6 条局限、8 条可反驳句——
它正文第 25 页就结束了，被截掉的 40 页是 Lemma 证明、Table 3–32 数值表和参考文献。

也就是说：**覆盖率低 ≠ 证据缺失**。这个指标量的是「喂进模型的字符占抽取字符的比例」，
而抽取出来的一大半是表格和文献列表。用它推断证据质量，从一开始就是错的。

> 这条记在这里，是为了防止下次有人（包括 agent）看到「覆盖率 46%」就去做重跑决策。
> 2026-09-03 那次我就是这么推的，一路做到给 vault 加告警、给三处证据表加标记，
> 最后被一条命令证伪。教训写在这儿比写在别处有用。

---

## 建议修法（低优先）

`closereading._pdf_text_with_stats` 多返回一个「被单页上限丢弃的字符数」，让调用方能把
`truncated` 拆成 `truncated_by_total_cap` / `truncated_by_page_cap` 两个原因码。

不拆的话，任何「提高上限」的验收都会把那 4 篇算成「没修好」，而它们本来就不该被这个
参数影响。

---

## 复现

```bash
PYTHONPATH=. python3 - <<'PY'
import json
ps = json.load(open('output/scholar_notes/literature_index.json'))['papers']
for x in sorted((p for p in ps if p.get('fulltext_truncated')),
                key=lambda e: e.get('fulltext_chars') or 0):
    fc, raw = x.get('fulltext_chars') or 0, x.get('fulltext_chars_raw') or 0
    cause = '总上限截尾' if fc >= 119000 else '单页上限削页(提高总上限无效)'
    print(f"{fc:>8,} / {raw:>8,}  {cause:<28} {x.get('citekey')}")
PY
```
