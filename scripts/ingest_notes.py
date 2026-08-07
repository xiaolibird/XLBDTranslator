# -*- coding: utf-8 -*-
"""按周/按需入库：把 digest 判过的论文接上「精读 → 周札记」。

周报（digest）每周一 09:00 判完就把结果丢在 output/scholar_digest/，从不写札记。
本 CLI 复用那批**已有的 filter-v3 裁决**（不重跑 LLM 筛选），补上精读与落盘，
产出 `科研札记_YYYY-MM-DD_全文精读.md`（周一日期）。

三条入口：
  # 看本周 digest 判出了什么（不写盘）
  PYTHONPATH=. python scripts/ingest_notes.py --list

  # 只入其中几篇（序号同 --list）
  PYTHONPATH=. python scripts/ingest_notes.py --pick 2,3,5

  # 本周全部入库（无人值守，launchd 周一 09:30 用这条）
  PYTHONPATH=. python scripts/ingest_notes.py --auto

  # 任意 DOI / arXiv id / 标题，一行一个（可能压根没在 Scholar 告警里出现过）
  PYTHONPATH=. python scripts/ingest_notes.py --papers papers.txt

本地 PDF 走另一条链路（三段式 agent 交叉核验）：
  PYTHONPATH=. python scripts/read_pdf.py ingest <目录或 PDF 路径>

退出码：0 成功 / 1 无论文可入 / 2 配置或输入错误。
"""
import argparse
import json
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar import ingest as ing                        # noqa: E402
from src.scholar.paths import repo_path                         # noqa: E402
from src.scholar.schema import ScholarSettings               # noqa: E402
from src.utils.logger import get_logger                      # noqa: E402
from src.utils.notify import notify                          # noqa: E402

logger = get_logger("ingest_cli")


def parse_pick(spec: str, n: int) -> list:
    """`2,3,5` 或 `1-4,7` → 0-based 下标；越界即报错，不静默忽略（少入库比乱入库更难发现）。"""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part.lstrip("-"):
            a, _, b = part.partition("-")
            lo, hi = int(a), int(b)
        else:
            lo = hi = int(part)
        if lo < 1 or hi > n or lo > hi:
            raise ValueError("序号 {} 超出 1..{}".format(part, n))
        out.extend(range(lo - 1, hi))
    seen, uniq = set(), []
    for i in out:
        if i not in seen:
            seen.add(i)
            uniq.append(i)
    return uniq


