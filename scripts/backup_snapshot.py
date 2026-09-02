# -*- coding: utf-8 -*-
"""不可再生核心的每周快照 → iCloud Drive（launchd 周日 20:00 + RunAtLoad 补跑）。

  python scripts/backup_snapshot.py                 # 守卫→打包→自检→入 iCloud→轮换
  python scripts/backup_snapshot.py --force         # 绕过 6 天守卫（验收/手动补份用）
  python scripts/backup_snapshot.py --restore-to DIR  # 解包最新快照到 DIR（永不覆盖原位）

收集范围（payload = A − 排除 + 附加）：
  A = output/scholar_notes/ 全部（含 manual/ 手动精读、books/、根下散页、rendered/）
  排除 = embeddings.sqlite3*（glob 连 -wal/-shm/.lock——stale wal 恢复时会回放毒害
         重建的库）、_archive/、.backfill_deepread_backup/、.pytest_cache/
  附加 = config/dedup_overrides.json + vault 的 git bundle --all（含全部提交历史）
  不收 = output/scholar_pdfs/（1.1G 可重新获取）、translations/ 等过程产物

工程决策（PRD 五轮对抗审核定稿，别"顺手优化"掉）：
- **执行顺序**：守卫读目录 → staging 打包+自检 → os.replace 入 iCloud → **成功后**
  才轮换/配额哨兵；replace 失败跳过后续、只发一条告警（不产生派生告警雨）。
- **staging 在 iCloud 之外**（~/Library/Caches/xlbd-backup，实测与 CloudDocs 同一
  APFS 卷 → os.replace 原子）：直接在 Mobile Documents 里写大 tar，iCloud 会对
  半截文件开始上传。staging 若挪到外置盘（跨卷），退回 copy+fsync+rename。
- **并发一致性是主防线**：launchd 唤醒会把睡眠期间错过的日历事件**坍缩补跑**——
  周日晚合盖、周一 09:05 开盖时，本 job 与 digest/ingest 同刻开火是常态而非边角，
  错峰保证不了。打包前后各读一次 索引 generated_at + topics_mtime，不一致 sleep
  重试，重试耗尽才告警。
- **6 天守卫**："上次成功" = XLBDBackups/ 顶层文件名时间戳最大值（backup_naming
  单一出处；不用本地状态文件——换机/清缓存会与目录真相分叉）。判据：
  CloudDocs 本体缺失（iCloud 登出/异常）→ 告警不 mkdir（否则备份静默落进一个
  不再同步的普通目录）；仅 XLBDBackups/ 缺失 → mkdir 视为首跑；目录在而 glob 空
  → 首跑放行；解析失败/未来戳文件不计入并 notify。
- **轮换**按文件名时间戳（不按 mtime）：保留最近 KEEP_RECENT 份 + 每自然月首份
  （上限 KEEP_MONTHLY_MONTHS 个月）；同时间戳的 tar/manifest/bundle 按组配对删除，
  孤儿组一并进将删清单；删除前把清单写日志。
- **restore 用 extractall(filter="data")**：tar 从 iCloud 取回不是完全可信介质，
  `../`/绝对路径/符号链接成员必须被拒（Python 3.12 默认 fully_trusted 不拒）。
- 配额哨兵 best-effort `brctl quota`：iCloud 配额满时本地写入照样"成功"、只是云端
  不传，"写失败才告警"抓不到这种失败。命令缺失（非 mac）静默跳过。

体量账（2026-09 实测）：payload gzip ≈31M + vault bundle ≈19M ≈ 50M/份；
8 周滚动 ≈400M；月度长期份 ≈0.6G/年；iCloud 配额实测剩 38.7G。
"""
import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.backup_naming import (                                   # noqa: E402
    BUNDLE_SUFFIX, MANIFEST_SUFFIX, SNAPSHOT_PREFIX, TAR_SUFFIX,
    format_ts, list_snapshots, parse_snapshot_ts,
)
from src.scholar.paths import repo_path                                   # noqa: E402
from src.utils.logger import get_logger                                   # noqa: E402
from src.utils.notify import notify                                       # noqa: E402

logger = get_logger("backup_snapshot")

# 目的地常量的唯一出处（lint_notes CLI 从这里 import 传给 freshness 备份子项）
CLOUDDOCS_ROOT = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
BACKUP_ROOT = CLOUDDOCS_ROOT / "XLBDBackups"
STAGING_DEFAULT = Path.home() / "Library" / "Caches" / "xlbd-backup"

