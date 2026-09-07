#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无 DOI / arXiv / PMID 的 keeper：联网**只读**盘点能不能补上标识（不写盘、不改索引）。

docs/bugs/2026-09-04-missing-identifier-backfill.md 定的两步，**顺序不能反**：
  1. 先盘点——对每篇拿标题查 Crossref 与 OpenAlex，产出三份名单：
       fillable        两源之一命中且标题相似度、年份、首作者三项对得上，DOI 可补
       ambiguous       有命中但年份或首作者对不上，要人看
       no_hit          两源都没有像样的命中
     再叠一层「建议放弃」：PMLR / OpenReview 键的篇目本就有出版方稳定标识，补 DOI 对身份没帮助；
     MAYBE 裁决且相似度不够高的，不值得为它动身份键。
  2. 命中的再考虑回填——**那是改身份键**（DOI 进 dedup_key 会连带 citekey / 向量库 chunk id /
     all_references），必须走「改身份键要扫派生物」的流程，不在本脚本里做。

用法：
    PYTHONPATH=. python3 scripts/inventory_missing_ids.py --email you@x.org            # 全量（约 317 篇 × 2 源）
    PYTHONPATH=. python3 scripts/inventory_missing_ids.py --email you@x.org --limit 20 # 先试 20 篇
    PYTHONPATH=. python3 scripts/inventory_missing_ids.py --dry-run                    # 不联网，只列候选
    PYTHONPATH=. python3 scripts/inventory_missing_ids.py --email you@x.org \
        --resume-from output/scholar_notes/_inventory/missing_ids_<上次>.json            # 隔天续跑没答上的那些

⚠️ OpenAlex 自 2026-09 起按日额度计费（每请求 $0.001，免费约 $0.1/日、UTC 零点重置），
额度用完一律 429 `Insufficient budget`——放慢没用。全库一天跑不完属常态，用 --resume-from 隔天续。

产出（默认 output/scholar_notes/_inventory/missing_ids_<时间戳>.json / .md）：
JSON 每条一记录（含两源原始命中），Markdown 是给人看的汇总与 fillable 清单。
礼貌：两源各带 mailto，路与路之间 --delay 秒；403/429 走 fulltext.polite_get 的退避与按宿主熔断。
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.paths import repo_path                                           # noqa: E402
from src.scholar.notes_index import iter_keepers                                    # noqa: E402
from src.scholar.crossref import (CROSSREF_API, best_match, title_similarity,       # noqa: E402
                                  _norm_title)
from src.scholar.fulltext import ipv4_client, polite_get                            # noqa: E402
from src.utils.logger import get_logger                                             # noqa: E402

logger = get_logger(__name__)

OPENALEX_API = "https://api.openalex.org/works"
SIM_FILLABLE = 0.92          # 标题相似度：三项齐才算 fillable
SIM_STRONG = 0.97            # 相似度极高时允许首作者缺失/对不上（元数据首作者本就常错）
SIM_CANDIDATE = 0.80         # 低于这个连 ambiguous 都不算，视为没命中
YEAR_TOLERANCE = 1           # 预印本 vs 正刊差一年是常态
MIN_TITLE_WORDS = 4          # 实词太少的标题对谁都像，不查（同 fulltext._ARXIV_TITLE_MIN_WORDS）
ZOTERO_TS = "http://127.0.0.1:1969"      # 本机 docker 里的 Zotero translation-server
TS_SOURCE = "zotero_ts"
ALL_SOURCES = {"crossref", "openalex", TS_SOURCE}

# **直链**来源（url → 出版商页 → DOI）的相似度门槛，比标题检索低。
# 依据不是想象，是实测：2026-09-06 拿 31 条真候选跑 translation-server，29 条拿到 DOI，
# 相似度 min 0.75 / p25 0.80 / 中位 1.00，**0.70 以下一条都没有**；低分那几条逐条人工核过，
# 全是同一篇（差异来自标题长度与分词，不是指错论文）。
# 为什么可以低：标题检索是「拿标题去猜哪篇」，相似度承担**识别**职责；直链是「顺着本条目
# 自己的 url 走到出版商页」，相似度只承担**确认**职责——两者要求本就不同。
# 年份与首作者两道闸照旧生效（实测里 martin2019Predictingr 库年份 2019 而 DOI 是 2025 刊，
# 正是靠年份闸判进 ambiguous 交人看的）。
SIM_DIRECT_LINK = 0.70
DIRECT_LINK_SOURCES = {TS_SOURCE}


