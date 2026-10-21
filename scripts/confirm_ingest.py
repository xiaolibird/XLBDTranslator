#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""入库确认（可信度加固 WO-8a，设计 §12.1）：每周入库/精读之前，用系统原生弹窗请用户过一遍。

不做 web、不做 TUI——只有 macOS 原生 osascript 弹窗与通知。

用法：
  python scripts/confirm_ingest.py                 # 手动：同步队列 → 弹窗 → 对已确认的周调 ingest
  python scripts/confirm_ingest.py --list          # 只看待确认队列（不弹窗、不写盘）
  python scripts/confirm_ingest.py --daily         # launchd 每日 job：同上，但当天提醒过就静默；
                                                   # 先发系统通知；无图形会话只通知不弹
  python scripts/confirm_ingest.py --date 2026-10-19      # 只处理该 digest run 日期的队列
  python scripts/confirm_ingest.py --no-ingest     # 只确认，不触发入库（之后手动跑 ingest_notes.py）

语义：
  * 取消 / 点「稍后」/ 超时无人应答 / 无图形会话 → 全部**挂起**（pending），不入库、不精读，
    明天再提醒，直到确认；取消不是丢弃。
  * 每页「确认本页」立刻落盘（队列 state + _eval/filter/stream_labels.jsonl 追加）。
  * 勾上筛选器原判 EXCLUDE 的 → 视同手选覆盖（stage=manual_pick，原判留在 llm_decision_before_override）。
  * 旧行为（不确认直接入库）：对 ingest_notes.py 用 --no-confirm（或环境变量 SCHOLAR_NO_CONFIRM=1）；
    回填（backfill_notes.py / monthly）与 manual 系列（read_pdf.py）本来就不经过本流程。

