# -*- coding: utf-8 -*-
"""书籍精读 CLI：整本书 → 章节分诊 → 按章深读 → 引文回验 → 归档进札记库。

与 read_pdf.py（单篇论文）的关系：同一套「脚本草稿 → agent 亲读核验 → 落库」协议，
但切分单位是**目录给出的章**而不是固定页窗，且多一道确定性引文回验。

    # 1. 建 manifest（书目 + 目录 + 页偏移）——书目字段不猜，用 --set 给全
    PYTHONPATH=. python scripts/book_digest.py manifest ~/books/rubin.pdf \\
        --slug LittleRubin2020 --type book --printed-toc-pages 5-10 \\
        --set title="Statistical Analysis with Missing Data" --set edition=3rd \\
        --set isbn=9781119482260 --set citekey=little2020rubin

    # 2. 分诊：每章对 config/topics.yaml 的研究问题打 0-3 分，产出热力图与深读队列
    PYTHONPATH=. python scripts/book_digest.py triage --slug LittleRubin2020 --apply

    # 3. 深读：为队列里的章建 draft bundle，并打印 agent 亲读计划
    PYTHONPATH=. python scripts/book_digest.py read --slug LittleRubin2020 --apply
    #    → agent 用 Read 工具亲读该章页窗，把终稿写回 close_reading_final +
    #      cross_check_report，status=final（协议同 read-paper skill）

    # 4. 回验：把每条逐字引句 grep 回原文被引页 ±1
    PYTHONPATH=. python scripts/book_digest.py verify --slug LittleRubin2020

    # 5. 归档：三道门全过的章 → 科研札记_<date>-<slug>_书籍精读.{md,docx,...} + 刷索引
    PYTHONPATH=. python scripts/book_digest.py finalize --slug LittleRubin2020

    # 随时看进度
    PYTHONPATH=. python scripts/book_digest.py status --slug LittleRubin2020

默认 dry-run：triage / read 不加 --apply 只打印将要做什么，不烧 LLM、不落盘。
"""
import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.settings import load_scholar_settings          # noqa: E402
from src.scholar.llm_client import LLMClient                    # noqa: E402
from src.utils.logger import get_logger                         # noqa: E402

logger = get_logger("book_digest")

REPO = Path(__file__).resolve().parents[1]
TOPICS_YAML = REPO / "config" / "topics.yaml"


def _notes_dir(settings) -> Path:
    return Path(settings.processing.notes_dir)


