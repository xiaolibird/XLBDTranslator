# -*- coding: utf-8 -*-
"""审库内文献的发表源质量：四分类 + 分级判据，只读不动库。

起因：用户问「除了 arXiv，库里是不是还混进了很多野鸡期刊/会议」，并提议「断层之下
全部删出库」。调研后有三处与最初设想不符，本脚本按修正后的口径实现：

**① 没有「IF 断层」可用。** OpenAlex 不给会议 proceedings 算 `2yr_mean_citedness`，
一律返 0.0 —— 按 IF 断层删，第一批被杀的是 KDD（0.0）和 WWW（0.0）。真正的断层是
**类型断层**：期刊/会议/预印本三者的指标根本不可比，必须先分类再判质量。

**② 刊名不携带质量信息。** 先试过正则（`International Journal of...` 之类），命中
28 个里绝大多数是正经甚至顶级刊——`International Journal of Computer Vision` 是 CV
顶刊 IJCV、`American Journal of Kidney Diseases` 是肾病顶刊。按名字杀会误伤一片。

**③ h-index 被发文量稀释。** `International Journal of Computer Applications`（公认
水刊）h=113 并不低，因为它发了 28094 篇；IJCV h=291 但只发 3994 篇。

## 主判据：单篇被引数按年龄归一，而不是刊级 IF

实证（Crossref）：KDD 2022 = 26 引/年、npj DM 2024 = 31、IOS Press 2021 = 2、
Hindawi 2021 = **0.8**、LNCS 2021 = **0.2**。它**跨类型可比**，正好绕开「会议无 IF」
这个死穴。

**硬约束**：库里 ≥2024 的新论文占 56%，天然没有引用。被引判据只对
`CITED_MIN_AGE` 年以上的论文生效，新论文一律不判——报告里显示为「太新，不判」。

## 数据源

- **Crossref**（`api.crossref.org/works/{doi}`）：免费无配额。`type` 字段直接给出
  `journal-article` / `proceedings-article` / `posted-content` / `book-chapter` 四分类，
  外加被引数、年份、ISSN。这是本脚本的主力。
- **OpenAlex**：2026-08 起改预算制（$0.001/请求、每日 UTC 午夜重置），一次全量 652 源
  就能打光当日免费额度。仅作可选的刊级 IF 补充，`RateLimited` 会在配额耗尽时中止。

## 用法

    PYTHONPATH=. python scripts/audit_venue_quality.py            # 全量（Crossref）
    PYTHONPATH=. python scripts/audit_venue_quality.py --offline  # 只用缓存，不发请求
    PYTHONPATH=. python scripts/audit_venue_quality.py --json out.json --md report.md

**本脚本只读**：不改 literature_index.json、不改任何 md、不碰向量库。唯一写入的是
两个缓存文件与 `--json`/`--md` 指定的产出。
"""
import argparse
import collections
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from src.scholar.settings import load_scholar_settings  # noqa: E402
from src.scholar.embed_store import INDEX_NAME          # noqa: E402

CACHE_NAME = "venue_quality_cache.json"        # OpenAlex 刊级（按刊名）
CROSSREF_CACHE = "crossref_works_cache.json"   # Crossref 篇级（按 DOI）
OA_API = "https://api.openalex.org/sources"
CR_API = "https://api.crossref.org/works/"
MAILTO = "liming1390@gmail.com"                # polite pool，仅用于标识调用方
UA = "scholar-venue-audit/1.0 (mailto:{})".format(MAILTO)
NAME_MATCH_MIN = 0.6

THIS_YEAR = 2026
CITED_MIN_AGE = 3          # 满几年才用被引判据（更新的论文天然没引用）
CITED_SUSPECT = 1.0        # 引/年 低于此值记可疑（实证 Hindawi 0.8、LNCS 0.2）
CITED_LOW = 3.0            # 引/年 低于此值记偏低（IOS Press 2.0 落在这一档）

