# -*- coding: utf-8 -*-
"""临时检索 arXiv / PubMed 的对话式 CLI（不入库，只报结果）。

复用流水线的 AcademicSearchClient，与月度 digest 完全解耦：
不写 seen、不进索引、不触发筛选/翻译。命中会对照本地
literature_index.json 标注「已在札记库」（按 DOI / arXiv id / 规范化标题）。

用法示例：
  PYTHONPATH=. python scripts/search_pubs.py "missing not at random EHR"
  PYTHONPATH=. python scripts/search_pubs.py "MNAR imputation" --source pubmed --days 90
  PYTHONPATH=. python scripts/search_pubs.py "graph transformer" --source arxiv \
      --from 2025-01-01 --to 2025-06-30 --max 10 --json
"""
import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.scholar.academic_search import AcademicSearchClient  # noqa: E402
from src.scholar.notes_index import INDEX_JSON                # noqa: E402

INDEX_PATH = PROJECT_ROOT / "output" / "scholar_notes" / INDEX_JSON
MAX_CAP = 100  # PubMed efetch 走 GET，PMID 拼进 URL；上限护栏防 URL 过长/被拒


def _norm_title(t: str) -> str:
    # 非字母数字（含所有 CJK）折成空格；CJK 标题会归一化为空串，故只能靠 DOI/arXiv id 命中
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def _norm_doi(d: str) -> str:
    """DOI 归一化：与仓库其它模块一致——去 http(s)://doi.org/ 与 doi: 前缀、小写、去尾标点。"""
    d = (d or "").strip().lower()
    d = d.replace("https://doi.org/", "").replace("http://doi.org/", "")
    if d.startswith("doi:"):
        d = d[4:]
    return d.strip().rstrip(".,;)")


def _norm_arxiv(a: str) -> str:
    return re.sub(r"v\d+$", "", (a or "").strip().lower())


def _load_index_keys():
    """从文献索引取 DOI / arXiv id / 规范化标题 → citekey 的映射，索引缺失/损坏时静默返回空。

    三类键混在一个 dict：DOI/arXiv id 保留 '.'/'/' 等分隔符，规范化标题只剩空格分隔的
    字母数字——键空间不重叠，跨类碰撞不会发生。空串键（如纯 CJK 标题）跳过，避免 keys[""]。

    同一篇论文常有两条索引项：keeper（duplicate_of 为空，指向主/高优先札记）与跨月/同月
    低优 duplicate（duplicate_of 非空）。二者共享 DOI/标题，同键相撞。按 duplicate 先、
    keeper 后排序写入，让 keeper 覆盖 duplicate——命中时优先报主札记的 citekey。
    仍保留 duplicate（不跳过）：有 8 篇论文的唯一索引项就是 duplicate 条目，跳过会漏标已收录。
    """
    keys = {}
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        # 整块包进 try：不止 JSON 解析失败要降级，"合法 JSON 但结构诡异"（顶层是 list、
        # papers 是字符串、entry 是 int 等）同样不能把 traceback 抛给 CLI 用户。
        papers = (data.get("papers") if isinstance(data, dict) else None) or []
        entries = papers.values() if isinstance(papers, dict) else papers
        # 只保留 dict 条目：混进非 dict（脏数据/手改坏）时跳过而非崩，且让 sorted 的 e.get 安全。
        entries = [e for e in entries if isinstance(e, dict)]
        for entry in sorted(entries, key=lambda e: not e.get("duplicate_of")):
            ck = entry.get("citekey") or "?"
            if entry.get("doi"):
                k = _norm_doi(entry["doi"])
                if k:
                    keys[k] = ck
            if entry.get("arxiv_id"):
                k = _norm_arxiv(entry["arxiv_id"])
                if k:
                    keys[k] = ck
            if entry.get("title"):
                k = _norm_title(entry["title"])
                if k:
                    keys[k] = ck
    except Exception:
        return keys
    return keys


def _in_library(item, index_keys):
    doi = _norm_doi(item.get("doi") or "")
    if doi and doi in index_keys:
        return index_keys[doi]
    aid = _norm_arxiv(item.get("arxiv_id") or "")
    if aid and aid in index_keys:
        return index_keys[aid]
    nt = _norm_title(item.get("title", ""))
    if nt and nt in index_keys:
        return index_keys[nt]
    return None


def _parse_date(s: str) -> date:
    # 抛 ArgumentTypeError（而非裸 ValueError）：argparse 直接透传本消息，
    # 否则用户看到的是 "invalid _parse_date value" 这种泄漏函数名的费解报错。
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"日期须为 YYYY-MM-DD 格式（如 2025-01-31），收到: {s!r}")


