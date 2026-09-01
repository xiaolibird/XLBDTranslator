#!/usr/bin/env python3
"""把某个手动精读月份的原始 PDF 按 citekey 重命名，并回填 bundle 的 pdf_path。

    PYTHONPATH=. python scripts/relink_manual_pdfs.py --month 2026-07-28-TFM \
        --pdf-dir ~/Desktop/Lab/Reading/2026-07-28-读书报告联想阅读 --expect 16
    # 默认 dry-run 只打计划表，加 --apply 才动手

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
import shutil
import sys
from pathlib import Path

from src.scholar.notes_index import INDEX_JSON, dedup_key_fields, norm_title
from src.scholar.paths import repo_path
from src.scholar.pdf_ingest import BUNDLE_SUFFIX, load_bundle


def _die(msg, rows=None):
    print("⛔ " + msg, file=sys.stderr)
    for r in (rows or [])[:20]:
        print("     " + str(r), file=sys.stderr)
    sys.exit(2)


def build_plan(month, pdf_dir, notes_dir, expect):
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
        plan.append({
            "bundle": bf, "data": b, "citekey": ck, "matched_by": how,
            "src": Path(b.get("pdf_path") or ""), "dst": pdf_dir / (ck + ".pdf"),
        })

    # ---- 3) 全量校验：任一条不过就整体 abort，不做部分执行 ----
    if notfinal:
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
            _die("源 PDF 不存在: {}".format(p["src"]))
        if p["dst"].exists():
            _die("目标名已被占用（可能撞上目录里的其它 PDF）: {}".format(p["dst"]))
    return plan


def main():
    ap = argparse.ArgumentParser(description="按 citekey 重命名手动精读的 PDF 并回填 pdf_path")
    ap.add_argument("--month", required=True, help="bundle 的 month，如 2026-07-28-TFM")
    ap.add_argument("--pdf-dir", required=True, help="PDF 归档目录")
    ap.add_argument("--notes-dir", default=None, help="默认 output/scholar_notes")
    ap.add_argument("--expect", type=int, default=0, help="期望篇数，对不上就 abort")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认只 dry-run）")
    args = ap.parse_args()

    pdf_dir = Path(args.pdf_dir).expanduser().resolve()
    notes_dir = (Path(args.notes_dir).expanduser().resolve() if args.notes_dir
                 else repo_path("output/scholar_notes"))

    plan = build_plan(args.month, pdf_dir, notes_dir, args.expect)

    print("{:28} {:6} {:38} → {}".format("citekey", "配法", "当前文件名", "目标文件名"))
    print("-" * 118)
    for p in sorted(plan, key=lambda x: x["citekey"]):
        mark = "＝" if p["already"] else ("补" if p["backfill_only"] else "→")
        print("{:28} {:6} {:38} {} {}".format(
            p["citekey"], p["matched_by"], p["src"].name or "(空)", mark, p["dst"].name))
    todo = [p for p in plan if not p["already"] and not p["backfill_only"]]
    backfill = [p for p in plan if p["backfill_only"]]
    print("\n共 {} 篇，需改名 {} 篇，仅补回填 {} 篇，已就位 {} 篇".format(
        len(plan), len(todo), len(backfill), len(plan) - len(todo) - len(backfill)))

    if not args.apply:
        print("（dry-run，未改动任何文件；加 --apply 执行）")
        return 0

    # ---- 4) 先搬文件，再原子回写 bundle ----
    # 顺序不可颠倒：反过来的话 mv 失败会留下指向不存在文件的 pdf_path，更难查。
    # 这个顺序下最坏是文件已改名而 bundle 未更新，重跑时经 backfill_only 半完成
    # 识别（src 缺失且 dst 已就位）跳过 mv、只补回填 pdf_path，自愈闭环。
    for p in plan:
        if not p["already"] and not p["backfill_only"]:
            shutil.move(str(p["src"]), str(p["dst"]))
        p["data"]["pdf_path"] = str(p["dst"])
        tmp = p["bundle"].with_suffix(".tmp")
        # 字节格式必须与 pdf_ingest.write_bundle 一致，否则以后 diff 全是噪声
        tmp.write_text(json.dumps(p["data"], ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(p["bundle"])
    print("✅ {} 篇已按 citekey 归位并回填 pdf_path".format(len(plan)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
