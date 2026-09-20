#!/usr/bin/env python3
"""把某个手动精读月份的原始 PDF 按 citekey 重命名，并回填 bundle 的 pdf_path。

    PYTHONPATH=. python scripts/relink_manual_pdfs.py --month 2026-07-28-TFM \
        --pdf-dir ~/Desktop/Lab/Reading/2026-07-28-读书报告联想阅读 --expect 16
    # 默认 dry-run 只打计划表，加 --apply 才动手

    # 归位模式（2026-09-20）：不指定 --pdf-dir，只处理 pdf_path **不在 Reading 下**的 bundle，
    # 把 PDF 搬进 ~/Desktop/Lab/Reading/<目录>/<citekey>.pdf 并回填。finalize 收尾会自动调它。
    PYTHONPATH=. python scripts/relink_manual_pdfs.py --month 2026-09 --home [--reading-dir 2026-09-20-SM源头] --apply

归位模式的目标目录：--reading-dir 优先；否则桶名带日期（YYYY-MM-DD[-主题]）就用桶名，
纯月份桶（YYYY-MM）用该 bundle 文件的修改日期（≈ 精读当天）建纯日期目录。已在 Reading 下的
PDF 一律不碰——那是用户按主题手工摆好的，既不挪目录也不改名。

为什么要有这一步：citekey 只在 finalize 写 md 时才生成（bundle 的 segment.citekey
恒为 null），而 ingest 早就把 PDF 的绝对路径写死进 bundle.pdf_path 了。想在 ingest
前知道 citekey 是不可能的，所以只能「临时名 ingest → 定型后一次性改名 + 回填」。

join 键用 notes_index.dedup_key_fields，并限定到 series=manual + 指定 month。
不是模糊匹配：索引里的 doi/arxiv_id/title 本就是从 bundle 的 segment.metadata 经
segment_from_bundle → entry_from_segment → sidecar → build_month_entries 逐字搬过去
的同一串，所以三级键必然精确命中。自写一套归一化只会引入与 _global_pass 判重打架
的第二口径。限定 month+series 是为了杜绝跨月同题串号（同一篇的 arXiv 版与正刊版
在全库范围内是两条记录）。
"""
import argparse
import json
import os
import re
import shutil
import sys
from datetime import date
from pathlib import Path

from src.scholar.notes_index import INDEX_JSON, dedup_key_fields, norm_title
from src.scholar.paths import repo_path
from src.scholar.pdf_ingest import BUNDLE_SUFFIX, load_bundle


def reading_root() -> Path:
    """精读 PDF 的归档根。环境变量仅供测试把根指到临时目录。"""
    return Path(os.environ.get("SCHOLAR_READING_ROOT")
                or (Path.home() / "Desktop" / "Lab" / "Reading")).expanduser().resolve()


_DATED_BUCKET = re.compile(r"^\d{4}-\d{2}-\d{2}(-.+)?$")


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def home_dir_for(month: str, bundle_file: Path, reading_dir: str = "") -> Path:
    """归位模式下一篇 PDF 该进哪个 Reading 子目录（规则见模块文档）。"""
    root = reading_root()
    if reading_dir:
        return root / reading_dir
    if _DATED_BUCKET.match(month):
        return root / month
    return root / date.fromtimestamp(bundle_file.stat().st_mtime).isoformat()


class PlanError(Exception):
    """计划校验不过。CLI 下转成 exit 2；被 finalize 内嵌调用时由调用方接住降级为告警。"""

    def __init__(self, msg, rows=None):
        super().__init__(msg)
        self.rows = rows or []


def _die(msg, rows=None):
    raise PlanError(msg, rows)


def _exit_with(e: "PlanError"):
    print("⛔ " + str(e), file=sys.stderr)
    for r in e.rows[:20]:
        print("     " + str(r), file=sys.stderr)
    sys.exit(2)