def main() -> int:
    ap = argparse.ArgumentParser(description="把 digest 判过的论文入库成周札记")
    src = ap.add_argument_group("来源（三选一，默认读本周 digest）")
    src.add_argument("--papers", default="", metavar="FILE",
                     help="任意 DOI/arXiv id/标题列表，一行一个（# 起头为注释）")
    src.add_argument("--week", default="", metavar="YYYY-MM-DD",
                     help="指定某周（该日期所在自然周，默认本周）")
    src.add_argument("--since", default="", metavar="YYYY-MM-DD", help="自定义起始日（覆盖 --week）")
    src.add_argument("--until", default="", metavar="YYYY-MM-DD", help="自定义结束日")

    act = ap.add_argument_group("动作")
    act.add_argument("--list", action="store_true", dest="list_only", help="只列出候选，不写盘")
    act.add_argument("--pick", default="", metavar="2,3,5", help="只入指定序号（同 --list 的编号）")
    act.add_argument("--auto", action="store_true", help="全部入库（无人值守；等价于不给 --pick）")

    ap.add_argument("--config", default="config/scholar.env")
    ap.add_argument("--digest-dir", default="output/scholar_digest")
    ap.add_argument("--top-n", type=int, default=5, help="全文精读篇数（默认 5）")
    ap.add_argument("--no-close-read", action="store_true", help="跳过全文精读（快，只出摘要级）")
    ap.add_argument("--no-index", action="store_true", help="收尾不刷新文献索引")
    ap.add_argument("--no-dedup", action="store_true", help="不跨库去重（调试用；会产生重复条目）")
    ap.add_argument("--label", default="", help="覆盖札记标签（默认所属周的周一日期）")
    ap.add_argument("--dry-run", action="store_true", help="走到写盘前停下")
    args = ap.parse_args()

    if args.top_n < 0:
        ap.error("--top-n 不能为负")
    if args.pick and args.auto:
        ap.error("--pick 与 --auto 互斥")

    cfg = Path(args.config)
    if not cfg.exists():
        print("找不到配置：{}".format(cfg), file=sys.stderr)
        return 2
    settings = ScholarSettings.from_env_file(cfg)
    if settings.llm.provider == "gemini" and settings.llm.model.startswith("gemini"):
        settings.llm.provider = "deepseek"      # 与既有 pipeline 一致
    # notes_dir 默认相对，语义是「仓库里的那个目录」——锚死，别随 cwd 漂（见 paths.repo_path）
    settings.processing.notes_dir = repo_path(settings.processing.notes_dir)

    # ---- 收集候选 ----
    if args.papers:
        pf = Path(args.papers)
        if not pf.exists():
            print("找不到列表文件：{}".format(pf), file=sys.stderr)
            return 2
        ids = ing.parse_identifiers(pf.read_text(encoding="utf-8"))
        if not ids:
            print("列表为空：{}".format(pf), file=sys.stderr)
            return 2
        logger.info("手动列表 {} 条，正在抓元数据…".format(len(ids)))
        email = settings.processing.zotero_email or settings.processing.external_email or ""
        segs = ing.segments_from_identifiers(ids, email=email)
        if not segs:
            logger.warning("一条都没解析出来")
            return 1
        logger.info("解析到 {}/{} 篇，正在打维度标签…".format(len(segs), len(ids)))
        ing.classify_segments(segs, settings, force_include=True)
        label = args.label or ing.week_label()
    else:
        try:
            if args.since:
                since = datetime.strptime(args.since, "%Y-%m-%d").date()
                until = (datetime.strptime(args.until, "%Y-%m-%d").date()
                         if args.until else date.today())
            else:
                anchor = (datetime.strptime(args.week, "%Y-%m-%d").date()
                          if args.week else date.today())
                since = ing.week_monday(anchor)
                until = since + timedelta(days=6)
        except ValueError as e:
            print("日期格式应为 YYYY-MM-DD：{}".format(e), file=sys.stderr)
            return 2
        segs = ing.load_digest_segments(repo_path(args.digest_dir), since=since, until=until)
        label = args.label or since.strftime("%Y-%m-%d")
        logger.info("digest 窗口 {} → {}：入选 {} 篇（复用已有裁决，未重跑筛选）".format(
            since, until, len(segs)))

    if not segs:
        print("该窗口没有入选论文。", file=sys.stderr)
        return 1

    # ---- 跨库去重（**在列清单之前**）----
    # digest 目录里混着历史 backfill 跑出来的产物（date_range 被外部检索撑到 2008 年，
    # 无法靠跨度区分），指定历史周会捞出几千篇早已入库的。先去重再展示，
    # 让 --list 显示的就是真正会入库的，--pick 的序号也才有意义。
    seen = None
    if not args.no_dedup:
        from src.scholar.notes_index import load_seen_keys
        seen = load_seen_keys(Path(settings.processing.notes_dir) / "literature_index.json")
        before = len(segs)
        # 只筛不改 seen——真正的去重（含元数据增强后的第二轮）在 run_ingest 里做
        segs = [s for s in segs if ing.dedup_key(s.metadata) not in seen]
        if before != len(segs):
            logger.info("跨库去重：{} 篇已在札记库中（索引 {} 键），剩 {} 篇".format(
                before - len(segs), len(seen), len(segs)))
        if not segs:
            print("该窗口的论文都已入库，无新论文。", file=sys.stderr)
            return 1

    # ---- 挑选 ----
    if args.pick:
        try:
            idx = parse_pick(args.pick, len(segs))
        except ValueError as e:
            print("--pick 解析失败：{}".format(e), file=sys.stderr)
            return 2
        segs = [segs[i] for i in idx]

    print("\n候选 {} 篇（label={}）：".format(len(segs), label))
    print("\n".join(ing.describe(segs)))
    if args.list_only:
        print("\n（--list 只看不写。用 --pick 2,3,5 入其中几篇，或 --auto 全入。）")
        return 0

    if args.dry_run:
        print("\n【dry-run】待入 {} 篇 → 科研札记_{}_全文精读.md（全文精读 top-{}）".format(
            len(segs), label, 0 if args.no_close_read else args.top_n))
        return 0

    rep = ing.run_ingest(segs, settings, label, top_n=args.top_n,
                         close_read=not args.no_close_read, seen=seen)
    if rep["status"] == "empty":
        print("\n去重后无新论文，未写盘。", file=sys.stderr)
        return 1

    if not args.no_index:
        from src.scholar.notes_index import update_index, write_outputs
        nd = Path(settings.processing.notes_dir)
        index_data = update_index(nd)
        write_outputs(index_data, nd)      # 增量：只重解析新增的周文件
        logger.info("文献索引已刷新")

        # best-effort 向量同步：周一 09:30 launchd 自动入库绝不能因 Ollama 没起而失败。
        # 任何异常（Ollama 不可达/模型未 pull/网络问题）只 log warning，不改变退出码，
        # 不中断脚本——向量库是索引的纯派生物，缺一次同步不影响本次入库结果。
        try:
            from src.scholar.embeddings import EmbeddingClient, resolve_embedding_base_url
            from src.scholar.embed_store import DB_NAME, sync_store
            client = EmbeddingClient(
                base_url=resolve_embedding_base_url(settings.llm),
                model=settings.llm.embedding_model,
            )
            try:
                stats = sync_store(nd / DB_NAME, index_data, client)
            finally:
                client.close()
            logger.info("向量库已同步：+{} 嵌入 / -{} 删除 / {} 元数据刷新".format(
                stats.embedded, stats.deleted, stats.meta_refreshed))
        except Exception as e:
            logger.warning("向量库同步跳过（不影响入库）：{}".format(e))
            # launchd 无人值守时 warning 没人看——弹系统通知，向量库静默落后要有人知道
            # （notify 自身失败静默，不会反过来影响入库退出码）
            notify("Scholar 周入库", "向量库同步失败（札记已入库）：{}".format(str(e)[:120]))

    print("\n{}".format("=" * 66))
    print("✅ {} 篇 → {}".format(rep["count"], rep["md"]))
    print("   全文精读 {} 篇 · citekey 命中 {}/{}".format(
        rep["full_text"], rep["citekey"], rep["count"]))
    print("=" * 66)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        # --auto 的窗口是本自然周，下周一的运行不会回捞上周：失败那周若无人知晓，
        # 该周 digest 判过的论文就永远不进札记库。fail-fast（run_ingest 的 0/N
        # RuntimeError、load_seen_keys 的索引损坏 RuntimeError）必须配套当场告警，
        # 「重跑即恢复」的前提——有人知道要重跑——才成立。
        traceback.print_exc()
        notify("周度札记入库失败", "{}: {}".format(type(e).__name__, str(e)[:200]))
        sys.exit(1)