def main():
    ap = argparse.ArgumentParser(
        description="临时检索 arXiv/PubMed 学术文献（不入库、只报结果，命中标注是否已在本机札记库）。",
        epilog=(
            "示例：\n"
            "  search_pubs.py '\"missing not at random\"[tiab]' --source pubmed --max 3\n"
            "  search_pubs.py 'ti:\"graph transformer\"' --source arxiv --max 5\n"
            "  search_pubs.py 'MNAR imputation' --days 90 --json\n"
            "排序：两库均按发表日期降序（API 无相关度排序能力）；检索式松了会混进弱相关新文，"
            "请用字段语法收紧（arXiv: ti:/abs:/cat:；PubMed: [tiab] 加引号短语）。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("query", help="检索式（两库通用；PubMed 支持字段语法如 [tiab]，arXiv 支持 ti:/abs:/cat:）")
    ap.add_argument("--source", choices=["arxiv", "pubmed", "all"], default="all",
                    help="检索哪个源，默认 all（两库都搜、不去重）")
    ap.add_argument("--max", type=int, default=15,
                    help=f"每个源的最大结果数（默认 15，上限 {MAX_CAP}）")
    ap.add_argument("--days", type=int, default=None,
                    help="只要最近 N 天（与 --from/--to 互斥）")
    ap.add_argument("--from", dest="date_from", type=_parse_date, default=None,
                    metavar="YYYY-MM-DD", help="起始日期（须与 --to 配对；与 --days 互斥）")
    ap.add_argument("--to", dest="date_to", type=_parse_date, default=None,
                    metavar="YYYY-MM-DD", help="结束日期（须与 --from 配对）")
    ap.add_argument("--abstract", action="store_true", help="打印完整摘要（默认截 240 字符）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（date 序列化为 YYYY-MM-DD 字符串，供 agent 消费）")
    args = ap.parse_args()

    if not args.query.strip():
        ap.error("检索式为空")
    if (args.date_from is None) != (args.date_to is None):
        ap.error("--from 与 --to 必须成对出现")
    if args.days is not None and args.date_from is not None:
        ap.error("--days 与 --from/--to 互斥，二选一")
    if args.max < 1:
        ap.error("--max 至少为 1")
    if args.max > MAX_CAP:
        print(f"⚠️ --max {args.max} 超过上限，收敛到 {MAX_CAP}", file=sys.stderr)
        args.max = MAX_CAP
    date_range = (args.date_from, args.date_to) if args.date_from else None

    index_keys = _load_index_keys()
    results = []
    failures = []  # 区分「检索失败」与「真的没命中」：失败源记这里
    with AcademicSearchClient() as client:
        if args.source in ("arxiv", "all"):
            try:
                results += client.search_arxiv(args.query, max_results=args.max,
                                               days=args.days, date_range=date_range)
            except Exception as e:
                print(f"⚠️ arXiv 检索失败: {e}", file=sys.stderr)
                failures.append("arXiv")
        if args.source in ("pubmed", "all"):
            try:
                results += client.search_pubmed(args.query, max_results=args.max,
                                                days=args.days, date_range=date_range)
            except Exception as e:
                print(f"⚠️ PubMed 检索失败: {e}", file=sys.stderr)
                failures.append("PubMed")

    # 请求的全部源都失败 → 这不是「没命中」，退出码非 0，避免误导为空结果
    if failures and not results:
        print(f"检索失败（{'/'.join(failures)}），未能获取结果。"
              f"常见原因：网络/代理故障或 API 限流。", file=sys.stderr)
        sys.exit(2)

    for it in results:
        it["in_library"] = _in_library(it, index_keys)

    if args.json:
        # 包成 {"results","failures"}：部分源失败时退出码仍为 0，纯数组会让 JSON 消费方
        # 只看到半份结果却无从察觉另一源已挂——failures 非空即须提示"可能不完整"。
        print(json.dumps({"results": results, "failures": failures},
                         ensure_ascii=False, default=str, indent=2))
        return

    if not results:
        print("没有命中。")
        return
    for i, it in enumerate(results, 1):
        authors = it.get("authors") or []
        author_str = ", ".join(authors[:3]) + (" 等" if len(authors) > 3 else "")
        lib = f"  📚 已在札记库（{it['in_library']}）" if it.get("in_library") else ""
        pub = it.get("published") or "?"
        print(f"\n[{i}] {it['title']}{lib}")
        print(f"    {it['source']} | {pub} | {it.get('journal') or '-'} | {author_str}")
        ids = " ".join(filter(None, [
            f"doi:{it['doi']}" if it.get("doi") else None,
            f"arXiv:{it['arxiv_id']}" if it.get("arxiv_id") else None,
            f"PMID:{it['pmid']}" if it.get("pmid") else None,
        ]))
        if ids:
            print(f"    {ids}")
        if it.get("url"):
            print(f"    {it['url']}")
        ab = (it.get("abstract") or "").strip()
        if ab:
            print(f"    {ab if args.abstract else ab[:240] + ('…' if len(ab) > 240 else '')}")
    # 结尾行按**实际生效的源**生成：此前写死「arXiv+PubMed 未去重」，单源检索或
    # 一源失败时既误导来源、又暗示做过一次不存在的跨源合并（skill 文档还把这行当判据引）。
    attempted = [s for s, key in (("arXiv", "arxiv"), ("PubMed", "pubmed"))
                 if args.source in (key, "all")]
    ok_sources = [s for s in attempted if s not in failures]
    dedup_note = f"{'+'.join(ok_sources)} 未去重；" if len(ok_sources) > 1 else ""
    tail = f"（{'/'.join(failures)} 检索失败已跳过）" if failures else ""
    print(f"\n共 {len(results)} 条（{dedup_note}📚 = 札记库已收录）{tail}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        sys.exit(130)