# 公认掠夺性 / 论文工厂出版商，按 **DOI 前缀** 判——不能按 Crossref 的 publisher 字段：
# Hindawi 被 Wiley 收购后 publisher 显示为 "Wiley"，按它判会漏掉全部 Hindawi 刊。
# 只收录学界共识明确的；MDPI(10.3390)/Frontiers(10.3389) 是**争议**不是掠夺，
# 刻意不列——Frontiers in Medicine IF 2.46，一刀切会误伤。
VENUE_LISTS = Path(__file__).resolve().parents[1] / "config" / "venue_lists.yaml"


def load_lists() -> dict:
    """读 config/venue_lists.yaml。缺文件就退回内置的最小黑名单——名单是判据的真相源，
    但不能因为它没了就把全库判成 gray（那会让「有没有野鸡」这个问题变成无答案）。"""
    try:
        import yaml
        d = yaml.safe_load(VENUE_LISTS.read_text(encoding="utf-8")) or {}
    except Exception as e:                               # noqa: BLE001
        print("⚠️ 读不到 {}（{}），退回内置最小黑名单".format(VENUE_LISTS, e), file=sys.stderr)
        return {"black": {"doi_prefix": _FALLBACK_BLACK}, "white": {}}
    d.setdefault("black", {}).setdefault("doi_prefix", {})
    d.setdefault("white", {})
    return d


def match_venue(entry: dict, cr: dict, lists: dict) -> tuple:
    """→ ("black"|"white"|"gray", 理由)。优先级 issn > doi_prefix > name_regex。

    ISSN 最准（不受刊名变体/大小写/缩写影响），来自 Crossref。会议大多没 ISSN，
    只能靠刊名正则。三者都不中就是 gray —— **gray 是常态不是问题**：库内 727 个
    venue 里 484 个只出现 1 次，白名单不可能列全。
    """
    doi = (entry.get("doi") or "").lower()
    issns = set((cr or {}).get("issns") or ([cr["issn"]] if (cr or {}).get("issn") else []))
    name = ((cr or {}).get("container") or entry.get("journal") or "").strip()
    black, white = lists.get("black") or {}, lists.get("white") or {}

    for k, why in (black.get("issn") or {}).items():
        if k in issns:
            return "black", why
    for pref, why in (black.get("doi_prefix") or {}).items():
        if doi.startswith(pref):
            return "black", why
    for nm, why in (black.get("name_exact") or {}).items():
        if name.lower() == nm.lower():
            return "black", why

    for k, why in (white.get("issn") or {}).items():
        if k in issns:
            return "white", why
    for pat in (white.get("name_regex") or []):
        if name and re.search(pat, name, re.I):
            return "white", "会议/刊名白名单命中：{}".format(pat[:40])
    return "gray", ""


_FALLBACK_BLACK = {
    "10.1155": "Hindawi（论文工厂丑闻，2023 起多刊被 Clarivate 除名）",
    "10.5120": "IJCA / Foundation of Computer Science（公认水刊，IF 0.42/发文 28094）",
    "10.4236": "SCIRP（公认掠夺性）",
    "10.4172": "OMICS International（公认掠夺性）",
    "10.19026": "Science Publishing Group（公认掠夺性）",
}
PREDATORY_PREFIX = _FALLBACK_BLACK      # 兼容旧引用

PREPRINT_HOSTS = ("arxiv", "medrxiv", "biorxiv", "researchsquare", "openreview",
                  "ssrn", "hal.science", "preprints.org", "osf.io")
PREPRINT_PREFIX = ("10.48550", "10.1101", "10.21203", "10.2139")
CONF_PREFIX = ("10.1145", "10.18653", "10.1609", "10.24963")   # ACM/ACL/AAAI/IJCAI
CONF_HOSTS = ("proceedings.mlr.press", "aclanthology.org", "openreview.net")
_CONF_RE = re.compile(r"proceedings|conference|symposium|workshop|annual meeting|congress",
                      re.I)
