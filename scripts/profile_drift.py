#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""月度研究画像漂移提醒（可信度加固 WO-8c，设计 §12.3）。

读近 4 周入库确认标签（stream_labels.jsonl），统计：
  * 补勾（筛选器 EXCLUDE → 你保留）按 exclude_reason、按语义聚类；
  * 取消（筛选器收 → 你丢弃）按 bucket 聚类。
只有「成簇」（同一原因/簇 ≥ 5 篇且占该类 ≥ 50%，阈值在 thresholds.DRIFT_*）才提醒：
系统通知 + 生成 <notes_dir>/_eval/filter/drift_<YYYY-MM>.md（簇内论文 + 建议的画像改动文字）。
**不改画像**；没有成簇时不通知、不写报告。

用法：
  python scripts/profile_drift.py                 # 分析近 4 周，成簇则写报告+通知
  python scripts/profile_drift.py --dry-run       # 只打印，不写文件、不通知
  python scripts/profile_drift.py --no-semantic   # 不做语义聚类（不连 embedding 服务）
  python scripts/profile_drift.py --as-of 2026-11-01   # 指定截止日（复现/补跑）

launchd：config/launchd/com.xlbd.scholar-drift.plist（每月 1 日 10:30），由 scripts/install_confirm.sh 安装。
退出码恒为 0（提醒类任务，不该因数据缺失报错）；参数错误 2。
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar import confirm as C                          # noqa: E402
from src.scholar import filter_eval as FE                     # noqa: E402
from src.scholar import profile_drift as PD                   # noqa: E402
from src.scholar import thresholds as TH                      # noqa: E402
from src.scholar.paths import repo_path                       # noqa: E402
from src.utils.logger import get_logger                       # noqa: E402

logger = get_logger("profile_drift")


def default_embed_fn(config: str):
    """本地 Ollama bge-m3 嵌入；构造失败返回 None（调用方降级为不做语义聚类）。"""
    try:
        from src.scholar.embeddings import EmbeddingClient, resolve_embedding_base_url
        from src.scholar.settings import load_scholar_settings
        st = load_scholar_settings(config)
        client = EmbeddingClient(base_url=resolve_embedding_base_url(st.llm), model=st.llm.embedding_model)
    except Exception as e:
        logger.warning("embedding 客户端不可用，跳过语义聚类：{}".format(e))
        return None
    return lambda texts: client.embed(list(texts))


def main(argv=None, *, embed_fn=None, notify=None, now=None) -> int:
    ap = argparse.ArgumentParser(description="月度研究画像漂移提醒", formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--notes-dir", default="output/scholar_notes")
    ap.add_argument("--config", default="config/scholar.env")
    ap.add_argument("--as-of", default="", metavar="YYYY-MM-DD", help="窗口截止（默认现在）")
    ap.add_argument("--weeks", type=int, default=None, help="回看周数（默认 thresholds.DRIFT_LOOKBACK_WEEKS）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件、不通知")
    ap.add_argument("--no-semantic", action="store_true", help="不做语义聚类")
    args = ap.parse_args(argv)

    if args.as_of:
        try:
            now = datetime.fromisoformat(args.as_of).replace(hour=23, minute=59, second=59)
        except ValueError:
            print("--as-of 应为 YYYY-MM-DD", file=sys.stderr)
            return 2
    now = now or datetime.now()
    notes = repo_path(args.notes_dir)
    weeks = args.weeks or TH.DRIFT_LOOKBACK_WEEKS
    labels = C.load_stream_labels(notes)
    if not labels:
        print("没有入库确认标签（{}），无从判断漂移。".format(C.labels_path(notes)))
        return 0
    if embed_fn is None and not args.no_semantic:
        embed_fn = default_embed_fn(args.config)
    a = PD.analyze(labels, now=now, lookback_weeks=weeks, embed_fn=None if args.no_semantic else embed_fn)
    print("窗口 {} → {}：标签 {} 条｜补勾 {}｜取消 {}｜成簇 {} 个｜语义聚类 {}".format(
        a.window_start.date(), a.window_end.date(), a.n_labels, a.n_added, a.n_removed,
        len(a.clusters), a.semantic_status))
    if not a.clusters:
        print("没有成簇的分歧，不提醒。")
        return 0
    report = PD.render_report(a)
    if args.dry_run:
        print("\n" + report)
        return 0
    out = FE.eval_dir(notes) / "drift_{}.md".format(now.strftime("%Y-%m"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print("已写入 {}".format(out))
    if notify is None:
        from src.utils.notify import notify
    notify("Scholar 研究画像漂移", "{}。详见 {}".format(PD.summary_text(a), out.name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
