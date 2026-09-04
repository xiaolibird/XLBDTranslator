# -*- coding: utf-8 -*-
"""备份快照：命名口径、守卫、打包/排除、自检、轮换、restore 安全、一致性重试。
全 tmp 构造，不碰 iCloud/生产库。
"""
import json
import os
import subprocess
import sys
import tarfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar import backup_naming as bn                                # noqa: E402
import scripts.backup_snapshot as bs                                       # noqa: E402

NOW = datetime(2026, 9, 6, 20, 0, 0)   # 一个周日 20:00


# ---------------------------------------------------------------------------
# 0. 命名口径（backup_naming 单一出处）
# ---------------------------------------------------------------------------

def test_ts_roundtrip_and_no_colon():
    name = bn.SNAPSHOT_PREFIX + bn.format_ts(NOW) + bn.TAR_SUFFIX
    assert ":" not in name                       # 冒号在 Finder/iCloud 有转写问题
    assert bn.parse_snapshot_ts(name) == NOW


def test_parse_icloud_placeholder():
    name = "." + bn.SNAPSHOT_PREFIX + bn.format_ts(NOW) + bn.TAR_SUFFIX + ".icloud"
    assert bn.parse_snapshot_ts(name) == NOW     # 驱逐的份仍是存在的份


def test_list_snapshots_excludes_weird_and_future(tmp_path):
    (tmp_path / (bn.SNAPSHOT_PREFIX + "20260901T200000" + bn.TAR_SUFFIX)).write_bytes(b"x")
    (tmp_path / (bn.SNAPSHOT_PREFIX + "20260901T200000 2.tar.gz")).write_bytes(b"x")  # iCloud 冲突副本
    (tmp_path / (bn.SNAPSHOT_PREFIX + "99991231T235959" + bn.TAR_SUFFIX)).write_bytes(b"x")  # 未来戳
    (tmp_path / "unrelated.txt").write_bytes(b"x")
    (tmp_path / ".DS_Store").write_bytes(b"x")
    valid, weird = bn.list_snapshots(tmp_path, now=NOW)
    assert [ts for ts, _ in valid] == [datetime(2026, 9, 1, 20, 0, 0)]
    # 冲突副本（" 2.tar.gz"）不是我们的产物：判异类并 notify，绝不计入"上次成功"
    assert any(" 2.tar.gz" in w for w in weird)
    assert any("9999" in w for w in weird)       # 未来戳必须进 weird
    # 无关文件与 .DS_Store 不算异类（不 notify 骚扰）
    assert "unrelated.txt" not in weird and ".DS_Store" not in weird


def test_latest_counts_placeholder(tmp_path):
    (tmp_path / ("." + bn.SNAPSHOT_PREFIX + "20260830T200000" + bn.TAR_SUFFIX + ".icloud")
     ).write_bytes(b"")
    assert bn.latest_snapshot_ts(tmp_path, now=NOW) == datetime(2026, 8, 30, 20, 0, 0)


def test_future_ts_never_counts_as_last_success(tmp_path):
    """未来戳若计入"上次成功"，6 天守卫会永远秒退、备份静默停摆（PRD 审定）。"""
    (tmp_path / (bn.SNAPSHOT_PREFIX + "99991231T235959" + bn.TAR_SUFFIX)).write_bytes(b"x")
    assert bn.latest_snapshot_ts(tmp_path, now=NOW) is None


# ---------------------------------------------------------------------------
# 夹具：迷你札记库 + 迷你 vault git 库
# ---------------------------------------------------------------------------