GUARD_DAYS = 6
KEEP_RECENT = 8
KEEP_MONTHLY_MONTHS = 24
QUOTA_MIN_BYTES = 5 * 1024**3          # brctl 剩余低于 5G 报警（~100 份的余量）
CONSISTENCY_RETRIES = 3

EXCLUDE_PATTERNS = ("embeddings.sqlite3*", "_archive", "_archive/*",
                    ".backfill_deepread_backup", ".backfill_deepread_backup/*",
                    ".pytest_cache", ".pytest_cache/*")


def _excluded(rel: str) -> bool:
    return any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(Path(rel).name, pat)
               for pat in EXCLUDE_PATTERNS)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _consistency_stamp(notes_dir: Path):
    """(索引 generated_at, topics_mtime)——打包前后各读一次，抓跨文件中间态。"""
    from src.scholar.embed_store import read_index_generated_at
    from src.scholar.lint_freshness import topics_mtime
    try:
        stamp = read_index_generated_at(notes_dir / "literature_index.json")
    except Exception:
        stamp = None
    return stamp, topics_mtime(notes_dir)


def build_tar(notes_dir: Path, dedup_overrides: Path, out_tar: Path) -> int:
    """打 payload tar（staging 内）。返回成员数。"""
    n = 0
    skipped_links = []

    def _filter(ti: tarfile.TarInfo):
        nonlocal n
        rel = ti.name.split("scholar_notes/", 1)[-1] if "scholar_notes/" in ti.name else ""
        if rel and _excluded(rel):
            return None
        # 库外符号链接是潜伏弹（审计实证）：打包时静默成功、自检绿，restore 的
        # data filter 却会对绝对/逃逸链接抛错 → 整份快照一个字节都恢复不出来。
        # 打包侧就排除并告警，别把雷埋进每一份快照。库内相对链接保留（恢复安全）。
        if ti.issym() and (ti.linkname.startswith("/") or ".." in ti.linkname):
            skipped_links.append(ti.name)
            return None
        n += 1
        return ti

    with tarfile.open(out_tar, "w:gz") as tar:
        tar.add(notes_dir, arcname="scholar_notes", filter=_filter)
        if dedup_overrides.exists():
            tar.add(dedup_overrides, arcname="config/dedup_overrides.json")
            n += 1
    if skipped_links:
        logger.warning("已排除 {} 个可能逃逸的符号链接成员（绝对/含..，restore 会拒收）：{}"
                       .format(len(skipped_links), "、".join(skipped_links[:5])))
    return n


def build_bundle(vault_dir: Path, out_bundle: Path) -> bool:
    """vault git bundle --all；失败重试一次（同窗 vault job 可能正在 commit，refs
    枚举中途移动会让首次失败——对象不可变，重试即好）。"""
    for attempt in (1, 2):
        try:
            p = subprocess.run(["git", "bundle", "create", str(out_bundle), "--all"],
                               cwd=str(vault_dir), capture_output=True, text=True,
                               timeout=300)
            if p.returncode == 0:
                v = subprocess.run(["git", "bundle", "verify", str(out_bundle)],
                                   cwd=str(vault_dir), capture_output=True, text=True,
                                   timeout=120)
                if v.returncode == 0:
                    return True
            logger.warning("vault bundle 第 {} 次失败：{}".format(
                attempt, (p.stderr or "")[:200]))
        except Exception as e:
            logger.warning("vault bundle 第 {} 次异常：{}".format(attempt, e))
        time.sleep(2)
    return False


def _move_in(src: Path, dest_dir: Path) -> Path:
    """staging → 备份目录。同卷 os.replace 原子；跨卷（外置盘场景）退回 copy+rename。"""
    dest = dest_dir / src.name
    try:
        os.replace(src, dest)
    except OSError:
        tmp = dest_dir / (".incoming_" + src.name)
        shutil.copy2(src, tmp)
        with open(tmp, "rb") as f:          # fsync：跨卷 copy 后确保落盘再 rename
            os.fsync(f.fileno())
        os.replace(tmp, dest)
        src.unlink(missing_ok=True)
    return dest


