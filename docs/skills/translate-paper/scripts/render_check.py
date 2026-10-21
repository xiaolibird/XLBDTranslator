#!/usr/bin/env python3
"""把 PDF 渲染成缩略总览图 + 指定页高清图，供编排者亲眼检查排版。

用法:
    python3 -I render_check.py <pdf> <输出目录> [--pages 1,6]

产物：sheet0.png、sheet1.png……（每张 8 页，60 dpi），以及 hi-<页>.png（110 dpi）。
依赖本机 pdftoppm（poppler）与 Pillow。
"""
import argparse
import glob
import subprocess
from pathlib import Path

from PIL import Image


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("out")
    ap.add_argument("--pages", default="1", help="要出高清图的页码，逗号分隔")
    a = ap.parse_args()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("*.png"):
        f.unlink()

    subprocess.run(["pdftoppm", "-r", "60", "-png", a.pdf, str(out / "p")], check=True)
    pages = sorted(glob.glob(str(out / "p-*.png")))
    for k in range(0, len(pages), 8):
        ims = [Image.open(f) for f in pages[k:k + 8]]
        w, h = ims[0].size
        sheet = Image.new("RGB", (w * 4, h * 2), "white")
        for i, im in enumerate(ims):
            sheet.paste(im, ((i % 4) * w, (i // 4) * h))
        sheet.save(out / f"sheet{k // 8}.png")
    for f in pages:
        Path(f).unlink()

    for p in [int(x) for x in a.pages.split(",") if x.strip()]:
        subprocess.run(["pdftoppm", "-r", "110", "-png", "-f", str(p), "-l", str(p),
                        "-singlefile", a.pdf, str(out / f"hi-{p}")], check=True)

    print(f"共 {len(pages)} 页")
    for f in sorted(out.glob("*.png")):
        print(f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
