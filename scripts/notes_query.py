# -*- coding: utf-8 -*-
"""札记库写作检索 CLI：按关键词 + role 轴（citable/refutable/method）查 highlights。

用法（仓库根运行）：
    python scripts/notes_query.py MNAR --role citable          # 写 claim 找可引证据
    python scripts/notes_query.py missingness shift --role method --cite
    python scripts/notes_query.py 缺失机制 --json

匹配语义（两级，刻意区分"真命中"与"标题命中"）：
  - 入选：每个关键词都出现在 标题/中译名/一句话用处/任一 highlight 句 中（跨字段 AND）；
  - 真命中：某条 highlight 句里含关键词——它决定展示内容与排序；
  - 传了 --role 时**必须**有该 role 的句子真命中，否则不入选（否则会拿到与 claim
    无关的句子当证据，例如靠 one_line 里"未建模 MNAR"命中却展示八条无关方法句）。

退出码：0=有命中；1=无命中；2=索引缺失或损坏（三种输出模式一致）。
输出的 [@citekey] 可直接粘进 pandoc 稿，书目挂 output/scholar_notes/all_references.json。
"""
import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from src.scholar.notes_index import INDEX_JSON, is_missing_citekey, load_index_file  # noqa: E402

INDEX_PATH = REPO / "output" / "scholar_notes" / INDEX_JSON  # 模块级常量，向后兼容

ROLE_HINT = {"citable": "可引用证据", "refutable": "可反驳观点", "method": "方法论借鉴"}


def _in_book(entry, book):
    """该条目是否属于某本书：专著看 citekey，编著的章看 book_key。"""
    return book in (entry.get("citekey"), entry.get("book_key"))
TIER_ORDER = {"high": 0, "mid": 1, "low": 2}
TIER_EMOJI = {"high": "🔴", "mid": "🟠", "low": "🟢"}
MONTH_RE = re.compile(r"^\d{4}(-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01]))?)?$")  # YYYY[-MM[-DD]]，月份/日语义校验
MAX_SHOWN_HITS = 4          # 人读模式每条最多展示几句（--json 不截断）


def _load_index(path: Path):
    """读索引；缺失/损坏/结构诡异给可操作提示（单点实现在 notes_index.load_index_file）。"""
    data, err = load_index_file(path)
    if err:
        print(err, file=sys.stderr)
    return data


def _match(entry, terms, role):
    """返回 (hit_highlights, source) 或 None（不入选）。

    hit_highlights = 含关键词的 highlight 句；source ∈ {"highlight", "title"}。
    """
    hls = [h for h in (entry.get("highlights") or []) if isinstance(h, dict)]
    if role:
        hls = [h for h in hls if h.get("role") == role]
    static = " ".join([entry.get("title") or "", entry.get("title_zh") or "",
                       entry.get("one_line") or ""]).lower()
    hl_texts = [(h, (h.get("text") or "").lower()) for h in hls]

    # 入选：每个词都要在静态字段或任一句子里出现（跨字段 AND，与展示无关）
    for t in terms:
        if t in static or any(t in txt for _, txt in hl_texts):
            continue
        return None
    hit = [h for h, txt in hl_texts if any(t in txt for t in terms)]
    if role and not hit:
        # role 模式下没有一句该角色的句子真含关键词 → 拿它当证据必然文不对题
        return None
    return hit, ("highlight" if hit else "title")