def rotate(backup_dir: Path, *, now: datetime, keep_recent: int = KEEP_RECENT,
           keep_monthly_months: int = KEEP_MONTHLY_MONTHS) -> list:
    """按时间戳分组轮换。返回已删除文件名清单（先写日志再删）。"""
    valid, _weird = list_snapshots(backup_dir, now=now)
    ts_all = sorted({ts for ts, _ in valid})
    keep = set(ts_all[-keep_recent:])
    monthly_first = {}
    for ts in ts_all:                       # 升序 → 每月第一份天然先到
        monthly_first.setdefault((ts.year, ts.month), ts)
    for (y, m), ts in monthly_first.items():
        if (now.year - y) * 12 + (now.month - m) <= keep_monthly_months:
            keep.add(ts)
    doomed = []
    for p in sorted(backup_dir.iterdir()):
        if p.is_dir():
            continue
        ts = parse_snapshot_ts(p.name)
        if ts is None or ts in keep or (ts > now):
            continue                       # 异类/未来戳不动（已在守卫处 notify 过）
        doomed.append(p)
    if doomed:
        logger.warning("轮换将删除 {} 个文件：{}".format(
            len(doomed), "、".join(p.name for p in doomed)))
        for p in doomed:
            try:
                p.unlink()
            except Exception as e:
                logger.warning("  删除失败（留待下轮）：{}（{}）".format(p.name, e))
    return [p.name for p in doomed]


def quota_sentinel():
    """best-effort：iCloud 剩余配额过低时 notify。命令缺失/失败静默跳过。"""
    try:
        p = subprocess.run(["brctl", "quota"], capture_output=True, text=True,
                           timeout=30)
        import re
        m = re.search(r"(\d+)\s+bytes of quota remaining", p.stdout or "")
        if m and int(m.group(1)) < QUOTA_MIN_BYTES:
            notify("Scholar 备份", "iCloud 剩余配额不足 {:.1f}G，备份可能停传".format(
                int(m.group(1)) / 1024**3))
    except Exception:
        pass


def restore(backup_dir: Path, target: Path, *, now: datetime = None) -> int:
    """解包最新快照到 target（必须不存在或为空目录——永不覆盖原位）。"""
    # now 必给：不给则未来戳文件会被当"最新快照"选中——时钟漂移/手放的未来戳文件
    # 会永久遮蔽所有真实快照（审计实证），且它没有 manifest、sha 核对整段被跳过
    valid, _ = list_snapshots(backup_dir, now=now or datetime.now())
    tars = [(ts, n) for ts, n in valid if n.endswith(TAR_SUFFIX)]
    if not tars:
        print("备份目录里没有可恢复的快照：{}".format(backup_dir), file=sys.stderr)
        return 2
    ts, name = tars[-1]
    tar_path = backup_dir / name
    if not tar_path.exists():
        print("最新快照是 iCloud 驱逐占位符，先整目录下载：brctl download '{}'".format(
            backup_dir), file=sys.stderr)
        return 2
    target = Path(target)
    if target.exists() and not target.is_dir():
        print("目标已存在且不是目录，拒绝解包：{}".format(target), file=sys.stderr)
        return 2
    if target.is_symlink() and not target.exists():
        print("目标是悬空符号链接，拒绝解包：{}".format(target), file=sys.stderr)
        return 2
    if target.is_dir() and any(target.iterdir()):
        print("目标目录非空，拒绝解包（永不覆盖）：{}".format(target), file=sys.stderr)
        return 2
    pre_existed = target.is_dir()      # 用户自建的目录：失败清理只清内容、不删本体
    target.mkdir(parents=True, exist_ok=True)
    manifest = backup_dir / (name[:-len(TAR_SUFFIX)] + MANIFEST_SUFFIX)
    if manifest.exists():
        try:
            recorded = json.loads(manifest.read_text(encoding="utf-8"))
            actual = _sha256(tar_path)
            if recorded.get("sha256_tar") and recorded["sha256_tar"] != actual:
                print("⚠️ tar 的 sha256 与 manifest 不符（云端损坏？），仍尝试解包但请核对",
                      file=sys.stderr)
        except Exception:
            pass
    elif (backup_dir / ("." + manifest.name + ".icloud")).exists():
        # docs 承诺"restore 会自动比 sha"——manifest 被驱逐时静默跳过等于违约（审计实证）
        print("⚠️ manifest 被 iCloud 驱逐，sha256 未核对。建议先：brctl download '{}'".format(
            backup_dir), file=sys.stderr)
    else:
        print("⚠️ 无 manifest，sha256 未核对", file=sys.stderr)
    try:
        with tarfile.open(tar_path, "r:gz") as tar:
            # data filter：拒 ../、绝对路径、符号链接逃逸（iCloud 取回≠完全可信）
            tar.extractall(target, filter="data")
    except Exception as exc:
        # 清掉半截产物再退——半恢复状态比没恢复更危险（人会误以为数据齐了）。
        # 清理要点（审计实证的两个坑）：resolve 穿透符号链接目标（rmtree 对 symlink
        # 静默拒删、半截产物残留）；用户自建目录只清内容不删本体（"永不覆盖原位"
        # 的精神是只动自己造的东西）。
        if isinstance(exc, (tarfile.LinkOutsideDestinationError,
                            tarfile.AbsoluteLinkError)):
            reason = "包内含库外符号链接成员（打包侧旧版未过滤），不是介质损坏——重取回无用"
        else:
            reason = "tar 损坏？{}：{}。可试更早的快照，或先 brctl download 重新取回本份".format(
                type(exc).__name__, exc)
        real = target.resolve()
        if pre_existed:
            for child in real.iterdir():
                (shutil.rmtree(child, ignore_errors=True) if child.is_dir()
                 else child.unlink(missing_ok=True))
        else:
            shutil.rmtree(real, ignore_errors=True)
        print("❌ 解包失败（{}），已清理半截产物".format(reason), file=sys.stderr)
        return 2
    bundle = backup_dir / (name[:-len(TAR_SUFFIX)] + BUNDLE_SUFFIX)
    print("✅ 已解包 {} → {}".format(name, target))
    if bundle.exists():
        # clone 先行：`git bundle verify` 需要在一个 git 仓库内执行，灾难日裸机上
        # verify-first 的 && 链第一步就断（终轮演练实证）；clone 自身就校验 bundle
        print("vault 恢复三步：git clone '{b}' ScholarVault && "
              "git -C ScholarVault bundle verify '{b}' && "
              "git -C ScholarVault remote remove origin".format(b=bundle))
    print("注意：快照可能含未入索引的札记（打包与 ingest 并发窗口），恢复后重跑一次 "
          "notes_index/lint 校对。")
    return 0


