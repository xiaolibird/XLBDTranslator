#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按 bundle 的 pdf_path 反查 PMLR 官方页，统一回填 journal / volume / pages / url。

背景：PMLR 会议论文普遍没有 DOI，ingest 只能走 pdf-llm 从首页解析元数据，结果
journal 字段散成十几种写法（"Proceedings of Machine Learning Research"、"ML4H 2025"、
空值…），volume/pages 全缺。后果是按 venue 检索会漏、出参考文献时卷期页不全。

本脚本用**权威源**修正：PMLR 每篇 abs 页的 `<div id="info">` 与 BibTeX 块含
"PMLR <vol>:<start>-<end>, <year>" 与 proceedings 全称。

用法：
    PYTHONPATH=. python3 scripts/backfill_pmlr_metadata.py --month 2026-08 --dry-run
    PYTHONPATH=. python3 scripts/backfill_pmlr_metadata.py --month 2026-08 --apply
回填后需重跑 finalize 让改动传播到札记与索引：
    PYTHONPATH=. python3 scripts/read_pdf.py finalize <该月任一 bundle>

只动 segment.metadata 的 journal/volume/pages/url（以及 year 缺失时补），
不碰 title/authors/doi/close_reading_* —— 标题作者已由 agent 亲读核对过，比页面更可信。
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx

# 批次目录名 → PMLR 卷号。新增批次在此登记即可。
FOLDER_TO_VOLUME = {
    "ML4H2025": "297",
    "CHIL2026": "333",
    "CHIL2025": "287",
    "MLHC2025": "298",
}

INFO_RE = re.compile(r"PMLR\s+(\d+)\s*:\s*([0-9]+\s*[-–]\s*[0-9]+)?", re.I)
BOOKTITLE_RE = re.compile(r"booktitle\s*=\s*\{([^}]*)\}", re.I)
PAGES_RE = re.compile(r"pages\s*=\s*\{\s*([0-9]+\s*[-–]+\s*[0-9]+)\s*\}", re.I)
YEAR_RE = re.compile(r"year\s*=\s*\{?\s*(\d{4})", re.I)


def volume_candidates(pdf_path: str):
    """pdf_path 的**所有**目录段上命中的已登记卷号集合。

    早先只看 `pdf_path.split('/')[-2]`（紧邻文件的那一层）：PDF 按作者/主题再分一层
    子目录就认不出，该篇被静默计入 skipped，连文件名都不打印。而且是子串匹配 + 无序
    dict 遍历——目录名同时含两个已登记关键词（`CHIL2025_CHIL2026`）时，命中哪个取决于
    FOLDER_TO_VOLUME 的书写顺序，结果无声地任选一个卷号。改为全路径扫描 + 返回集合，
    由调用方对「0 个」与「多于 1 个」分别报错。
    """
    parts = Path(pdf_path).parts[:-1]        # 只看目录段，文件名不参与
    return {vol for folder, vol in FOLDER_TO_VOLUME.items()
            if any(folder in p for p in parts)}


def volume_of(pdf_path: str):
    """唯一命中时返回卷号；认不出或同时命中多个（无法判定）返回 None。"""
    vols = volume_candidates(pdf_path)
    return vols.pop() if len(vols) == 1 else None


def fetch_pmlr(vol: str, pid: str, client: httpx.Client):
    """取 PMLR abs 页，解析 booktitle/volume/pages/year/url。失败返回 None。"""
    url = "https://proceedings.mlr.press/v{}/{}.html".format(vol, pid)
    try:
        r = client.get(url, timeout=30.0, follow_redirects=True)
        if r.status_code != 200:
            return None, "HTTP {}".format(r.status_code)
    except Exception as exc:  # 网络抖动不该中断整批
        return None, type(exc).__name__
    html = r.text

    out = {"url": url, "volume": vol}
    m = PAGES_RE.search(html)
    if m:
        out["pages"] = re.sub(r"\s*[-–]+\s*", "-", m.group(1))
    else:                       # 退回 info 行的 "PMLR 287:337-368"
        m2 = INFO_RE.search(html)
        if m2 and m2.group(2):
            out["pages"] = re.sub(r"\s*[-–]\s*", "-", m2.group(2))
    m = BOOKTITLE_RE.search(html)
    if m:
        out["journal"] = re.sub(r"\s+", " ", m.group(1)).strip()
    m = YEAR_RE.search(html)
    if m:
        out["year"] = m.group(1)
    return out, None


