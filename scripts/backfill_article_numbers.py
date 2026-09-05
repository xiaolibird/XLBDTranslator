#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 references.json 里缺 `page` 的期刊条目按 DOI 回查 Crossref，补 article-number（e-locator）。

背景（docs/bugs/2026-09-04-crossref-article-number-lost.md）：Crossref 对纯电子刊把定位号放在
`article-number` 而不是 `page`，此前 parse_crossref_work 只取 `page` → CSL 缺 page → pandoc 出稿
成 `Nucleic Acids Res. 2026;54(1).` 少了定位号。代码已修（page 空时取 article-number），本脚本处置存量。

与 `scripts/repair_references.py --force` 的区别：那个是**整月重建**（每条都回查、整份文件重写、
索引兜底会覆盖既有更权威的字段），爆炸半径大；本脚本**只碰缺 page 且带 DOI 的期刊条目**，只改
`page` 一个键，其余字节不动。默认 dry-run。

用法：
    PYTHONPATH=. python3 scripts/backfill_article_numbers.py --email you@x.org             # 只看
    PYTHONPATH=. python3 scripts/backfill_article_numbers.py --email you@x.org --apply     # 落盘
    PYTHONPATH=. python3 scripts/notes_index.py                                            # 再重建全局书目

--apply 时先把要改的每份 references.json 原样拷进 <notes_dir>/.refs_backfill_backup/<时间戳>/，
再 tmp+os.replace 原子写回。改完记得跑 notes_index.py，all_references.json 才会跟着变。
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.paths import repo_path                       # noqa: E402
from src.scholar.crossref import (CROSSREF_API, parse_crossref_work)  # noqa: E402
from src.scholar.fulltext import polite_get                    # noqa: E402
from src.scholar.fulltext import ipv4_client                   # noqa: E402
from src.utils.logger import get_logger                        # noqa: E402

logger = get_logger(__name__)

BACKUP_DIRNAME = ".refs_backfill_backup"


def is_candidate(item: Dict[str, Any]) -> bool:
    """缺 page、带非 arXiv DOI 的期刊条目。"""
    if not isinstance(item, dict) or item.get("type") != "article-journal":
        return False
    doi = str(item.get("DOI") or "").strip()
    if not doi or doi.lower().startswith("10.48550/arxiv"):
        return False
    return not str(item.get("page") or "").strip()


UNCHECKED = object()     # lookup 的哨兵：额度（--limit）用完、这条**没回查**——不是「源里也没有」
UNREACHABLE = object()   # lookup 的哨兵：问了但**源没答上**（429/5xx/超时）——同样不是「源里也没有」
#
# 为什么要第二个哨兵：`crossref_by_doi` 把 404、非 200、超时、传输异常**一律**吞成 None
# （crossref.py:171），与「Crossref 里确实没这篇」同形。于是 make_lookup 里那个 warn 钩子
# 在生产中根本够不到，HTTP 故障会被计进 n_none 并打印成「源里也没有定位号 N 条」——
# 这个数会被人抄进台账当成 Crossref 确认过的结论（2026-09-05 对抗审计 Lens C 实证：
# 台账里「其余 187 条源里本就没有」就是这么来的，不可信）。故本脚本自己发请求自己看状态码。


def make_lookup(fetch: Callable[[str], Any], budget: List[int],
                cache: Optional[Dict[str, Optional[str]]] = None,
                delay: float = 0.0,
                warn: Optional[Callable[[str, str], None]] = None) -> Callable[[str], Any]:
    """回查闭包工厂：把 --limit 额度约束下的 lookup 从 main() 抽出来，好直测。

    budget 是单元素列表（闭包内可变计数）。fetch(doi) 返回 Crossref 应答 dict，失败可抛异常。
    额度用完返回 UNCHECKED——这条**没回查**，不是「源里也没有」，两种口径不许混
    （第 1 轮审计 R1-3.3；第 2 轮 R2-3.13 发现 main() 里那个闭包没有任何测试）。
    """
    cache = {} if cache is None else cache

    def lookup(doi: str):
        if doi in cache:
            return cache[doi]
        if budget[0] <= 0:
            return UNCHECKED
        budget[0] -= 1
        try:
            got = fetch(doi)
        except Exception as e:                            # noqa: BLE001
            if warn is not None:
                warn(doi, type(e).__name__)
            if delay > 0:
                time.sleep(delay)
            return UNREACHABLE                            # 不写缓存：本轮后面可以再问
        if got is UNREACHABLE:                            # fetch 自己报「没答上」
            if warn is not None:
                warn(doi, "unreachable")
            if delay > 0:
                time.sleep(delay)
            return UNREACHABLE
        page = (got or {}).get("pages") or None
        cache[doi] = page
        if delay > 0:
            time.sleep(delay)
        return page

    return lookup


def plan_updates(items: List[Dict[str, Any]], lookup: Callable[[str], Any]
                 ) -> Tuple[List[Tuple[int, str]], int, int, int]:
    """纯函数：对候选逐条 lookup(doi) → 定位号。

    返回 ([(下标, 新 page)], 候选数, 源里也没有的条数, 未回查条数, 源没答上的条数)。
    lookup 返回 UNCHECKED 表示这条没回查（--limit 用完）——单独计数，不许混进「源里也没有」
    （第 1 轮审计 R1-3.3：先用 --limit 20 试跑的人会把「N 条源里也没有」当成 Crossref 确认过的结论）。
    """
    updates: List[Tuple[int, str]] = []
    n_cand = n_none = n_unchecked = n_unreachable = 0
    for i, it in enumerate(items):
        if not is_candidate(it):
            continue
        n_cand += 1
        page = lookup(str(it["DOI"]).strip())
        if page is UNCHECKED:
            n_unchecked += 1
        elif page is UNREACHABLE:
            n_unreachable += 1
        elif page:
            updates.append((i, page))
        else:
            n_none += 1
    return updates, n_cand, n_none, n_unchecked, n_unreachable


