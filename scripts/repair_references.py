# -*- coding: utf-8 -*-
"""重建某个月度札记缺失的 `{stem}.references.json`（CSL-JSON 书目）。

用于修复「md 在、CSL 不在」的月份——通常是 backfill 中途被打断留下的。缺了它，
`build_all_references` 只能用 `_fallback_csl` 从索引字段兜底，产出的条目**没有卷、期、页**，
pandoc 渲染出的参考文献会缺信息。

    PYTHONPATH=. python scripts/repair_references.py --month 2026-06 --dry-run
    PYTHONPATH=. python scripts/repair_references.py --month 2026-06

元数据优先级：Crossref（按 DOI 直查，给卷期页）→ arXiv（按 id，给权威作者）→ 索引字段兜底。
arXiv 预印本本就没有卷期页，走兜底是正确结果而非退化。

**默认拒绝覆盖已存在的 CSL**（--force 才覆盖）：那些文件是 write_notes 在有 PaperSegment
上下文时产出的，信息比这里能重建的更全，不该被这个工具盖掉。
退出码 0=成功 / 1=该月无条目或全部失败 / 2=参数或索引问题。
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.notes_index import INDEX_JSON, _fallback_csl   # noqa: E402
from src.utils.logger import get_logger                         # noqa: E402

logger = get_logger("repair_refs")

# arXiv 序号是 4 位（2015 前）或 5 位。末尾的 (?![\d.]) 是必需的边界保护——否则畸形
# DOI（如 6 位序号）会被**截断**成一个看似合法的 id，拿去查 arXiv 只会静默取回错的论文。
ARXIV_IN_DOI = re.compile(r"^10\.48550/arxiv\.(\d{4}\.\d{4,5})(?![\d.])", re.I)


def _csl_from_crossref(item, entry):
    """Crossref 归一化 dict → CSL 条目（沿用月度 CSL 的字段口径）。"""
    out = {"id": entry["citekey"], "type": "article-journal",
           "title": item.get("title") or entry.get("title") or ""}
    if item.get("authors"):
        au = []
        for n in item["authors"]:
            parts = (n or "").split()
            if len(parts) == 1:
                au.append({"family": parts[0]})
            elif parts:
                au.append({"family": parts[-1], "given": " ".join(parts[:-1])})
        if au:
            out["author"] = au
    for src, dst in (("journal", "container-title"), ("doi", "DOI"), ("url", "URL"),
                     ("volume", "volume"), ("issue", "issue"), ("pages", "page")):
        if item.get(src):
            out[dst] = item[src]
    d = item.get("publication_date")
    if d is not None:
        out["issued"] = {"date-parts": [[d.year, d.month, d.day]]}
    elif entry.get("year"):
        out["issued"] = {"date-parts": [[entry["year"]]]}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="重建月度札记缺失的 CSL-JSON 书目")
    ap.add_argument("--month", required=True, help="月份标签，如 2026-06 或 2026-07-27")
    ap.add_argument("--series", default="auto", choices=["auto", "manual"])
    ap.add_argument("--notes-dir", default="output/scholar_notes")
    ap.add_argument("--email", default="", help="Crossref 礼貌池邮箱（建议填）")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的 CSL（默认拒绝）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    nd = Path(args.notes_dir)
    ip = nd / INDEX_JSON
    if not ip.exists():
        print("找不到索引：{}".format(ip), file=sys.stderr)
        return 2
    try:
        idx = json.loads(ip.read_text(encoding="utf-8"))
        assert isinstance(idx.get("papers"), list)
    except Exception as exc:
        print("索引损坏（{}）：{}".format(type(exc).__name__, ip), file=sys.stderr)
        return 2

    rows = [e for e in idx["papers"]
            if e.get("month") == args.month and e.get("series", "auto") == args.series]
    if not rows:
        print("索引中没有 month={} series={} 的条目".format(args.month, args.series),
              file=sys.stderr)
        return 1

    suffix = "全文精读" if args.series == "auto" else "手动精读"
    stem = "科研札记_{}_{}".format(args.month, suffix)
    out_path = nd / "{}.references.json".format(stem)
    if out_path.exists() and not args.force:
        print("已存在且更权威，拒绝覆盖：{}\n（确需重建请加 --force）".format(out_path),
              file=sys.stderr)
        return 2
    if not (nd / "{}.md".format(stem)).exists():
        print("对应札记不存在：{}.md".format(stem), file=sys.stderr)
        return 2

    from src.scholar.crossref import crossref_by_doi
    from src.scholar.academic_search import fetch_arxiv_by_id
    from src.scholar.fulltext import ipv4_client

    items, n_cr, n_ax, n_fb = [], 0, 0, 0
    with ipv4_client(timeout=30) as cli:
        for e in rows:
            got = None
            if e.get("doi") and not e["doi"].lower().startswith("10.48550/arxiv"):
                try:
                    got = crossref_by_doi(e["doi"], email=args.email, client=cli)
                except Exception:
                    got = None
            if got:
                items.append(_csl_from_crossref(got, e))
                n_cr += 1
                continue
            # 索引里 arxiv_id 常为空，但 arXiv 的 DOI 形如 10.48550/arXiv.2606.11570，
            # id 就嵌在里面——不提取就永远走不到这一支。
            aid = e.get("arxiv_id")
            if not aid:
                m = ARXIV_IN_DOI.match(e.get("doi") or "")
                aid = m.group(1) if m else None
            if aid:
                try:
                    ax = fetch_arxiv_by_id(aid, cli)
                except Exception as exc:      # arXiv API 限流/超时是常态，退回索引即可
                    logger.warning("  ⚠️ arXiv 查询失败（{}）: {}".format(aid, type(exc).__name__))
                    ax = None
                if ax and ax.get("authors"):
                    merged = dict(e)
                    merged["authors"] = ax["authors"]       # 用 arXiv 的权威作者覆盖
                    items.append(_fallback_csl(merged))
                    n_ax += 1
                    continue
            items.append(_fallback_csl(e))
            n_fb += 1

    logger.info("  {} 条：Crossref {} · arXiv {} · 索引兜底 {}".format(
        len(items), n_cr, n_ax, n_fb))
    rich = sum(1 for it in items if it.get("volume") or it.get("page"))
    if args.dry_run:
        print("\n【dry-run】将写 {}（{} 条，其中 {} 条带卷/页）".format(
            out_path, len(items), rich))
        return 0
    out_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n{}\n✅ {}（{} 条，其中 {} 条带卷/页）\n   下一步：PYTHONPATH=. python scripts/notes_index.py".format(
        "=" * 66, out_path, len(items), rich))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)
