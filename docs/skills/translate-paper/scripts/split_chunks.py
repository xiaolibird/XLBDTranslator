#!/usr/bin/env python3
"""把主 .tex 切成导言区 + 若干正文块，供子代理并行翻译。

用法:
    python3 -I split_chunks.py <主 .tex> <chunks 输出目录> [--target 15000] [--max-chunks 8]

只在 \\section / \\subsection / \\appendix 行切，保证不切断环境。
产物：preamble.en.tex（\\begin{document} 之前）、A.en.tex、B.en.tex……、manifest.json。
\\input / \\include 的子文件不展开，manifest 里列出来，由编排者决定哪些要单独派翻译。
"""
import argparse
import json
import re
from pathlib import Path

CUT = re.compile(r"^\s*\\(section|subsection|appendix)\b")
INPUT = re.compile(r"\\(?:input|include)\{([^}]+)\}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tex")
    ap.add_argument("out")
    ap.add_argument("--target", type=int, default=15000, help="每块目标字符数")
    ap.add_argument("--max-chunks", type=int, default=8)
    a = ap.parse_args()

    src = Path(a.tex)
    lines = src.read_text(errors="ignore").splitlines(keepends=True)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    begin = next(i for i, l in enumerate(lines) if re.match(r"\s*\\begin\{document\}", l))
    (out / "preamble.en.tex").write_text("".join(lines[:begin]))
    body = lines[begin:]

    # 切点候选 = 每个 \section/\subsection/\appendix 行
    cuts = [i for i, l in enumerate(body) if CUT.match(l)]
    total = sum(len(l) for l in body)
    target = max(a.target, total // a.max_chunks + 1)

    starts, acc = [0], 0
    sizes = [len(l) for l in body]
    for i in range(len(body)):
        if i in cuts and i != starts[-1] and acc >= target:
            starts.append(i)
            acc = 0
        acc += sizes[i]
    starts.append(len(body))

    manifest = {"source": str(src), "begin_document_line": begin + 1, "chunks": [],
                "inputs": []}
    for k in range(len(starts) - 1):
        name = chr(ord("A") + k)
        seg = body[starts[k]:starts[k + 1]]
        (out / f"{name}.en.tex").write_text("".join(seg))
        first = next((l.strip()[:80] for l in seg if CUT.match(l)), seg[0].strip()[:80])
        manifest["chunks"].append({
            "name": name,
            "lines": [begin + starts[k] + 1, begin + starts[k + 1]],
            "chars": sum(len(l) for l in seg),
            "starts_with": first,
        })
    for f in INPUT.findall("".join(body)):
        p = src.parent / (f if f.endswith(".tex") else f + ".tex")
        manifest["inputs"].append({"file": str(p.relative_to(src.parent)),
                                   "chars": p.stat().st_size if p.exists() else None})

    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