def main() -> int:
    ap = argparse.ArgumentParser(description="按关键词+role 检索科研札记库（写作取证用）")
    ap.add_argument("terms", nargs="+", help="关键词（多词 AND，大小写不敏感）")
    ap.add_argument("--role", choices=["citable", "refutable", "method"],
                    help="只看该角色的句子：citable=可引证据 refutable=可反驳 method=方法借鉴")
    ap.add_argument("--tier", choices=["high", "mid", "low"], help="优先级层过滤")
    ap.add_argument("--month", help="限定月份，YYYY / YYYY-MM / YYYY-MM-DD（前缀匹配）")
    ap.add_argument("--series", choices=["auto", "manual", "book"],
                    help="auto=流水线精读 manual=手动深读 book=书籍精读")
    ap.add_argument("--book", default=None, metavar="CITEKEY",
                    help="只看某本书（专著的 citekey，或编著各章所属书的 book_key）")
    ap.add_argument("--full-text-only", action="store_true", help="只要有全文精读的条目")
    ap.add_argument("--limit", type=int, default=10, help="最多显示条数（默认 10，0=不限）")
    ap.add_argument("--cite", action="store_true", help="只输出可直接粘贴的 [@a; @b] 引用串")
    ap.add_argument("--json", action="store_true", dest="as_json", help="结构化输出（含 total）")
    ap.add_argument("--notes-dir", default=None,
                    help="札记目录（默认 output/scholar_notes）")
    ap.add_argument("--index", default=None,
                    help="文献索引 JSON 路径（默认 notes_dir/literature_index.json）")
    args = ap.parse_args()

    terms = [t.strip().lower() for t in args.terms if t and t.strip()]
    if not terms:
        ap.error("关键词为空")
    if args.limit < 0:
        ap.error("--limit 不能为负（0=不限量）")
    if args.month and not MONTH_RE.match(args.month):
        ap.error("--month 格式应为 YYYY / YYYY-MM / YYYY-MM-DD，收到：{}".format(args.month))

    if args.notes_dir:
        notes_dir = REPO / args.notes_dir
    else:
        notes_dir = None
    if args.index:
        index_path = Path(args.index)
    elif notes_dir:
        index_path = notes_dir / INDEX_JSON
    else:
        index_path = INDEX_PATH  # 模块级常量，允许测试 monkeypatch
    index = _load_index(index_path)
    if index is None:
        return 2
    if index.get("citekey_collisions"):
        print("⚠️ citekey 撞键 {} 组，引用前先跑 notes_index.py --fix-collisions".format(
            len(index["citekey_collisions"])), file=sys.stderr)

    results = []
    skipped_missing = 0
    for e in index["papers"]:
        if not isinstance(e, dict) or e.get("duplicate_of") or not e.get("citekey"):
            continue
        # MISSING-KEY 占位键不对应任何真实文献；build_all_references/vault/embed_store
        # 三处消费方都拦截了它，这里必须一致，否则 --cite 会吐出书目里查无此人的死引用。
        if is_missing_citekey(e):
            skipped_missing += 1
            continue
        if args.tier and e.get("priority_tier") != args.tier:
            continue
        if args.month and not (e.get("month") or "").startswith(args.month):
            continue
        if args.series and e.get("series") != args.series:
            continue
        if args.full_text_only and not e.get("has_full_text_reading"):
            continue
        if args.book and not _in_book(e, args.book):
            continue
        m = _match(e, terms, args.role)
        if m:
            results.append((e, m[0], m[1]))

    # 排序：优先级层 → 句级真命中优先于纯标题命中 → 真命中句数多者靠前
    results.sort(key=lambda r: (TIER_ORDER.get(r[0].get("priority_tier"), 3),
                                0 if r[2] == "highlight" else 1, -len(r[1])))
    shown = results if args.limit == 0 else results[:args.limit]
    truncated = len(results) - len(shown)
    if skipped_missing:
        print("⚠️ {} 条占位键（MISSING-KEY，无 Zotero citekey）条目已跳过——补 citekey 后"
              "重跑 notes_index.py 重建索引".format(skipped_missing), file=sys.stderr)

    if args.as_json:
        print(json.dumps({
            "total": len(results), "shown": len(shown), "truncated": truncated,
            "query": {"terms": terms, "role": args.role},
            "results": [{
                "citekey": e.get("citekey"), "title": e.get("title"), "year": e.get("year"),
                "one_line": e.get("one_line"), "priority_tier": e.get("priority_tier"),
                "series": e.get("series"), "month": e.get("month"), "match_source": src,
                "note": "{}:{}".format(e.get("note_file"), e.get("note_line")),
                "highlights": hits,
            } for e, hits, src in shown],
        }, ensure_ascii=False, indent=2))
        return 0 if shown else 1

    if args.cite:
        if not shown:
            print("无命中", file=sys.stderr)
            return 1
        # 书籍命中带 pandoc 页码定位符：[@little2020rubin, p. 247]
        def _cite_one(e, hits):
            pages = next((h.get("pages") for h in hits if h.get("pages")), None)
            return "@{}{}".format(e.get("citekey"),
                                  ", p. {}".format(pages) if pages else "")

        print("[" + "; ".join(_cite_one(e, hits) for e, hits, _ in shown) + "]")
        if truncated:
            print("⚠️ 共 {} 条命中，仅输出前 {} 条（--limit 调整，0=不限）".format(
                len(results), len(shown)), file=sys.stderr)
        return 0

    if not shown:
        print("无命中（试试减少关键词、去掉 --role/--tier 过滤）")
        return 1
    print("命中 {} 条（显示 {}）{}\n".format(
        len(results), len(shown),
        "· role={}({})".format(args.role, ROLE_HINT[args.role]) if args.role else ""))
    for e, hits, src in shown:
        print("[@{}] ({}) {} {}".format(
            e.get("citekey"), e.get("year") or "?", TIER_EMOJI.get(e.get("priority_tier"), ""),
            (e.get("title") or "")[:90]))
        if e.get("one_line"):
            print("    ⭐ {}".format(e["one_line"]))
        if src == "title":
            print("    （关键词命中标题/一句话用处，非句级证据）")
        for h in hits[:MAX_SHOWN_HITS]:
            # 书籍证据显示「章 · 页」：精读分节名（方法与数据…）对一本 460 页的书
            # 没有定位价值，写作时要的是能直接落进 [@key, p. N] 的页码。
            where = h.get("chapter") or h.get("section")
            page_sfx = " ⟨p.{}⟩".format(h["pages"]) if h.get("pages") else ""
            print("    [{}·{}] {}{}".format(h.get("role"), where, h.get("text"), page_sfx))
        if len(hits) > MAX_SHOWN_HITS:
            print("    …还有 {} 条命中句（--json 看全部）".format(len(hits) - MAX_SHOWN_HITS))
        print("    ↳ {}:{}".format(e.get("note_file"), e.get("note_line")))
        print()
    if truncated:
        print("（另有 {} 条未显示，--limit 调整，0=不限）".format(truncated))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)
