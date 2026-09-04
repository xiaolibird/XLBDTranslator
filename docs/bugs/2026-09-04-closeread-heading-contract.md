# 精读节标题是机读锚点：往里加后缀会静默丢掉 reading_source（已踩过一次）

日期：2026-09-04
状态：**未发生于主干，已防复发（2026-09-04 补齐其余契约对的往返测试）**
严重度：中——静默丢字段，无报错、无日志，md 肉眼看不出异常
发现者：xiaolibird / Claude（2026-09-03 会话；由对抗审 subagent 实测复现）

---

## 事情经过

为了标注「这篇正文被截断」，我把精读节标题从两态改成三态：

```
### 全文精读 · 来源 `unpaywall`                       ← 原来
### 全文精读（正文截断，覆盖 47%） · 来源 `unpaywall`    ← 改成
```

看起来只是多了个括号。实测结果：

```python
from src.scholar.notes_index import _CLOSEREAD_RE
for l in ["### 全文精读 · 来源 `unpaywall`",
          "### 全文精读（正文截断，覆盖 47%） · 来源 `unpaywall`"]:
    m = _CLOSEREAD_RE.match(l)
    print(repr(m.group(1)), repr(m.group(2)))

# '全文精读' 'unpaywall'
# '全文精读' None          ← reading_source 没了
```

---

## 根因

`src/scholar/_citekey_utils.py` 的正则形如：

```
^### (全文精读|精读（仅摘要降级）)(?: · 来源 `(.+?)`)?
```

第一组匹配完「全文精读」后，紧接着期望 ` · 来源`。遇到 `（` 时，**「· 来源」那一组是可选的，
于是它匹配空**——`match` 仍然成功、`group(1)` 仍然正确，只有 `group(2)` 静默变 `None`。

不是抛异常，不是 `match` 失败，是**部分成功**。这也是它危险的原因：任何「解析成功就当没问题」
的检查都拦不住。

## 影响面（如果当时合入）

- 库里 43 份 md **没有 sidecar**（见 `2026-09-04-auto-sidecar-missing.md`），索引只能走
  `parse_note_md`——这条路上 `reading_source` 会全丢；
- 而 `reading_source` 是 `export_timeline_xlsx` 区分「手动全文精读 / 脚本全文通读」、
  vault front matter、索引统计三处的判据；
- 最讽刺的是：**恰恰是被截断的那批**（也就是这次改动想保护的对象）会丢字段。
  为了标注一个信息，弄丢了另一个。

---

## 结论与现状

改动已撤回。**精读节标题必须保持两态**，任何附加信息另起一行，不要碰这个锚点。
`src/scholar/notes.py` 的渲染处已留注释说明。

新增回归测试 `test/test_closeread_chunk_coupling.py::test_closeread_heading_stays_two_state_and_keeps_source`
——它断言两态标题在 `_CLOSEREAD_RE` 下 `group(2)` 必须取回 `reading_source`。

> 这个文件同时钉住 `closeread_max_chars` / `closeread_max_chunks` / `_SYNTH_NOTES_BUDGET`
> 三个跨文件耦合常量，与本条 bug 是同一批产出。

---

## 一般性教训

这个仓库里有若干「渲染函数 ↔ 解析正则」的成对契约，靠注释维系：

- `notes._paper_section` ↔ `notes_index._CLOSEREAD_RE` / `_SECTION_RE` / `_TAG_LINE_RE`
- `notes._book_line` ↔ `notes_index._parse_book_line`
- `_citekey_utils.render_tag_line` ↔ `_citekey_utils.TAG_LINE_RE`
- `backfill_deepread._render_closeread` ↔ 上面这一整套（docstring 明写「改一处同步两处」）

**注释不会失败，测试会。** 建议给每对契约都补一条往返测试（渲染 → 解析 → 字段逐一相等），
成本很低。目前只有精读节标题这一对有。

---

## 修复（2026-09-04 台账批）

按本条「一般性教训」给另两对渲染↔解析契约补了往返测试（渲染 → 解析 → 字段逐一相等）：
`render_tag_line ↔ TAG_LINE_RE`（全部 tag × 有/无页码锚，含含〔〕的句子）、`notes._book_line ↔ notes_index._parse_book_line`（chapter 与 book 两种条目）。
见 `test/test_bugs_batch_2026_09.py` W11 节。`backfill_deepread._render_closeread ↔ 解析` 那一对由既有 `test_backfill_deepread.py` 覆盖。
