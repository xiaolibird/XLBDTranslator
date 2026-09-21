#!/usr/bin/env python3
"""原文截图的命令行入口：核验时直接看 PDF 版面，而不是相信文本抽取。

    # 看某几页（页码按札记口径：期刊印刷页或 PDF 页序，自动判别）
    PYTHONPATH=. python scripts/shot_pdf.py <pdf 或 citekey> --pages 8 10-11
    # 裁某几张图/表（中英文标签都行）
    PYTHONPATH=. python scripts/shot_pdf.py <pdf 或 citekey> --labels "Table 2" 图3 "Extended Data Fig 1"
    # 一篇论文的证据涉及的全部页面与图表（按索引里的 highlights）
    PYTHONPATH=. python scripts/shot_pdf.py <citekey> --all

输出默认落会话无关的临时目录 `/tmp/page_shots/<名字>/`（核验用的一次性图，不进札记库）；
加 `--out <目录>` 改位置。打印的每个路径都可以直接用 Read 工具打开看。

为什么核验要看图：文本抽取在双栏论文上会把左右两栏交错，表格会行列串位——脚本草稿
引错数字多半出在这里（见 memory: scholar-script-draft-bias）。图上的数字就是原文的数字。
归档时的截图不走这里：`read_pdf.py finalize` 会自动生成并嵌进 md/docx。
"""
import argparse
import json
import sys
from pathlib import Path

from src.scholar import page_shots as PS
from src.scholar.paths import repo_path


def _resolve(target: str):
    """PDF 路径原样用；否则当 citekey，到 Reading 等目录里找 `<citekey>.pdf`。返回 (pdf, 名字)。"""
    p = Path(target).expanduser()
    if p.suffix.lower() == ".pdf":
        if not p.is_file():
            sys.exit("⛔ PDF 不存在: {}".format(p))
        return p, p.stem
    import importlib.util
    spec = importlib.util.spec_from_file_location("locate_pdf", Path(__file__).resolve().parent / "locate_pdf.py")
    lp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lp)
    hits = lp.tier0_citekey_filename(target)
    if hits:
        return Path(hits[0]["path"]), target
    # 早期入库的 PDF 没按 citekey 改名：按索引标题到手动精读 bundle 里取 pdf_path
    from src.scholar.notes_index import norm_title
    notes = repo_path("output/scholar_notes")
    idx = json.loads((notes / "literature_index.json").read_text("utf-8"))
    titles = {norm_title(e.get("title")) for e in idx["papers"]
              if e.get("citekey") == target and e.get("duplicate_of") is None}
    for bf in sorted((notes / "manual").glob("*/*.paper.json")):
        try:
            d = json.loads(bf.read_text("utf-8"))
        except Exception:                                    # noqa: BLE001
            continue
        if norm_title(((d.get("segment") or {}).get("metadata") or {}).get("title")) in titles \
                and Path(d.get("pdf_path") or "").is_file():
            return Path(d["pdf_path"]), target
    sys.exit("⛔ 找不到 {} 的本地 PDF（Reading 下无 {}.pdf，bundle 里也没有可用的 pdf_path）；请直接给 PDF 路径"
             .format(target, target))


def main():
    ap = argparse.ArgumentParser(description="渲染论文的页面/图表截图，供核验时直接看原文版面")
    ap.add_argument("target", help="PDF 路径，或已按 citekey 归位的论文的 citekey")
    ap.add_argument("--pages", nargs="*", default=[], help="页码，如 8 10-11")
    ap.add_argument("--labels", nargs="*", default=[], help='图表标签，如 "Table 2" 图3')
    ap.add_argument("--all", action="store_true", help="按索引 highlights 渲染该 citekey 涉及的全部页面与图表")
    ap.add_argument("--out", default=None, help="输出目录（默认 /tmp/page_shots/<名字>）")
    args = ap.parse_args()

    pdf, name = _resolve(args.target)
    hl = [{"text": "", "page": p} for p in args.pages] + [{"text": lab, "page": None} for lab in args.labels]
    if args.all:
        idx = json.loads(repo_path("output/scholar_notes/literature_index.json").read_text("utf-8"))
        ent = [e for e in idx["papers"] if e.get("citekey") == name and e.get("duplicate_of") is None]
        if not ent:
            sys.exit("⛔ 索引里没有 citekey={}".format(name))
        hl += [{"text": h.get("text", ""), "page": h.get("pages") or h.get("page")} for h in ent[0].get("highlights") or []]
    if not hl:
        ap.error("至少给 --pages / --labels / --all 之一")

    out_root = Path(args.out).expanduser() if args.out else Path("/tmp/page_shots")
    got = PS.shots_for_paper(pdf, name, hl, out_root, force=True)
    m, miss = got.pop("_map", {}), got.pop("_missing", [])
    print("PDF: {}\n页码口径: {}{}".format(
        pdf, {"label": "原文印刷页（PDF 页标签）", "header": "原文印刷页（页眉/页脚推得）",
              "pdf-order": "PDF 页序", "none": "—",
              "pdf-order(header-ignored)": "PDF 页序（页眉印的是期刊页码，但给的页码低于首个印刷页）"}.get(m.get("how"), m.get("how")),
        "，PDF 第 1 页 = p.{}".format(m["offset"] + 1) if m.get("offset") else ""))
    for rel, v in sorted(got.items()):
        what = (v.get("label") or "p.{}".format(v.get("printed") or v.get("pdf_page")))
        extra = "  ← 裁不准，已退回整页" if v.get("how") == "figure-fallback" else (
            "  ← 按图注宽松匹配，请核对是不是这张" if "lenient" in (v.get("how") or "") else "")
        print("  {:22s} {}{}".format(what, out_root / rel, extra))
    if miss:
        print("⚠️ 未能截到: {}".format("、".join(miss)))
    return 0 if got else 1


if __name__ == "__main__":
    sys.exit(main())
