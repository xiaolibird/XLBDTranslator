#!/usr/bin/env python3
"""准备用户提供的论文文件，或检查 arXiv 许可并下载源码。

用法: fetch_arxiv.py <arXiv id 或链接> <工作目录> [--provided-file <本地 PDF/TeX/目录>]
用户提供模式离线运行；链接模式保留许可检查。meta.json 区分许可与翻译依据。
"""
import argparse
import shutil
import gzip
import io
import json
import re
import sys
import tarfile
import urllib.request
from pathlib import Path

UA = {"User-Agent": "translate-paper-skill/0.1 (personal research use)"}

ALLOW = {
    "by": "CC BY",
    "by-sa": "CC BY-SA",
    "by-nc": "CC BY-NC",
    "by-nc-sa": "CC BY-NC-SA",
    "zero": "CC0",
}
BLOCK_CC = {"by-nd": "CC BY-ND", "by-nc-nd": "CC BY-NC-ND"}


def get(url: str) -> bytes:
    # arXiv 的 http 会静默返回空，必须走 https
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def classify(abs_html: str) -> tuple[str, str, str]:
    """返回 (许可名, 许可 URL, verdict)。verdict ∈ allow / block / unknown。"""
    m = re.search(r"creativecommons\.org/(licenses|publicdomain)/([a-z-]+)/([\d.]+)/?", abs_html)
    if m:
        kind, code, ver = m.groups()
        url = f"https://creativecommons.org/{kind}/{code}/{ver}/"
        if code in ALLOW:
            return f"{ALLOW[code]} {ver}", url, "allow"
        if code in BLOCK_CC:
            return f"{BLOCK_CC[code]} {ver}", url, "block"
        return f"CC {code} {ver}", url, "unknown"
    m = re.search(r"arxiv\.org/licenses/([a-z-]+)/([\d.]+)/?", abs_html)
    if m:
        code, ver = m.groups()
        url = f"http://arxiv.org/licenses/{code}/{ver}/"
        return f"arXiv {code} {ver}", url, "block"
    return "未声明（默认 arXiv 分发许可）", "", "block"


def meta_tags(abs_html: str, name: str) -> list[str]:
    return [m for m in re.findall(rf'<meta name="{name}" content="([^"]*)"', abs_html)]


def extract(blob: bytes, dest: Path) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tf:
            safe = [m for m in tf.getmembers()
                    if not (m.name.startswith("/") or ".." in Path(m.name).parts)
                    and (m.isfile() or m.isdir())]
            tf.extractall(dest, members=safe)
            return [m.name for m in safe if m.isfile()]
    except tarfile.ReadError:
        pass
    # 单文件投稿：gzip 压缩的一个 .tex
    try:
        data = gzip.decompress(blob)
    except OSError:
        data = blob
    if data[:4] == b"%PDF":
        (dest / "paper.pdf").write_bytes(data)
        return ["paper.pdf"]
    (dest / "main.tex").write_bytes(data)
    return ["main.tex"]


def find_toplevel(dest: Path) -> str | None:
    readme = dest / "00README.json"
    if readme.exists():
        try:
            for s in json.loads(readme.read_text())["sources"]:
                if s.get("usage") == "toplevel":
                    return s["filename"]
        except (KeyError, ValueError):
            pass
    cands = [p for p in dest.rglob("*.tex")
             if re.search(r"^\s*\\documentclass", p.read_text(errors="ignore"), re.M)]
    if len(cands) == 1:
        return str(cands[0].relative_to(dest))
    for p in cands:
        if re.search(r"\\begin\{document\}", p.read_text(errors="ignore")):
            return str(p.relative_to(dest))
    return None


def prepare_provided(source: Path, work: Path, aid: str) -> int:
    source = source.expanduser().resolve()
    if not source.exists():
        raise ValueError(f"用户提供文件不存在：{source}")
    # Work must not contain the source, or be contained in a provided project.
    if source == work or work in source.parents or (source.is_dir() and source in work.parents):
        raise ValueError("工作目录和用户提供文件/项目必须分离")
    meta = {
        "arxiv_id": aid,
        "input_origin": "user_provided",
        "translation_basis": "user_provided_content",
        "provided_path": str(source),
        "license": "not_checked",
        "license_url": "",
        "verdict": "allow",
    }
    if source.is_dir():
        if any(p.is_symlink() for p in source.rglob("*")):
            raise ValueError("源码目录含符号链接，请提供不含链接的项目副本")
        if not any(source.rglob("*.tex")):
            raise ValueError("源码目录中没有 .tex 文件")
        shutil.copytree(source, work / "eprint", dirs_exist_ok=True)
        top = find_toplevel(work / "eprint")
        if top is None:
            raise ValueError("无法识别主 .tex 文件，请明确指定主文件")
        meta.update(mode="latex", toplevel=top)
    elif source.suffix.lower() == ".pdf":
        with source.open("rb") as stream:
            if b"%PDF-" not in stream.read(1024):
                raise ValueError("文件不是有效 PDF：缺少 PDF 文件头")
        dest = work / "input" / source.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        meta.update(mode="pdf", input_file=str(dest), toplevel=None)
    elif source.suffix.lower() == ".tex":
        dest = work / "eprint" / source.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        meta.update(mode="latex", toplevel=source.name)
    else:
        raise ValueError("仅支持用户提供的 .pdf、.tex 或 LaTeX 项目目录")
    work.mkdir(parents=True, exist_ok=True)
    (work / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("arxiv_id")
    parser.add_argument("work", type=Path)
    parser.add_argument("--provided-file", type=Path,
                        help="仅用于用户实际提供或明确指定的本地文件/源码目录")
    args = parser.parse_args()
    aid, work = args.arxiv_id.strip(), args.work.expanduser().resolve()
    if args.provided_file is not None:
        try:
            return prepare_provided(args.provided_file, work, aid)
        except (OSError, ValueError) as exc:
            print(f"输入错误：{exc}", file=sys.stderr)
            return 2
    aid = re.sub(r"^(https?://)?arxiv\.org/(abs|html|pdf)/", "", aid).removesuffix(".pdf").rstrip("/")
    work.mkdir(parents=True, exist_ok=True)

    abs_html = get(f"https://arxiv.org/abs/{aid}").decode("utf-8", "ignore")
    lic, lic_url, verdict = classify(abs_html)
    meta = {
        "arxiv_id": aid,
        "title": (meta_tags(abs_html, "citation_title") or [""])[0],
        "authors": meta_tags(abs_html, "citation_author"),
        "abs_url": f"https://arxiv.org/abs/{aid}",
        "license": lic,
        "license_url": lic_url,
        "verdict": verdict,
        "input_origin": "remote_link",
        "translation_basis": "open_license" if verdict == "allow" else None,
    }
    (work / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    print(json.dumps(meta, ensure_ascii=False, indent=2))

    if verdict != "allow":
        print(f"\n⛔ 许可为「{lic}」，未提供本链接路径的全文翻译授权依据。不下载源码；用户实际提供的文件可用 --provided-file 处理。",
              file=sys.stderr)
        return 3

    files = extract(get(f"https://arxiv.org/e-print/{aid}"), work / "eprint")
    top = find_toplevel(work / "eprint")
    meta.update({"toplevel": top, "n_files": len(files)})
    (work / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    print(f"\n✅ 源码已解到 {work / 'eprint'}（{len(files)} 个文件），主文件：{top}")
    if top is None:
        print("⚠️ 没找到主 .tex（可能只有 PDF），本 skill 只支持 LaTeX 源码。", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