def _page_list(spec: str):
    """"5-10" / "5,6,7" / "5-8,12" → [5,6,7,8,12]。"""
    out = []
    for part in str(spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def _kv(pairs):
    """--set k=v … → dict（int 字段自动转型）。"""
    out = {}
    for raw in pairs or []:
        if "=" not in raw:
            raise SystemExit("--set 需要 key=value 形式：{!r}".format(raw))
        k, v = raw.split("=", 1)
        k = k.strip()
        v = v.strip()
        if k in ("year", "page_offset", "split_level"):
            out[k] = int(v)
        elif k in ("authors", "editors"):
            out[k] = [x.strip() for x in v.split(";") if x.strip()]
        else:
            out[k] = v
    return out


def _load(settings, slug):
    from src.scholar.book_ingest import BookManifest
    from src.scholar.book_notes import book_dir
    bdir = book_dir(_notes_dir(settings), slug)
    if not (bdir / "book.manifest.json").exists():
        raise SystemExit("找不到 manifest：{}／先跑 `book_digest.py manifest`".format(bdir))
    return BookManifest.load(bdir), bdir


class _AdHocSpec:
    """manifest.extra_questions 的一项，鸭子类型对齐 TopicSpec 在分诊里用到的字段。"""

    def __init__(self, slug: str, title: str, question: str):
        self.slug, self.title, self.question = slug, title, question
        self.queries, self.buckets, self.roles = [], [], []


def _specs(manifest=None):
    """分诊轴 = config/topics.yaml 的概念页问题 + 本书专属问题（见 extra_questions 文档）。"""
    from src.scholar.topics import load_topic_specs
    specs = list(load_topic_specs(TOPICS_YAML))
    seen = {s.slug for s in specs}
    for q in (getattr(manifest, "extra_questions", None) or []):
        slug = (q.get("slug") or "").strip()
        if slug and slug not in seen:
            seen.add(slug)
            specs.append(_AdHocSpec(slug, (q.get("title") or slug).strip(),
                                    (q.get("question") or "").strip()))
    return specs


def _interests():
    from src.scholar.research_profile import get_profile
    p = get_profile()
    return p.get("interests") if isinstance(p, dict) else getattr(p, "interests", "")


# ---------------- manifest ----------------

def cmd_manifest(args):
    from src.scholar.book_ingest import build_manifest
    from src.scholar.book_notes import book_dir

    settings = load_scholar_settings(args.config)
    man = build_manifest(
        Path(args.pdf).expanduser(), slug=args.slug, entry_type=args.type,
        custom_toc_path=Path(args.toc_csv).expanduser() if args.toc_csv else None,
        split_level=args.split_level,
        page_offset=args.page_offset,
        printed_toc_pages=_page_list(args.printed_toc_pages) if args.printed_toc_pages else None,
        overrides=_kv(args.set))
    bdir = book_dir(_notes_dir(settings), args.slug)
    # 保留既有的分诊结果与本书专属问题：重建 manifest 常常只是为了修目录/页偏移，
    # 顺手把几十次 LLM 调用换来的分诊矩阵清空，是纯损失。--reset 才真的清。
    old = None
    if (bdir / "book.manifest.json").exists() and not args.reset:
        from src.scholar.book_ingest import BookManifest
        old = BookManifest.load(bdir)
        man.triage = old.triage
        man.extra_questions = old.extra_questions
        man.ledger = old.ledger
        if not args.set and old.citekey and not man.citekey:
            man.citekey = old.citekey
    path = man.save(bdir)

    print("\n📕 {}".format(man.title or man.slug))
    print("   类型 {} · {} 页 · 目录来源 {} · 页偏移 {:+d}".format(
        man.entry_type, man.n_pages, man.toc_source or "无", man.page_offset))
    print("   切出 {} 章：".format(len(man.chapters)))
    for c in man.chapter_objs():
        lo, hi = c.printed_range(man.page_offset)
        print("     {:>2}. pp.{:>4}-{:<4} ({:>3}p)  {}".format(c.number, lo, hi, c.n_pages,
                                                               c.title[:58]))
    if old is not None:
        print("   （已保留 {} 章的分诊结果与 {} 条本书专属问题）".format(
            len(man.triage), len(man.extra_questions)))
    print("\n   manifest → {}".format(path))
    if not man.chapters:
        print("\n   ⚠️ 没切出章节：给 --printed-toc-pages（书自印的目录页 PDF 页码）"
              "或 --toc-csv。**不会**退回页窗盲切。")
        return 1
    missing = [k for k in ("title", "isbn") if not getattr(man, k)]
    if missing:
        print("   ⚠️ 书目缺字段 {}：用 --set 补齐后重跑，否则引用会错"
              .format("/".join(missing)))
    return 0


# ---------------- questions ----------------

def cmd_questions(args):
    settings = load_scholar_settings(args.config)
    man, bdir = _load(settings, args.slug)
    if args.clear:
        man.extra_questions = []
    for raw in args.add:
        parts = [x.strip() for x in raw.split("|")]
        if len(parts) != 3 or not parts[0]:
            raise SystemExit("--add 需要 slug|标题|问题 三段：{!r}".format(raw))
        man.extra_questions = [q for q in man.extra_questions if q.get("slug") != parts[0]]
        man.extra_questions.append({"slug": parts[0], "title": parts[1], "question": parts[2]})
    if args.add or args.clear:
        man.save(bdir)
        if man.triage:
            print("⚠️ 已有分诊结果是按旧问题集打的分，新问题上无分数——"
                  "重跑 triage --apply 才会补齐（旧分数会被同章覆盖）。")
    specs = _specs(man)
    print("\n分诊轴（共 {} 问；前 {} 问来自 config/topics.yaml）：".format(
        len(specs), len(specs) - len(man.extra_questions)))
    for s in specs:
        own = "★" if any(q.get("slug") == s.slug for q in man.extra_questions) else " "
        print("  {} {:<28} {}".format(own, s.slug, (s.question or "")[:78]))
    return 0


# ---------------- triage ----------------

def cmd_triage(args):
    from src.scholar.book_ingest import extract_pages
    from src.scholar.book_triage import (ChapterTriage, heatmap_md, selected_chapters,
                                         triage_book)

    settings = load_scholar_settings(args.config)
    man, bdir = _load(settings, args.slug)
    specs = _specs(man)
    only = _page_list(args.only) if args.only else None
    targets = [c for c in man.chapter_objs() if only is None or c.number in set(only)]

    if not args.apply:
        print("\n[dry-run] 将对 {} 章 × {} 个研究问题分诊（每章 1 次 LLM 调用）"
              .format(len(targets), len(specs)))
        for c in targets:
            lo, hi = c.printed_range(man.page_offset)
            print("   ch{:>2} pp.{}-{}  {}".format(c.number, lo, hi, c.title[:60]))
        print("\n加 --apply 实际执行。")
        return 0

    pages = extract_pages(Path(man.pdf_path))
    llm = LLMClient(settings.llm)
    fresh = triage_book(man, pages, specs, llm, _interests(), model=args.model,
                        only=only, max_workers=args.workers)
    merged = {int(k): ChapterTriage.from_dict(v) for k, v in (man.triage or {}).items()}
    merged.update(fresh)
    man.triage = {str(k): v.to_dict() for k, v in sorted(merged.items())}
    man.save(bdir)

    heat = heatmap_md(man, merged, specs)
    hpath = bdir / "triage.md"
    hpath.write_text(heat, encoding="utf-8")
    sel = selected_chapters(man, merged)
    print("\n✅ 分诊完成：{} / {} 章进深读队列（{} / {} 页）".format(
        len(sel), len(man.chapters),
        sum(c.n_pages for c in sel), sum(c.n_pages for c in man.chapter_objs())))
    print("   热力图 → {}".format(hpath))
    return 0


# ---------------- read ----------------

def cmd_read(args):
    from src.scholar.book_ingest import extract_pages
    from src.scholar.book_notes import chapter_bundle_path, load_chapter_bundle, write_chapter_bundle
    from src.scholar.book_triage import ChapterTriage, selected_chapters

    settings = load_scholar_settings(args.config)
    man, bdir = _load(settings, args.slug)
    notes_dir = _notes_dir(settings)
    triage = {int(k): ChapterTriage.from_dict(v) for k, v in (man.triage or {}).items()}

    if args.only:
        want = set(_page_list(args.only))
        targets = [c for c in man.chapter_objs() if c.number in want]
    elif triage:
        targets = selected_chapters(man, triage)
    else:
        print("⚠️ 尚未分诊，且未给 --only：先跑 triage，或显式 --only 指定章号。")
        return 1

    # 已 final 的章不重建（会丢掉 agent 的核验成果，同 read_pdf 的 final bundle 保护）
    todo = []
    for c in targets:
        p = chapter_bundle_path(notes_dir, man.slug, c.number)
        if p.exists() and not args.force:
            try:
                if load_chapter_bundle(p).get("status") == "final":
                    continue
            except Exception:                        # noqa: BLE001
                pass
        todo.append(c)

    if not args.apply:
        print("\n[dry-run] 将为 {} 章建 draft bundle（已 final 的已跳过）".format(len(todo)))
        for c in todo:
            lo, hi = c.printed_range(man.page_offset)
            print("   ch{:>2} pp.{}-{} ({}p) {}".format(c.number, lo, hi, c.n_pages,
                                                        c.title[:56]))
        print("\n加 --apply 实际执行。")
        return 0

    pages = extract_pages(Path(man.pdf_path))
    for c in todo:
        write_chapter_bundle(chapter_bundle_path(notes_dir, man.slug, c.number),
                             manifest=man, chapter=c, status="draft",
                             draft_status="pending-agent",
                             draft_note="等待 agent 亲读（本链路不跑脚本草稿，见 SKILL）")
    _print_read_plan(man, todo, notes_dir)
    return 0


def _print_read_plan(man, chapters, notes_dir):
    """打印 agent 亲读计划。协议与 docs/skills/read-paper 同源，切分单位换成章。"""
    from src.scholar.book_notes import chapter_bundle_path
    print("\n" + "=" * 72)
    print("📖 agent 亲读计划：{}".format(man.title or man.slug))
    print("   PDF: {}".format(man.pdf_path))
    print("   页码换算：原书页码 = PDF 页序 {:+d}".format(man.page_offset))
    print("=" * 72)
    for c in chapters:
        lo, hi = c.printed_range(man.page_offset)
        print("\n▸ ch{} {}".format(c.number, c.title[:66]))
        print("   Read pages={}-{}   （原书 pp.{}-{}，共 {} 页）".format(
            c.pdf_start, c.pdf_end, lo, hi, c.n_pages))
        print("   写回 → {}".format(chapter_bundle_path(notes_dir, man.slug, c.number)))
    print("\n每章写回 close_reading_final（句级 tag + page 为**原书**页码）、")
    print("cross_check_report（verified_count ≥ 1），status=final；")
    print("逐字英文引句用双引号包裹并标 page —— verify 会把它 grep 回原文，对不上即拒收。")


# ---------------- import-legacy ----------------

def cmd_import_legacy(args):
    """把旧手工 digest 导入为**草稿轨**（不是终稿），并出覆盖/引文体检报告。

    为什么只进草稿轨：见 src/scholar/book_legacy.py 模块文档（实测 18 份 Rubin 旧
    digest 里逐字引句总共只有 7 条，几乎没有可回验的东西，且它们正是分块 LLM 通读
    的产物——双轨协议要拿亲读去校的就是这一轨）。
    """
    from src.scholar.book_ingest import page_index_for
    from src.scholar.book_legacy import (audit_legacy_quotes, coverage_gaps, infer_offset,
                                         legacy_script_note, map_to_chapters, parse_legacy_file)
    from src.scholar.book_notes import chapter_bundle_path, load_chapter_bundle, write_chapter_bundle

    settings = load_scholar_settings(args.config)
    man, bdir = _load(settings, args.slug)
    notes_dir = _notes_dir(settings)

    src = Path(args.dir).expanduser()
    files = sorted(src.glob("*.md"))
    if not files:
        raise SystemExit("目录里没有 .md：{}".format(src))
    digests = [parse_legacy_file(f) for f in files]

    off = infer_offset(digests)
    print("\n📥 旧 digest {} 份".format(len(digests)))
    if off is not None:
        flag = "✅ 与 manifest 一致" if off == man.page_offset else \
               "⚠️ 与 manifest 的 {:+d} 不一致".format(man.page_offset)
        print("   由文件名/头部推出的页偏移 {:+d}  {}".format(off, flag))

    mapping = map_to_chapters(digests, man)
    print("\n   章 ↔ 旧 digest 映射：")
    for ch in man.chapter_objs():
        ds = mapping.get(ch.number) or []
        lo, hi = ch.printed_range(man.page_offset)
        print("     ch{:>2} pp.{:>3}-{:<3}  {:<2}份  {}".format(
            ch.number, lo, hi, len(ds), ", ".join(d.name for d in ds) or "（无）"))

    gaps = coverage_gaps(digests, man)
    if gaps:
        print("\n   ⚠️ 旧 digest 未覆盖的正文页（深读队列的补洞目标）：")
        for a, b in gaps:
            print("      PDF pp.{}-{}  （原书 pp.{}-{}，{} 页）".format(
                a, b, a + man.page_offset, b + man.page_offset, b - a + 1))
    else:
        print("\n   旧 digest 覆盖了全部正文页。")

    audit = audit_legacy_quotes(digests, page_index_for(man))
    print("\n   逐字引句体检：{}/{} 条回验通过（{:.0%}）".format(
        audit["passed"], audit["total"], audit["pass_rate"]))
    for c in audit["checks"]:
        if not c["ok"]:
            print("      ⛔ {} · {} 「{}…」".format(c["file"], c["reason"], c["quote"][:56]))
    if audit["total"] < 20:
        print("      ℹ️ 可回验引句极少 —— 旧 digest 基本是中文归纳，不能当证据入库，"
              "只作亲读对照基线（这正是它进草稿轨的理由）。")

    if not args.apply:
        print("\n[dry-run] 加 --apply 才会把它们写进各章 bundle 的 close_reading_script。")
        return 0

    n = 0
    for ch in man.chapter_objs():
        ds = mapping.get(ch.number) or []
        if not ds:
            continue
        path = chapter_bundle_path(notes_dir, man.slug, ch.number)
        if path.exists():
            try:
                if load_chapter_bundle(path).get("status") == "final" and not args.force:
                    continue        # 不覆盖已核验成果
            except Exception:                        # noqa: BLE001
                pass
        write_chapter_bundle(path, manifest=man, chapter=ch, status="draft",
                             draft_status="legacy-digest",
                             draft_note="旧手工 digest 导入（{}），未经引文回验，仅作亲读对照基线"
                                        .format(", ".join(d.name for d in ds)))
        # 草稿正文另存：close_reading_script 是结构化 CloseReading，旧 digest 是自由文本，
        # 塞不进那个 schema，故落成同名 .legacy.md 供 agent Read。
        (bdir / "ch{:02d}.legacy.md".format(ch.number)).write_text(
            legacy_script_note(ch, ds), encoding="utf-8")
        n += 1
    print("\n✅ 已为 {} 章写入草稿轨（bundle status=draft + ch??.legacy.md）".format(n))
    return 0


# ---------------- verify ----------------

def cmd_verify(args):
    from src.scholar.book_ingest import page_index_for
    from src.scholar.book_notes import (CHAPTER_BUNDLE_SUFFIX, book_dir, load_chapter_bundle,
                                        quotecheck_path)
    from src.scholar.quote_verify import verify_close_reading
    from src.scholar.schema import CloseReading

    settings = load_scholar_settings(args.config)
    man, bdir = _load(settings, args.slug)
    notes_dir = _notes_dir(settings)
    idx = page_index_for(man)

    files = sorted(bdir.glob("*{}".format(CHAPTER_BUNDLE_SUFFIX)))
    if not files:
        print("没有章 bundle：先跑 read。")
        return 1

    tot = ok = 0
    for bf in files:
        data = load_chapter_bundle(bf)
        if not data.get("close_reading_final"):
            continue
        number = int((data.get("chapter") or {}).get("number") or 0)
        cr = CloseReading(**data["close_reading_final"])
        rep = verify_close_reading(cr, idx, slack=args.slack, chapter=number)
        data["quote_verify"] = rep.to_dict()
        from src.scholar.notes_index import _atomic_write
        _atomic_write(bf, json.dumps(data, ensure_ascii=False, indent=2))
        _atomic_write(quotecheck_path(notes_dir, man.slug, number),
                      json.dumps(rep.to_dict(), ensure_ascii=False, indent=2))
        tot += rep.total
        ok += rep.passed
        mark = "✅" if rep.pass_rate >= args.min_rate else "⛔"
        print("{} ch{:>2}  {:>3}/{:<3} 引句通过（{:.0%}）".format(
            mark, number, rep.passed, rep.total, rep.pass_rate))
        for c in rep.flagged[:args.show]:
            print("      · {}  「{}…」".format(c.reason, c.quote[:60]))
    print("\n合计 {}/{} 条引句通过（{:.0%}）".format(ok, tot, (ok / tot) if tot else 1.0))
    return 0


# ---------------- finalize ----------------

def cmd_finalize(args):
    from src.scholar.book_notes import note_label, rebuild_book

    settings = load_scholar_settings(args.config)
    man, bdir = _load(settings, args.slug)
    label = args.label or note_label(man, args.date or date.today().isoformat())
    from src.scholar.notes_index import validate_note_label
    validate_note_label(label)

    res = rebuild_book(_notes_dir(settings), man, label, settings=settings,
                       allow_removals=args.allow_removals, quote_pass_min=args.min_rate)
    if res.get("refused"):
        print("\n⛔ 净删除止损闸触发，整本未动。被拒收 bundle：{}"
              .format(", ".join((res.get("skipped") or []) + (res.get("broken") or []))))
        return 2
    print("\n✅ 归档 {} 条条目 → {}".format(res.get("papers"), res.get("md")))
    if res.get("skipped"):
        print("   未纳入（草稿/未过门禁）：{}".format(", ".join(res["skipped"])))
    if res.get("broken"):
        print("   ⚠️ 结构非法：{}".format(", ".join(res["broken"])))
    return 0


# ---------------- status ----------------

def cmd_status(args):
    from src.scholar.book_notes import (CHAPTER_BUNDLE_SUFFIX, check_gates, load_chapter_bundle)
    from src.scholar.book_triage import ChapterTriage, selected_chapters

    settings = load_scholar_settings(args.config)
    man, bdir = _load(settings, args.slug)
    triage = {int(k): ChapterTriage.from_dict(v) for k, v in (man.triage or {}).items()}
    sel = {c.number for c in selected_chapters(man, triage)} if triage else set()

    print("\n📕 {} · {} 章 · 目录来源 {} · 页偏移 {:+d}".format(
        man.title or man.slug, len(man.chapters), man.toc_source or "无", man.page_offset))
    print("   分诊 {}/{} 章；深读队列 {} 章".format(len(triage), len(man.chapters), len(sel)))
    print()
    print("   章  分诊  队列  bundle    门禁")
    for c in man.chapter_objs():
        t = triage.get(c.number)
        bf = bdir / "ch{:02d}{}".format(c.number, CHAPTER_BUNDLE_SUFFIX)
        if bf.exists():
            try:
                data = load_chapter_bundle(bf)
                st = data.get("status") or "?"
                gate = check_gates(data, quote_pass_min=args.min_rate)
                gate_txt = "✅" if gate.ok else gate.reason[:44]
            except Exception as e:                   # noqa: BLE001
                st, gate_txt = "坏", "读不出：{}".format(str(e)[:36])
        else:
            st, gate_txt = "—", ""
        print("   {:>3}  {:>3}   {:>3}   {:<8}  {}".format(
            c.number, (t.max_score if t else "—"), "✓" if c.number in sel else "",
            st, gate_txt))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="书籍精读流水线")
    ap.add_argument("--config", default="config/scholar.env")
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("manifest", help="建 book manifest（书目+目录+页偏移）")
    m.add_argument("pdf")
    m.add_argument("--slug", required=True, help="书的短名（目录名与札记标签用）")
    m.add_argument("--type", choices=["book", "chapter"], required=True,
                   help="book=专著(全书一个citekey) / chapter=编著文集(每章一个)")
    m.add_argument("--toc-csv", default=None)
    m.add_argument("--printed-toc-pages", default=None,
                   help="书自印目录页的 PDF 页码，如 5-10（无书签时用）")
    m.add_argument("--split-level", type=int, default=2, help="「章」所在的目录层级")
    m.add_argument("--page-offset", type=int, default=None,
                   help="原书页码 = PDF 页序 + offset；不给则自动探测")
    m.add_argument("--set", action="append", default=[],
                   help="补书目字段：--set isbn=978... --set authors='A; B'")
    m.add_argument("--reset", action="store_true",
                   help="连同已有的分诊结果/本书专属问题一起清空（默认保留）")
    m.set_defaults(func=cmd_manifest)

    q = sub.add_parser("questions", help="查看/设置本书专属的分诊问题")
    q.add_argument("--slug", required=True)
    q.add_argument("--add", action="append", default=[],
                   help="slug|标题|问题（可重复）；不给则只打印当前问题清单")
    q.add_argument("--clear", action="store_true", help="清空本书专属问题")
    q.set_defaults(func=cmd_questions)

    t = sub.add_parser("triage", help="逐章对研究问题打 0-3 分")
    t.add_argument("--slug", required=True)
    t.add_argument("--only", default=None, help="只分诊这些章，如 1-5,14")
    t.add_argument("--model", default=None)
    t.add_argument("--workers", type=int, default=4)
    t.add_argument("--apply", action="store_true", help="实际执行（默认 dry-run）")
    t.set_defaults(func=cmd_triage)

    r = sub.add_parser("read", help="为深读队列建 draft bundle + 打印 agent 亲读计划")
    r.add_argument("--slug", required=True)
    r.add_argument("--only", default=None, help="指定章号，如 3,7-9")
    r.add_argument("--force", action="store_true", help="连已 final 的章也重建（丢核验成果）")
    r.add_argument("--apply", action="store_true")
    r.set_defaults(func=cmd_read)

    il = sub.add_parser("import-legacy", help="导入旧手工 digest 为草稿轨 + 覆盖/引文体检")
    il.add_argument("--slug", required=True)
    il.add_argument("--dir", required=True, help="旧 digest 目录（*.md）")
    il.add_argument("--force", action="store_true", help="连已 final 的章也覆盖")
    il.add_argument("--apply", action="store_true")
    il.set_defaults(func=cmd_import_legacy)

    v = sub.add_parser("verify", help="逐字引句 grep 回原文被引页")
    v.add_argument("--slug", required=True)
    v.add_argument("--slack", type=int, default=1, help="允许的页码偏差")
    v.add_argument("--min-rate", type=float, default=0.8)
    v.add_argument("--show", type=int, default=5, help="每章打印几条未通过引句")
    v.set_defaults(func=cmd_verify)

    f = sub.add_parser("finalize", help="过门禁的章 → 札记四件套 + 刷索引")
    f.add_argument("--slug", required=True)
    f.add_argument("--label", default=None, help="札记标签；默认 <today>-<slug>")
    f.add_argument("--date", default=None)
    f.add_argument("--min-rate", type=float, default=0.8)
    f.add_argument("--allow-removals", action="store_true")
    f.set_defaults(func=cmd_finalize)

    s = sub.add_parser("status", help="看进度")
    s.add_argument("--slug", required=True)
    s.add_argument("--min-rate", type=float, default=0.8)
    s.set_defaults(func=cmd_status)

    args = ap.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