def _mk_notes(tmp_path):
    notes = tmp_path / "notes"
    (notes / "topics" / "qa").mkdir(parents=True)
    (notes / "manual" / "2026-09-01").mkdir(parents=True)
    (notes / "literature_index.json").write_text(
        json.dumps({"generated_at": "2026-09-06T10:00:00", "papers": []}),
        encoding="utf-8")
    (notes / "all_references.json").write_text("[]", encoding="utf-8")
    (notes / "科研札记_2026-09_全文精读.md").write_text("札记内容", encoding="utf-8")
    (notes / "manual" / "2026-09-01" / "深读.md").write_text("手动精读", encoding="utf-8")
    (notes / "topics" / "mnar.md").write_text("概念页", encoding="utf-8")
    (notes / "topics" / "qa" / "qa-1.md").write_text("问答", encoding="utf-8")
    # 必须被排除的：向量库全家桶 + 归档 + 缓存
    (notes / "embeddings.sqlite3").write_bytes(b"BIGDB" * 100)
    (notes / "embeddings.sqlite3-wal").write_bytes(b"WAL")
    (notes / "embeddings.sqlite3-shm").write_bytes(b"SHM")
    (notes / "embeddings.sqlite3.lock").write_bytes(b"")
    (notes / "_archive").mkdir()
    (notes / "_archive" / "old.json").write_text("{}", encoding="utf-8")
    (notes / ".pytest_cache").mkdir()
    (notes / ".pytest_cache" / "junk").write_text("x", encoding="utf-8")
    return notes


def _mk_vault_repo(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "00-总览.md").write_text("vault 内容", encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "commit", "-qm", "基线"]):
        subprocess.run(cmd, cwd=vault, env=env, check=True, capture_output=True)
    return vault


def _run(tmp_path, notes=None, vault=None, backup=None, now=NOW, **kw):
    notes = notes or _mk_notes(tmp_path)
    vault = vault if vault is not None else _mk_vault_repo(tmp_path)
    backup = backup or (tmp_path / "backups")
    staging = tmp_path / "staging"
    rc = bs.run_snapshot(notes, vault, backup, staging, now=now,
                         retry_sleep=0.01, **kw)
    return rc, notes, vault, backup


# ---------------------------------------------------------------------------
# 1. 端到端：快照 → restore → 逐文件 sha256
# ---------------------------------------------------------------------------

def test_snapshot_restore_roundtrip_sha256(tmp_path, monkeypatch):
    monkeypatch.setattr(bs, "quota_sentinel", lambda: None)
    rc, notes, vault, backup = _run(tmp_path)
    assert rc == 0
    files = sorted(p.name for p in backup.iterdir())
    assert any(f.endswith(bn.TAR_SUFFIX) for f in files)
    assert any(f.endswith(bn.MANIFEST_SUFFIX) for f in files)
    assert any(f.endswith(bn.BUNDLE_SUFFIX) for f in files)

    target = tmp_path / "restored"
    assert bs.restore(backup, target, now=NOW) == 0
    # 逐文件 sha256：payload 里每个该在的文件都完整还原
    for rel in ("科研札记_2026-09_全文精读.md", "manual/2026-09-01/深读.md",
                "topics/mnar.md", "topics/qa/qa-1.md", "literature_index.json"):
        a, b = notes / rel, target / "scholar_notes" / rel
        assert bs._sha256(a) == bs._sha256(b), rel
    # 排除项一个都不许进包
    restored = {str(p.relative_to(target)) for p in target.rglob("*") if p.is_file()}
    assert not any("embeddings.sqlite3" in r for r in restored)
    assert not any("_archive" in r for r in restored)
    assert not any(".pytest_cache" in r for r in restored)
    # bundle 可 verify + clone
    bundle = next(backup.glob("*" + bn.BUNDLE_SUFFIX))
    # verify 必须挂在一个真仓库上：`git bundle verify` 需要 cwd 是 git 仓库，不传 cwd 就
    # 继承 pytest 的工作目录——在真仓库里恰好通过，在任何非 git 目录（沙箱快照、tarball
    # 解包、审计副本）里报 "need a repository to verify a bundle"，而失败信息指不到真因。
    # 这里按恢复手册的三步来：先 clone 出仓库，再在它里面 verify。
    clone = tmp_path / "vault_clone"
    c = subprocess.run(["git", "clone", "-q", str(bundle), str(clone)],
                       capture_output=True, text=True)
    assert c.returncode == 0, c.stderr
    v = subprocess.run(["git", "-C", str(clone), "bundle", "verify", str(bundle)],
                       capture_output=True, text=True)
    assert v.returncode == 0, v.stderr
    # manifest 数字自洽
    manifest = json.loads(next(backup.glob("*" + bn.MANIFEST_SUFFIX)).read_text())
    assert manifest["files"] > 0 and manifest["sha256_tar"]
    assert manifest["index_generated_at"] == "2026-09-06T10:00:00"