退出码：0 正常（含「无待确认」「已挂起」）/ 2 参数或路径错误。入库子进程失败不改本脚本退出码，
只通知（失败条目留着，次日自动重试，最多 3 次）。
"""
import argparse
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar import confirm as C                          # noqa: E402
from src.scholar import thresholds as TH                      # noqa: E402
from src.scholar.paths import repo_path                       # noqa: E402
from src.utils.logger import get_logger                       # noqa: E402

logger = get_logger("confirm_ingest")
NOTIFY_TITLE = "Scholar 入库确认"


def _seen_keys(notes_dir):
    """库内已有键：已在库的论文不再入队去问。索引损坏时 fail-soft（只会多问几篇，ingest 自己会去重）。"""
    from src.scholar.notes_index import INDEX_JSON, load_seen_keys
    try:
        return load_seen_keys(Path(notes_dir) / INDEX_JSON)
    except RuntimeError as e:
        logger.warning("  ⚠️ 读不出文献索引，入队不排除已在库论文：{}".format(e))
        return set()


def run_ingest_for_week(monday: date, runner=None) -> int:
    """对某自然周调 ingest_notes.py（子进程：沿用其全部无人值守错误处理与通知）。返回退出码。"""
    runner = runner or (lambda argv: subprocess.run(argv, check=False).returncode)
    argv = [sys.executable, str(Path(__file__).resolve().parent / "ingest_notes.py"),
            "--week", monday.isoformat(), "--auto", "--top-n", "5"]
    logger.info("  入库：{}".format(" ".join(argv[1:])))
    return runner(argv)


def ingest_confirmed(notes_dir, run_dates, *, notify, runner=None, now=None) -> dict:
    """对这些 run 日期所在的自然周逐周入库；成功（0/3）标记 ingested，失败计数。"""
    from src.scholar.ingest import week_monday
    weeks = {}
    for ds in sorted(run_dates):
        weeks.setdefault(week_monday(date.fromisoformat(ds)), []).append(ds)
    out = {}
    for monday, dates in sorted(weeks.items()):
        rc = run_ingest_for_week(monday, runner)
        out[monday.isoformat()] = rc
        if rc in (0, 3):
            for ds in dates:
                C.mark_ingested(notes_dir, ds, now)
        else:
            capped = sum(C.bump_ingest_attempts(notes_dir, ds) for ds in dates)
            if capped:
                notify(NOTIFY_TITLE, "{} 周的已确认论文入库 {} 次仍未成功（也可能是都已在库）；"
                                     "不再自动重试，请查日志".format(monday, C.MAX_INGEST_ATTEMPTS))
    return out


def main(argv=None, *, dialogs=None, runner=None, today=None, now=None) -> int:
    ap = argparse.ArgumentParser(description="入库确认弹窗（原生 osascript）",
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--notes-dir", default="output/scholar_notes")
    ap.add_argument("--digest-dir", default="output/scholar_digest")
    ap.add_argument("--date", default="", metavar="YYYY-MM-DD", help="只处理该 digest run 日期")
    ap.add_argument("--list", action="store_true", dest="list_only", help="只列出待确认，不弹窗")
    ap.add_argument("--daily", action="store_true", help="launchd 每日提醒模式（当天提醒过则静默）")
    ap.add_argument("--no-ingest", action="store_true", help="确认后不触发入库")
    ap.add_argument("--lookback-days", type=int, default=TH.CONFIRM_QUEUE_LOOKBACK_DAYS,
                    help="自动入队只扫最近多少天的 digest run（默认 %(default)s）")
    args = ap.parse_args(argv)

    now = now or datetime.now()
    today = today or now.date()
    notes_dir, digest_dir = repo_path(args.notes_dir), repo_path(args.digest_dir)
    if not digest_dir.exists():
        print("找不到 digest 目录：{}".format(digest_dir), file=sys.stderr)
        return 2
    if dialogs is None:
        from src.utils.osa_dialog import OsaDialogs
        dialogs = OsaDialogs()

    only = None
    since, until = today - timedelta(days=args.lookback_days), today
    if args.date:
        try:
            d = date.fromisoformat(args.date)
        except ValueError:
            print("--date 应为 YYYY-MM-DD", file=sys.stderr)
            return 2
        only, since, until = {d.isoformat()}, d, d

    if args.list_only:
        rows = C.pending_items(notes_dir, only)
        for i, (_, it) in enumerate(rows, 1):
            print(C.item_label(i, it))
        print("待确认 {} 篇".format(len(rows)))
        return 0

    rep = C.sync_queue(notes_dir, digest_dir, since, until, seen=_seen_keys(notes_dir), now=now)
    if rep.runs_new:
        logger.info("新入队 {} 个 digest run，共 {} 篇待确认".format(rep.runs_new, rep.items_added))

    pending = C.pending_items(notes_dir, only)
    retry_dates = set(C.keep_run_dates_uningested(notes_dir))     # 此前已确认、入库没成功的

    if pending:
        if args.daily and C.prompted_today(notes_dir, today):
            logger.info("今天已提醒过（{} 篇仍待确认），静默".format(len(pending)))
        else:
            dialogs.notify(NOTIFY_TITLE, "{} 篇论文待确认入库（未确认的不会入库/精读）".format(len(pending)))
            sess = C.run_session(notes_dir, dialogs, now=now, run_dates=only,
                                 require_gui=True)
            if args.daily:
                C.mark_prompted(notes_dir, today)
            logger.info("弹窗会话：{} · 保留 {} 丢弃 {} 仍挂起 {}{}".format(
                sess.status, sess.n_keep, sess.n_drop, sess.n_still_pending,
                "（{}）".format(sess.reason) if sess.reason else ""))
            print("确认结果：保留 {} · 丢弃 {} · 仍挂起 {}{}".format(
                sess.n_keep, sess.n_drop, sess.n_still_pending,
                "（{}）".format(sess.reason) if sess.reason else ""))
            retry_dates |= set(sess.keep_run_dates)
    else:
        print("没有待确认条目。")

    if retry_dates and not args.no_ingest:
        if only:
            retry_dates &= only
        res = ingest_confirmed(notes_dir, retry_dates, notify=dialogs.notify, runner=runner, now=now)
        print("入库子任务退出码：{}".format(res))
    elif retry_dates:
        print("已确认待入库的 run 日期：{}（--no-ingest：请手动跑 ingest_notes.py --week …）".format(
            sorted(retry_dates)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)
