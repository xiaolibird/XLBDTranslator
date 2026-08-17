# -*- coding: utf-8 -*-
"""科研札记库 → Obsidian vault（per-paper 笔记 + 语义近邻 + 静态 MOC）。

    PYTHONPATH=. python scripts/build_vault.py --vault-dir ~/Documents/ScholarVault --dry-run
    PYTHONPATH=. python scripts/build_vault.py --vault-dir ~/Documents/ScholarVault

默认只收**已精读**的条目（约 299 篇）；`--include-abstract-only` 纳入仅摘要的 INCLUDE，
`--include-maybe` 放宽到 keeper 全集。
--vault-dir 必填无默认：vault 是用户资产（含手写札记），不能默认写进仓库或 iCloud 目录。
退出码 0=成功 / 1=有 conflict 或切片失败 / 2=索引缺失或损坏。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.paths import repo_path               # noqa: E402
from src.scholar.vault import write_vault          # noqa: E402
from src.scholar.topics import (                   # noqa: E402
    sync_topics_to_vault, VAULT_TOPICS_DIR, load_topic_specs, TopicError, DEFAULT_CONFIG,
)
from src.scholar.notes_index import INDEX_JSON     # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="把科研札记库生成为 Obsidian vault")
    ap.add_argument("--vault-dir", required=True, help="vault 目录（必填，建议自行 git init）")
    ap.add_argument("--notes-dir", default="output/scholar_notes")
    ap.add_argument("--include-abstract-only", action="store_true",
                    help="把 INCLUDE 但无精读的条目也纳入（默认只出已精读的）")
    ap.add_argument("--include-maybe", action="store_true",
                    help="放宽为 keeper 全集（含 MAYBE）")
    ap.add_argument("--neighbors", type=int, default=5, help="语义近邻数，0=关闭（默认 5）")
    ap.add_argument("--topics-config", default=DEFAULT_CONFIG,
                    help="概念页定义（yaml），仅用于给 vault 侧退役页打 retired 标记"
                         "（X6/B1，见 sync_topics_to_vault 的 specs= 参数）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 篇（调试用）")
    ap.add_argument("--dry-run", action="store_true", help="只算不写")
    ap.add_argument("--force-regen", action="store_true",
                    help="强制重写生成块（用户区尽力保留）；同时把原本卡住的 conflict 笔记"
                         "（哨兵被删/frontmatter 改坏/生成块内被手写批注）也直接覆盖")
    args = ap.parse_args()
    if args.limit < 0:                      # 负值会走 Python 负切片，静默少生成几篇
        ap.error("--limit 不能为负（0=不限）")
    if args.neighbors < 0:
        ap.error("--neighbors 不能为负（0=关闭相似边）")
    if args.limit and not args.dry_run:
        # limit 是抽样调试语义：write_vault 会自动跳过 MOC/落选笔记清理，但仍会
        # 真实写盘这 N 篇——不是纯只读的 dry-run，写盘前把这点说清楚。
        print("ℹ️ --limit={} + 非 dry-run：只会写这 {} 篇，且本轮跳过 MOC/落选笔记"
              "清理（不会误删其余未处理到的笔记，也不会补全清理）。".format(
                  args.limit, args.limit))

    notes_dir = repo_path(args.notes_dir)   # 相对路径锚死仓库根，别随 cwd 漂
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
                          include_maybe=args.include_maybe,
                          include_abstract_only=args.include_abstract_only,
                          k=args.neighbors,
                          dry_run=args.dry_run, limit=args.limit,
                          force_regen=args.force_regen)
    except OSError as exc:
        # 权限/磁盘满等：给可读提示而不是裸 traceback，且与 conflict 的退出码 1 区分开。
        # 注意此时可能已写入部分文件（无事务），提示用户重跑即可收敛。
        print("写盘失败（{}）：{}\n已写入的部分文件保留，修复后重跑即可收敛。".format(
            type(exc).__name__, exc), file=sys.stderr)
        return 2

    # 概念页同步（output/scholar_notes/topics/ -> vault/02-主题/）。放在 write_vault
    # 之后是因为它要用 rep["filenames"] 把 `[@citekey]` 转成能点开的 wiki 链接。
    # 没有 topics 目录时是空转，不影响原有链路。
    # X6：sync_topics_to_vault 的 specs= 是 B1「退役页在 vault 侧打 retired: true」的
    # 唯一开关——此前这里从没传过，B1 的单测全过，但真实 vault 同步永远走不到那个
    # 分支（接线接到了空气里）。config/topics.yaml 加载失败不该拖累 per-paper 笔记
    # 这条主线（它是主线之外的派生标记），降级为 specs=None，等价于此前的行为
    # （不打退役标记），只在 stderr 提示一句。
    try:
        topic_specs = load_topic_specs(repo_path(args.topics_config))
    except TopicError as exc:
        print("⚠️ 概念页配置加载失败，本轮 vault 侧不标记退役页（不影响概念页同步本身）：{}".format(
            exc), file=sys.stderr)
        topic_specs = None

    trep = {}
    try:
        trep = sync_topics_to_vault(notes_dir, vault_dir, rep.get("filenames") or {},
                                    dry_run=args.dry_run, specs=topic_specs)
    except OSError as exc:
        print("概念页同步写盘失败（{}）：{}".format(type(exc).__name__, exc), file=sys.stderr)
        return 2

    print("\n{}".format("=" * 66))
    print("{} vault: {}".format("【dry-run】" if args.dry_run else "✅", vault_dir))
    print("  文献 {} 篇（新建 {} · 更新 {} · 未变 {}）· 索引页 {} 个 · 写盘 {} 个文件".format(
        rep["selected"], rep["new"], rep["merged"], rep["unchanged"],
        rep["moc_pages"], rep["written"]))
    if trep.get("new") or trep.get("merged") or trep.get("unchanged"):
        print("  🧭 概念页 {} 页（新建 {} · 更新 {} · 未变 {}）→ {}/".format(
            trep["new"] + trep["merged"] + trep["unchanged"], trep["new"], trep["merged"],
            trep["unchanged"], VAULT_TOPICS_DIR))
    if trep.get("conflicts"):
        print("  ⛔ 概念页 {} 页有冲突，未覆盖：{}".format(
            len(trep["conflicts"]), ", ".join(trep["conflicts"][:5])))
    if trep.get("skipped"):
        print("  ⏭ 概念页跳过 {} 个：{}".format(
            len(trep["skipped"]), ", ".join(trep["skipped"][:5])))
    if trep.get("prune_skipped_reason"):
        # 「本轮没确认清楚，所以没敢删」必须说出来：不说的话，一个长期解析失败的源文件
        # 会让 vault 侧永远留着一份下线页，而日志上看起来一切正常。
        print("  🛡 {}".format(trep["prune_skipped_reason"]))
    if trep.get("pruned"):
        print("  🧹 清理 {} 个已下线的概念页：{}".format(
            len(trep["pruned"]), ", ".join(trep["pruned"][:5])))
    if rep.get("cleanup_skipped_due_to_limit"):
        print("  ⏭ 本轮 --limit 生效，已跳过 MOC/落选笔记清理（不代表零落选，只是没算）")
    if rep.get("pruned"):
        notes = [x for x in rep["pruned"] if x.startswith("01-")]
        print("  🧹 清理 {} 个过期文件（{} 篇落选笔记 + {} 个索引页；仅限无手写内容的）".format(
            len(rep["pruned"]), len(notes), len(rep["pruned"]) - len(notes)))
    if rep.get("orphan_papers"):
        print("  ✍️ {} 篇已落选但含你的手写内容，保留未删：{}".format(
            len(rep["orphan_papers"]), ", ".join(rep["orphan_papers"][:5])))
    if rep["slice_failures"]:
        print("  ⚠️ {} 篇切不出正文（索引与 md 失步）：{}".format(
            len(rep["slice_failures"]), ", ".join(rep["slice_failures"][:6])))
    if rep.get("force_overwritten"):
        print("  🔧 --force-regen 覆盖了 {} 篇原本冲突的笔记（frontmatter/用户区已尽力保留，"
              "抽不出来的退回了空白）：".format(len(rep["force_overwritten"])))
        for k in rep["force_overwritten"][:10]:
            print("     - {}".format(k))
    if rep["conflicts"]:
        print("  ⛔ {} 篇有冲突（哨兵被删或 frontmatter 坏），已另存 .conflict.md，原文件未动：".format(
            len(rep["conflicts"])))
        for k in rep["conflicts"][:10]:
            print("     - {}".format(k))
        print("     手工合并后删掉对应 .conflict.md 即可，或加 --force-regen 直接强制覆盖。")
    print("=" * 66)
    return 1 if (rep["conflicts"] or rep["slice_failures"] or trep.get("conflicts")) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)
