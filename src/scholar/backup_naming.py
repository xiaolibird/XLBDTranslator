# -*- coding: utf-8 -*-
"""备份快照的命名/扫描口径——**唯一出处**，三个消费方共用：

  scripts/backup_snapshot.py   守卫（"上次成功"= 目录内文件名时间戳最大值）与轮换
  src/scholar/lint_freshness.py  备份年龄子项（按文件名判龄，不打开任何快照内容）
  docs/backup_restore.md         恢复手册引用的命名规则

口径漂移 = 永久假阳性（同 export_timeline_xlsx 的 sidecar 命名教训），所以解析
逻辑不许二次实现。

设计要点（PRD 对抗审核定稿）：
- 时间戳内嵌文件名且**无冒号**（`YYYYMMDDTHHMMSS`）：冒号在 Finder/iCloud 有转写
  问题；轮换/守卫/freshness 一律按文件名时间戳排序，**不按 mtime**——iCloud 驱逐/
  重下载会刷新 mtime，按 mtime 会误删较新的份。
- iCloud 驱逐占位符 `.<原名>.icloud` 归一化回原名参与计数（驱逐的份仍是存在的份；
  形态基于文档约定，未在本机实测——当前无被驱逐实例）。
- 解析失败（用户手放文件、iCloud 冲突副本 `xxx 2.tar.gz`）与**未来时间戳**一律
  不计入"上次成功"——未来戳若计入会让 6 天守卫永远秒退、备份静默停摆。
- 只扫目录**顶层**：bootstrap/ 等子目录不在守卫/轮换扫描面。
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

SNAPSHOT_PREFIX = "xlbd_scholar_"
TS_FORMAT = "%Y%m%dT%H%M%S"
TAR_SUFFIX = ".tar.gz"
MANIFEST_SUFFIX = ".manifest.json"
BUNDLE_SUFFIX = ".vault.bundle"

# 全名严格匹配（时间戳后必须紧跟已知后缀之一）：iCloud 冲突副本 `xxx 2.tar.gz`
# 前缀能对上但不是我们的产物，必须判异类并 notify——它计入"上次成功"会掩盖真缺份
_TS_RE = re.compile(
    r"^" + re.escape(SNAPSHOT_PREFIX) + r"(\d{8}T\d{6})(?:" +
    "|".join(re.escape(s) for s in (TAR_SUFFIX, MANIFEST_SUFFIX, BUNDLE_SUFFIX)) +
    r")$")


def format_ts(dt: datetime) -> str:
    return dt.strftime(TS_FORMAT)


def parse_snapshot_ts(name: str) -> Optional[datetime]:
    """从文件名（或 `.name.icloud` 占位符名）解析时间戳；解析不了返回 None。"""
    n = name
    if n.startswith(".") and n.endswith(".icloud"):
        n = n[1:-len(".icloud")]
    m = _TS_RE.match(n)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), TS_FORMAT)
    except ValueError:
        return None


def list_snapshots(backup_dir: Path, *, now: Optional[datetime] = None,
                   ) -> Tuple[List[Tuple[datetime, str]], List[str]]:
    """扫目录顶层 → (有效快照 [(ts, 归一化文件名)] 按 ts 升序, 异类文件名清单)。

    异类 = 解析失败或未来戳（now 给定时）；调用方决定是否 notify。
    目录不可达返回 ([], [])——守卫/freshness 各自决定语义，这里不抛。
    """
    valid: List[Tuple[datetime, str]] = []
    weird: List[str] = []
    try:
        entries = list(Path(backup_dir).iterdir())
    except Exception:
        return [], []
    for p in entries:
        if p.is_dir():
            continue
        ts = parse_snapshot_ts(p.name)
        norm = p.name[1:-len(".icloud")] if (
            p.name.startswith(".") and p.name.endswith(".icloud")) else p.name
        if ts is None:
            # 只把"长得像我们的产物却解析不了"的算异类；无关文件（.DS_Store）忽略
            if norm.startswith(SNAPSHOT_PREFIX):
                weird.append(p.name)
            continue
        if now is not None and ts > now:
            weird.append(p.name)
            continue
        valid.append((ts, norm))
    valid.sort()
    return valid, weird


def latest_snapshot_ts(backup_dir: Path, *, now: Optional[datetime] = None,
                       ) -> Optional[datetime]:
    """"上次成功快照"的时间戳（目录内文件名时间戳最大值），没有则 None。

    tar 与 manifest 谁在都算——os.replace 只在自检通过后发生，目录里任何一个
    带合法时间戳的产物都天然是成功份的遗迹。
    """
    valid, _ = list_snapshots(backup_dir, now=now)
    return valid[-1][0] if valid else None