def run_snapshot(notes_dir: Path, vault_dir: Path, backup_dir: Path,
                 staging_dir: Path, *, force: bool = False,
                 guard_days: int = GUARD_DAYS, now: datetime = None,
                 retry_sleep: float = 30.0) -> int:
    now = now or datetime.now()

    # ---- 守卫 ----
    if backup_dir == BACKUP_ROOT and not CLOUDDOCS_ROOT.is_dir():
        logger.error("iCloud Drive 本体不存在（登出/未启用？）：{}".format(CLOUDDOCS_ROOT))
        notify("Scholar 备份失败", "iCloud Drive 不可用，本周快照未产生")
        return 2
    first_run = not backup_dir.is_dir()
    if first_run:
        backup_dir.mkdir(parents=True, exist_ok=True)
    valid, weird = list_snapshots(backup_dir, now=now)
    if weird:
        logger.warning("备份目录有 {} 个异类文件（解析失败/未来戳，不计入守卫）：{}".format(
            len(weird), "、".join(weird[:5])))
    last = valid[-1][0] if valid else None
    if last and not force and (now - last).total_seconds() < guard_days * 86400:
        logger.info("距上次成功快照 {:.1f} 天（<{} 天守卫），秒退。".format(
            (now - last).total_seconds() / 86400, guard_days))
        return 0
    # weird 的 notify 放守卫之后：RunAtLoad 让本函数每次登录都跑，一个长期躺着的
    # 冲突副本若在秒退路径上也弹通知，等于每次登录骚扰一条（审计实证）
    if weird:
        notify("Scholar 备份", "备份目录有异类文件（冲突副本/手放？）：{}".format(
            "、".join(weird[:3])))

    # ---- 打包（staging）+ 一致性重试 ----
    # mkdtemp 私有子目录：两个实例同刻开火（launchd 补跑 + 手动并发是常态）共用
    # staging 时，胜者把共享 tar replace 走、败者在 stat() 处裸崩且无告警（压测
    # 4/4 复现）。私有目录让每个实例自持产物；无论成败收尾统一清理，staging
    # 不再累积半成品（此前"下次覆盖"的说法不成立——文件名内嵌时间戳每次不同）。
    staging_dir.mkdir(parents=True, exist_ok=True)
    import tempfile
    work_dir = Path(tempfile.mkdtemp(prefix="snap_", dir=staging_dir))
    name_base = SNAPSHOT_PREFIX + format_ts(now)
    tar_path = work_dir / (name_base + TAR_SUFFIX)
    try:
        return _snapshot_inner(notes_dir, vault_dir, backup_dir, work_dir,
                               name_base, tar_path, now=now, retry_sleep=retry_sleep)
    except Exception as exc:
        logger.error("快照未预期失败：{}: {}".format(type(exc).__name__, exc))
        notify("Scholar 备份失败", "快照未预期失败：{}".format(str(exc)[:150]))
        return 2
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _snapshot_inner(notes_dir: Path, vault_dir: Path, backup_dir: Path,
                    work_dir: Path, name_base: str, tar_path: Path, *,
                    now: datetime, retry_sleep: float) -> int:
    for attempt in range(1, CONSISTENCY_RETRIES + 1):
        pre = _consistency_stamp(notes_dir)
        n_members = build_tar(notes_dir, repo_path("config/dedup_overrides.json"),
                              tar_path)
        post = _consistency_stamp(notes_dir)
        if pre == post:
            break
        logger.warning("打包期间源在变（第 {}/{} 次）：{} → {}，{}s 后重试".format(
            attempt, CONSISTENCY_RETRIES, pre, post, retry_sleep))
        if attempt == CONSISTENCY_RETRIES:
            notify("Scholar 备份失败", "连续 {} 次打包都撞上写窗口，本周快照未产生".format(
                CONSISTENCY_RETRIES))
            tar_path.unlink(missing_ok=True)
            return 2
        time.sleep(retry_sleep)

    # ---- 自检 ----
    with tarfile.open(tar_path, "r:gz") as tar:
        actual_members = len(tar.getnames())
    if actual_members != n_members:
        notify("Scholar 备份失败", "tar 自检成员数不符（{} != {}）".format(
            actual_members, n_members))
        tar_path.unlink(missing_ok=True)
        return 2
    sha_tar = _sha256(tar_path)

    bundle_path = work_dir / (name_base + BUNDLE_SUFFIX)
    bundle_ok = vault_dir.is_dir() and build_bundle(vault_dir, bundle_path)
    if vault_dir.is_dir() and not bundle_ok:
        # bundle 失败不弃整份快照（payload 独立成立），但必须告警
        notify("Scholar 备份", "vault bundle 打包失败，本份快照不含 vault 历史")

    manifest = {
        "ts": format_ts(now),
        "files": n_members,
        "bytes": tar_path.stat().st_size,
        "sha256_tar": sha_tar,
        "sha256_bundle": _sha256(bundle_path) if bundle_ok else None,
        "index_generated_at": pre[0],
        "topics_mtime": pre[1],
        "excludes": list(EXCLUDE_PATTERNS),
        "written_at": now.isoformat(timespec="seconds"),
    }
    manifest_path = work_dir / (name_base + MANIFEST_SUFFIX)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                             encoding="utf-8")

    # ---- 入 iCloud（自检全过才动；顺序 tar → bundle → manifest）----
    try:
        dest_tar = _move_in(tar_path, backup_dir)
        if bundle_ok:
            _move_in(bundle_path, backup_dir)
        _move_in(manifest_path, backup_dir)
    except Exception as e:
        logger.error("入库失败：{}".format(e))
        notify("Scholar 备份失败", "快照写入 iCloud 失败：{}".format(str(e)[:150]))
        return 2
    logger.info("✅ 快照已入库：{}（{} 个文件 / {:.1f}M{}）".format(
        dest_tar.name, n_members, manifest["bytes"] / 1024**2,
        " + vault bundle" if bundle_ok else ""))

    # ---- 成功后才轮换 + 配额哨兵 ----
    rotate(backup_dir, now=now)
    quota_sentinel()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="札记库不可再生核心 → iCloud 周快照")
    ap.add_argument("--notes-dir", default=None, help="默认 output/scholar_notes")
    ap.add_argument("--vault-dir", default=str(Path.home() / "Documents" / "ScholarVault"))
    ap.add_argument("--backup-dir", default=None, help="默认 iCloud XLBDBackups/")
    ap.add_argument("--staging-dir", default=str(STAGING_DEFAULT))
    ap.add_argument("--force", action="store_true",
                    help="绕过 6 天守卫（环境变量 XLBD_BACKUP_FORCE=1 等价，验收用）")
    ap.add_argument("--guard-days", type=int, default=GUARD_DAYS)
    ap.add_argument("--restore-to", default=None, metavar="DIR",
                    help="解包最新快照到 DIR（必须不存在或为空；永不覆盖原位）")
    args = ap.parse_args()

    backup_dir = Path(args.backup_dir).expanduser() if args.backup_dir else BACKUP_ROOT
    if args.restore_to:
        return restore(backup_dir, Path(args.restore_to).expanduser())

    notes_dir = (Path(args.notes_dir).expanduser() if args.notes_dir
                 else repo_path("output/scholar_notes"))
    if not notes_dir.is_dir():
        print("札记库不存在：{}".format(notes_dir), file=sys.stderr)
        return 2
    force = args.force or os.environ.get("XLBD_BACKUP_FORCE") == "1"
    return run_snapshot(notes_dir, Path(args.vault_dir).expanduser(), backup_dir,
                        Path(args.staging_dir).expanduser(), force=force,
                        guard_days=args.guard_days)


if __name__ == "__main__":
    sys.exit(main())