def _answered(rec: Dict[str, Any]) -> set:
    """这条记录里哪些源**确实答上了**。新报告直接读 sources_ok；旧报告退化成「出过命中即算答过」。"""
    if rec.get("sources_ok") is not None:
        return set(rec["sources_ok"])
    return {h.get("source") for h in (rec.get("hits") or []) if h.get("source")}


# ---------------- 纯函数：可单测 ----------------

def title_words(t: str) -> List[str]:
    return re.findall(r"[a-z]{4,}", (t or "").lower())


def family_name(author: str) -> str:
    """'Xiaoli Bird' / 'Bird, Xiaoli' / 'X. Bird' → 'bird'。"""
    a = (author or "").strip()
    if not a:
        return ""
    if "," in a:
        return _norm_title(a.split(",")[0])
    return _norm_title(a.split()[-1])


def first_author_matches(entry_authors: List[str], hit_authors: List[str]) -> Optional[bool]:
    """None = 任一侧没有作者信息（不算对上也不算对不上）。"""
    if not entry_authors or not hit_authors:
        return None
    return family_name(entry_authors[0]) == family_name(hit_authors[0]) and bool(family_name(entry_authors[0]))


def year_ok(entry_year, hit_year) -> Optional[bool]:
    try:
        a, b = int(entry_year), int(hit_year)
    except (TypeError, ValueError):
        return None
    return abs(a - b) <= YEAR_TOLERANCE


def classify(entry: Dict[str, Any], hits: List[Dict[str, Any]],
             failed_sources: Optional[List[str]] = None) -> Tuple[str, Optional[Dict[str, Any]], str]:
    """把两源的命中归成 fillable / ambiguous / no_hit / incomplete，并给出建议放弃的理由（可为空串）。

    hits 每条：{"source", "title", "doi", "year", "authors", "sim"}。返回 (类别, 最佳命中|None, 建议放弃理由)。
    `failed_sources` 是**没答上**的源（额度耗尽 / 限流 / 传输异常）。没有像样命中且有源没答上时
    判 `incomplete` 而不是 `no_hit`——「问过了确实没有」和「压根没问成」不是一回事，
    后者记成结论会让这批篇目被永久判死（同本批 R3-A1 的教训）。
    """
    dk = str(entry.get("dedup_key") or "")
    is_maybe = str(entry.get("decision") or "").upper() == "MAYBE"
    drop = ""
    if dk.startswith(("pmlr:", "openreview:")):
        drop = "出版方已有稳定标识（{}），补 DOI 对身份没帮助".format(dk.split(":")[0])
    if len(title_words(entry.get("title") or "")) < MIN_TITLE_WORDS:
        return "ambiguous", None, (drop or "标题实词不足 {} 个，检索结果不可信".format(MIN_TITLE_WORDS))

    def _floor(h):
        """候选门槛按来源分档：直链只需确认，标题检索要承担识别。"""
        return SIM_DIRECT_LINK if h.get("source") in DIRECT_LINK_SOURCES else SIM_CANDIDATE

    def _ident(h):
        """这条命中给出了可用身份标识没有——DOI 或（经直链确认的）PMID/PMC 都算。"""
        return h.get("doi") or h.get("pmid") or h.get("pmc")

    good = [h for h in hits if _ident(h) and (h.get("sim") or 0) >= _floor(h)]
    if not good:
        if failed_sources:
            return "incomplete", None, (drop or "")
        return "no_hit", None, (drop or ("裁决为 MAYBE，不值得为它动身份键" if is_maybe else ""))
    good.sort(key=lambda h: -(h.get("sim") or 0))
    # 两源各命中一个像样的候选、DOI 却不同（预印本 vs 正刊、勘误、同名不同文）——这是「要人看」
    # 的最强信号，不能按相似度取第一个就当 fillable（第 1 轮审计 R1-3.4）
    dois = {_norm_doi(h.get("doi")) for h in good if h.get("doi")}
    if len(dois) > 1:
        best = good[0]
        note = "两源 DOI 不一致：{}".format(" vs ".join(sorted(dois)))
        return "ambiguous", best, (drop + "；" if drop else "") + note
    best = good[0]
    sim = best.get("sim") or 0
    # MAYBE 裁决**且相似度不够高**才建议放弃：标题逐字命中的 MAYBE 篇目值得留给人判断（R1-3.6）
    if is_maybe and sim < SIM_STRONG and not drop:
        drop = "裁决为 MAYBE 且相似度 {:.2f} < {:.2f}，不值得为它动身份键".format(sim, SIM_STRONG)
    yo = year_ok(entry.get("year"), best.get("year"))
    fa = first_author_matches(entry.get("authors") or [], best.get("authors") or [])
    fill_at = SIM_DIRECT_LINK if best.get("source") in DIRECT_LINK_SOURCES else SIM_FILLABLE
    if sim >= fill_at and yo is not False and (
            best.get("source") in DIRECT_LINK_SOURCES        # 直链：来源即证据，不再要首作者
            or fa is True or (fa is None and sim >= SIM_STRONG) or sim >= 0.99):
        return "fillable", best, drop
    return "ambiguous", best, drop


