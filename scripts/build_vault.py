# -*- coding: utf-8 -*-
"""科研札记库 → Obsidian vault（per-paper 笔记 + 语义近邻 + 静态 MOC）。

    PYTHONPATH=. python scripts/build_vault.py --vault-dir ~/Documents/ScholarVault --dry-run
    PYTHONPATH=. python scripts/build_vault.py --vault-dir ~/Documents/ScholarVault

--vault-dir 必填无默认：vault 是用户资产（含手写札记），不能默认写进仓库或 iCloud 目录。
退出码 0=成功 / 1=有 conflict 或切片失败 / 2=索引缺失或损坏。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.vault import write_vault          # noqa: E402
from src.scholar.notes_index import INDEX_JSON     # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="把科研札记库生成为 Obsidian vault")
    ap.add_argument("--vault-dir", required=True, help="vault 目录（必填，建议自行 git init）")
    ap.add_argument("--notes-dir", default="output/scholar_notes")
    ap.add_argument("--include-maybe", action="store_true",
                    help="放宽为 keeper 全集（默认只出 INCLUDE 或已精读）")
    ap.add_argument("--neighbors", type=int, default=5, help="语义近邻数，0=关闭（默认 5）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 篇（调试用）")
    ap.add_argument("--dry-run", action="store_true", help="只算不写")
    ap.add_argument("--force-regen", action="store_true",
                    help="强制重写生成块（用户区仍保留）")
    args = ap.parse_args()
    if args.limit < 0:                      # 负值会走 Python 负切片，静默少生成几篇
        ap.error("--limit 不能为负（0=不限）")
    if args.neighbors < 0:
        ap.error("--neighbors 不能为负（0=关闭相似边）")

    notes_dir = Path(args.notes_dir)
    index_path = notes_dir / INDEX_JSON
    if not index_path.exists():
        print("找不到索引：{}\n先跑：PYTHONPATH=. python scripts/notes_index.py".format(index_path),
              file=sys.stderr)
        return 2
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        assert isinstance(index.get("papers"), list)
    except Exception as exc:
        print("索引损坏（{}）：{}".format(type(exc).__name__, index_path), file=sys.stderr)
        return 2

    vault_dir = Path(args.vault_dir).expanduser()
    try:
        rep = write_vault(index, notes_dir, vault_dir,
                          include_maybe=args.include_maybe, k=args.neighbors,
                          dry_run=args.dry_run, limit=args.limit,
                          force_regen=args.force_regen)
    except OSError as exc:
        # 权限/磁盘满等：给可读提示而不是裸 traceback，且与 conflict 的退出码 1 区分开。
        # 注意此时可能已写入部分文件（无事务），提示用户重跑即可收敛。
        print("写盘失败（{}）：{}\n已写入的部分文件保留，修复后重跑即可收敛。".format(
            type(exc).__name__, exc), file=sys.stderr)
        return 2

    print("\n{}".format("=" * 66))
    print("{} vault: {}".format("【dry-run】" if args.dry_run else "✅", vault_dir))
    print("  文献 {} 篇（新建 {} · 更新 {} · 未变 {}）· 索引页 {} 个 · 写盘 {} 个文件".format(
        rep["selected"], rep["new"], rep["merged"], rep["unchanged"],
        rep["moc_pages"], rep["written"]))
    if rep.get("pruned"):
        print("  🧹 清理过期索引页 {} 个（分片规则变更后的残留）".format(len(rep["pruned"])))
    if rep.get("orphan_papers"):
        print("  ℹ️ {} 篇笔记已不在入选范围（未删除，可能含你的手写内容）：{}".format(
            len(rep["orphan_papers"]), ", ".join(rep["orphan_papers"][:5])))
    if rep["slice_failures"]:
        print("  ⚠️ {} 篇切不出正文（索引与 md 失步）：{}".format(
            len(rep["slice_failures"]), ", ".join(rep["slice_failures"][:6])))
    if rep["conflicts"]:
        print("  ⛔ {} 篇有冲突（哨兵被删或 frontmatter 坏），已另存 .conflict.md，原文件未动：".format(
            len(rep["conflicts"])))
        for k in rep["conflicts"][:10]:
            print("     - {}".format(k))
        print("     手工合并后删掉对应 .conflict.md 即可。")
    print("=" * 66)
    return 1 if (rep["conflicts"] or rep["slice_failures"]) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)