# ---------------------------------------------------------------------------
# 2. 守卫
# ---------------------------------------------------------------------------

def test_guard_blocks_within_six_days_and_force_bypasses(tmp_path, monkeypatch):
    monkeypatch.setattr(bs, "quota_sentinel", lambda: None)
    rc, notes, vault, backup = _run(tmp_path)
    assert rc == 0
    n1 = len(list(backup.iterdir()))
    # 4 天后：守卫秒退，无新产物
    rc2 = bs.run_snapshot(notes, vault, backup, tmp_path / "staging",
                          now=NOW + timedelta(days=4), retry_sleep=0.01)
    assert rc2 == 0 and len(list(backup.iterdir())) == n1
    # force 绕过
    rc3 = bs.run_snapshot(notes, vault, backup, tmp_path / "staging",
                          now=NOW + timedelta(days=4), force=True, retry_sleep=0.01)
    assert rc3 == 0 and len(list(backup.iterdir())) > n1
    # 7 天后：正常产出
    rc4 = bs.run_snapshot(notes, vault, backup, tmp_path / "staging",
                          now=NOW + timedelta(days=11), retry_sleep=0.01)
    assert rc4 == 0


def test_guard_first_run_and_weird_files(tmp_path, monkeypatch):
    monkeypatch.setattr(bs, "quota_sentinel", lambda: None)
    backup = tmp_path / "backups"
    backup.mkdir()
    (backup / (bn.SNAPSHOT_PREFIX + "not-a-ts.tar.gz")).write_bytes(b"x")     # 手放
    (backup / (bn.SNAPSHOT_PREFIX + "99991231T235959.tar.gz")).write_bytes(b"x")  # 未来戳
    rc, _n, _v, _b = _run(tmp_path, backup=backup)
    assert rc == 0                                # 异类不计入守卫，首份照常产出
    assert any(bn.parse_snapshot_ts(p.name) == NOW for p in backup.iterdir())


# ---------------------------------------------------------------------------
# 3. 轮换
# ---------------------------------------------------------------------------

def _fake_group(backup, ts, *, tar=True, manifest=True):
    base = bn.SNAPSHOT_PREFIX + bn.format_ts(ts)
    if tar:
        (backup / (base + bn.TAR_SUFFIX)).write_bytes(b"t")
    if manifest:
        (backup / (base + bn.MANIFEST_SUFFIX)).write_text("{}", encoding="utf-8")


def test_rotation_keeps_recent_and_monthly_firsts(tmp_path):
    backup = tmp_path / "backups"
    backup.mkdir()
    # 2026-01 起每月两份（1 号与 15 号），到 2026-09 共 18 份
    stamps = []
    for m in range(1, 10):
        for d in (1, 15):
            ts = datetime(2026, m, d, 20, 0, 0)
            stamps.append(ts)
            _fake_group(backup, ts)
    deleted = bs.rotate(backup, now=NOW, keep_recent=4, keep_monthly_months=24)
    kept_ts = {bn.parse_snapshot_ts(p.name) for p in backup.iterdir()}
    # 每月 1 号那份（月首）全保
    for m in range(1, 10):
        assert datetime(2026, m, 1, 20, 0, 0) in kept_ts, m
    # 最近 4 份全保（8/15、9/1、9/15 不存在——最近 4 个时间戳）
    for ts in sorted(stamps)[-4:]:
        assert ts in kept_ts
    # 被删的是"既非月首又非最近 4 份"的 15 号份
    assert datetime(2026, 3, 15, 20, 0, 0) not in kept_ts
    assert deleted                                # 将删清单非空
    # 配对删除：删掉的时间戳 tar 与 manifest 都没了
    for name in deleted:
        assert not (backup / name).exists()