_SERIES_RE = re.compile(r"lecture notes|studies in health technology|"
                        r"communications in computer|ifip advances|smart innovation", re.I)

_WORD = re.compile(r"[a-z]{3,}")
_STOP = {"the", "and", "for", "journal", "international", "proceedings", "conference",
         "annual", "transactions", "letters", "review", "reviews"}


def _toks(s: str) -> set:
    return {w for w in _WORD.findall((s or "").lower()) if w not in _STOP}


def keepers(index: dict):
    out = []
    for e in index.get("papers") or []:
        ck = e.get("citekey")
        if not isinstance(e, dict) or not ck or e.get("duplicate_of"):
            continue
        if ck.startswith("MISSING-KEY-") or e.get("citekey_source") == "missing":
            continue
        out.append(e)
    return out


def is_preprint(entry: dict) -> bool:
    u = (entry.get("url") or "").lower()
    if any(h in u for h in PREPRINT_HOSTS):
        return True
    if entry.get("arxiv_id"):
        return True
    return (entry.get("doi") or "").lower().startswith(PREPRINT_PREFIX)


class RateLimited(RuntimeError):
    """OpenAlex 配额耗尽。**必须中止而不是继续**——继续只会把几百个假「查不到」写进缓存，
    下次复跑还当真。2026-08 起 OpenAlex 改成预算制（$0.001/请求、每日 UTC 午夜重置），
    一次全量 652 个源就能把当日免费额度打光。"""


# ---------------- Crossref（篇级，主力） ----------------

