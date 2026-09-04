# 索引没有 keeper 视图：按 citekey 建字典必被 duplicate 覆盖，验收已两次误判

日期：2026-09-04
状态：**已修复（2026-09-04）**，见文末。原状态：未修复
严重度：中——不损坏数据，但让每一次人工/脚本验收都可能读出反的结论
发现者：xiaolibird / Claude（2026-08 首次踩，2026-09-03 再次踩）

---

## 位置

- `output/scholar_notes/literature_index.json` 的 `papers` 数组：**同一个 citekey 可以有多条**，
  跨月重复的论文各月一条，靠 `duplicate_of` 字段区分（keeper 为 `None`）
- `src/scholar/notes_index.py` 产出该结构
- `src/scholar/embed_store.py:chunks_from_index()` 与 `scripts/backfill_deepread.py:_keepers()`
  各自实现了一遍 keeper 过滤——**但没有对外暴露的公共 helper**

## 报错情况

不报错。表现为验收结论直接反了。任何人写下这行：

```python
idx = {e['citekey']: e for e in index['papers'] if e.get('citekey')}
```

拿到的是**最后一条**，而最后一条经常是 duplicate。duplicate 的
`has_full_text_reading` / `reading_source` / `priority_tier` 都可能与 keeper 不同。

## 两次实测

| 时间 | 现象 | 真相 |
|---|---|---|
| 2026-08 | `bauer2025Sepsis` 被报成「变薄」11 → 6 | 实际 11 → 29，读到的是 duplicate |
| 2026-09-03 | `fink2022Comparing`、`hong2021Research` 被报成「写了但索引没认」 | 两篇的 keeper 都好好的（`unpaywall` / `local-pdf` 全文精读），读到的是 duplicate |

第一次已写进 `docs/decisions/legacy_closeread_backfill_2026-08.md` 的「批量跑暴露的问题」，
**记了但没修**，所以第二次原样再踩。

## 建议修法

在 `src/scholar/notes_index.py`（或 `embed_store`）暴露一个公共函数，
让所有验收/统计代码只能拿到 keeper：

```python
def keepers_by_citekey(index: dict) -> dict[str, dict]:
    """citekey → keeper 条目。duplicate 一律不进——按 citekey 建字典会被它覆盖，
    已两次让验收读出反的结论。"""
```

`backfill_deepread._keepers()` 与 `embed_store.chunks_from_index()` 的两份口径合并到这里，
避免第三次各写各的。

---

## 修复（2026-09-04 台账批）

`src/scholar/notes_index.py` 新增公共 helper：`iter_keepers(index, *, include_retracted=True)`（duplicate / 无 citekey / MISSING-KEY 占位键一律不进）与
`keepers_by_citekey(index)`（citekey → keeper；撞键态取先出现者）。`embed_store.chunks_from_index` 与 `backfill_deepread._keepers` 两份手写过滤改为委托
（向量库口径另剔撤稿并保留日志行）。**写统计/验收代码请一律经 `keepers_by_citekey` 取条目**，别再 `{e["citekey"]: e for e in papers}`。
第 5 轮终审补：`scripts/verify_deepread_batch.py` 也是验收入口（补深度批跑完后的验收），
原先自持一份过滤且**不剔 MISSING-KEY 占位键**，一并改为委托——现在共三处委托。
其余自带过滤的消费方（vault / topics / qa / lint_checks / notes_query）不是验收代码，本批未改。
测试：`test/test_bugs_batch_2026_09.py` W6 节（天真写法读到 duplicate 的对照 + 三处委托）。