def test_rotation_handles_placeholders_and_orphans(tmp_path):
    backup = tmp_path / "backups"
    backup.mkdir()
    old = datetime(2020, 1, 15, 20, 0, 0)         # 超出 24 个月月度保留
    # 驱逐占位符形态的老份 + 孤儿（有 tar 无 manifest）
    (backup / ("." + bn.SNAPSHOT_PREFIX + bn.format_ts(old) + bn.TAR_SUFFIX + ".icloud")
     ).write_bytes(b"")
    _fake_group(backup, datetime(2020, 2, 15, 20, 0, 0), manifest=False)
    # 近 10 份把"最近 keep_recent 份"的名额占满，2020 的老份才会真被挤出窗口
    for i in range(10):
        _fake_group(backup, NOW - timedelta(days=7 * i))
    bs.rotate(backup, now=NOW, keep_recent=8, keep_monthly_months=24)
    names = {p.name for p in backup.iterdir()}
    assert not any("2020" in n for n in names)    # 占位符与孤儿组都被轮换掉


# ---------------------------------------------------------------------------
# 4. restore 安全
# ---------------------------------------------------------------------------

def test_restore_refuses_nonempty_target(tmp_path, monkeypatch):
    monkeypatch.setattr(bs, "quota_sentinel", lambda: None)
    rc, _n, _v, backup = _run(tmp_path)
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "existing.txt").write_text("x", encoding="utf-8")
    assert bs.restore(backup, target, now=NOW) == 2        # 永不覆盖


def test_restore_rejects_path_traversal_members(tmp_path):
    """tar 来自 iCloud 非完全可信：../ 成员必须被 filter="data" 拒掉。"""
    backup = tmp_path / "backups"
    backup.mkdir()
    evil_tar = backup / (bn.SNAPSHOT_PREFIX + bn.format_ts(NOW) + bn.TAR_SUFFIX)
    payload = tmp_path / "p.txt"
    payload.write_text("evil", encoding="utf-8")
    with tarfile.open(evil_tar, "w:gz") as tar:
        tar.add(payload, arcname="../escaped.txt")
    target = tmp_path / "restored"
    # 第 1 轮审计后行为：filter="data" 拒掉成员 → 捕获并清理半截产物、退 2（不裸抛）
    assert bs.restore(backup, target, now=NOW) == 2
    assert not (tmp_path / "escaped.txt").exists()
    assert not target.exists()                    # 半截产物已清


# ---------------------------------------------------------------------------
# 5. 一致性重试 / 配额哨兵 / 目录判据
# ---------------------------------------------------------------------------

def test_consistency_retry_exhaustion_returns_2(tmp_path, monkeypatch):
    monkeypatch.setattr(bs, "quota_sentinel", lambda: None)
    seq = iter(range(100))
    monkeypatch.setattr(bs, "_consistency_stamp",
                        lambda nd: ("stamp-{}".format(next(seq)), 0.0))
    notified = []
    monkeypatch.setattr(bs, "notify", lambda t, m: notified.append((t, m)))
    rc, _n, _v, backup = _run(tmp_path)
    assert rc == 2
    assert not any(p.name.endswith(bn.TAR_SUFFIX) for p in backup.iterdir())
    assert notified                               # 重试耗尽必须告警


def test_quota_sentinel_missing_brctl_is_silent(monkeypatch):
    def _boom(*a, **kw):
        raise FileNotFoundError("brctl")
    monkeypatch.setattr(bs.subprocess, "run", _boom)
    bs.quota_sentinel()                           # 不抛即过（Linux CI 也走这条）


