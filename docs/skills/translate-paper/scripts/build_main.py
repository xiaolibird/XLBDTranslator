#!/usr/bin/env python3
"""把导言区中文化，拼接译文块，写入译者注（出处 + 许可），产出可用 xelatex 编译的主文件。

用法:
    python3 -I build_main.py --work <工作目录> --chunks <chunks 目录> --out <build 目录>/main_zh.tex

前提：<工作目录>/meta.json（fetch_arxiv.py 产出）、<chunks>/preamble.en.tex、<chunks>/*.zh.tex。
build 目录应事先由编排者复制好 eprint/ 全部文件，并用译好的 \\input 子文件覆盖同名原件。
"""
import argparse
import json
import re
from pathlib import Path

THEOREM_ZH = {
    "Theorem": "定理", "Proposition": "命题", "Lemma": "引理", "Corollary": "推论",
    "Definition": "定义", "Assumption": "假设", "Remark": "注", "Example": "例",
    "Conjecture": "猜想", "Claim": "断言", "Fact": "事实", "Observation": "观察",
    "Condition": "条件", "Notation": "记号", "Problem": "问题", "Hypothesis": "假说",
    "Property": "性质", "Algorithm": "算法",
}
CREF_ZH = r"""
\makeatletter\@ifpackageloaded{cleveref}{%
\crefformat{section}{第~#2#1#3~节}\Crefformat{section}{第~#2#1#3~节}%
\crefformat{subsection}{第~#2#1#3~节}\Crefformat{subsection}{第~#2#1#3~节}%
\crefformat{appendix}{附录~#2#1#3}\Crefformat{appendix}{附录~#2#1#3}%
\crefformat{equation}{式~(#2#1#3)}\Crefformat{equation}{式~(#2#1#3)}%
\crefname{figure}{图}{图}\Crefname{figure}{图}{图}%
\crefname{table}{表}{表}\Crefname{table}{表}{表}%
\crefname{theorem}{定理}{定理}\crefname{lemma}{引理}{引理}%
\crefname{proposition}{命题}{命题}\crefname{corollary}{推论}{推论}%
\crefname{definition}{定义}{定义}\crefname{assumption}{假设}{假设}%
\crefname{remark}{注}{注}\crefname{algorithm}{算法}{算法}%
}{}\makeatother
"""
NAMES_ZH = r"""
\renewcommand{\proofname}{证明}
\renewcommand{\figurename}{图}
\renewcommand{\tablename}{表}
\renewcommand{\appendixname}{附录}
\renewcommand{\abstractname}{摘要}
\providecommand{\algorithmname}{算法}
"""


def localize_preamble(pre: str, times_like: bool) -> str:
    # pdflatex 专用行在 xelatex 下要么报错要么和 fontspec 打架
    pre = re.sub(r"^(\s*\\pdfoutput\s*=\s*1)", r"% \1", pre, flags=re.M)
    pre = re.sub(r"^(\s*\\usepackage\[[^\]]*\]\{(inputenc|fontenc)\})", r"% \1", pre, flags=re.M)
    pre = re.sub(r"^(\s*\\usepackage\{(inputenc|fontenc)\})", r"% \1", pre, flags=re.M)
    # ctex 紧跟 \documentclass
    pre = re.sub(r"(\\documentclass(\[[^\]]*\])?\{[^}]+\}[^\n]*\n)",
                 r"\1\\usepackage[UTF8,scheme=chinese,heading=false,fontset=fandol,linespread=1.15]{ctex}\n",
                 pre, count=1)

    # \newtheorem{env}[shared]{Name}[within] 里的 Name
    def th(m):
        name = m.group(3)
        return m.group(1) + "{" + THEOREM_ZH.get(name, name) + "}"
    pre = re.sub(r"(\\newtheorem\*?\{[^}]+\}(\[[^\]]*\])?)\{([A-Za-z ]+)\}", th, pre)

    tail = NAMES_ZH + r"\AtBeginDocument{\captionsetup{labelsep=quad}}" + "\n" + CREF_ZH
    if times_like:
        tail += r"\setmainfont{TeX Gyre Termes}\setsansfont{TeX Gyre Heros}\setmonofont{TeX Gyre Cursor}" + "\n"
    return pre.rstrip() + "\n\n% ===== translate-paper：中文化 =====\n" + tail + "\n"


def notice_text(meta: dict) -> str:
    authors = meta.get("authors") or []
    who = authors[0].split(",")[0] + " 等人" if len(authors) > 1 else (authors[0] if authors else "原作者")
    return (r"\textbf{译者注：}本文是 " + who + r"论文 \emph{" + meta["title"] + r"}（arXiv:"
            + meta["arxiv_id"] + r"，\url{" + meta["abs_url"] + r"}）的中文译本。原文按 "
            + meta["license"] + r" 许可发布（\url{" + meta["license_url"] + r"}），本译文依同一许可提供。"
            r"译文未经原作者审阅，公式、表格数据与参考文献保留原文，如有出入以原文为准。")


def insert_notice(body: str, note: str) -> tuple[str, str]:
    m = re.search(r"\\printAffiliationsAndNotice\{[^}]*\}", body)
    if m:
        return body[:m.start()] + r"\printAffiliationsAndNotice{" + note + "}" + body[m.end():], "printAffiliationsAndNotice"
    block = "\n{\\par\\noindent\\footnotesize " + note + "\\par\\medskip}\n"
    m = re.search(r"\\end\{abstract\}", body)
    if m:
        return body[:m.end()] + block + body[m.end():], "after-abstract"
    m = re.search(r"\\maketitle", body)
    if m:
        return body[:m.end()] + block + body[m.end():], "after-maketitle"
    m = re.search(r"\\begin\{document\}", body)
    return body[:m.end()] + block + body[m.end():], "after-begin-document"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--chunks", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    work, chunks, out = Path(a.work), Path(a.chunks), Path(a.out)
    meta = json.loads((work / "meta.json").read_text())
    pre = (chunks / "preamble.en.tex").read_text()
    build = out.parent
    styles = "".join(p.read_text(errors="ignore") for p in build.glob("*.sty")) + pre
    times_like = bool(re.search(r"\{(times|mathptmx|newtxtext|ptm)\}", styles))

    parts = sorted(p for p in chunks.glob("[A-Z].zh.tex"))
    missing = [p.name.replace(".en.", ".zh.") for p in chunks.glob("[A-Z].en.tex")
               if not (chunks / p.name.replace(".en.", ".zh.")).exists()]
    if missing:
        raise SystemExit(f"缺译文块：{missing}")
    body = "".join(p.read_text() for p in parts)
    body, where = insert_notice(body, notice_text(meta))

    out.write_text(localize_preamble(pre, times_like) + body)

    # 已知样式文件里写死的英文标签（本地副本，可随时加条目）
    patched = []
    for sty in build.glob("*.sty"):
        s = sty.read_text(errors="ignore")
        s2 = s.replace(r"\centerline{\large\bf Abstract}", r"\centerline{\large\bf 摘要}")
        s2 = s2.replace("{ }Correspondence to: ", "{ }通讯作者：")
        if s2 != s:
            sty.write_text(s2)
            patched.append(sty.name)

    print(f"写出 {out}（{len(parts)} 块，译者注位置：{where}，Times 字体替换：{times_like}，"
          f"样式补丁：{patched or '无'}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
