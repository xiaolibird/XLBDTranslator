# auto 月度札记缺 sidecar：阅读深度量尺永久不可恢复，且写入失败被静默吞掉

日期：2026-09-04
状态：**路线 A 已修（2026-09-04）+ 存量认赔**，见文末。原状态：未修复（修法有两条路待选）
严重度：中——不丢句子，丢的是「这篇读了多少正文」这把尺子，且**无法事后补**（md 不存该信息）
发现者：xiaolibird / Claude（2026-09-03 精读入库会话，查 legacy tag 真相源时顺带撞见）

---

## 现象

`literature_index.json` 里有一批条目，`fulltext_chars` / `fulltext_chars_raw` /
`fulltext_truncated` 三个字段全是 `None`，`reading_depth` 是 `unknown-legacy`——
**即使它们的那次运行日志里明确打过「已截断」告警**。

抽三篇实测（2026-09-03）：

| citekey | month | reading_depth | fulltext_truncated |
|---|---|---|---|
| `mohapatra2024Differentially` | 2023-10 | unknown-legacy | None |
| `liu2025Fedrecon` | 2025-04 | unknown-legacy | None |
| `hu2026Exploring` | 2026-02 | unknown-legacy | None |

---

## 根因

**这些月份根本没有 sidecar 文件。**

```
$ ls output/scholar_notes/科研札记_2023-10_全文精读.index.json
ls: No such file or directory
```

全库实测：

```
全文精读 md 76 份，缺 sidecar 43 份
缺失月份: 2023-01 → 2026-08（43 个月，连续）
有 sidecar 的: 2021-01 → 2026-08（26 个月）
```

数据流是**单向的** md → sidecar/索引，没有反向渲染器：

- `notes.write_notes` 从内存 `PaperSegment` **同时**生成 md 与 sidecar
  （`src/scholar/notes.py:318-341`），sidecar 走 `entry_from_segment`，量尺齐全；
- sidecar 不存在时，`notes_index.build_month_entries` 退回 `parse_note_md` 从 md 解析
  （`src/scholar/notes_index.py:400-432`）；
- 而 **md 不存这三个字段**——`ingest._rehydrate_close_readings` 的 docstring 自己写明
  「量尺字段 md 没存、回读后为默认值」。

于是量尺在 md-only 的月份上**结构性不可恢复**：不是没写对，是压根没有第二个地方存过。

### 为什么 sidecar 会缺：首要嫌疑是静默吞异常

`src/scholar/notes.py:338-341`：

```python
        except Exception as e:
            sidecar_path = None
            logger.warning("  ⚠️ 写索引 sidecar 失败（不影响札记）: {}".format(e))
```

`warning` 级、不中断、不重试、md 照常提交。过夜回填里这一行会淹没在几千行日志中，
**没有任何事后可查的痕迹**（日志已轮转）。「不影响札记」这句注释在当时成立，
但它成立的前提是「sidecar 可以重建」——而按上面的数据流，重建不了。

> 说明：以上是**推断**。也可能是这些月份由某条不传 `emit_index_sidecar` 或早于
> sidecar 特性的路径产出。要坐实需要复原当时的调用链，我没做。但无论哪种成因，
> **量尺不可恢复**这个后果是确定的。

---

## 影响面

- **43 个月的 auto 全文精读条目量尺全空**，占全文精读 md 的 57%。
- 索引里 `fulltext_truncated` 有明确取值的只有 120 条，`None` 432 条，**字段缺失 1933 条**
  （全库 2485）。所以任何「全库只有 18 篇被截断 / 0.75%」的统计**都是下界，不是实数**——
  真实分母只能用「测过的那 410 篇 chunked 深读」，其中截断 17 篇 = 4.1%。
- `vault` 的 front matter 现在会透传 `fulltext_truncated: null`，Dataview 按 `= true`
  过滤时不会把未知混进来（这点是对的），但用户也无从区分「没截断」与「没测过」。

---

## 两条修法（待选）

**路线 A：补 sidecar。** 让 `write_notes` 的 sidecar 写入失败**不再静默**——至少提到
`error` 级并在 `write_notes` 的返回摘要里带一个 `sidecar_ok=False`，让调用方能判定。
存量 43 个月无法补（内存对象早没了），只能标记为「量尺未知」。

**路线 B：把量尺渲进 md。** 加一行机读可解析的元信息（如
`**取证覆盖**: 120000/256992`），让 `parse_note_md` 能捞回来。代价是动 md 格式契约——
`notes._paper_section` / `notes_index` 的行首正则 / `backfill_deepread._render_closeread`
三处必须同步，而 2026-09-03 刚因为往精读节标题里塞后缀踩过一次
（见 `2026-09-04-closeread-heading-contract.md`）。

我倾向 **A + 存量认赔**：B 要动的是这个库里最脆弱的那个契约，而量尺的用途（判断该不该
回原文核实）在存量上已经可以被「reading_depth=unknown-legacy」这个标记覆盖大半。

---

## 复现

```bash
python3 - <<'PY'
import glob, os, re
md = sorted(glob.glob('output/scholar_notes/科研札记_*_全文精读.md'))
miss = [os.path.basename(f) for f in md
        if not os.path.exists(f.replace('.md', '.index.json'))]
print(f"全文精读 md {len(md)} 份，缺 sidecar {len(miss)} 份")
PY
```

---

## 修复（2026-09-04 台账批）

选了本条推荐的 **A + 存量认赔**：`notes.write_notes` 写 sidecar 失败时 `logger.error`（不再是 warning，文案写明「量尺将无法从 md 回读」），
返回值新增两态 `sidecar_ok: bool`（仅 `emit_index_sidecar=True` 时出现）与 `sidecar_error`；md 仍照常提交。调用方可据此判定，此前只能看 `index_sidecar` 键在不在，
把「没要求写」与「要求了但失败」混成一态。路线 B（量尺渲进 md）不做——`2026-09-04-closeread-heading-contract.md` 刚证明那是全库最脆的契约。
存量 43 个月的量尺不可恢复，维持 `reading_depth=unknown-legacy` 标记。
测试：`test/test_bugs_batch_2026_09.py::test_write_notes_reports_sidecar_failure_loudly` / `test_write_notes_sidecar_ok_flag_two_states`。
