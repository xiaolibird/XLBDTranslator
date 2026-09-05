# -*- coding: utf-8 -*-
"""科研札记文献索引 CLI（核心逻辑在 src/scholar/notes_index.py）。

  python scripts/notes_index.py                      # 增量（默认：只重解析新/变更月份）
  python scripts/notes_index.py --full               # 全量重建
  python scripts/notes_index.py --since 2025-01 --until 2025-12   # 区间重解析
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.paths import repo_path  # noqa: E402
from src.scholar.notes_index import (  # noqa: E402
    update_index, write_outputs, fix_citekey_collisions, fix_inline_citekeys,
)


def _print_inline_plan(res: dict, applied: bool) -> None:
    """--fix-inline-citekeys 的计划/结果打印（dry-run 与 --apply 共用）。"""
    planned = res.get("planned") or []
    if not planned and not res.get("skipped"):
        print("✅ 没有 duplicate 条目的行内 citekey 与 keeper 不一致，无事可做")
        return
    print("{} {} 条行内键改动{}：".format("🔧 已执行" if applied else "📋 计划",
                                      len(planned), "" if applied else "（dry-run，未写盘；加 --apply 落盘）"))
    for pl in planned:
        where = ("改 keeper 所在月（基键回归规范态，派生物会同步）" if pl["shape"] == "suffix-keeper"
                 else "改 duplicate 所在月（三处：md / .references.json / .index.json 凡存在者）")
        print("   {:<14} {} → {}   {} / {}   [{}]".format(
            pl["shape"], pl["old"], pl["new"], pl["month"], pl["note_file"], where))
        if pl["shape"] == "suffix-keeper":
            suf = pl["old"][len(pl["new"]):]
            # 词面判据认不出「两个键恰好差一个像后缀的词尾」。`_suffix_seq()` 是 b, c, d, … 依次发的，
            # 所以真正的消歧后缀绝大多数是单个 `b`（第一次撞键）；`s`（英文复数：
            # zhang2024Model / zhang2024Models）与多字母尾巴都更像「两个键本来就差一个词尾」。
            if suf not in ("b", "c", "d"):
                print("      ⚠️ 后缀 {!r} 不像消歧序列（正常是 b/c/d）：请确认这真是消歧后缀，"
                      "而不是两个键本来就差一个词尾（如英文单复数）".format(suf))
    for s in res.get("skipped") or []:
        print("   ⏭ 跳过：{}".format(s))
    if applied:
        print("   成功 {} / 拒绝（磁盘未动）{} / 半改 {}".format(
            res.get("applied", 0), len(res.get("refused") or []), len(res.get("partial") or [])))
        for s in res.get("partial") or []:
            print("   ⛔ 半改，务必人工核对：{}".format(s))
        for s in res.get("refused") or []:
            print("   ⚠️ 未改：{}".format(s))
        if "remaining" in res:
            print("   重建索引后仍不一致：{} 条".format(res["remaining"]))


def main():
    ap = argparse.ArgumentParser(description="构建/更新科研札记文献索引")
    ap.add_argument("--notes-dir", default="output/scholar_notes")
    ap.add_argument("--full", action="store_true", help="全量重建（忽略增量缓存）")
    ap.add_argument("--since", default=None, help="强制重扫 >= 此月份（YYYY-MM）")
    ap.add_argument("--until", default=None, help="强制重扫 <= 此月份（YYYY-MM）")
    ap.add_argument("--fix-collisions", action="store_true",
                    help="自动修复 citekey 撞键（后出现月加 b/c 后缀，改 md+references.json）")
    ap.add_argument("--fix-inline-citekeys", action="store_true",
                    help="修复 duplicate 条目行内 citekey 与 keeper 不一致（默认只打印计划；配 --apply 落盘。"
                         "stale-dup 形态改 dup 所在月三处；suffix-keeper 形态把 keeper 的 <基键>b 改回基键）")
    ap.add_argument("--apply", action="store_true",
                    help="与 --fix-inline-citekeys 连用：真正写盘（否则 dry-run）")
    args = ap.parse_args()

    notes_dir = repo_path(args.notes_dir)   # 相对路径锚死仓库根，别随 cwd 漂
    if not notes_dir.is_dir():
        print("❌ 札记目录不存在: {}".format(notes_dir))
        return 1
    rc = 0
    if args.fix_inline_citekeys:
        side = {}
        res = fix_inline_citekeys(notes_dir, apply=args.apply, side_out=side)
        _print_inline_plan(res, applied=args.apply)
        if res.get("partial"):
            rc = 1
        if side.get("error"):
            print("\n⛔ 改键已落盘，但向量库自动同步失败——检索会吐已注销的旧 citekey。"
                  "请手动跑 `PYTHONPATH=. python scripts/notes_embed.py` 后再引用。")
            rc = 1
        if not args.apply:
            return rc          # dry-run：不顺手重建索引（fix_inline_citekeys 已 update_index 一次）
    if args.fix_collisions:
        side = {}
        n = fix_citekey_collisions(notes_dir, side_out=side)
        print("🔧 撞键改键 {} 篇".format(n))
        if side.get("error"):
            print("\n⛔ 改键已落盘，但向量库自动同步失败——检索会吐已注销的旧 citekey。"
                  "请手动跑 `PYTHONPATH=. python scripts/notes_embed.py` 后再引用。")
            rc = 1
    index = update_index(notes_dir, full=args.full, since=args.since, until=args.until)
    wrote = write_outputs(index, notes_dir)
    uniq = [e for e in index["papers"] if not e.get("duplicate_of")]
    n_months = len({v.get("month") for v in index["months"].values()})
    print("✅ 索引 {} 份札记（{} 个自然月）/ {} 篇（去重后 {}），撞键 {} 组 | 写盘: {}".format(
        len(index["months"]), n_months, len(index["papers"]), len(uniq),
        len(index["citekey_collisions"]),
        ", ".join(k for k, v in wrote.items() if v) or "无变化"))
    stale = index.get("stale_inline_citekeys") or []
    if stale:
        print("⚠️ {} 条 duplicate 的行内 citekey 与其 keeper 不一致（从该月札记抄引用会引错论文 / "
              "基键从全局书目消失）：".format(len(stale)))
        for s in stale[:10]:
            print("     {}".format(s))
        if len(stale) > 10:
            print("     …等共 {} 条".format(len(stale)))
        print("   → 修复：scripts/notes_index.py --fix-inline-citekeys（先看计划；加 --apply 落盘）")
    return rc


if __name__ == "__main__":
    sys.exit(main())
