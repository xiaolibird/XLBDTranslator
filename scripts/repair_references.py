# -*- coding: utf-8 -*-
"""重建某个月度札记缺失的 `{stem}.references.json`（CSL-JSON 书目）。

用于修复「md 在、CSL 不在」的月份——通常是 backfill 中途被打断留下的。缺了它，
`build_all_references` 只能用 `_fallback_csl` 从索引字段兜底，产出的条目**没有卷、期、页**，
pandoc 渲染出的参考文献会缺信息。

    PYTHONPATH=. python scripts/repair_references.py --month 2026-06 --dry-run
    PYTHONPATH=. python scripts/repair_references.py --month 2026-06

元数据优先级：Crossref（按 DOI 直查，给卷期页）→ arXiv（按 id，给权威作者）→ 索引字段兜底。
arXiv 预印本本就没有卷期页，走兜底是正确结果而非退化。

**默认拒绝覆盖已存在的 CSL**（--force 才覆盖）：那些文件是 write_notes 在有 PaperSegment
上下文时产出的，信息比这里能重建的更全，不该被这个工具盖掉。
退出码 0=成功 / 1=该月无条目或全部失败 / 2=参数或索引问题。
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.paths import repo_path                            # noqa: E402
from src.scholar.notes_index import INDEX_JSON, _fallback_csl   # noqa: E402
from src.utils.logger import get_logger                         # noqa: E402

logger = get_logger("repair_refs")

# arXiv 序号是 4 位（2015 前）或 5 位。末尾的 (?![\d.]) 是必需的边界保护——否则畸形
# DOI（如 6 位序号）会被**截断**成一个看似合法的 id，拿去查 arXiv 只会静默取回错的论文。
ARXIV_IN_DOI = re.compile(r"^10\.48550/arxiv\.(\d{4}\.\d{4,5})(?![\d.])", re.I)


def _csl_from_crossref(item, entry):
    """Crossref 归一化 dict → CSL 条目（沿用月度 CSL 的字段口径）。"""
    out = {"id": entry["citekey"], "type": "article-journal",
           "title": item.get("title") or entry.get("title") or ""}
    if item.get("authors"):
        au = []
        for n in item["authors"]:
            parts = (n or "").split()
            if len(parts) == 1:
                au.append({"family": parts[0]})
            elif parts:
                au.append({"family": parts[-1], "given": " ".join(parts[:-1])})
        if au:
            out["author"] = au
    for src, dst in (("journal", "container-title"), ("doi", "DOI"), ("url", "URL"),
                     ("volume", "volume"), ("issue", "issue"), ("pages", "page")):
        if item.get(src):
            out[dst] = item[src]
    d = item.get("publication_date")
    if d is not None:
        # 与 notes.build_csl_item 同一口径：issued（出版日期）按真实精度截断，
        # 不把各来源补出来的占位月日当成确切出版日
        from src.scholar._citekey_utils import date_parts
        out["issued"] = {"date-parts": date_parts(d, item.get("date_precision"))}
    elif entry.get("year"):
        out["issued"] = {"date-parts": [[entry["year"]]]}
    return out



def _crossref_or_unreachable(doi, email, client):
    """(命中|None, 源是否答上)。**不能直接用 crossref_by_doi**：它把 404 / 非 200 / 超时 /
    传输异常一律吞成 None（crossref.py:171），与「源里确实没这篇」同形，于是一次网络抖动
    写下的索引兜底，事后再也分辨不出该不该补。这里自己看状态码。"""
    from src.scholar.crossref import CROSSREF_API, parse_crossref_work
    from src.scholar.fulltext import polite_get
    try:
        r = polite_get(client, "{}/{}".format(CROSSREF_API, doi),
                       params={"mailto": email} if email else {})
    except Exception:                                      # noqa: BLE001
        return None, False
    if r.status_code == 404:
        return None, True                                  # 源答了：确实没有
    if r.status_code != 200:
        return None, False
    try:
        item = (r.json() or {}).get("message")
    except Exception:                                      # noqa: BLE001
        return None, False
    if not item:
        return None, False      # 200 却没有 message：没答上，别和 404 混为一谈
    return parse_crossref_work(item), True


# 预印本仓库的 DOI 前缀：这些在 Crossref 里**命中良好，但本来就没有卷、没有页**，
# 拿「缺卷缺页」当「索引兜底」判据会把它们全收进来，每跑一次重查一次、永远修不成。
# 真库实测：无卷无页且带 DOI 的 375 条里，arXiv 206 + Research Square 43 + bioRxiv/medRxiv 38
# + SSRN 9 = 296 条（79%）属于此类（2026-09-05 对抗审核 MAJOR-1）。
_PREPRINT_DOI_PREFIXES = (
    "10.48550",   # arXiv
    "10.21203",   # Research Square
    "10.1101",    # bioRxiv / medRxiv
    "10.2139",    # SSRN
    "10.36227",   # TechRxiv
    "10.31234", "10.31235",   # PsyArXiv / SocArXiv
)


def _is_index_fallback(it):
    """疑似「索引兜底」形态：期刊条目、既没卷也没页、且不是预印本。

    ⚠️ 这是**启发式**不是判据——真正的索引兜底没有留下任何标记，只能按形状猜。
    排除预印本后仍可能有 online-first / e-locator 型的正刊条目落进来；它们重查一次
    发现源里也没有就会计进 `still_none`，不会被改坏（只填空缺、不覆盖已有值），
    但会占用一次网络请求。宁可多问一次，也不能漏掉真正被抖动写坏的那些。
    """
    if it.get("volume") or it.get("page"):
        return False
    if str(it.get("type") or "") not in ("", "article-journal"):
        return False                      # posted-content / chapter 等本就不该有卷页
    doi = str(it.get("DOI") or "").lower()
    return not doi.startswith(_PREPRINT_DOI_PREFIXES)


def _repair_fallbacks(out_path, args):
    """只对已存在文件里的索引兜底条目重查 Crossref，命中就地升级；**只升不降**。

    解掉这个双向死锁：一次 Crossref 抖动 → 该月落成索引兜底书目 → 下次跑同一命令在**联网之前**
    就撞上「已存在，拒绝覆盖」直接退 2 → 唯一逃生门 --force 又被台账明文禁止（那是整月重建、
    索引兜底会覆盖既有更权威字段）。2026-09-05 对抗审计 Lens C。
    """
    import json as _json
    from src.scholar.fulltext import ipv4_client
    items = _json.loads(out_path.read_text(encoding="utf-8"))
    targets = [i for i, it in enumerate(items)
               if _is_index_fallback(it) and it.get("DOI")
               and not str(it["DOI"]).lower().startswith("10.48550/arxiv")]
    print("索引兜底条目 {} / {} 条可重查".format(len(targets), len(items)))
    if not targets:
        print("没有需要修复的条目。")
        return 0
    fixed = still_none = unreachable = 0
    with ipv4_client(timeout=30) as cli:
        for i in targets:
            got, ok = _crossref_or_unreachable(str(items[i]["DOI"]), args.email, cli)
            if not ok:
                unreachable += 1
                continue
            if not got:
                still_none += 1
                continue
            # `_csl_from_crossref(item, entry)` 的 entry 是**索引行**（取 entry["citekey"]），
            # 而这里手上是**CSL 条目**（身份键叫 id，全库 2507 条没有一条带 citekey）。
            # 直接传进去必 KeyError——第一版就是这么写的，一命中真数据就崩，
            # 而当时的测试恰好把这个函数桩掉了（2026-09-05 对抗审核 BLOCKER 1/2）。
            shim = {"citekey": items[i].get("id"), "title": items[i].get("title")}
            new = _csl_from_crossref(got, shim)
            # **只填空缺，不覆盖已有值**。「新值非空就写」挡得住空值，挡不住**精度降级**：
            # issued 恒为非空 dict，Crossref 若只给年份，会把盘上的 [[2024,5,1]] 覆写成 [[2024]]；
            # author 同理会被空格切分的名整体替换（对抗审核 M1，真库 137 条在射程内）。
            merged = dict(items[i])
            for k, v in new.items():
                if v in (None, "", [], {}):
                    continue
                if merged.get(k) in (None, "", [], {}):        # 只补空缺
                    merged[k] = v
            if merged != items[i]:
                items[i] = merged
                fixed += 1
    print("升级 {} 条 · 源里确实没有 {} 条 · 源没答上 {} 条".format(fixed, still_none, unreachable))
    if unreachable:
        print("⚠️ {} 条这次没问成（不是源里没有），网络恢复后可再跑一次本命令。".format(unreachable))
    if args.dry_run:
        print("【dry-run】未写盘。")
        return 0
    if fixed:
        # 备份名带时间戳：固定名会让「第一趟修一半、网络恢复后按提示再跑一趟」把原件盖掉——
        # 保险恰好在它唯一该起作用的场景（多趟修复要回滚）失效（对抗审核 MAJOR-2）。
        bak = out_path.with_name("{}.{}.bak".format(out_path.name,
                                                   datetime.now().strftime("%Y%m%dT%H%M%S")))
        bak.write_bytes(out_path.read_bytes())
        out_path.write_text(_json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        print("已写 {}（原件备份 {}）\n   下一步：PYTHONPATH=. python scripts/notes_index.py"
              .format(out_path, bak.name))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="重建月度札记缺失的 CSL-JSON 书目")
    ap.add_argument("--month", required=True, help="月份标签，如 2026-06 或 2026-07-27")
    ap.add_argument("--series", default="auto", choices=["auto", "manual"])
    ap.add_argument("--notes-dir", default="output/scholar_notes")
    ap.add_argument("--email", default="", help="Crossref 礼貌池邮箱（建议填）")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的 CSL（默认拒绝）")
    ap.add_argument("--repair-fallbacks", action="store_true",
                    help="定向修复：只对已存在文件里**索引兜底形态**（缺卷且缺页）的条目重查 "
                         "Crossref，命中就地升级、只升不降；其余条目一个字节不动。"
                         "用来解掉「一次网络抖动写下兜底书目、之后被拒绝覆盖闸挡住再也修不回来」"
                         "那个双向死锁——不必动 --force（那是整月重建，会覆盖既有更权威字段）。")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    nd = repo_path(args.notes_dir)   # 相对路径锚死仓库根，别随 cwd 漂
    ip = nd / INDEX_JSON
    if not ip.exists():
        print("找不到索引：{}".format(ip), file=sys.stderr)
        return 2
    try:
        idx = json.loads(ip.read_text(encoding="utf-8"))
        assert isinstance(idx.get("papers"), list)
    except Exception as exc:
        print("索引损坏（{}）：{}".format(type(exc).__name__, ip), file=sys.stderr)
        return 2

    rows = [e for e in idx["papers"]
            if e.get("month") == args.month and e.get("series", "auto") == args.series]
    if not rows:
        print("索引中没有 month={} series={} 的条目".format(args.month, args.series),
              file=sys.stderr)
        return 1

    suffix = "全文精读" if args.series == "auto" else "手动精读"
    stem = "科研札记_{}_{}".format(args.month, suffix)
    out_path = nd / "{}.references.json".format(stem)
    if args.repair_fallbacks:
        if not out_path.exists():
            print("--repair-fallbacks 需要已存在的书目文件，但它不在：{}\n"
                  "（要新建整月书目请去掉该参数）".format(out_path), file=sys.stderr)
            return 2
        if args.force:
            print("⚠️ --repair-fallbacks 与 --force 同时给出：按定向修复执行，--force 被忽略。",
                  file=sys.stderr)
        return _repair_fallbacks(out_path, args)
    if out_path.exists() and not args.force:
        print("已存在且更权威，拒绝覆盖：{}\n"
              "（只想补回当初因网络抖动落成索引兜底的那几条：加 --repair-fallbacks，只升不降；"
              "确需整月重建才加 --force）".format(out_path), file=sys.stderr)
        return 2
    if not (nd / "{}.md".format(stem)).exists():
        print("对应札记不存在：{}.md".format(stem), file=sys.stderr)
        return 2

    from src.scholar.crossref import crossref_by_doi
    from src.scholar.academic_search import fetch_arxiv_by_id
    from src.scholar.fulltext import ipv4_client

    items, n_cr, n_ax, n_fb, n_unreachable = [], 0, 0, 0, 0
    with ipv4_client(timeout=30) as cli:
        for e in rows:
            got = None
            if e.get("doi") and not e["doi"].lower().startswith("10.48550/arxiv"):
                # crossref_by_doi 把 404/非200/超时一律吞成 None，与「源里确实没这篇」同形。
                # 这里必须分开：**没答上**而落成索引兜底的条目，是可以也应该改天补回的
                #（2026-09-05 对抗审计 Lens C：一次抖动写下的兜底，之后被拒绝覆盖闸挡住，
                # 而唯一的逃生门 --force 又被台账明文禁止——修复通道被双向堵死）。
                got, ok = _crossref_or_unreachable(e["doi"], args.email, cli)
                if not ok:
                    n_unreachable += 1
            if got:
                items.append(_csl_from_crossref(got, e))
                n_cr += 1
                continue
            # 索引里 arxiv_id 常为空，但 arXiv 的 DOI 形如 10.48550/arXiv.2606.11570，
            # id 就嵌在里面——不提取就永远走不到这一支。
            aid = e.get("arxiv_id")
            if not aid:
                m = ARXIV_IN_DOI.match(e.get("doi") or "")
                aid = m.group(1) if m else None
            if aid:
                try:
                    ax = fetch_arxiv_by_id(aid, cli)
                except Exception as exc:      # arXiv API 限流/超时是常态，退回索引即可
                    logger.warning("  ⚠️ arXiv 查询失败（{}）: {}".format(aid, type(exc).__name__))
                    ax = None
                if ax and ax.get("authors"):
                    merged = dict(e)
                    merged["authors"] = ax["authors"]       # 用 arXiv 的权威作者覆盖
                    items.append(_fallback_csl(merged))
                    n_ax += 1
                    continue
            items.append(_fallback_csl(e))
            n_fb += 1

    logger.info("  {} 条：Crossref {} · arXiv {} · 索引兜底 {}{}".format(
        len(items), n_cr, n_ax, n_fb,
        "（其中 {} 条是**源没答上**，不是源里没有）".format(n_unreachable) if n_unreachable else ""))
    if n_unreachable:
        logger.warning("  ⚠️ {} 条因 Crossref 没答上而落成索引兜底（缺卷/期/页）。"
                       "网络恢复后用 `--repair-fallbacks` 定向补回，别用 --force。".format(n_unreachable))
    rich = sum(1 for it in items if it.get("volume") or it.get("page"))
    if args.dry_run:
        print("\n【dry-run】将写 {}（{} 条，其中 {} 条带卷/页）".format(
            out_path, len(items), rich))
        return 0
    out_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n{}\n✅ {}（{} 条，其中 {} 条带卷/页）\n   下一步：PYTHONPATH=. python scripts/notes_index.py".format(
        "=" * 66, out_path, len(items), rich))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)
