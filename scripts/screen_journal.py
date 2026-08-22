# -*- coding: utf-8 -*-
"""期刊全量筛选 CLI——分步扫描工具。

针对指定期刊做两阶段文献筛选：
  Stage 1 (search)：PubMed 全量检索 + 标题级黑名单预筛 → search_results.json
  Stage 2 (filter)：filter-v3 LLM 三态裁决 → candidates.json + candidates.md

两步可独立运行、中间介入人工审查降量效果后再续跑。也支持一步到底。

用法示例：
  # 先看黑名单效果（不花钱）
  PYTHONPATH=. python scripts/screen_journal.py "NPJ Digit Med" \\
      --from 2021-08-01 --to 2026-08-01 --stage search

  # 审查降量分布后，续跑 filter-v3
  PYTHONPATH=. python scripts/screen_journal.py "NPJ Digit Med" --stage filter

  # 一步到底（search + filter 连跑）
  PYTHONPATH=. python scripts/screen_journal.py "NPJ Digit Med" \\
      --from 2021-08-01 --to 2026-08-01 --stage all
"""
import argparse
import sys
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.scholar.journal_screen import JournalScreener  # noqa: E402
from src.scholar.paths import repo_path  # noqa: E402
from src.scholar.schema import ScholarSettings  # noqa: E402
from src.scholar.settings import load_scholar_settings  # noqa: E402

DEFAULT_MAX_RESULTS = 5000  # npj DM 五年约 2500 篇，上限绰绰有余


def _load_settings() -> ScholarSettings:
    """统一走共享 loader（历史动机保留：裸 ScholarSettings() 会丢配置里的
    embedding_model 等项，且 notes_dir 随 cwd 漂，从非仓库根运行时向量库找不到、
    filter 的 library_neighbors 静默全空——锚定逻辑现在在 loader 里）。
    与收敛前的行为差异（有意）：缺失回退从静默变有警告；config 写 gemini 时
    筛选 LLM 与全链一致改走 deepseek。"""
    return load_scholar_settings()


def _parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(
            "日期须为 YYYY-MM-DD 格式（如 2021-08-01），收到: {!r}".format(s))


def cmd_search(args, output_dir: Path):
    """Stage 1: PubMed 检索 + 黑名单预筛"""
    settings = _load_settings()
    screener = JournalScreener(output_dir, settings)

    email = getattr(settings.processing, "external_email", "") or ""

    date_range = (args.date_from, args.date_to)
    result = screener.search_and_blacklist(
        journal_name=args.journal,
        date_range=date_range,
        max_results=args.max_results,
        email=email,
    )

    path = screener.save_search_results(result)

    # 打印降量摘要
    print("\n" + "=" * 60)
    print("Stage 1 完成: PubMed 检索 + 标题黑名单预筛")
    print("=" * 60)
    print("期刊: {}".format(result["journal"]))
    print("日期范围: {} → {}".format(*result["date_range"]))
    print("PubMed 检索: {} 篇".format(result["total_pubmed"]))
    print("黑名单排除: {} 篇".format(result["blacklist_excluded"]))
    print("保留进入 filter: {} 篇".format(len(result["kept"])))
    if result["blacklist_breakdown"]:
        print("\n黑名单命中分布:")
        for cat, cnt in sorted(result["blacklist_breakdown"].items(),
                               key=lambda x: -x[1]):
            print("  {}: {} 篇".format(cat, cnt))
    print("\n结果已保存: {}".format(path))
    print("\n下一步: python scripts/screen_journal.py '{}' --stage filter\n".format(
        args.journal))


def cmd_filter(args, output_dir: Path):
    """Stage 2: filter-v3 LLM 裁决（--no-llm 时仅关键词筛选）"""
    settings = _load_settings()
    screener = JournalScreener(output_dir, settings)

    search_results = screener.load_search_results()
    result = screener.run_filter(search_results, llm_filter=not args.no_llm)
    json_path, md_path = screener.export_candidates(result)

    # 打印裁决摘要
    stats = result.get("filter_stats", {})
    print("\n" + "=" * 60)
    print("Stage 2 完成: filter-v3 LLM 三态裁决")
    print("=" * 60)
    print("输入论文: {} 篇".format(result.get("after_blacklist", "?")))
    print("候选论文: {} 篇".format(result.get("candidates_count", "?")))
    if stats:
        for k in ["INCLUDE", "MAYBE", "EXCLUDE", "THREAT", "BENCHMARK"]:
            if k in stats and stats[k]:
                print("  {}: {}".format(k, stats[k]))

    # 预览 top 候选
    candidates = result.get("candidates", [])
    if candidates:
        print("\nTop 10 候选预览:")
        print("-" * 60)
        for i, c in enumerate(candidates[:10], 1):
            title = (c.get("title") or "")[:70]
            flags = " [{}]".format(",".join(c["flags"])) if c.get("flags") else ""
            in_lib = " 📚" if c.get("in_library") else ""
            print("  {:2d}. {} ({}){}{} → {}".format(
                i, title, c.get("year", "?"),
                flags, in_lib,
                (c.get("one_line") or "")[:50]))

    print("\n候选人读列表: {}".format(md_path))
    print("JSON: {}".format(json_path))
    print("\n下一步: 审查 candidates.md → 勾选 DOI 保存 picks.txt → ingest_notes.py --papers picks.txt\n")


def main():
    ap = argparse.ArgumentParser(
        description="期刊全量筛选——PubMed 检索 + 黑名单预筛 + filter-v3 LLM 裁决",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  # Step 1: 检索+黑名单\n"
            '  PYTHONPATH=. python scripts/screen_journal.py "NPJ Digit Med" '
            "--from 2021-08-01 --to 2026-08-01 --stage search\n\n"
            "  # Step 2: filter-v3 裁决\n"
            '  PYTHONPATH=. python scripts/screen_journal.py "NPJ Digit Med" --stage filter\n\n'
            "  # 一步到底\n"
            '  PYTHONPATH=. python scripts/screen_journal.py "NPJ Digit Med" '
            "--from 2021-08-01 --to 2026-08-01 --stage all\n"
        ),
    )
    ap.add_argument("journal", help="期刊名（PubMed 规范写法，如 'NPJ Digit Med'）")
    ap.add_argument("--from", dest="date_from", type=_parse_date,
                    default=None, metavar="YYYY-MM-DD",
                    help="起始日期（stage 含 search 时必填）")
    ap.add_argument("--to", dest="date_to", type=_parse_date,
                    default=None, metavar="YYYY-MM-DD",
                    help="结束日期（stage 含 search 时必填）")
    ap.add_argument("--stage", choices=["search", "filter", "all"], default="search",
                    help="执行阶段: search（检索+黑名单）、filter（续跑筛选）、all（一步到底）")
    ap.add_argument("--no-llm", action="store_true",
                    help="filter 阶段跳过 LLM，仅用关键词白名单生成候选（DeepSeek 不可用时）")
    ap.add_argument("--max-results", type=int, default=DEFAULT_MAX_RESULTS,
                    help="PubMed 最大检索数（默认 %(default)s）")
    ap.add_argument("--output-dir", default="output/journal_screen",
                    help="输出目录（默认 %(default)s）")
    args = ap.parse_args()

    output_dir = repo_path(args.output_dir)

    try:
        if args.stage in ("search", "all"):
            if args.date_from is None or args.date_to is None:
                ap.error("Stage search/all 需要 --from 和 --to")
            cmd_search(args, output_dir)

        if args.stage in ("filter", "all"):
            cmd_filter(args, output_dir)

    except FileNotFoundError as e:
        print("错误: {}".format(e), file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
