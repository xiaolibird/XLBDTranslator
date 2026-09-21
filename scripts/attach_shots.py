#!/usr/bin/env python3
"""给已归档的**自动**月度/周札记补原文截图（逐句出处 + 证据页整页 + 图表）。

    PYTHONPATH=. python scripts/attach_shots.py                 # 干跑：只解析每篇的 PDF、报覆盖率
    PYTHONPATH=. python scripts/attach_shots.py --apply         # 真的出图并把图片行插回 md
    PYTHONPATH=. python scripts/attach_shots.py --apply --tier high     # 只做高优先级
    PYTHONPATH=. python scripts/attach_shots.py --apply --note 科研札记_2025-06_全文精读.md

为什么是「补」而不是重建：自动链路的 digest JSON 从来不存精读分节，证据句只在月度 md 里，
没法像手动精读那样整月 write_notes 重写。出图与插图都复用 notes.write_notes 那一套
（shots_for_one / _shot_lines_after / _shot_pages_block），这里只负责两件事：
  ① 给每篇找到**它自己的** PDF（找不到或对不上标题就不配，宁缺毋错）；
  ② 调 notes.attach_shots_to_md 逐份 md 插图（幂等，重跑一字不变）。
以后新入库的自动札记在 write_notes 时就直接出图（notes.auto_shot_kwargs），不再需要这一步。

PDF 来源按可靠程度依次尝试，找到后都要过标题核对：
  ledger   补深度账本里记下的那份（backfill_deepread 改造后才有）
  reading  ~/Desktop/Lab/Reading/*/<citekey>.pdf
  pid      output/scholar_pdfs/<md5(标题|前三作者)[:16]>.pdf —— 入库精读时就落在这里
  digest   digest JSON 里记过的 paper_id → 同上（标题/作者后来被 Crossref 改过时靠它）
  locate   scripts/locate_pdf.py（Zotero / Spotlight）
结果缓存在 assets/shots/_auto_pdf_map.json（含「找不到」），重跑不再重复搜索；--refresh 忽略缓存。
"""
import argparse
import glob
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.scholar.paths import repo_path  # noqa: E402

MAP_NAME = "_auto_pdf_map.json"
_STOP = set("with from that this their into over under using based toward towards through between among "
            "about across after before study analysis model models data approach method methods learning".split())


def _title_words(title: str):
    t = (title or "").lower()
    if re.search(r"[一-鿿]", t):
        return [ch for ch in t if "一" <= ch <= "鿿"]
    return [w for w in re.findall(r"[a-z]{4,}", t) if w not in _STOP]


def pdf_title_matches(pdf: Path, title: str, pages: int = 2) -> bool:
    """PDF 前两页里要出现标题的大部分实词（中文按字）。防的是「文件名/编号撞上了、内容是别的论文」。"""
    import fitz
    words = _title_words(title)
    if len(words) < 3:
        return False                          # 标题太短，核对不了：宁可不配
    try:
        with fitz.open(str(pdf)) as d:
            txt = " ".join(d[i].get_text() for i in range(min(pages, d.page_count)))
    except Exception:
        return False
    txt = re.sub(r"-\s*\n\s*", "", txt).lower()
    if re.search(r"[一-鿿]", words[0]):
        return sum(1 for ch in set(words) if ch in txt) / len(set(words)) >= 0.8
    body = set(re.findall(r"[a-z]{4,}", txt))
    return sum(1 for w in set(words) if w in body) / len(set(words)) >= 0.7


def _pid(title, authors):
    from src.scholar.academic_search import _generate_paper_id
    return _generate_paper_id(title or "", list(authors or []))


def _digest_pids(digest_dir: Path):
    """digest JSON 里的 (doi → paper_id)、(规范标题 → paper_id)。"""
    from src.scholar.notes_index import norm_title
    by_doi, by_title = {}, {}
    for f in sorted(glob.glob(str(digest_dir / "digest_*.json"))):
        try:
            d = json.loads(Path(f).read_text("utf-8"))
        except Exception:
            continue
        for s in (d.get("segments") or []) if isinstance(d, dict) else []:
            m = s.get("metadata") or {}
            pid = s.get("paper_id") or m.get("paper_id")
            if not pid:
                continue
            if m.get("doi"):
                by_doi.setdefault(m["doi"].lower(), pid)
            if m.get("title"):
                by_title.setdefault(norm_title(m["title"]), pid)
    return by_doi, by_title


