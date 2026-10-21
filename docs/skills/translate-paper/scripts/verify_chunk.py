#!/usr/bin/env python3
"""核对一块译文与原文的结构是否一致。

用法:
    python3 -I verify_chunk.py <原文.tex> <译文.tex> [<原文2> <译文2> ...]

检查：\\label/\\ref/\\eqref/\\cite*/\\begin/\\end/\\input/\\includegraphics 计数与参数、
$ 数、花括号配平、行内与行间数学的多重集（顺序可因中文语序变化，内容必须逐字相同，
比较前去掉空白）。全部通过退出码 0，否则 1。

--ignore-text：比较数学时把 \\text{...} 的内容清空。用于编排者把公式里的英文说明改成中文之后的复核。
"""
import re
import sys
from collections import Counter
from pathlib import Path

ARG_CMDS = ["label", "ref", "eqref", "cref", "Cref", "autoref", "input", "include",
            "includegraphics"]
CITE = re.compile(r"\\cite[a-zA-Z]*\*?(?:\[[^\]]*\])*\{([^}]*)\}")
DISPLAY_ENVS = r"equation\*?|align\*?|gather\*?|multline\*?|eqnarray\*?|flalign\*?"


def strip_comments(s: str) -> str:
    return re.sub(r"(?<!\\)%.*", "", s)


def args(s: str, cmd: str) -> list[str]:
    return re.findall(rf"\\{cmd}(?:\[[^\]]*\])?\{{([^}}]*)\}}", s)


IGNORE_TEXT = False


def norm_math(v: str) -> str:
    v = re.sub(r"\s+", "", v)
    if IGNORE_TEXT:
        v = re.sub(r"\\text\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", r"\\text{}", v)
    return v


def math_pieces(s: str) -> Counter:
    pieces = []
    pieces += re.findall(rf"\\begin\{{({DISPLAY_ENVS})\}}(.*?)\\end\{{\1\}}", s, re.S)
    s2 = re.sub(rf"\\begin\{{({DISPLAY_ENVS})\}}.*?\\end\{{\1\}}", " ", s, flags=re.S)
    pieces += [("[]", m) for m in re.findall(r"\\\[(.*?)\\\]", s2, re.S)]
    s2 = re.sub(r"\\\[.*?\\\]", " ", s2, flags=re.S)
    pieces += [("$$", m) for m in re.findall(r"\$\$(.*?)\$\$", s2, re.S)]
    s2 = re.sub(r"\$\$.*?\$\$", " ", s2, flags=re.S)
    pieces += [("$", m) for m in re.findall(r"(?<!\\)\$(.+?)(?<!\\)\$", s2, re.S)]
    pieces += [("()", m) for m in re.findall(r"\\\((.*?)\\\)", s2, re.S)]
    return Counter((k, norm_math(v)) for k, v in pieces)


def braces(s: str) -> tuple[int, int]:
    t = s.replace(r"\{", "").replace(r"\}", "")
    return t.count("{"), t.count("}")


def check(en_path: Path, zh_path: Path) -> list[str]:
    en = strip_comments(en_path.read_text(errors="ignore"))
    zh = strip_comments(zh_path.read_text(errors="ignore"))
    probs = []
    for cmd in ARG_CMDS:
        a, b = Counter(args(en, cmd)), Counter(args(zh, cmd))
        if a != b:
            probs.append(f"\\{cmd}: 原文 {sum(a.values())} / 译文 {sum(b.values())}，"
                         f"缺 {dict(a - b)} 多 {dict(b - a)}")
    a, b = Counter(CITE.findall(en)), Counter(CITE.findall(zh))
    if a != b:
        probs.append(f"\\cite: 缺 {dict(a - b)} 多 {dict(b - a)}")
    for env in ("begin", "end"):
        a, b = Counter(args(en, env)), Counter(args(zh, env))
        if a != b:
            probs.append(f"\\{env}: 缺 {dict(a - b)} 多 {dict(b - a)}")
    if en.count("$") - en.count(r"\$") != zh.count("$") - zh.count(r"\$"):
        probs.append(f"$ 数不同：原文 {en.count('$')} / 译文 {zh.count('$')}")
    be, bz = braces(en), braces(zh)
    if (be[0] - be[1]) != (bz[0] - bz[1]):
        probs.append(f"花括号差额不同：原文 {be} / 译文 {bz}")
    ma, mb = math_pieces(en), math_pieces(zh)
    if ma != mb:
        miss, extra = ma - mb, mb - ma
        probs.append(f"数学片段不一致：缺 {sum(miss.values())} 段，多 {sum(extra.values())} 段")
        for (k, v), _ in list(miss.items())[:5]:
            probs.append(f"    缺 [{k}] {v[:100]}")
        for (k, v), _ in list(extra.items())[:5]:
            probs.append(f"    多 [{k}] {v[:100]}")
    return probs


def main() -> int:
    global IGNORE_TEXT
    pairs = [x for x in sys.argv[1:] if x != "--ignore-text"]
    IGNORE_TEXT = "--ignore-text" in sys.argv[1:]
    if not pairs or len(pairs) % 2:
        print(__doc__)
        return 2
    bad = 0
    for en, zh in zip(pairs[::2], pairs[1::2]):
        probs = check(Path(en), Path(zh))
        status = "✅" if not probs else "❌"
        print(f"{status} {Path(zh).name}")
        for p in probs:
            print(f"   {p}")
        bad += bool(probs)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
