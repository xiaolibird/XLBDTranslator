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

from src.scholar.notes_index import (  # noqa: E402
    update_index, write_outputs, fix_citekey_collisions,
)


def main():
    ap = argparse.ArgumentParser(description="构建/更新科研札记文献索引")
    ap.add_argument("--notes-dir", default="output/scholar_notes")
    ap.add_argument("--full", action="store_true", help="全量重建（忽略增量缓存）")
    ap.add_argument("--since", default=None, help="强制重扫 >= 此月份（YYYY-MM）")
    ap.add_argument("--until", default=None, help="强制重扫 <= 此月份（YYYY-MM）")
    ap.add_argument("--fix-collisions", action="store_true",
                    help="自动修复 citekey 撞键（后出现月加 b/c 后缀，改 md+references.json）")
    args = ap.parse_args()

    notes_dir = Path(args.notes_dir)
    if not notes_dir.is_dir():
        print("❌ 札记目录不存在: {}".format(notes_dir))
        return 1
    if args.fix_collisions:
        n = fix_citekey_collisions(notes_dir)
        print("🔧 撞键改键 {} 篇".format(n))
    index = update_index(notes_dir, full=args.full, since=args.since, until=args.until)
    wrote = write_outputs(index, notes_dir)
    uniq = [e for e in index["papers"] if not e.get("duplicate_of")]
    print("✅ 索引 {} 个月 / {} 篇（去重后 {}），撞键 {} 组 | 写盘: {}".format(
        len(index["months"]), len(index["papers"]), len(uniq),
        len(index["citekey_collisions"]),
        ", ".join(k for k, v in wrote.items() if v) or "无变化"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
