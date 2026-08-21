#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""存量归一：把月度书目里 CSL `issued`（出版日期）的**占位月日**降到真实精度。

⚠️ 术语：CSL 的 `issued` = 出版日期，`issue` = 期号，两者无关，别读混。
（脚本名刻意避开 "issue" 词根，就是为了不让人扫一眼以为它在改期号。）

## 问题
`PaperMetadata.publication_date` 是 date 类型，必须凑齐年月日；而 Crossref 常只给
`[[2026]]`、PubMed 常只给 Year+Month、pdf-llm 只抽得到年 —— 各来源一律补 1。
于是全库 445 条 `[[Y,1,1]]` + 689 条 `[[Y,M,1]]` 里的月日多半是假的，
pandoc 的 author-date 样式会照着渲染出论文并不存在的月份（"2026 (January)"）。

## 两阶段
- **Phase 1（网络，权威）**：`[[Y,1,1]]` 且有 DOI 的条目回查 Crossref，
  **按返回 date-parts 的长度**写回真实精度（这是唯一能把"真元旦出版"与"占位"分开的办法）。
- **Phase 2（本地，启发式）**：Phase 1 没覆盖到的，按形状倒推
  （`(m,d)==(1,1)`→只留年、`d==1`→留年月），与 `_citekey_utils.infer_date_precision`
  同一口径，保证脚本归一的结果与将来 regen 重写的结果一致。

已知误伤（可接受）：Phase 2 会把真实"某月 1 日出版"降为月精度、真实元旦降为年精度。
只丢渲染用的月/日，**年份永不丢**，检索与身份键（dedup_key/citekey）完全不受影响。

## 同步 bundle
手动精读月份的 references.json 会被 `read_pdf.py regen` 从 bundle 整篇重写。
所以命中的条目还要把 `date_precision`（Phase 1 命中时连 `publication_date`）写回
对应 bundle 的 `segment.metadata`，否则下次 regen 就把归一成果冲掉了。

## 用法
    PYTHONPATH=. python3 scripts/normalize_pub_dates.py                 # dry-run 看统计
    PYTHONPATH=. python3 scripts/normalize_pub_dates.py --apply --i-know-this-is-a-one-shot-migration
    PYTHONPATH=. python3 scripts/notes_index.py --full                  # 收尾：重建 all_references

**只改 78 个月度 `*.references.json` 与 manual bundle；不碰 `all_references.json`**
（它由 notes_index 每次 --full 从月度文件整体重建，手改必被覆盖）。