def test_vault_missing_still_produces_payload(tmp_path, monkeypatch):
    """vault 不在（新机/未建）不弃整份快照——payload 独立成立。"""
    monkeypatch.setattr(bs, "quota_sentinel", lambda: None)
    rc, _n, _v, backup = _run(tmp_path, vault=tmp_path / "no_vault")
    assert rc == 0
    names = [p.name for p in backup.iterdir()]
    assert any(n.endswith(bn.TAR_SUFFIX) for n in names)
    assert not any(n.endswith(bn.BUNDLE_SUFFIX) for n in names)


# ---------------------------------------------------------------------------
# 6. plist 文本级回归（照 vault plist 的先例，不用 plistlib——注释含 -- 会被 expat 拒）
# ---------------------------------------------------------------------------

def test_backup_plist_has_calendar_and_runatload():
    plist = (Path(__file__).resolve().parents[1] /
             "config" / "launchd" / "com.xlbd.scholar-backup.plist").read_text(
                 encoding="utf-8")
    assert "<string>com.xlbd.scholar-backup</string>" in plist
    assert "StartCalendarInterval" in plist
    assert "<key>RunAtLoad</key>" in plist        # 关机跨周日不补跑，登录补跑是必需
    assert "backup_snapshot.py" in plist
    assert "cron_backup.err.log" in plist


# ---------------------------------------------------------------------------
# 7. 第 2 轮审计修复的回归
# ---------------------------------------------------------------------------

def test_escaping_symlink_members_excluded_at_pack_time(tmp_path, monkeypatch):
    """库外符号链接是潜伏弹：打包静默绿、restore 整份拒收。打包侧就排除。"""
    monkeypatch.setattr(bs, "quota_sentinel", lambda: None)
    notes = _mk_notes(tmp_path)
    (notes / "evil_abs.md").symlink_to("/etc/hosts")
    (notes / "evil_rel.md").symlink_to("../../outside.md")
    (notes / "ok_rel.md").symlink_to("科研札记_2026-09_全文精读.md")   # 库内相对：保留
    rc, _n, _v, backup = _run(tmp_path, notes=notes)
    assert rc == 0
    target = tmp_path / "restored"
    assert bs.restore(backup, target, now=NOW) == 0        # 整份可恢复
    names = {p.name for p in (target / "scholar_notes").iterdir()}
    assert "evil_abs.md" not in names and "evil_rel.md" not in names
    assert "ok_rel.md" in names


def test_restore_failure_keeps_user_precreated_dir(tmp_path):
    """用户自建空目录 + 坏 tar：清内容、保目录本体。"""
    backup = tmp_path / "backups"
    backup.mkdir()
    bad = backup / (bn.SNAPSHOT_PREFIX + bn.format_ts(NOW) + bn.TAR_SUFFIX)
    bad.write_bytes(b"\x1f\x8b" + b"garbage" * 10)         # 合法 gzip 头的坏 tar
    user_dir = tmp_path / "user_made"
    user_dir.mkdir()
    assert bs.restore(backup, user_dir, now=NOW) == 2
    assert user_dir.is_dir()                               # 本体还在
    assert not any(user_dir.iterdir())                     # 内容已清


def test_restore_missing_manifest_warns(tmp_path, capsys):
    """manifest 缺失/被驱逐必须明说"sha 未核对"，不许静默（docs 承诺自动比）。"""
    backup = tmp_path / "backups"
    backup.mkdir()
    import io
    import tarfile as tf
    tar_path = backup / (bn.SNAPSHOT_PREFIX + bn.format_ts(NOW) + bn.TAR_SUFFIX)
    with tf.open(tar_path, "w:gz") as tar:
        data = b"content"
        info = tf.TarInfo("scholar_notes/a.md")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    # 驱逐占位符形态
    (backup / ("." + tar_path.name[:-len(bn.TAR_SUFFIX)] + bn.MANIFEST_SUFFIX + ".icloud")
     ).write_bytes(b"")
    assert bs.restore(backup, tmp_path / "r1", now=NOW) == 0
    assert "被 iCloud 驱逐" in capsys.readouterr().err