def resolve_pdfs(entries, notes_dir: Path, cache: dict, refresh: bool = False, use_locate: bool = True):
    """{citekey: {"path": str|None, "how": str}}。先查缓存，再按来源梯子逐级找，找到的都过标题核对。"""
    from src.scholar.notes_index import norm_title
    scratch = repo_path("output/scholar_pdfs")
    reading = Path.home() / "Desktop" / "Lab" / "Reading"
    try:
        ledger = json.loads((notes_dir / "backfill_deepread_progress.json").read_text("utf-8")).get("done") or {}
    except Exception:
        ledger = {}
    by_doi, by_title = _digest_pids(repo_path("output/scholar_digest"))
    loc = None
    if use_locate:
        import importlib.util
        spec = importlib.util.spec_from_file_location("locate_pdf", REPO / "scripts" / "locate_pdf.py")
        loc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loc)
    out = {}
    for e in entries:
        ck = e["citekey"]
        c = cache.get(ck)
        # 负结果只在「上次搜得不比这次弱」时沿用：--no-locate 干跑记下的「找不到」不能挡住带 Spotlight 的这一次
        if c and not refresh and ((c.get("path") and Path(c["path"]).is_file())
                                  or (c.get("path") is None and (c.get("locate") or not use_locate))):
            out[ck] = c
            continue
        title = e.get("title") or ""
        cands = []
        if (ledger.get(ck) or {}).get("pdf"):
            cands.append((ledger[ck]["pdf"], "ledger"))
        cands += [(p, "reading") for p in sorted(glob.glob(str(reading / "*" / (ck + ".pdf"))))]
        cands.append((str(scratch / (_pid(title, e.get("authors"))[:16] + ".pdf")), "pid"))
        for pid in {by_doi.get((e.get("doi") or "").lower()), by_title.get(norm_title(title))} - {None}:
            cands.append((str(scratch / (pid[:16] + ".pdf")), "digest"))
        hit = None
        for p, how in cands:
            if Path(p).is_file() and pdf_title_matches(Path(p), title):
                hit = {"path": p, "how": how}
                break
        if hit is None and loc is not None:
            q = e.get("doi") or title
            try:
                _, res = loc.locate(q)
            except Exception:
                res = []
            for r in res[:3]:
                p = r.get("path") or ""
                if p.lower().endswith(".pdf") and Path(p).is_file() and pdf_title_matches(Path(p), title):
                    hit = {"path": p, "how": "locate:" + str(r.get("source") or "")}
                    break
        out[ck] = hit or {"path": None, "how": "not-found", "locate": bool(use_locate)}
    return out


def main():
    ap = argparse.ArgumentParser(description="给已归档的自动月度/周札记补原文截图")
    ap.add_argument("--apply", action="store_true", help="真的出图并改写 md（默认只解析 PDF、报覆盖率）")
    ap.add_argument("--tier", choices=["high", "mid", "low"], action="append",
                    help="只做这些优先级（可重复）；缺省全做")
    ap.add_argument("--note", action="append", help="只处理这些札记文件名（可重复）")
    ap.add_argument("--refresh", action="store_true", help="忽略 PDF 解析缓存，重新找")
    ap.add_argument("--no-locate", action="store_true", help="不用 Spotlight/Zotero 兜底（快）")
    args = ap.parse_args()

    notes_dir = repo_path("output/scholar_notes")
    idx = json.loads((notes_dir / "literature_index.json").read_text("utf-8"))
    entries = [e for e in idx["papers"]
               if e.get("series") == "auto" and e.get("duplicate_of") is None and e.get("highlights")
               and (not args.tier or e.get("priority_tier") in args.tier)
               and (not args.note or e.get("note_file") in args.note)]
    map_path = notes_dir / "assets" / "shots" / MAP_NAME
    try:
        cache = json.loads(map_path.read_text("utf-8"))
    except Exception:
        cache = {}
    res = resolve_pdfs(entries, notes_dir, cache, refresh=args.refresh, use_locate=not args.no_locate)
    cache.update(res)
    map_path.parent.mkdir(parents=True, exist_ok=True)
    map_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), "utf-8")
    found = {ck: v for ck, v in res.items() if v.get("path")}
    by_how = {}
    for v in res.values():
        k = v["how"].split(":")[0]
        by_how[k] = by_how.get(k, 0) + 1
    print("自动札记有句级证据的篇目 {} | 找到本篇 PDF {}（{:.0%}）| 来源 {}".format(
        len(res), len(found), len(found) / max(len(res), 1), by_how))
    if not args.apply:
        print("（干跑：未出图、未改 md；加 --apply 执行）")
        return 0

    from src.scholar.notes import attach_shots_to_md
    from src.scholar.embeddings import passage_embed_fn
    from src.scholar.settings import load_scholar_settings
    settings = load_scholar_settings()
    embed = passage_embed_fn(settings.llm)
    files = sorted({e["note_file"] for e in entries if e["citekey"] in found})
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    bdir = notes_dir / "_archive" / "attach_shots_{}".format(stamp)
    bdir.mkdir(parents=True, exist_ok=True)
    pdf_for = lambda ck: (found.get(ck) or {}).get("path")   # noqa: E731
    tot = {"papers": 0, "shot_papers": 0, "passages": 0, "files_changed": 0}
    for i, name in enumerate(files, 1):
        md = notes_dir / name
        if not md.is_file():
            continue
        shutil.copy2(md, bdir / name)                      # output/ 不进 git，回滚只能靠这份备份
        rep = attach_shots_to_md(md, pdf_for, notes_dir, embed_fn=embed, apply=True)
        for k in ("papers", "shot_papers", "passages"):
            tot[k] += rep[k]
        tot["files_changed"] += int(rep["changed"])
        print("[{}/{}] {} 出图 {} 篇 · 逐句出处 {} 句{}".format(
            i, len(files), name, rep["shot_papers"], rep["passages"], "" if rep["changed"] else "（无变化）"), flush=True)
    print("✅ 札记 {} 份（改动 {}）| 出图论文 {} | 逐句出处 {} 句 | 备份 {}".format(
        len(files), tot["files_changed"], tot["shot_papers"], tot["passages"], bdir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