⚠️ 一次性迁移，跑完请勿再跑：本轮之后新入库的论文会带真实的 `date_precision`，
其中不乏"确实是某月 1 日出版"的条目，重跑 Phase 2 会把它们误截。故 --apply 需显式带
`--i-know-this-is-a-one-shot-migration` 确认开关。
"""
import argparse
import json
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from src.scholar.paths import repo_path  # noqa: E402
from src.scholar.notes_index import INDEX_JSON  # noqa: E402
from src.scholar._citekey_utils import infer_date_precision  # noqa: E402
from src.utils.logger import get_logger  # noqa: E402

logger = get_logger("normalize_pub_dates")

CROSSREF_API = "https://api.crossref.org/works"


def _dp_of(item):
    """取条目的 date-parts 首段，形如 [2026, 1, 1]；缺失/畸形返回 None。"""
    dp = ((item.get("issued") or {}).get("date-parts") or [None])[0]
    return dp if isinstance(dp, list) and dp and isinstance(dp[0], int) else None


def crossref_precision(doi, client, mailto):
    """回查 Crossref 拿真实精度与日期。返回 (precision, date_parts) 或 (None, None)。"""
    d = (doi or "").strip().replace("https://doi.org/", "").strip("/")
    if not d:
        return None, None
    try:
        r = client.get("{}/{}".format(CROSSREF_API, d),
                       params={"mailto": mailto} if mailto else {}, timeout=30)
        if r.status_code != 200:
            return None, None
        msg = (r.json() or {}).get("message") or {}
    except Exception as exc:
        logger.warning("  ⚠️ Crossref 查询失败 {}: {}".format(d, type(exc).__name__))
        return None, None
    for key in ("issued", "published-print", "published-online", "published"):
        parts = (msg.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and isinstance(parts[0][0], int):
            dp = [int(x) for x in parts[0][:3]]
            return ("day" if len(dp) >= 3 else "month" if len(dp) >= 2 else "year"), dp
    return None, None


def main():
    ap = argparse.ArgumentParser(description="归一月度书目里 issued（出版日期）的占位月日")
    ap.add_argument("--notes-dir", default="output/scholar_notes")
    ap.add_argument("--apply", action="store_true", help="真正写盘（默认 dry-run）")
    ap.add_argument("--i-know-this-is-a-one-shot-migration", dest="confirmed",
                    action="store_true", help="--apply 必须同时带此开关（防重跑误截新数据）")
    ap.add_argument("--no-network", action="store_true", help="跳过 Phase 1，全走启发式")
    ap.add_argument("--mailto", default="lrzlr2014@gmail.com", help="Crossref polite pool 邮箱")
    ap.add_argument("--sleep", type=float, default=0.15, help="Crossref 请求间隔秒")
    ap.add_argument("--backup-dir", default="", help="写盘前备份到此目录（强烈建议）")
    args = ap.parse_args()

    if args.apply and not args.confirmed:
        print("❌ --apply 必须同时带 --i-know-this-is-a-one-shot-migration\n"
              "   这是一次性存量迁移；本轮之后新论文带真实 date_precision，重跑会误截"
              "「确实是某月 1 日出版」的条目。")
        return 2

    nd = repo_path(args.notes_dir)
    files = sorted(nd.glob("*.references.json"))
    if not files:
        print("❌ 未找到月度书目：{}".format(nd))
        return 1

    if args.apply and args.backup_dir:
        bd = Path(args.backup_dir).expanduser()
        bd.mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.copy2(f, bd / f.name)
        print("📦 已备份 {} 个月度书目 → {}".format(len(files), bd))

    # 先扫一遍：哪些条目需要处理、哪些有 DOI 可回查
    todo = []            # (file, item, dp)
    for f in files:
        try:
            items = json.loads(f.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("  ⚠️ 跳过坏文件 {}: {}".format(f.name, exc))
            continue
        for it in items:
            dp = _dp_of(it)
            if dp and len(dp) >= 3 and dp[2] == 1:      # 日=1 才可疑（含 1/1）
                todo.append((f, it, dp))
    jan1 = [t for t in todo if t[2][1] == 1]
    day1 = [t for t in todo if t[2][1] != 1]
    with_doi = [t for t in jan1 if (t[1].get("DOI") or "").strip()]
    print("待处理 {} 条：其中 [[Y,1,1]] {} 条（{} 条有 DOI 可回查）、[[Y,M,1]] {} 条"
          .format(len(todo), len(jan1), len(with_doi), len(day1)))

    # Phase 1：有 DOI 的 jan1 回查 Crossref
    resolved = {}        # id(item) -> (precision, dp)
    stats = Counter()
    if with_doi and not args.no_network:
        print("\nPhase 1 · 回查 Crossref（{} 条）…".format(len(with_doi)))
        ua = {"User-Agent": "scholar-notes-normalize/1.0 (mailto:{})".format(args.mailto)}
        with httpx.Client(headers=ua, follow_redirects=True) as c:
            for i, (f, it, dp) in enumerate(with_doi, 1):
                prec, real = crossref_precision(it.get("DOI"), c, args.mailto)
                if prec:
                    resolved[id(it)] = (prec, real)
                    stats["crossref_" + prec] += 1
                else:
                    stats["crossref_miss"] += 1
                if i % 50 == 0:
                    print("   {}/{} …".format(i, len(with_doi)), flush=True)
                time.sleep(args.sleep)
        print("   命中：day {} / month {} / year {}；未命中 {}".format(
            stats["crossref_day"], stats["crossref_month"],
            stats["crossref_year"], stats["crossref_miss"]))

    # 落定每条的新 date-parts
    changes = Counter()
    per_file = {}
    for f, it, dp in todo:
        if id(it) in resolved:
            prec, real = resolved[id(it)]
            new_dp = (real[:1] if prec == "year" else
                      real[:2] if prec == "month" else real[:3])
            src = "crossref"
        else:
            from datetime import date as _d
            try:
                prec = infer_date_precision(_d(dp[0], dp[1], dp[2]))
            except Exception:
                continue
            new_dp = dp[:1] if prec == "year" else dp[:2] if prec == "month" else dp[:3]
            src = "heuristic"
        if new_dp == dp:
            changes["unchanged"] += 1
            continue
        changes["{}_{}".format(src, prec)] += 1
        per_file.setdefault(f, []).append((it, new_dp, prec))

    print("\n将变更 {} 条：".format(sum(v for k, v in changes.items() if k != "unchanged")))
    for k, v in sorted(changes.items()):
        print("   {:<20} {}".format(k, v))

    if not args.apply:
        print("\n（dry-run，未写盘。确认后加 --apply --i-know-this-is-a-one-shot-migration）")
        return 0

    # 写盘：月度书目
    for f, rows in per_file.items():
        items = json.loads(f.read_text(encoding="utf-8"))
        by_id = {it.get("id"): it for it in items}
        for it, new_dp, _prec in rows:
            tgt = by_id.get(it.get("id"))
            if tgt is not None:
                tgt["issued"] = {"date-parts": [new_dp]}
        tmp = f.with_suffix(f.suffix + ".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(f)
    print("\n✅ 已更新 {} 个月度书目".format(len(per_file)))

    # 同步 manual bundle：否则下次 regen 会按 bundle 里的旧 date 重算，冲掉归一成果
    touched = _sync_bundles(nd, per_file)
    print("✅ 已同步 {} 个 manual bundle 的 date_precision".format(touched))
    print("\n收尾请跑：PYTHONPATH=. python3 scripts/notes_index.py --full  （重建 all_references）")
    return 0


def _sync_bundles(nd: Path, per_file) -> int:
    """把归一后的精度写回 manual bundle 的 segment.metadata（按 citekey→DOI/标题 匹配）。"""
    idx_path = nd / INDEX_JSON
    if not idx_path.exists():
        return 0
    idx = json.loads(idx_path.read_text(encoding="utf-8"))
    # citekey → (month, doi, title)
    info = {p.get("citekey"): p for p in idx.get("papers", []) if p.get("series") == "manual"}
    want = {}
    for _f, rows in per_file.items():
        for it, new_dp, prec in rows:
            if it.get("id") in info:
                want[it["id"]] = (prec, new_dp)
    if not want:
        return 0

    n = 0
    for ck, (prec, new_dp) in want.items():
        p = info[ck]
        mdir = nd / "manual" / (p.get("month") or "")
        if not mdir.is_dir():
            continue
        for bf in mdir.glob("*.paper.json"):
            try:
                data = json.loads(bf.read_text(encoding="utf-8"))
            except Exception:
                continue
            meta = ((data.get("segment") or {}).get("metadata")) or {}
            same_doi = (meta.get("doi") or "").lower() == (p.get("doi") or "").lower()
            same_title = (meta.get("title") or "") == (p.get("title") or "")
            if not (same_doi and meta.get("doi")) and not same_title:
                continue
            meta["date_precision"] = prec
            if len(new_dp) >= 3:           # Crossref 给了确切日期，顺手校正 date 本身
                meta["publication_date"] = "{:04d}-{:02d}-{:02d}".format(*new_dp[:3])
            data["segment"]["metadata"] = meta
            tmp = bf.with_suffix(bf.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(bf)
            n += 1
            break
    return n


if __name__ == "__main__":
    sys.exit(main())