def crossref_lookup(client: httpx.Client, doi: str) -> dict:
    """按 DOI 查一篇。返回 {} = 查不到；返回 {"_http": code} = 网络/服务端失败。

    **查不到与请求失败必须分开**：前者是事实（如 arXiv 的 10.48550 DOI 就不在 Crossref），
    后者是噪音。混在一起会让「查不到」这个数字随网络抖动漂移，报告就不可复现了。
    """
    try:
        r = client.get(CR_API + doi, timeout=25.0)
    except Exception:                                    # noqa: BLE001
        return {"_http": "exc"}
    if r.status_code == 404:
        return {}
    if r.status_code != 200:
        return {"_http": r.status_code}
    try:
        m = r.json()["message"]
    except Exception:                                    # noqa: BLE001
        return {"_http": "badjson"}
    yr = None
    for k in ("issued", "published-print", "published-online", "created"):
        parts = (m.get(k) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            yr = parts[0][0]
            break
    ct = m.get("container-title") or []
    return {
        "type": m.get("type") or "",
        "cited": m.get("is-referenced-by-count") or 0,
        "year": yr,
        "publisher": m.get("publisher") or "",
        # **存全部 ISSN**：期刊通常有 print + online 两个（JAMIA 是
        # ['1067-5027','1527-974X']），只存第一个会让白名单按另一个写的条目全部漏配
        # ——实测 JAMIA 37 篇里 24 篇因此掉进 gray。
        "issns": list(m.get("ISSN") or []),
        "issn": (m.get("ISSN") or [""])[0],   # 兼容旧字段
        "container": ct[0] if ct else "",
        "volume": m.get("volume") or "",
        "issue": m.get("issue") or "",
    }


# ---------------- OpenAlex（刊级，可选补充） ----------------

def query_source(client: httpx.Client, name: str) -> dict:
    """查一个发表源的刊级指标。返回 {} 表示查不到或名字对不上。

    **防模糊匹配误判**：`display_name.search` 是模糊搜索，查冷门刊很可能返回一个名字
    相近的无关刊。故对返回名与查询名做词面重合校验，低于阈值一律当查不到——宁可说
    「不知道」，也不给一篇论文扣一顶查错了的帽子。
    """
    try:
        r = client.get(OA_API, params={"filter": "display_name.search:" + name,
                                       "per-page": 3, "mailto": MAILTO})
        if r.status_code == 429:
            raise RateLimited(r.text[:200])
        if r.status_code != 200:
            return {}
        res = r.json().get("results") or []
    except RateLimited:
        raise
    except Exception:                                    # noqa: BLE001
        return {}
    want = _toks(name)
    if not want:
        return {}
    best, best_score = None, 0.0
    for s in res:
        score = len(want & _toks(s.get("display_name") or "")) / len(want)
        if score > best_score:
            best, best_score = s, score
    if not best or best_score < NAME_MATCH_MIN:
        return {}
    st = best.get("summary_stats") or {}
    return {"matched": best.get("display_name"), "match_score": round(best_score, 2),
            "if2y": round(st.get("2yr_mean_citedness") or 0.0, 2),
            "h": st.get("h_index"), "works": best.get("works_count"),
            "doaj": bool(best.get("is_in_doaj")), "type": best.get("type"),
            "publisher": best.get("host_organization_name") or ""}


# ---------------- 四分类 ----------------

def classify_kind(entry: dict, cr: dict) -> str:
    """preprint / conference / journal / series / other。

    优先级：预印本 > Crossref type（权威）> 刊名与 DOI 前缀的规则兜底。
    **必须有第四、五档**：LNCS/IOS Press 这类丛书在期刊与会议之间二义，而 229 篇
    无 DOI 无刊名的杂项（学位论文 / ResearchGate PDF / Scholar 主页）哪一档都不属于，
    硬塞进三分类只会让报告失真。
    """
    if is_preprint(entry):
        return "preprint"
    t = (cr or {}).get("type") or ""
    if t == "posted-content":
        return "preprint"
    if t == "proceedings-article":
        return "conference"
    j = (entry.get("journal") or "").strip()
    if t == "book-chapter" or _SERIES_RE.search(j):
        return "series"
    if t == "journal-article":
        return "journal"
    # Crossref 没覆盖时的规则兜底
    u = (entry.get("url") or "").lower()
    doi = (entry.get("doi") or "").lower()
    if _CONF_RE.search(j) or doi.startswith(CONF_PREFIX) or any(h in u for h in CONF_HOSTS):
        return "conference"
    if j:
        return "journal"
    return "other"


def grade(entry: dict, cr: dict, kind: str, lists: dict = None) -> tuple:
    """返回 (档位, 理由)。档位 ∈ predatory/whitelisted/suspect/low/ok/too-new/unknown。

    判定顺序即可信度顺序：**人工名单 > 预印本性质 > 篇级被引**。

    名单为什么排最前：被引数测的是「这篇论文影响力」，不是「这个 venue 是不是野鸡」。
    实测把 3 篇真 AAAI/NeurIPS 冷门论文判成了可疑（eiben2021Parameterized 是理论 CS，
    5 年 2 引）。所以白名单命中就直接收工，不让被引判据再去踩一脚。
    """
    lists = lists or {}
    tag, why = match_venue(entry, cr, lists)
    if tag == "black":
        return "predatory", why
    if tag == "white":
        return "whitelisted", "白名单：{}".format(why)
    if kind == "preprint":
        return "preprint", "预印本，未经同行评审（不判质量）"
    yr = (cr or {}).get("year") or entry.get("year")
    try:
        yr = int(yr)
    except (TypeError, ValueError):
        yr = None
    if not cr or cr.get("_http") or "cited" not in cr:
        return "unknown", "Crossref 无数据"
    if yr is None:
        return "unknown", "无年份，无法按年龄归一"
    age = THIS_YEAR - yr
    if age < CITED_MIN_AGE:
        return "too-new", "{} 年发表，不足 {} 年，被引判据不适用".format(yr, CITED_MIN_AGE)
    per = cr["cited"] / max(1, age)
    if per < CITED_SUSPECT:
        return "suspect", "{} 引 / {} 年 = {:.1f} 引每年".format(cr["cited"], age, per)
    if per < CITED_LOW:
        return "low", "{} 引 / {} 年 = {:.1f} 引每年".format(cr["cited"], age, per)
    return "ok", "{} 引 / {} 年 = {:.1f} 引每年".format(cr["cited"], age, per)


# ---------------- 主流程 ----------------

GRADE_LABEL = {
    "predatory": "🔴 黑名单命中（公认掠夺性，确定）",
    "whitelisted": "✅ 白名单命中（领域公认可信）",
    "suspect": "🟠 被引极低（< {:.0f} 引/年）".format(CITED_SUSPECT),
    "low": "🟡 被引偏低（< {:.0f} 引/年）".format(CITED_LOW),
    "ok": "🟢 正常",
    "preprint": "⚪ 预印本（未经同行评审，不判质量）",
    "too-new": "🆕 太新，被引判据不适用",
    "unknown": "❓ 无数据",
}
KIND_LABEL = {"journal": "期刊", "conference": "会议", "preprint": "预印本",
              "series": "丛书/会议集", "other": "其他（学位论文等）"}


def main() -> int:
    ap = argparse.ArgumentParser(description="审库内发表源质量（只读）")
    ap.add_argument("--json", dest="as_json", default="", help="明细写到该 JSON")
    ap.add_argument("--md", dest="as_md", default="", help="报告写到该 markdown")
    ap.add_argument("--offline", action="store_true", help="只用缓存，不发任何请求")
    ap.add_argument("--openalex", action="store_true",
                    help="额外补刊级 IF（配额有限，默认关）")
    args = ap.parse_args()

    s = load_scholar_settings(patch_gemini=False)
    nd = Path(s.processing.notes_dir)
    idx = json.loads((nd / INDEX_NAME).read_text(encoding="utf-8"))
    ps = keepers(idx)
    lists = load_lists()

    cr_path = nd / CROSSREF_CACHE
    cr_cache = {}
    if cr_path.exists():
        try:
            cr_cache = json.loads(cr_path.read_text(encoding="utf-8"))
        except Exception:                                # noqa: BLE001
            cr_cache = {}

    todo = [e for e in ps
            if (e.get("doi") or "").strip() and (e["doi"].strip().lower() not in cr_cache)]
    stat = collections.Counter()
    if todo and not args.offline:
        print("查 Crossref：{} 篇（缓存已有 {}）…".format(len(todo), len(cr_cache)), flush=True)
        with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True) as c:
            for i, e in enumerate(todo, 1):
                doi = e["doi"].strip().lower()
                info = crossref_lookup(c, doi)
                if info.get("_http"):
                    stat["http_fail"] += 1               # 不写缓存，留待重跑
                else:
                    cr_cache[doi] = info
                    stat["hit" if info else "miss"] += 1
                if i % 100 == 0:
                    print("   {}/{}（命中 {} 查无 {} 失败 {}）".format(
                        i, len(todo), stat["hit"], stat["miss"], stat["http_fail"]), flush=True)
                    cr_path.write_text(json.dumps(cr_cache, ensure_ascii=False), encoding="utf-8")
                time.sleep(0.06)
        cr_path.write_text(json.dumps(cr_cache, ensure_ascii=False), encoding="utf-8")
        print("   完成：命中 {} / 查无 {} / 请求失败 {}\n".format(
            stat["hit"], stat["miss"], stat["http_fail"]), flush=True)

    oa_cache = {}
    oap = nd / CACHE_NAME
    if oap.exists():
        try:
            oa_cache = json.loads(oap.read_text(encoding="utf-8"))
        except Exception:                                # noqa: BLE001
            oa_cache = {}

    rows = []
    for e in ps:
        doi = (e.get("doi") or "").strip().lower()
        cr = cr_cache.get(doi) or {}
        kind = classify_kind(e, cr)
        vtag, _vw = match_venue(e, cr, lists)
        g, why = grade(e, cr, kind, lists)
        rows.append({"citekey": e.get("citekey"), "kind": kind, "grade": g, "why": why,
                     "venue_list": vtag,
                     "journal": (e.get("journal") or cr.get("container") or "").strip(),
                     "decision": e.get("decision"), "role": e.get("role"),
                     "year": cr.get("year") or e.get("year"), "cited": cr.get("cited"),
                     "doi": doi, "n_hl": len(e.get("highlights") or []),
                     "title": (e.get("title") or "")[:90]})

    n = len(rows)
    by_kind = collections.Counter(r["kind"] for r in rows)
    by_grade = collections.Counter(r["grade"] for r in rows)

    out = []
    out.append("库内 keeper {} 篇\n".format(n))
    out.append("## 类型分布")
    for k, c in by_kind.most_common():
        out.append("  {:<16} {:>5} 篇 ({:.1%})".format(KIND_LABEL.get(k, k), c, c / n))
    out.append("\n## 质量分档")
    for g in ("predatory", "whitelisted", "suspect", "low", "ok", "preprint",
              "too-new", "unknown"):
        c = by_grade.get(g, 0)
        if c:
            out.append("  {:<34} {:>5} 篇 ({:.1%})".format(GRADE_LABEL[g], c, c / n))

    out.append("\n## 名单三态（gray 是常态：727 个 venue 里 484 个只出现 1 次）")
    by_list = collections.Counter(r["venue_list"] for r in rows)
    for t in ("white", "black", "gray"):
        c = by_list.get(t, 0)
        out.append("  {:<8} {:>5} 篇 ({:.1%})".format(t, c, c / n))
    gray_ven = collections.Counter(
        r["journal"] for r in rows
        if r["venue_list"] == "gray" and r["kind"] not in ("preprint", "other") and r["journal"])
    out.append("\n  灰态 venue {} 个；其中 ≥3 篇的 {} 个（值得联网核实的量级）：".format(
        len(gray_ven), sum(1 for v in gray_ven.values() if v >= 3)))
    for k, v in gray_ven.most_common(25):
        if v >= 3:
            out.append("    {:>3} 篇  {}".format(v, k[:72]))

    for g in ("predatory", "suspect"):
        sub = [r for r in rows if r["grade"] == g]
        if not sub:
            continue
        out.append("\n## {} —— 逐篇（{} 篇）".format(GRADE_LABEL[g], len(sub)))
        for r in sorted(sub, key=lambda r: (r["kind"], -(r["n_hl"] or 0))):
            out.append("  [{}] {:<26} {} | {} | {} 条证据".format(
                KIND_LABEL.get(r["kind"], r["kind"]), r["citekey"], r["decision"],
                r["role"] or "-", r["n_hl"]))
            out.append("       {}".format(r["title"]))
            out.append("       {} | {}".format(r["journal"][:58] or "(无刊名)", r["why"]))

    # 硬验收：顶会必须落在 conference 档且不被判 predatory/suspect
    out.append("\n## 误判自检（顶会/顶刊必须不在 🔴🟠 里）")
    probes = ["SIGKDD", "Web Conference", "AAAI", "Neural Information", "CVPR",
              "Machine Learning for Health", "npj Digital Medicine"]
    for p in probes:
        hit = [r for r in rows if p.lower() in (r["journal"] or "").lower()]
        if not hit:
            out.append("  {:<28} 库内无匹配".format(p))
            continue
        bad = [r for r in hit if r["grade"] in ("predatory", "suspect")]
        out.append("  {:<28} {} 篇，其中被判 🔴🟠 的 {} {}".format(
            p, len(hit), len(bad), "❌ " + ",".join(r["citekey"] for r in bad[:3]) if bad else "✅"))

    text = "\n".join(out)
    print(text)
    if args.as_md:
        Path(args.as_md).write_text(text + "\n", encoding="utf-8")
        print("\n报告已写入 {}".format(args.as_md))
    if args.as_json:
        Path(args.as_json).write_text(
            json.dumps({"rows": rows, "by_kind": dict(by_kind), "by_grade": dict(by_grade),
                        "fetch": dict(stat)}, ensure_ascii=False, indent=1), encoding="utf-8")
        print("明细已写入 {}".format(args.as_json))
    return 0


if __name__ == "__main__":
    sys.exit(main())
