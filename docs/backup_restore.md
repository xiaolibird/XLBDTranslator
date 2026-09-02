# 札记库备份与恢复手册

> 对应 `scripts/backup_snapshot.py` + launchd `com.xlbd.scholar-backup`（周日 20:00 + 登录补跑）。
> 快照在 iCloud Drive：`~/Library/Mobile Documents/com~apple~CloudDocs/XLBDBackups/`

## 一份快照里有什么

| 文件 | 内容 |
|---|---|
| `xlbd_scholar_<YYYYMMDDTHHMMSS>.tar.gz` | `output/scholar_notes/` 全部（**含 manual/ 手动精读、books/**；排除可重建的 `embeddings.sqlite3*`、`_archive/` 等）+ `config/dedup_overrides.json` |
| `…​.vault.bundle` | `~/Documents/ScholarVault` 的完整 git 历史（`git bundle --all`） |
| `…​.manifest.json` | 文件数 / 字节数 / sha256 / 打包时的索引 generated_at / 排除清单 |

**不在快照里、恢复后需重建的**：向量库（`PYTHONPATH=. python scripts/notes_embed.py`，全量约 2-6 分钟）、`output/scholar_pdfs/`（按需重新获取）。

## 恢复步骤

```bash
# 0) 快照若显示为 .icloud 占位符（被"优化存储"驱逐），先整目录取回：
brctl download ~/Library/Mobile\ Documents/com~apple~CloudDocs/XLBDBackups/

# 1) 解包最新快照到一个空目录（永不覆盖原位；内部用 extractall(filter="data")
#    拒绝 ../ 与绝对路径成员）：
PYTHONPATH=. python scripts/backup_snapshot.py --restore-to ~/restore_scholar

# 2) 核对后手动搬回 output/scholar_notes/（先把现场留档再动手）；
#    快照里的 config/dedup_overrides.json 也要搬回 config/——重建索引靠它压制
#    已人工裁决过的去重误报（演练实测压掉 17 对）

# 3) vault 三步——clone 先行：`git bundle verify` 必须在一个 git 仓库内执行，
#    裸机上 verify-first 会直接报 "need a repository"（演练实证）；clone 自身就校验
#    bundle。clone 会留一个指向 bundle 路径的悬空 remote，须删掉（vault 本无 remote）：
git clone <bundle 文件> ~/Documents/ScholarVault
git -C ~/Documents/ScholarVault bundle verify <bundle 文件>
git -C ~/Documents/ScholarVault remote remove origin

# 4) 重建派生物 + 校对（快照可能含未入索引的札记——打包与 ingest 的并发窗口）：
PYTHONPATH=. python scripts/notes_index.py --full
PYTHONPATH=. python scripts/notes_embed.py
PYTHONPATH=. python scripts/lint_notes.py --skip-contradictions --offline
```

## 已知边界（写在前面，灾难日不慌）

- **历史份完整性不承诺**：sha256 自检只在写入当刻做过；iCloud"优化存储"可能驱逐旧份，
  重下载后靠 manifest 的 sha256 核对（`--restore-to` 会自动比）。
- **占位符形态**（`.<原名>.icloud`）基于文档约定，未在本机实测（当前无被驱逐实例）。
- **缺份检出时效**：freshness lint 的备份子项按文件名时间戳判龄（阈值 14 天），
  但它挂在月度 lint 节奏上——备份 job 彻底死掉的最坏检出延迟约 6 周。
  上线第一周请人工核对一次 XLBDBackups/ 目录与 `cron_backup.log`。
- **iCloud 配额**：约 50M/份，8 周滚动 ≈400M + 月度长期份 ≈0.6G/年（保留 24 个月）；
  脚本每次成功后 best-effort 查 `brctl quota`，剩余 <5G 弹通知。
- **手动测过之后 kickstart 会"秒退"**：6 天守卫认目录里的文件名时间戳。要验证
  launchd 语境真能写 iCloud，用 `XLBD_BACKUP_FORCE=1` 强制产出一份。