def build_plan(month, pdf_dir, notes_dir, expect, home=False, reading_dir=""):
    idx = json.loads((notes_dir / INDEX_JSON).read_text("utf-8"))

    # ---- 1) citekey 查找表，定域到本批 ----
    by_key, by_title, dup = {}, {}, []
    for e in idx.get("papers", []):
        if e.get("series") != "manual" or e.get("month") != month:
            continue
        k = dedup_key_fields(e.get("doi"), e.get("arxiv_id"), e.get("title"), fallback="")
        if k in by_key:
            dup.append(k)
        by_key[k] = e["citekey"]
        by_title[norm_title(e.get("title"))] = e["citekey"]
    if dup:
        _die("索引内 join 键重复，先查 literature_index：", dup)
    if not by_key:
        _die("索引里没有 series=manual & month={} 的条目——先跑 finalize".format(month))

    # ---- 2) 扫 bundle 出计划，全程只读 ----
    bundle_dir = notes_dir / "manual" / month
    if not bundle_dir.is_dir():
        _die("bundle 目录不存在: {}".format(bundle_dir))

    plan, unmatched, notfinal = [], [], []
    for bf in sorted(bundle_dir.glob("*" + BUNDLE_SUFFIX)):
        b = load_bundle(bf)
        if b.get("status") != "final":
            notfinal.append(bf.name)
            continue
        m = (b.get("segment") or {}).get("metadata") or {}
        k = dedup_key_fields(m.get("doi"), m.get("arxiv_id"), m.get("title"),
                             fallback=b.get("paper_id", ""))
        ck = by_key.get(k)
        how = "key"
        if not ck:
            ck = by_title.get(norm_title(m.get("title")))
            how = "title"
        if not ck:
            unmatched.append((bf.name, (m.get("title") or "")[:60]))
            continue
        src = Path(b.get("pdf_path") or "")
        if home:
            if src.name and _under(src, reading_root()):
                continue        # 已在 Reading 下：用户手工摆好的，不挪不改名
            dst = home_dir_for(month, bf, reading_dir) / (ck + ".pdf")
        else:
            dst = pdf_dir / (ck + ".pdf")
        plan.append({
            "bundle": bf, "data": b, "citekey": ck, "matched_by": how,
            "src": src, "dst": dst,
        })

    # ---- 3) 全量校验：任一条不过就整体 abort，不做部分执行 ----
    if notfinal and not home:     # 归位模式下 draft 只是「还没轮到」，跳过即可
        _die("有 {} 篇未 final：".format(len(notfinal)), notfinal)
    if unmatched:
        _die("有 {} 篇 join 不上（禁止猜测）：".format(len(unmatched)), unmatched)
    if expect and len(plan) != expect:
        _die("计划 {} 篇 ≠ 期望 {} 篇".format(len(plan), expect))
    cks = [p["citekey"] for p in plan]
    if len(set(cks)) != len(cks):
        _die("citekey 撞键，先跑 notes_index.py --fix-collisions", sorted(cks))
    for p in plan:
        p["already"] = p["src"].resolve() == p["dst"].resolve() if p["src"].name else False
        # 半完成识别：上次 --apply 在「mv 成功、bundle 回填前」中断时，src（bundle 里的
        # 旧路径）已不存在而 dst 已就位——dst 名由 citekey 决定论推出，不是猜测。此时
        # 视为 mv 已完成，本次跳过 mv 只补写 pdf_path，让「重跑自愈」真正成立而非整体 abort。
        p["backfill_only"] = (not p["already"] and bool(p["src"].name)
                              and not p["src"].is_file() and p["dst"].is_file())
        if p["already"] or p["backfill_only"]:
            continue
        if not p["src"].is_file():
            if home:
                # 归位模式是 finalize 的收尾：一条死链不该拦住同月其余篇目归位，标出来即可
                p["dead"] = True
                continue
            _die("源 PDF 不存在: {}".format(p["src"]))
        if p["dst"].exists():
            _die("目标名已被占用（可能撞上目录里的其它 PDF）: {}".format(p["dst"]))
    return plan


def apply_plan(plan):
    """先搬文件，再原子回写 bundle。返回实际处理的篇数（死链不计）。

    顺序不可颠倒：反过来的话 mv 失败会留下指向不存在文件的 pdf_path，更难查。
    这个顺序下最坏是文件已改名而 bundle 未更新，重跑时经 backfill_only 半完成
    识别（src 缺失且 dst 已就位）跳过 mv、只补回填 pdf_path，自愈闭环。
    """
    n = 0
    for p in plan:
        if p.get("dead"):
            continue
        if not p["already"] and not p["backfill_only"]:
            p["dst"].parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p["src"]), str(p["dst"]))
        p["data"]["pdf_path"] = str(p["dst"])
        tmp = p["bundle"].with_suffix(".tmp")
        # 字节格式必须与 pdf_ingest.write_bundle 一致，否则以后 diff 全是噪声
        tmp.write_text(json.dumps(p["data"], ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(p["bundle"])
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser(description="按 citekey 重命名手动精读的 PDF 并回填 pdf_path")
    ap.add_argument("--month", required=True, help="bundle 的 month，如 2026-07-28-TFM")
    ap.add_argument("--pdf-dir", default=None, help="PDF 归档目录（与 --home 二选一）")
    ap.add_argument("--home", action="store_true",
                    help="归位模式：只处理 pdf_path 不在 Reading 下的 bundle，搬进 Reading 并回填")
    ap.add_argument("--reading-dir", default="",
                    help="归位模式的目标子目录名（如 2026-09-20-SM源头）；缺省按桶名/精读日期推")
    ap.add_argument("--notes-dir", default=None, help="默认 output/scholar_notes")
    ap.add_argument("--expect", type=int, default=0, help="期望篇数，对不上就 abort")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认只 dry-run）")
    args = ap.parse_args()

    if bool(args.pdf_dir) == bool(args.home):
        ap.error("--pdf-dir 与 --home 必须二选一")
    pdf_dir = Path(args.pdf_dir).expanduser().resolve() if args.pdf_dir else None
    notes_dir = (Path(args.notes_dir).expanduser().resolve() if args.notes_dir
                 else repo_path("output/scholar_notes"))

    try:
        plan = build_plan(args.month, pdf_dir, notes_dir, args.expect,
                          home=args.home, reading_dir=args.reading_dir)
    except PlanError as e:
        _exit_with(e)

    print("{:28} {:6} {:38} → {}".format("citekey", "配法", "当前文件名", "目标文件名"))
    print("-" * 118)
    for p in sorted(plan, key=lambda x: x["citekey"]):
        mark = ("✗死链" if p.get("dead") else
                "＝" if p["already"] else ("补" if p["backfill_only"] else "→"))
        shown = (str(Path(p["dst"].parent.name) / p["dst"].name) if args.home else p["dst"].name)
        print("{:28} {:6} {:38} {} {}".format(
            p["citekey"], p["matched_by"], p["src"].name or "(空)", mark, shown))
    todo = [p for p in plan if not p["already"] and not p["backfill_only"] and not p.get("dead")]
    backfill = [p for p in plan if p["backfill_only"]]
    print("\n共 {} 篇，需改名 {} 篇，仅补回填 {} 篇，已就位 {} 篇".format(
        len(plan), len(todo), len(backfill), len(plan) - len(todo) - len(backfill)))

    if not args.apply:
        print("（dry-run，未改动任何文件；加 --apply 执行）")
        return 0

    n = apply_plan(plan)
    print("✅ {} 篇已按 citekey 归位并回填 pdf_path".format(n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
