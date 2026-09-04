# 一次 run 内第二篇的备份会覆盖第一篇的原件——`restore` 恢复出混合态还报告成功

日期：2026-09-04
状态：**已修复（2026-09-04 台账批）**
严重度：**高**——`backfill_deepread` 唯一的回滚手段就是这个备份目录（`output/` 不在 git 内），
而它在**同一次 run 有两篇及以上落在同一份 md** 时就已经不可靠；这是本库最常见的情形
发现者：2026-09-04 台账批第 3 轮对抗审计（压测镜片判 BLOCKER，验伪镜片端到端复现）

---

## 速查

| 项 | 内容 |
|---|---|
| 触发命令 | `PYTHONPATH=. python scripts/backfill_deepread.py run --apply`（同一月份有 ≥2 篇待重读时） |
| 具体位置 | `scripts/backfill_deepread.py` 的 `backup_files()`；`stamp` 在 `cmd_run` 的循环**外**只算一次 |
| 报错情况 | **无**。备份、写盘、回执、退出码全部正常 |
| 影响 | `.backfill_deepread_backup/<stamp>/` 里存的不是这次 run 的原件，而是**第 1 篇改完之后**的 md；`cmd_restore` 恢复出「第 1 篇的新精读 + 其余篇的旧内容」这种混合态，并报告成功 |
| 复现测试 | `test/test_backfill_deepread.py::test_backup_files_never_overwrites_the_original_within_one_run` |

## 根因

```python
stamp = datetime.now().strftime("%Y%m%dT%H%M%S")     # cmd_run 循环外，一次 run 只算一次
...
for entry in targets:                                 # 每篇一轮
    bdir = backup_files(notes_dir, stamp, [md_path, sc_path])   # 同一个 stamp → 同一个目录
```

而 `backup_files` 无条件 `shutil.copy2`：第 2 篇写盘前再备份同一份 md 时，把第 1 篇**已经改完**
的版本盖了上去。原件（这次 run 开始前的状态）就此消失。

`replace_closeread` 是逐篇改同一份 md 的，所以「同一次 run 多篇落在同一月」是常态而非边角。

## 修复

`backup_files` 改为**同名文件已存在就跳过**：第一份进来的才是这次 run 的原件。
`stamp` 保持每次 run 一个（这正是 `restore <stamp>` 的语义——回到那次 run 之前）。

## 生产实证（第 3 轮验伪镜片只读法证，拿账本 `done[ck]["backup"]` 反查磁盘）

这条**已经发生过很多次**，不是理论风险：

| run stamp | 篇数 / md 份数 | 同一 stamp 下被覆盖 ≥1 次的 md |
|---|---|---|
| `20260824T080224` | 97 / 53 | 27（最多 4 次） |
| `20260904T094942`（今天的 `--expand` 批） | 68 / 37 | 16（最多 5 次） |
| `20260828T072017` | 19 / 12 | 5 |
| `20260827T092346` | 20 / 14 | 6 |

按目标集只读估算：默认补深度批 94 篇散在 37 份 md（29 份含 >1 篇）、`--expand` 批 125 篇散在
58 份 md（35 份含 >1 篇）——即一次完整 run 跑完，**约 82%~91% 被改写的篇目失去跑批前备份**。

**降级恢复仍在**：`.backfill_deepread_backup/` 虽被排除在 iCloud 周快照之外，但月度 md 与
sidecar 本体是进快照的（8 周档 + 24 月档）。跑批前的原件可以从最近一次周快照解包捞回，
代价是人工比对、且最多差一周。这也是本条判「高」而不是「阻塞」的原因。

## 顺带记一条（未修）

`cmd_restore` 还原之后**不动账本**：被回退掉的那几篇仍留在 `done` 里，下次 `run` 会按
`skip = set(led["done"])` 跳过它们，且无任何告警。要真正回退一篇，得手工把它从
`backfill_deepread_progress.json` 的 `done` 里摘掉。本批未改（restore 是手动、低频、有人盯着的操作）。

## 备注

`.backfill_deepread_backup/` 被 `scripts/backup_snapshot.py` 排除在 iCloud 周快照之外
（19M，且当前态本身在快照里）。所以这个备份目录只有本机一份——它可靠与否格外重要。