def _norm_doi(d) -> str:
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", str(d or "").strip().lower())


# ---------------- 联网：两源 ----------------

_URL_ID_PATS = [
    ("pmid",  re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d{6,9})", re.I)),
    ("pmc",   re.compile(r"/pmc/articles/(PMC\d+)", re.I)),
    ("doi",   re.compile(r"doi\.org/(10\.\d{4,9}/[^?#\s]+)", re.I)),
    ("doi",   re.compile(r"/doi/(?:abs/|full/|pdf/|epdf/)?(10\.\d{4,9}/[^?#\s]+)", re.I)),
    ("arxiv", re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", re.I)),
]


def extract_url_identifier(url: str):
    """条目自己的 url 里是否直接写着标识。返回 (类型, 值) 或 None。

    这批条目的 url 是入库时记下的落地页，标识就嵌在里面——`pubmed.../35309007/`、
    `/pmc/articles/PMC8378618/`、`/doi/10.1145/xxx`。抽出来零网络、零额度、且是**直链**，
    比拿标题去模糊检索可信得多。真库实测 301 条待查里 41 条属此类。
    """
    u = (url or "").strip()
    for kind, pat in _URL_ID_PATS:
        m = pat.search(u)
        if m:
            return kind, m.group(1).rstrip(".,;")
    return None


def query_translation_server(payload: str, *, endpoint: str = "web", timeout: float = 45.0):
    """本机 Zotero translation-server：`/web` 收 url、`/search` 收标识。返回 (命中|None, 是否答上)。

    为什么用它：`/web` 顺着条目自己的 url 走到出版商页面把 DOI 抓回来，是**直链**而非模糊匹配；
    本地 docker、无外部日额度，不受 OpenAlex 按日计费的限制（2026-09-06 实测出版商域名 29/31 命中）。
    抓不动的（聚合站、反爬页）返回 (None, False) 落到下一个源，绝不当成「源里也没有」。
    """
    import urllib.error
    import urllib.request
    url = "{}/{}".format(ZOTERO_TS, endpoint)
    req = urllib.request.Request(url, data=(payload or "").encode("utf-8"),
                                 headers={"Content-Type": "text/plain"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
    except Exception:                                      # noqa: BLE001
        return None, False                                 # 服务没起 / 抓不动 / 解析不出：没答上
    it = body[0] if isinstance(body, list) and body else (body if isinstance(body, dict) else {})
    if not isinstance(it, dict) or not it.get("title"):
        return None, False
    doi = (it.get("DOI") or "").strip() or None
    year = None
    m = re.search(r"(\d{4})", str(it.get("date") or ""))
    if m:
        year = int(m.group(1))
    authors = []
    for c in (it.get("creators") or []):
        if (c or {}).get("creatorType") == "author":
            nm = (c.get("lastName") or "").strip() or (c.get("name") or "").strip()
            if nm:
                authors.append(nm)
    return {"source": TS_SOURCE, "title": it.get("title") or "", "doi": doi,
            "year": year, "authors": authors, "sim": 0.0}, True


def query_crossref(title: str, email: str, client) -> Tuple[Optional[Dict[str, Any]], bool]:
    """返回 (命中|None, 源是否答上)。**「源答了但没有」与「源根本没答上」必须分开**——
    后者记成 no_hit 就是把暂时性失败沉淀成确定性结论（同本批 R3-A1 那条教训）。

    这里**不能用** `crossref.crossref_lookup`：它把 HTTP 失败与「查了没有」一律吞成 None
    （crossref.py:137 的 `except → return None`），正是本函数要区分的那两态。故自己发请求、
    自己看状态码，命中解析仍复用 `best_match`（判据只留一份）。顺带让 Crossref 也走
    `polite_get` 的退避与按宿主熔断——此前四路里只有它是裸 get。
    """
    params = {"query.bibliographic": title, "rows": 3}
    if email:
        params["mailto"] = email
    try:
        r = polite_get(client, CROSSREF_API, params=params, timeout=30.0)
        if r.status_code != 200:
            return None, False
        items = (r.json().get("message") or {}).get("items") or []
    except Exception as e:                                        # noqa: BLE001
        logger.warning("  ⚠️ Crossref 检索失败（{}）: {}".format(title[:40], e))
        return None, False
    got = best_match(items, title)
    if not got:
        return None, True
    d = got.get("publication_date")
    year = getattr(d, "year", None) if d is not None else None
    return {"source": "crossref", "title": got.get("title") or "", "doi": got.get("doi"),
            "year": year, "authors": list(got.get("authors") or []),
            "sim": title_similarity(title, got.get("title") or "")}, True


def query_openalex(title: str, email: str, client) -> Tuple[Optional[Dict[str, Any]], bool]:
    """返回 (命中|None, 源是否答上)。

    2026-09-05 实测：OpenAlex 已改成**按日额度计费**（每请求 $0.001，免费额度约 $0.1/日、
    UTC 零点重置），额度用完一律 429 `Insufficient budget`。这不是限流、放慢没用，
    因此必须记成「没答上」而不是「源里没有」，否则一整批尾巴会被永久判成 no_hit。
    """
    # 记忆里踩过：无 DOI 的 PMLR 论文必须用 filter=title.search，别用 search=（那会全走 arXiv 侧）
    q = re.sub(r"[^\w\s\-]", " ", title)[:200]
    params = {"filter": "title.search:{}".format(q), "per-page": 3}
    if email:
        params["mailto"] = email
    try:
        r = polite_get(client, OPENALEX_API, params=params, timeout=30.0)
        if r.status_code != 200:
            return None, False
        results = (r.json() or {}).get("results") or []
    except Exception as e:                                        # noqa: BLE001
        logger.warning("  ⚠️ OpenAlex 检索失败（{}）: {}".format(title[:40], e))
        return None, False
    best = None
    for w in results:
        t = w.get("title") or w.get("display_name") or ""
        sim = title_similarity(title, t)
        if best is None or sim > best["sim"]:
            doi = (w.get("doi") or "").replace("https://doi.org/", "") or None
            authors = [((a.get("author") or {}).get("display_name") or "") for a in (w.get("authorships") or [])]
            best = {"source": "openalex", "title": t, "doi": doi, "year": w.get("publication_year"),
                    "authors": authors, "sim": sim}
    return best, True


def select_targets(index: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    # 撤稿条目不进清单：本库口径是「保留札记、踢出向量库与书目」，为它补 DOI 进身份键没有收益，
    # 却会进 fillable 名单诱导改键（第 1 轮审计 R1-3.7）
    for e in iter_keepers(index, include_retracted=False):
        dk = str(e.get("dedup_key") or "")
        # 索引里没有 pmid 字段（e.get("pmid") 恒 None），标识是否已有要看 dedup_key 的前缀
        if e.get("doi") or e.get("arxiv_id") or dk.startswith(("doi:", "arxiv:", "pmid:")):
            continue
        if str(e.get("entry_type") or "article") in ("book", "chapter") or dk.startswith("isbn:"):
            continue
        if not (e.get("title") or "").strip():
            continue
        out.append(e)
    return out


def render_md(records: List[Dict[str, Any]], stamp: str) -> str:
    from collections import Counter
    c = Counter(r["category"] for r in records)
    n_drop = sum(1 for r in records if r.get("drop_reason"))
    n_inc = c.get("incomplete", 0)
    lines = ["# 无标识 keeper 盘点（{}）".format(stamp), "",
             "只读盘点，未改任何文件。共 {} 篇：fillable {} / ambiguous {} / no_hit {} / incomplete {}；"
             "其中带「建议放弃」标注 {} 篇。"
             .format(len(records), c.get("fillable", 0), c.get("ambiguous", 0), c.get("no_hit", 0), n_inc, n_drop), ""]
    if n_inc:
        lines += ["> ⚠️ **incomplete {} 篇**：有检索源没答上（OpenAlex 按日额度用尽 / 限流 / 传输异常），".format(n_inc),
                  "> 这不是「源里没有」。补跑：`--resume-from <本报告.json>`（只重查没答上的那些源）。", ""]
    lines += [
             "下一步若要回填，**必须**当成改身份键处理（DOI 进 dedup_key → citekey / 向量库 chunk id / "
             "all_references 联动），走「改身份键要扫派生物」流程；不要用补元数据脚本顺手做。", "",
             "## fillable（两源之一命中，标题/年份/首作者对得上）", "",
             "| citekey | 裁决 | 命中源 | 相似度 | DOI | 年份(库/源) | 建议放弃 |", "|---|---|---|---|---|---|---|"]
    for r in records:
        if r["category"] != "fillable":
            continue
        b = r.get("best") or {}
        lines.append("| {} | {} | {} | {:.2f} | {} | {}/{} | {} |".format(
            r["citekey"], r.get("decision") or "", b.get("source"), b.get("sim") or 0,
            b.get("doi") or ("PMID:" + b["pmid"] if b.get("pmid") else "")
            or ("PMC:" + b["pmc"] if b.get("pmc") else ""),
            r.get("year") or "", b.get("year") or "", r.get("drop_reason") or ""))
    lines += ["", "## ambiguous（有命中但要人看）", "",
              "| citekey | 命中源 | 相似度 | DOI | 年份(库/源) | 首作者(库/源) | 备注 |", "|---|---|---|---|---|---|---|"]
    for r in records:
        if r["category"] != "ambiguous":
            continue
        b = r.get("best") or {}
        ea = (r.get("authors") or [""])[0]
        ha = (b.get("authors") or [""])[0] if b else ""
        lines.append("| {} | {} | {} | {} | {}/{} | {} / {} | {} |".format(
            r["citekey"], b.get("source") or "", ("{:.2f}".format(b["sim"]) if b else ""), b.get("doi") or "",
            r.get("year") or "", b.get("year") or "", ea[:24], ha[:24], r.get("drop_reason") or ""))
    lines += ["", "## no_hit（两源都答了、都没有像样的命中）", "",
              ", ".join(r["citekey"] for r in records if r["category"] == "no_hit") or "（无）", ""]
    inc = [r for r in records if r["category"] == "incomplete"]
    if inc:
        lines += ["", "## incomplete（有源没答上，结论不成立，须补跑）", "",
                  ", ".join("{}（缺 {}）".format(r["citekey"], "/".join(r.get("sources_failed") or [])) for r in inc), ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--notes-dir", default="output/scholar_notes")
    ap.add_argument("--email", default="", help="Crossref/OpenAlex 礼貌池邮箱（强烈建议填）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=1.0, help="两次网络调用之间的间隔（秒）")
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--dry-run", action="store_true", help="不联网，只列出候选")
    ap.add_argument("--resume-from", default="",
                    help="上一份报告的 .json：只重查其中 incomplete（有源没答上）的篇目，其余原样结转。"
                         "OpenAlex 免费额度按日重置（UTC 零点），全库跑不完时隔天用它续跑。"
                         "给 `latest` 则自动挑 _inventory/ 里最新那份（每日续跑用这个，"
                         "免得手抄时间戳抄错）。")
    args = ap.parse_args()

    nd = repo_path(args.notes_dir)
    ip = nd / "literature_index.json"
    if not ip.exists():
        print("索引不存在：{}".format(ip), file=sys.stderr)
        return 2
    index = json.loads(ip.read_text(encoding="utf-8"))
    targets = select_targets(index)
    if str(args.resume_from).strip().lower() == "latest":
        # 每日续跑：自己挑最新报告。手抄时间戳是纯粹的出错来源，抄错的后果是
        # 「拿旧报告当基线」——已答上的源被当没答过重问，白烧当天的额度。
        inv_dir = repo_path(args.out_dir) if args.out_dir else nd / "_inventory"
        found = sorted(inv_dir.glob("missing_ids_*.json"))
        if not found:
            print("--resume-from latest：{} 下还没有报告，请先跑一次全量。".format(inv_dir),
                  file=sys.stderr)
            return 2
        args.resume_from = str(found[-1])       # 文件名带 %Y%m%dT%H%M%S，字典序即时间序
        print("续跑基线（latest 自动选中）：{}".format(found[-1].name))
    prev_recs: Dict[str, Dict[str, Any]] = {}
    carried: List[Dict[str, Any]] = []
    ask_by_key: Dict[str, List[str]] = {}
    if args.resume_from:
        prev_path = Path(args.resume_from)
        if not prev_path.is_absolute():
            prev_path = repo_path(args.resume_from)
        prev = json.loads(prev_path.read_text(encoding="utf-8"))
        prev_recs.update({r.get("citekey"): r for r in (prev.get("records") or [])})
        # 只重查「还有源没答上」的；已成结论的原样结转，不再花额度。
        # 旧报告没有 sources_ok 字段时退化成「出过命中的源即算答过」——出过命中必然答过，
        # 这条推断只会把「答了但没命中」的源多问一次，不会把没问成的当成结论（安全方向）。
        # 实词不足的篇目是**故意不问**（问了也不可信），不是「没问成」——不能进重查集，
        # 否则每次续跑都把它们算进「待重查」、永远消不掉
        redo = {ck: sorted(ALL_SOURCES - _answered(r)) for ck, r in prev_recs.items()
                if (ALL_SOURCES - _answered(r))
                and len(title_words(r.get("title") or "")) >= MIN_TITLE_WORDS}
        carried = [r for ck, r in prev_recs.items() if ck not in redo]
        targets = [e for e in targets if e.get("citekey") in redo
                   or e.get("citekey") not in prev_recs]      # 上次之后新入库的也补上
        ask_by_key = redo
        print("续跑：上一份报告 {} 条，其中待重查 {} 条（缺 {}），原样结转 {} 条".format(
            len(prev_recs), len(redo),
            "/".join(sorted({x for v in redo.values() for x in v})) or "无", len(carried)))
    if args.limit > 0:
        targets = targets[:args.limit]
    print("无 DOI/arXiv/PMID 的 keeper：{} 篇{}".format(len(targets), "（--limit 生效）" if args.limit else ""))
    if args.dry_run:
        for e in targets[:50]:
            print("  {:<32} {} | {}".format(e.get("citekey"), e.get("year"), (e.get("title") or "")[:70]))
        if len(targets) > 50:
            print("  … 共 {} 篇".format(len(targets)))
        return 0

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    out_dir = repo_path(args.out_dir) if args.out_dir else nd / "_inventory"
    out_dir.mkdir(parents=True, exist_ok=True)
    records: List[Dict[str, Any]] = []
    with ipv4_client(timeout=30.0) as cli:
        for i, e in enumerate(targets, start=1):
            title = e.get("title") or ""
            hits = []
            # 标题实词不足的条目 classify 一律判 ambiguous、不采信命中——那就别白打两次网络
            #（生产 317 篇里有 16 篇这样；第 1 轮审计 R1-3.5）
            failed: List[str] = []
            # 续跑时只补问缺的那一源；旧命中原样带过来，别重复花额度
            ask = set(ask_by_key.get(e.get("citekey"), sorted(ALL_SOURCES)))
            prev_hits = (prev_recs.get(e.get("citekey"), {}) if args.resume_from else {}).get("hits") or []
            hits.extend(h for h in prev_hits if h.get("source") not in ask)
            # ---- 直链优先：本条目自己的 url 就指向那篇论文，不必拿标题去猜 ----
            # 顺序是有意的：先走零成本/精确的路，标题模糊检索只用来补它们够不到的。
            # 这也把 OpenAlex 的日额度留给真正只能靠标题的那些条目。
            if TS_SOURCE in ask:
                u = (e.get("url") or "").strip()
                if not u:
                    # 没有 url 就**没问过**，不能记成「答上了」——那会让日后补了 url 的条目
                    # 在续跑时被跳过。同一个病：没问成不许记成问过（本轮反复在修的那类）。
                    ask.discard(TS_SOURCE)
                got = ok = None
                ident = extract_url_identifier(u)
                if ident:                                  # ① url 里直接写着标识 → /search
                    got, ok = query_translation_server(ident[1], endpoint="search")
                if got is None and u:                      # ② 落地页 → /web
                    got, ok2 = query_translation_server(u, endpoint="web")
                    ok = ok or ok2
                if got:
                    got["sim"] = title_similarity(title, got.get("title") or "")
                    # url 里抽到的 PMID/PMC 一旦被 TS 用标题确认，它**本身就是可用的身份标识**
                    # （dedup_key 支持 pmid: 档）。不带进来的话，这类条目会被报成 no_hit——
                    # 「身份已经确认了却说没找到」，与本轮一直在修的是同一类记账错误。
                    if ident and ident[0] in ("pmid", "pmc") and not got.get("doi"):
                        got[ident[0]] = ident[1]
                    hits.append(got)
                elif u:
                    failed.append(TS_SOURCE)               # 抓不动 ≠ 源里没有
                if args.delay > 0 and u:
                    time.sleep(args.delay)
            if len(title_words(title)) >= MIN_TITLE_WORDS:
                if "crossref" in ask:
                    cr, cr_ok = query_crossref(title, args.email, cli)
                    if cr:
                        hits.append(cr)
                    if not cr_ok:
                        failed.append("crossref")
                    if args.delay > 0:
                        time.sleep(args.delay)
                if "openalex" in ask:
                    oa, oa_ok = query_openalex(title, args.email, cli)
                    if oa:
                        hits.append(oa)
                    if not oa_ok:
                        failed.append("openalex")
                    if args.delay > 0:
                        time.sleep(args.delay)
            else:
                ask = set()          # 实词不足：两源都没问，别记成「问过了」
            cat, best, drop = classify(e, hits, failed)
            records.append({"citekey": e.get("citekey"), "title": title, "year": e.get("year"),
                            "authors": list(e.get("authors") or [])[:3], "decision": e.get("decision"),
                            "dedup_key": e.get("dedup_key"), "month": e.get("month"),
                            "category": cat, "best": best, "hits": hits, "drop_reason": drop,
                            # 上一轮已答上的源要**结转**，否则下次续跑会把它们当没问过重问一遍——
                            # `--resume-from` 的全部意义就是「只补问没答上的那一源」
                            # （2026-09-07 实测：不结转会让 crossref 对全部 301 条重问）。
                            "sources_ok": sorted((_answered(prev_recs.get(e.get("citekey"), {}))
                                                  - set(ask)) | (ask - set(failed))),
                            "sources_failed": failed})
            if i % 25 == 0 or i == len(targets):
                from collections import Counter
                c = Counter(r["category"] for r in records)
                print("  [{}/{}] fillable {} / ambiguous {} / no_hit {} / incomplete {}".format(
                    i, len(targets), c.get("fillable", 0), c.get("ambiguous", 0),
                    c.get("no_hit", 0), c.get("incomplete", 0)), flush=True)

    records = carried + records
    jp = out_dir / "missing_ids_{}.json".format(stamp)
    mp = out_dir / "missing_ids_{}.md".format(stamp)
    jp.write_text(json.dumps({"generated_at": stamp, "n": len(records), "records": records},
                             ensure_ascii=False, indent=1), encoding="utf-8")
    mp.write_text(render_md(records, stamp), encoding="utf-8")
    print("已写：{}\n      {}".format(jp, mp))
    return 0


if __name__ == "__main__":
    sys.exit(main())
