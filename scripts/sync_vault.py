# -*- coding: utf-8 -*-
"""札记库 → Obsidian vault 自动同步（陈旧判定 + 重建 + vault 侧 git 提交 + 失败告警）。

    PYTHONPATH=. python scripts/sync_vault.py --vault-dir ~/Documents/ScholarVault
    PYTHONPATH=. python scripts/sync_vault.py --vault-dir ~/... --dry-run
    PYTHONPATH=. python scripts/sync_vault.py --vault-dir ~/... --force --no-commit

与直接跑 `build_vault.py` 的三个差别，也就是「能挂进定时任务」所需的三件事：

1. **陈旧判定**：索引的 `generated_at` 与 vault `_meta.json` 的 `source_index_generated_at`
   相同、且 notes_dir/topics/ 的最新 mtime 与 `_meta.json` 的 `source_topics_mtime` 也相同，
   才判定"已是最新"退出，不重建、不提交（W5：概念页由 `_refresh_topics` 在索引写盘**之后**
   才异步生成，经常在索引不再变之后才落盘完成，只看 `source_index_generated_at` 会让 vault
   侧概念页无限期停在旧版本——见 `vault_stamp`/`topics_mtime`）。定时/监视触发因此可以随便空转。
2. **git 提交**：vault 是用户资产且不受仓库 git 管辖（在 ~/Documents 下自成一库）。
   自动写盘必须留回滚点，否则一次静默重建把手写内容写坏就找不回来了。
   只在真有改动时提交；无改动不留空提交。
3. **失败告警**：冲突/切片失败/索引损坏走系统通知，成功只写日志——定时任务若成功
   也弹窗，几周后就会被无视，真出事那次也一起被无视。

**为什么用 subprocess 调 build_vault.py 而不是直接 import write_vault**：让报告格式、
默认口径（只收已精读）、退出码语义只有一个出处；重建后的权威状态从 vault 的 `_meta.json`
读回来（比解析 stdout 可靠）。子进程用 `sys.executable`，与 launchd 里 ProgramArguments[0]
是同一个 python 实体二进制，因此**复用既有的 TCC 完全磁盘访问授权**，不必再授权一次
（这也是这个脚本写成 .py 而不是 .sh 的原因：换成 bash 就得给 /bin/bash 单独授权）。

退出码 0=已同步或本就最新 / 1=有冲突或切片失败 / 2=索引缺失或损坏 / 3=vault 已更新但 git 提交失败
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.paths import repo_path            # noqa: E402
from src.scholar.notes_index import INDEX_JSON     # noqa: E402
from src.utils.notify import notify                # noqa: E402

REPO = Path(__file__).resolve().parents[1]
BUILD_VAULT = REPO / "scripts" / "build_vault.py"


def log(msg):
    print("[{}] {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def read_index(index_path, tries, settle):
    """读索引。缺失返回 "missing"；重试耗尽仍解析失败返回 None——那是真损坏。

    `notes_index.py` 写索引已是 tmp+os.replace 原子写，WatchPaths 触发时不会再读到
    半写的 JSON。重试只是给外部竞态（编辑器保存、磁盘抖动）留的廉价保险；重试后
    仍解析不了就只剩一种解释：索引真的损坏（磁盘故障/人为改坏）。调用方必须告警
    而不是当「还没就绪」静默跳过——上游 backfill/ingest 遇同样损坏会 fail-fast，
    索引从此不再变动、WatchPaths 不再触发，静默会让 vault 永久停摆。
    """
    for i in range(tries):
        if not index_path.exists():
            return "missing"
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(data.get("papers"), list):
                return data
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            pass
        if i < tries - 1:
            log("索引解析失败，{}s 后重试（{}/{}）".format(settle, i + 1, tries))
            time.sleep(settle)
    return None


def topics_mtime(notes_dir):
    """notes_dir/topics/ 下所有 `*.md`（**含子目录**）的最新 mtime；目录不存在或为空返回 None。

    与 `src.scholar.vault.write_vault` 写进 `_meta.json` 的 `source_topics_mtime`
    是同一个口径（同样 `rglob("*.md")` 取 max mtime），两边才能直接比对——
    **改一处必须改另一处**，口径一旦分叉，陈旧判定会永远为真或永远为假。

    用 rglob 而不是 glob：`topics/qa/` 是 P4 的问答归档子目录。非递归扫描会让
    「归档一次问答」完全不改变这个时间戳（索引没变、`topics/*.md` 也没变），
    陈旧判定于是认为"已同步"，新归档的问答**永远到不了 Obsidian**——
    正是 W5 那个真实事故（vault 侧概念页滞后 2 小时、靠一次无关的索引变动才凑巧自愈）
    换个目录层级重演一次。
    """
    d = Path(notes_dir) / "topics"
    if not d.exists():
        return None
    times = [p.stat().st_mtime for p in d.rglob("*.md")]
    return max(times) if times else None


def vault_stamp(vault_dir):
    """返回 (index_generated_at, topics_mtime)。vault 全新或 `_meta.json` 坏了则
    两者都是 None → 当作陈旧，重建即修。

    W5：此前只返回 `source_index_generated_at` 一个值。概念页（topics/*.md）由
    `_refresh_topics` 在索引写盘**之后**才另起子进程异步生成，经常在索引"已经不再
    变"之后才落盘完成——WatchPaths 不会为它再触发一次，只看索引快照的陈旧判定会
    误判"已完全同步"，vault 侧概念页因此可能无限期停在旧版本（实测过靠"凑巧后来
    有一次无关的索引变动"才顺带同步过去，滞后 2 小时）。
    """
    try:
        meta = json.loads((vault_dir / "_meta.json").read_text(encoding="utf-8"))
        return meta.get("source_index_generated_at"), meta.get("source_topics_mtime")
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None, None   # vault 全新或 _meta 坏了 → 当作陈旧，重建即修


def git(vault_dir, *argv):
    return subprocess.run(["git", "-C", str(vault_dir), *argv],
                          capture_output=True, text=True, check=False)


def commit_vault(vault_dir, counts, conflicts, stamp):
    """vault 侧提交。不是 repo 就跳过；没有改动就不留空提交。"""
    if not (vault_dir / ".git").exists():
        log("vault 未 git init，跳过提交（建议 git init 以便回滚）")
        return 0
    if git(vault_dir, "rev-parse", "--verify", "HEAD").returncode != 0:
        log("vault git 库还没有任何提交，跳过自动提交（先手工 commit 一次建基线）")
        return 0
    if not git(vault_dir, "status", "--porcelain").stdout.strip():
        log("vault 内容与上次提交一致，无需提交")
        return 0

    head = "自动同步 {} 篇 / {} 索引页（新建 {} · 更新 {}）".format(
        counts.get("selected", "?"), counts.get("moc_pages", "?"),
        counts.get("new", "?"), counts.get("merged", "?"))
    if conflicts:
        head += " ⛔{} 篇冲突".format(len(conflicts))
    body = "索引快照: {}\n由 scripts/sync_vault.py 自动提交".format(stamp or "未知")

    if git(vault_dir, "add", "-A").returncode != 0:
        log("git add 失败")
        return 3
    r = git(vault_dir, "commit", "-m", head, "-m", body)
    if r.returncode != 0:
        log("git commit 失败：{}".format((r.stderr or r.stdout).strip()[:300]))
        return 3
    sha = git(vault_dir, "rev-parse", "--short", "HEAD").stdout.strip()
    log("已提交 {} {}".format(sha, head))
    return 0


def main():
    ap = argparse.ArgumentParser(description="札记库 → Obsidian vault 自动同步")
    ap.add_argument("--vault-dir", required=True, help="vault 目录（必填，与 build_vault.py 同）")
    ap.add_argument("--notes-dir", default="output/scholar_notes")
    ap.add_argument("--force", action="store_true", help="忽略陈旧判定，强制重建")
    ap.add_argument("--dry-run", action="store_true", help="只算不写，也不提交")
    ap.add_argument("--no-commit", action="store_true", help="重建但不在 vault 侧提交")
    ap.add_argument("--settle", type=float, default=5.0, help="索引解析失败的重试间隔秒（默认 5）")
    ap.add_argument("--tries", type=int, default=3, help="判定索引损坏前的解析尝试次数（默认 3）")
    ap.add_argument("--build-arg", action="append", default=[], metavar="ARG",
                    help="透传给 build_vault.py 的额外参数，可重复（如 --build-arg=--neighbors=8）")
    args = ap.parse_args()
    if args.tries < 1:
        ap.error("--tries 至少为 1")
    if args.settle < 0:
        ap.error("--settle 不能为负")

    vault_dir = Path(args.vault_dir).expanduser()
    index_path = repo_path(args.notes_dir) / INDEX_JSON

    index = read_index(index_path, args.tries, args.settle)
    if index == "missing":
        log("找不到索引：{}".format(index_path))
        return 2
    if index is None:
        # 原子写落地后「半写入」已不存在，解析失败即真损坏；退 0 会让 launchd
        # 视为成功且此后再无触发（见 read_index docstring），必须报警并按 2 退出。
        log("索引重试后仍解析失败，疑似损坏：{}".format(index_path))
        notify("札记 vault 同步失败", "literature_index.json 重试后仍解析失败，疑似损坏")
        return 2

    stamp = index.get("generated_at")
    cur_topics_mtime = topics_mtime(repo_path(args.notes_dir))
    have_stamp, have_topics_mtime = vault_stamp(vault_dir)
    # W5：索引快照与概念页 mtime 任一落后即重建——只看索引快照会让 vault 侧概念页
    # 在索引"已经不再变但概念页刚落盘"的窗口里被误判成"已完全同步"，见 vault_stamp。
    if have_stamp == stamp and have_topics_mtime == cur_topics_mtime and not args.force:
        log("vault 已是最新（索引快照 {}，概念页 mtime {}），跳过".format(
            stamp, cur_topics_mtime))
        return 0
    log("索引 {} → vault {}，开始重建（概念页 mtime {} → {}）".format(
        stamp, have_stamp or "（无）", have_topics_mtime, cur_topics_mtime))

    cmd = [sys.executable, str(BUILD_VAULT), "--vault-dir", str(vault_dir),
           "--notes-dir", args.notes_dir, *args.build_arg]
    if args.dry_run:
        cmd.append("--dry-run")
    rc = subprocess.run(cmd, cwd=str(REPO), check=False).returncode

    if rc == 2:
        notify("札记 vault 同步失败", "build_vault 写盘或索引错误，见 cron_vault.log")
        return 2
    if args.dry_run:
        return rc

    # 权威状态从 _meta.json 读回，而不是解析 stdout
    try:
        meta = json.loads((vault_dir / "_meta.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        log("重建后读不回 _meta.json（{}）".format(type(exc).__name__))
        meta = {}
    counts, conflicts = meta.get("counts", {}), meta.get("conflicts", [])
    failures = meta.get("slice_failures", [])

    git_rc = 0 if args.no_commit else commit_vault(
        vault_dir, counts, conflicts, meta.get("source_index_generated_at"))

    if conflicts or failures:
        notify("札记 vault 同步有冲突",
               "{} 篇冲突 / {} 篇切片失败，手工合并 .conflict.md".format(len(conflicts), len(failures)))
    return git_rc or rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)
