#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""周 digest 自动筛选（替代入库确认弹窗）：LLM 代用户勾选 → 入库 → 事后一份报告。

用法：
  python scripts/auto_triage.py               # 同步队列 → 研判 → 落盘决策 → 入库 → 报告 + 系统通知
  python scripts/auto_triage.py --dry-run     # 只研判并打印/写报告，不落盘决策、不入库
  python scripts/auto_triage.py --date 2026-11-02   # 只处理该 digest run 日期
  python scripts/auto_triage.py --no-ingest   # 只做决策，不触发入库

口径与失败语义见 src/scholar/auto_triage.py。覆盖某天的决定：
  python scripts/confirm_ingest.py --reopen YYYY-MM-DD   （已入库则拒绝），再手动走原弹窗。
退出码：0 正常（含无待处理）/ 1 LLM 整体不可用或周一批次缺失 / 2 参数或路径错误。
"""
import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.scholar import auto_triage as AT                         # noqa: E402
from src.scholar import confirm as C                               # noqa: E402
from src.scholar import thresholds as TH                           # noqa: E402
from src.scholar.llm_client import LLMClient                       # noqa: E402
from src.scholar.notes_index import INDEX_JSON                     # noqa: E402
from src.scholar.paths import repo_path                            # noqa: E402
from src.scholar.schema import ScholarSettings                     # noqa: E402
from src.utils.logger import get_logger                            # noqa: E402

import confirm_ingest as CI                                        # noqa: E402  （复用 sync/入库编排）

logger = get_logger("auto_triage_cli")
NOTIFY_TITLE = "Scholar 自动筛选"


def main(argv=None, *, llm=None, notify=None, runner=None, today=None, now=None, call=None) -> int:
    ap = argparse.ArgumentParser(description="周 digest 自动筛选")
    ap.add_argument("--config", default="config/scholar.env")
    ap.add_argument("--notes-dir", default="output/scholar_notes")
    ap.add_argument("--digest-dir", default="output/scholar_digest")
    ap.add_argument("--date", default="", metavar="YYYY-MM-DD", help="只处理该 digest run 日期")
    ap.add_argument("--dry-run", action="store_true", help="只研判+出报告，不落盘决策、不入库")
    ap.add_argument("--no-ingest", action="store_true", help="落盘决策后不触发入库")
    ap.add_argument("--lookback-days", type=int, default=TH.CONFIRM_QUEUE_LOOKBACK_DAYS)
    args = ap.parse_args(argv)

    now = now or datetime.now()
    today = today or now.date()
    notes_dir, digest_dir = repo_path(args.notes_dir), repo_path(args.digest_dir)
    if not digest_dir.exists():
        print("找不到 digest 目录：{}".format(digest_dir), file=sys.stderr)
        return 2
    if notify is None:
        from src.utils.osa_dialog import OsaDialogs
        notify = OsaDialogs().notify

    health_alert = None
    only, since, until = None, today - timedelta(days=args.lookback_days), today
    if args.date:
        try:
            d = date.fromisoformat(args.date)
        except ValueError:
            print("--date 应为 YYYY-MM-DD", file=sys.stderr)
            return 2
        only, since, until = {d.isoformat()}, d, d

    rep = None
    if not args.dry_run:        # dry-run 不写队列文件
        rep = C.sync_queue(notes_dir, digest_dir, since, until, seen=CI._seen_keys(notes_dir), now=now)
        if rep.runs_new:
            logger.info("新入队 {} 个 digest run，共 {} 篇".format(rep.runs_new, rep.items_added))

    # 健康判定放在 sync 之后：digest 恰好在 10:00 边界完成时，以同步后的最新状态为准，
    # 不先发一条无法撤回的误报。sync 读取失败也不能再被“没有待确认条目”吞掉。
    if not args.date and not args.dry_run:
        health_alert = AT.weekly_digest_alert(digest_dir, now)
        if rep and rep.runs_failed:
            msg = "digest 批次同步失败（{}），请修复 JSON/排除清单后重跑。".format(
                ", ".join(rep.runs_failed))
            if health_alert:
                health_alert = AT.DigestHealthAlert(
                    health_alert.batch_date, health_alert.message + " " + msg)
            else:
                # 伪批次键排序后拼接：不同天失败集合顺序不同也能命中同一条去重记录。
                health_alert = AT.DigestHealthAlert("sync:" + ",".join(sorted(rep.runs_failed)), msg)
        if health_alert:
            logger.error(health_alert.message)
            print(health_alert.message, file=sys.stderr)
            if not AT.digest_alert_already_sent(notes_dir, health_alert.batch_date):
                notify("Scholar 周 digest 可能漏跑", health_alert.message)
                try:
                    AT.record_digest_alert(notes_dir, health_alert.batch_date, now)
                except OSError as e:
                    logger.warning("周批健康通知状态写回失败：{}".format(e))

    rows = C.pending_items(notes_dir, only)
    retry_dates = set(C.keep_run_dates_uningested(notes_dir))     # 此前已保留、入库没成功的
    if only:
        retry_dates &= only

    tr = AT.Triage()
    if rows:
        if llm is None:
            cfg = repo_path(args.config)
            settings = ScholarSettings.from_env_file(cfg) if cfg.exists() else ScholarSettings()
            llm = LLMClient(settings.llm)
            model = settings.llm.filter_model or settings.llm.model
        else:
            model = None
        try:
            lib = AT.library_titles(notes_dir / INDEX_JSON)
        except Exception as e:                                    # noqa: BLE001 —— 去重是加分项，读不出不阻断
            logger.warning("读不出库内标题，跳过标题去重：{}".format(e))
            lib = {}
        tr = AT.triage_items(rows, llm, model=model, lib_titles=lib, call=call)
        if not args.dry_run:
            AT.apply(notes_dir, tr, now=now)
            keep_dates = {C.load_queue(qp).get("run_date", "") for qp, _, _ in tr.keep} - {""}
            retry_dates |= keep_dates
    else:
        print("没有待确认条目。")

    ingest_note = ""
    if retry_dates and not args.no_ingest and not args.dry_run:
        res = CI.ingest_confirmed(notes_dir, retry_dates, notify=notify, runner=runner, now=now)
        ingest_note = "；".join("{} 周 退出码 {}（0/3=成功或全已在库）".format(w, rc) for w, rc in res.items())
        print("入库子任务：{}".format(ingest_note))
    elif retry_dates and args.no_ingest:
        ingest_note = "已跳过（--no-ingest）；待入库 run：{}".format(sorted(retry_dates))

    if rows:
        report = AT.render_report(tr, today=today.isoformat(), ingest_note=ingest_note)
        out = notes_dir / C.PENDING_DIRNAME / "auto_triage_{}{}.md".format(
            today.isoformat(), "_dryrun" if args.dry_run else "")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(report)
        print("报告：{}".format(out))
        if not args.dry_run:
            notify(NOTIFY_TITLE, "{}（报告 {}）".format(AT.notify_text(tr), out.name))
        if tr.undecided and not (tr.keep or tr.drop_llm or tr.drop_dup):
            return 1                                              # LLM 整体不可用
    return 1 if health_alert else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)
