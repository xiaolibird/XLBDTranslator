# `digest --month` 会静默整篇覆盖历史月度札记，无存在性检查、无备份、无提示

日期：2026-09-04
状态：**已修复（2026-09-04）**，见文末。原状态：未修复
严重度：高——**会真丢数据**。`output/` 不在 git 内，札记 md 没有第二份拷贝，覆盖即永久丢失
发现者：xiaolibird / Claude（2026-09-03 会话，调查 auto 线 md 真相源时由两个独立 subagent 分别撞见）

---

## 现象

对任意一个**历史月份**跑一次：

```bash
python scholar_main.py digest --month 2023-05 --zotero --close-read
```

`output/scholar_notes/科研札记_2023-05_全文精读.md` 会被**整篇重造**——用这次运行抓到的
论文集合覆盖掉原来那份。原文件里的精读句、句级标记、人工修订全部消失，**没有任何提示、
没有备份、退出码 0**。

---

## 根因

`src/scholar/workflow.py:_step_sync_zotero` 的落盘调用（`workflow.py:1707-1716`）：

```python
        summary = write_notes(
            self.segments,
            citekeys,
            out_dir=proc.notes_dir,
            ...
            filename=note_fname,
        )
```

而 `note_fname` 在上面几行按月份拼出（`workflow.py:1703-1706`）：

```python
        label = self.date_range[0].strftime('%Y-%m') if self.date_range \
            else datetime.now().strftime('%Y-%m-%d')
        note_fname = "科研札记_{}{}".format(label, cr_fname)
```

`--month 2023-05` 会让 `date_range[0]` 落在 2023-05，于是 `note_fname` **恰好等于**
既有历史札记的文件名。`write_notes` 内部是 `_atomic_write(note_path, md_content)`
（`src/scholar/notes.py:344`）——原子替换，**不检查目标是否已存在**。

### 对比：同类路径都有护栏，唯独这条没有

| 入口 | 护栏 | 位置 |
|---|---|---|
| `scripts/backfill_notes.py` | `if note_md.exists() and not args.force: 跳过` | `backfill_notes.py:85` |
| `scripts/read_pdf.py finalize` | 净删除止损闸 + 写盘前备份 | `read_pdf.py:408` |
| `scripts/backfill_deepread.py` | 每次写盘前复制 md+sidecar 到 `.backfill_deepread_backup/<时间戳>/` | 模块 docstring |
| **`workflow._step_sync_zotero`** | **无** | `workflow.py:1707` |

`backfill_deepread` 的 docstring 把道理写得很清楚：

> `output/` 全部在 .gitignore 内（`git ls-files output/scholar_notes` 为空），
> **回滚不能靠 git**

同一个仓库里三条路径都认了这个前提并各自加了护栏，只有这条没有。

---

## 触发条件（不是理论风险）

`scripts/monthly_backfill.sh` 走的就是 `digest --month`。任何人——或未来的某个
agent——为了补某个历史月的漏抓而跑一次，那个月就没了。2026-09-03 的会话里，
我在评估「改 md 会不会被顶回去」时把这条列为三条覆盖路径中**最隐蔽的一条**：
另外两条（`backfill_notes --force`、`backfill_deepread run --apply`）都要显式加参数，
只有这条看起来像个无害的补数据操作。

---

## 建议修法

与 `backfill_notes.py:85` 对齐，加同款存在性检查 + `--force` 逃生门：

```python
if note_path.exists() and not force:
    logger.warning("  ⏭ {} 已存在，跳过（--force 覆盖）".format(note_path.name))
    return None
```

如果覆盖是某些场景的正当需求，那至少复用 `backfill_deepread` 那套写盘前备份——
本仓库已经有现成实现（`backup_files` / `restore` 子命令），不必新写一套。

---

## 复现（**会毁数据，只在副本上做**）

```bash
cp -a output/scholar_notes /tmp/notes_backup_$(date +%s)   # 先备份
wc -l output/scholar_notes/科研札记_2023-05_全文精读.md      # 记下原行数
python scholar_main.py digest --month 2023-05 --zotero --close-read
wc -l output/scholar_notes/科研札记_2023-05_全文精读.md      # 对比
```

---

## 修复（2026-09-04 台账批）

按本条建议修法落地（拒绝 + 逃生门 + 逃生时先备份），护栏两道：

- **开跑前预检**（`src/scholar/cli.py::run_digest`）：`zotero_enabled` ∧ 显式 `--month/--since/--until` 区间 ∧ 目标 md 已存在 ∧ 未加 `--overwrite-notes`
  → 打 `⛔ 目标月度札记已存在` 并 `exit 1`，**不抓邮件、不调 LLM**。判据是**四件套任一存在**（md/references/sidecar/docx，与 `backup_note_files` 同源）——md 缺而 sidecar 在的半态同样会被整篇覆盖，而 sidecar 是阅读深度量尺唯一的无损源。抽成 `cli.preflight_existing_note(workflow, month_range)` 以便测试盯住接线。
- **写盘前守卫**（`workflow._step_sync_zotero`）：同条件 → `error` + `notify` + 返回 `{"skipped_existing": …}`，一字不动，**且排在 Zotero 写库与精读之前**。
  `--overwrite-notes` 时先把旧 md/references/sidecar/docx 复制到 `<notes_dir>/.digest_overwrite_backup/<时间戳>/`（`notes.backup_note_files`），再覆盖。
- 常规 `--days` 跑同日重跑不预检（digest 主产出不该整个中止），只走写盘守卫。护栏不进 `write_notes`：read_pdf regen / backfill `--force` 的整篇重写是设计内行为。
- `monthly_backfill.sh` 现在会在已有月份上被拒绝——这正是本条要的；要重造某月请显式加 `--overwrite-notes`。
- 测试：`test/test_bugs_batch_2026_09.py` W4 节（拒绝零写盘 / 逃生门备份四件套 / 无既有直接写 / CLI 标志 / 路径同源）。
