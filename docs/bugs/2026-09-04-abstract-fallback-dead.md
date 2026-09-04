# 摘要降级通路对存量脚本实质失效：abstracts.json 只有 3 篇，而摘要明明存在 md 里

日期：2026-09-04
状态：**已修复（2026-09-04）**，见文末。**注意本条修的是「产出」，收货由 `--accept-abstract` 控制、默认关**。原状态：未修复
严重度：中——让「全文拿不到就读摘要」这条既有降级路径在存量重读时形同虚设
发现者：xiaolibird / Claude（2026-09-03 索引层升格会话）

---

## 位置

- `scripts/backfill_deepread.py:segment_from_entry()`——摘要来源写死为
  `notes_dir/abstracts.json`，查不到就填空串
- `output/scholar_notes/abstracts.json`——**全库只有 3 条**
- `src/scholar/closereading.py:226` `close_read()`——`body_text` 为空时打
  `⚠️ 无全文也无摘要，跳过精读` 并返回 None
- 而每月札记 md 里，每篇论文节下都有 `### 摘要` 一节，内容俱在
  （例：`output/scholar_notes/科研札记_2024-03_全文精读.md` 里 `cai2026Contrastive` 那节）

## 报错情况

```
⚠️ 无全文也无摘要，跳过精读(27): Domain Adaptation with Invariant Representation Le
  全文精读 0/1 篇（其中真全文 0，top-1）
   ❌ 重读未产出，跳过（不写盘）
```

日志说的「也无摘要」不成立——摘要在 md 里躺着，只是没人去读。

## 根因

`abstracts.json` 是入库时另存的旁路文件，历史上只对极少数篇目写过（现存 3 条）。
`backfill_deepread` 这类**对存量做手术**的脚本沿用了它当唯一摘要源，
却没有走 md 回读（`notes_index` 的 md-parse 分支本来就会解析 `### 摘要`）。

## 影响面

2026-09-03 那批 148 个失败篇，**没有一篇走成摘要降级**——全是零产出。
其中相当一部分（B 档 76 篇）是闭源、全文确实拿不到的，
本可以退而求其次留一份摘要级精读，结果连这个都没有。

## 与另一条设计决策的张力（必须一起想）

`scripts/backfill_deepread.py:cmd_run` 里我刚加了一道闸：**expand 批拒收摘要级产出**。
理由是那批的目标就是「升格成全文精读」，写一份摘要级进去会把篇目钉成 `done`
（下次跳过）却仍是索引层（`has_full_text_reading` 恒假），两头不着。

所以这两件事的正确组合大概是：

- 补深度批（`unknown-legacy`）：**该**能读到 md 摘要，全文抓不到时至少不退化成零；
- expand 批（索引层升格）：仍拒收摘要级，进待下载清单等 PDF。

也就是说修法不是「让摘要到处可用」，而是**把摘要源修对，再让两条批次各自决定收不收**。

## 建议修法

`segment_from_entry` 增加回退：`abstracts.json` 查不到时，按 `note_file` + `note_line`
从 md 的该篇节里回读 `### 摘要`。定位逻辑与 `backfill_deepread._find_paper_span()` 同源，
可直接复用，不必新写解析。

---

## 修复（2026-09-04 台账批）

`scripts/backfill_deepread.py`：新增 `abstract_from_note_md(notes_dir, entry)`——按 `note_file` + `note_line` 用既有 `_find_paper_span` 定位该篇，
读 `### 摘要` 节到下一标题/精读分节/篇末 `---` 为止；`*摘要暂无*` 占位与定位失败一律返回空串。`segment_from_entry(..., notes_dir=None)` 新增可选参数：
`abstracts.json` 查不到时回读 md；`cmd_run` 传入 `notes_dir` 并打印「摘要取自该篇 md 节」。不传 `notes_dir` = 旧行为（其它调用方零改动）。

### 与「谁来收货」的组合（第 4 轮审计更正）

本条最初写的「补深度批现在能降级成摘要级」是**假的**：补深度批的目标集在 `select_targets` 里
本就限定 `has_full_text_reading=True`，摘要级 100% 会被那道覆盖闸拒收——修复的净效果只剩
**每篇白烧一次 LLM**（`close_read` 本来在 body 为空时直接 return None，一次都不调）。
台账自述该批 148/172 篇抓不到全文，而额度耗尽正是同一批里另一条缺陷的现实故障。

现在的口径：

- **默认两批都不收摘要级**，且**连摘要都不供给**（`abstract_source_dir` 返回 None →
  `segment_from_entry` 不回读 md → `close_read` 立刻 return None，零 LLM）。
  回读 md 摘要这件事本身仍然有用：它把「⚠️ 无全文也无摘要」这个假报错换成了
  账本里如实的 `abstract_only` + `had_fulltext`。
- **`--accept-abstract`** 才既供给又收货，且只放开「本来就没有全文精读」的篇目，
  **永远不放开覆盖既有全文精读**（覆盖会把 `has_full_text_reading` 从真翻成假、回执还打 ✅）。

测试：`test_abstract_from_note_md_reads_the_right_paper`（产出）、
`test_abstract_source_dir_is_gated_by_accept_abstract`（别白读）、
`test_abstract_level_never_overwrites_an_existing_fulltext_reading` 与
`test_abstract_only_gate_predicate`（收货闸的四格真值表）。