def main():
    ap = argparse.ArgumentParser(description="按 pdf_path 反查 PMLR 回填卷期页")
    ap.add_argument("--notes-dir", default="output/scholar_notes")
    ap.add_argument("--month", required=True, help="bundle 所在月份目录，如 2026-08")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="只打印将要写入的内容")
    g.add_argument("--apply", action="store_true", help="真正写盘")
    ap.add_argument("--sleep", type=float, default=0.4, help="请求间隔秒（对 PMLR 礼貌）")
    args = ap.parse_args()

    bundle_dir = Path(args.notes_dir) / "manual" / args.month
    bundles = sorted(bundle_dir.glob("*.paper.json"))
    if not bundles:
        print("未找到 bundle：{}".format(bundle_dir))
        return 1

    changed = failed = 0
    skipped = []                # (bundle 名, 原因) —— 逐条列出，不再只汇总成一个计数
    rows = []
    with httpx.Client(headers={"User-Agent": "scholar-notes-backfill/1.0"}) as client:
        for bp in bundles:
            d = json.loads(bp.read_text(encoding="utf-8"))
            pdf_path = d.get("pdf_path") or ""
            vols = volume_candidates(pdf_path)
            if len(vols) != 1:
                skipped.append((bp.name, (
                    "路径同时命中多个已登记批次 {}，无法判定卷号".format(sorted(vols))
                    if vols else "路径里没有已登记的批次目录（非 PMLR 或需在 "
                                 "FOLDER_TO_VOLUME 登记）")))
                continue
            vol = vols.pop()
            pid = Path(pdf_path).stem          # ji25a.pdf → ji25a
            info, err = fetch_pmlr(vol, pid, client)
            time.sleep(args.sleep)
            if not info:
                failed += 1
                rows.append((pid, "v" + vol, "!! 取页失败: {}".format(err), ""))
                continue

            meta = ((d.get("segment") or {}).get("metadata")) or {}
            before = (meta.get("journal"), meta.get("volume"), meta.get("pages"))
            meta["journal"] = info.get("journal") or meta.get("journal")
            meta["volume"] = info.get("volume")
            if info.get("pages"):
                meta["pages"] = info["pages"]
            meta["url"] = meta.get("url") or info.get("url")
            # publication_date 缺失时用卷年份补一个 1 月 1 日占位（仅当原本为空）
            if not meta.get("publication_date") and info.get("year"):
                meta["publication_date"] = "{}-01-01".format(info["year"])
            after = (meta.get("journal"), meta.get("volume"), meta.get("pages"))
            rows.append((pid, "v" + vol,
                         "{} | {} | {}".format(*[x or "-" for x in before]),
                         "{} | {} | {}".format(*[x or "-" for x in after])))
            if before != after:
                changed += 1
            if args.apply:
                d["segment"]["metadata"] = meta
                bp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")

    print("{:<18} {:<6} {:<52} {}".format("paper_id", "vol", "回填前 journal|vol|pages", "回填后"))
    for r in rows:
        print("{:<18} {:<6} {:<52} {}".format(r[0], r[1], r[2][:50], r[3][:60]))
    print("\n命中 PMLR 的 {} 篇：变更 {}、取页失败 {}；跳过 {}"
          .format(len(rows), changed, failed, len(skipped)))
    if skipped:
        print("\n跳过明细（未回填，请核对是否漏了批次登记）：")
        for name, why in skipped:
            print("    {:<44} {}".format(name, why))
    if args.dry_run:
        print("（dry-run，未写盘。确认无误后加 --apply，然后重跑 read_pdf.py finalize 传播）")
    else:
        print("已写盘。请重跑：PYTHONPATH=. python3 scripts/read_pdf.py finalize "
              "output/scholar_notes/manual/{}/<任一bundle>.paper.json".format(args.month))
    return 0


if __name__ == "__main__":
    sys.exit(main())