def apply_updates(items: List[Dict[str, Any]], updates: List[Tuple[int, str]]) -> None:
    for i, page in updates:
        items[i]["page"] = page


def _load(path: Path):
    """references.json 是 CSL-JSON 数组；容忍 {"references": [...]} 这种包裹形态，原样写回。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data, None
    if isinstance(data, dict):
        for k in ("references", "items"):
            if isinstance(data.get(k), list):
                return data[k], (data, k)
    raise ValueError("不认识的 references.json 形态：{}".format(path.name))


def _atomic_write(path: Path, payload) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--notes-dir", default="output/scholar_notes")
    ap.add_argument("--email", default="", help="Crossref 礼貌池邮箱（建议填）")
    ap.add_argument("--delay", type=float, default=1.0, help="两次 Crossref 调用之间的间隔（秒）")
    ap.add_argument("--limit", type=int, default=0, help="最多回查多少条候选（0 = 全部）")
    ap.add_argument("--month", action="append", default=[], help="只处理这些月份（可重复）")
    ap.add_argument("--apply", action="store_true", help="真写盘（默认只干跑）")
    args = ap.parse_args()

    nd = repo_path(args.notes_dir)
    files = sorted(nd.glob("科研札记_*.references.json"))
    if args.month:
        files = [f for f in files if any(("_{}_".format(m) in f.name or "_{}.".format(m) in f.name) for m in args.month)]
    if not files:
        print("没有可处理的 references.json", file=sys.stderr)
        return 1

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup_dir = nd / BACKUP_DIRNAME / stamp
    cache: Dict[str, Optional[str]] = {}
    budget = [args.limit if args.limit > 0 else 10 ** 9]
    tot_cand = tot_upd = tot_none = tot_files = tot_unchecked = tot_unreach = 0

    with ipv4_client(timeout=30.0) as cli:
        def _fetch(doi):
            """自己发请求自己看状态码——**不能用 crossref_by_doi**：它把 404 / 非 200 / 超时 /
            传输异常一律吞成 None（crossref.py:171），与「源里确实没这篇」同形，
            于是 HTTP 故障会被计进「源里也没有」（2026-09-05 对抗审计 Lens C）。
            命中解析仍复用 parse_crossref_work，判据只留一份。"""
            try:
                r = polite_get(cli, "{}/{}".format(CROSSREF_API, doi),
                               params={"mailto": args.email} if args.email else {})
            except Exception:                                  # noqa: BLE001
                return UNREACHABLE
            if r.status_code == 404:
                return None                                    # 源答了：确实没这篇
            if r.status_code != 200:
                return UNREACHABLE                             # 429/5xx：没答上
            try:
                item = (r.json() or {}).get("message")
            except Exception:                                  # noqa: BLE001
                return UNREACHABLE
            return parse_crossref_work(item) if item else None

        def _warn(doi, exc_name):
            logger.warning("  ⚠️ Crossref 回查失败（{}）: {}".format(doi, exc_name))

        lookup = make_lookup(_fetch, budget, cache, delay=args.delay, warn=_warn)

        for f in files:
            try:
                items, wrapper = _load(f)
            except Exception as e:                            # noqa: BLE001
                logger.warning("  ⚠️ 跳过（{}）：{}".format(e, f.name))
                continue
            if not any(is_candidate(it) for it in items):
                continue
            updates, n_cand, n_none, n_unchecked, n_unreach = plan_updates(items, lookup)
            tot_cand += n_cand
            tot_none += n_none
            tot_unchecked += n_unchecked
            tot_unreach += n_unreach
            print("{:<48} 候选 {:>3} → 可补 {:>3}（源里也没有 {}{}{}）".format(
                f.name, n_cand, len(updates), n_none,
                "，未回查 {}".format(n_unchecked) if n_unchecked else "",
                "，源没答上 {}".format(n_unreach) if n_unreach else ""))
            for i, page in updates[:6]:
                print("      {} → page={}".format(items[i].get("id"), page))
            if updates and args.apply:
                backup_dir.mkdir(parents=True, exist_ok=True)
                (backup_dir / f.name).write_bytes(f.read_bytes())
                apply_updates(items, updates)
                _atomic_write(f, wrapper[0] if wrapper else items)
                tot_files += 1
            tot_upd += len(updates)
            if budget[0] <= 0:
                print("  （--limit 用完，后续文件未回查）")
                break

    print("\n合计：候选 {} 条，可补 {} 条，源里也没有定位号 {} 条{}{}".format(
        tot_cand, tot_upd, tot_none,
        "，未回查 {} 条（--limit 用完，不是源里没有）".format(tot_unchecked) if tot_unchecked else "",
        "，源没答上 {} 条（429/5xx/超时，**不是**源里没有，改天重跑）".format(tot_unreach)
        if tot_unreach else ""))
    if args.apply:
        print("已写 {} 份文件；原件备份在 {}\n下一步：PYTHONPATH=. python3 scripts/notes_index.py  （重建 all_references.json）"
              .format(tot_files, backup_dir))
    else:
        print("dry-run：未写盘。确认后加 --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
