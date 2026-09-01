# -*- coding: utf-8 -*-
"""定位本地 PDF：给 citekey / DOI / arXiv ID / 标题，直接给出本地文件路径。

三级回退（2026-07-28 实测验证，见 memory zotero-local-pdf-locate）：
  1. Better BibTeX JSON-RPC（http://localhost:23119）→ Zotero storage 里的附件绝对路径
  2. 札记索引 literature_index.json → 把 citekey/DOI 翻译成标题/arXiv 号，喂给第 3 级
  3. Spotlight mdfind（文件名+全文内容）兜底扫 ~/Downloads 与 ~/Zotero/storage

用法（stdout 只打最佳路径，方便管道；来源与候选走 stderr）：
  python scripts/locate_pdf.py little1988Test
  python scripts/locate_pdf.py 10.1002/9781119482260
  python scripts/locate_pdf.py 2101.09986
  python scripts/locate_pdf.py "Multi-view Integration Learning clinical time series"
  python scripts/locate_pdf.py <query> --all     # 列出全部候选
  python scripts/locate_pdf.py <query> --json    # 结构化输出

依赖：仅标准库。Zotero 未运行时自动跳过第 1 级并提示。
"""
import argparse
import difflib
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

BBT_RPC = "http://localhost:23119/better-bibtex/json-rpc"
INDEX_JSON = Path(__file__).resolve().parents[1] / "output" / "scholar_notes" / "literature_index.json"
# 2026-09-01 起按日期命名的论文精读目录（YYYY-MM[-DD][-批次]）统一挪到 ~/Desktop/Lab/Reading/，
# ~/Downloads 只剩 wanna-read/文献整理 等非精读归档；两处都搜，Zotero 附件目录兜底。
SEARCH_DIRS = [Path.home() / "Desktop" / "Lab" / "Reading", Path.home() / "Downloads",
               Path.home() / "Zotero" / "storage"]

CONF_THRESHOLD = 0.45  # Spotlight 兜底低于此分只算低置信候选，不当结果输出

RE_DOI = re.compile(r"\b10\.\d{4,9}/\S+", re.I)
RE_ARXIV = re.compile(r"\b(\d{4}\.\d{4,5})(v\d+)?\b")
RE_CITEKEY = re.compile(r"^[a-z][a-z-]*\d{4}[A-Za-z][\w-]*$")


def classify(query):
    q = query.strip()
    m = RE_DOI.search(q)
    if m:
        return "doi", m.group(0).rstrip(".,;")
    m = RE_ARXIV.fullmatch(q) or (RE_ARXIV.search(q) if "arxiv" in q.lower() else None)
    if m:
        return "arxiv", m.group(1)
    if RE_CITEKEY.match(q) and " " not in q:
        return "citekey", q
    return "title", q


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def title_sim(a, b):
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


# ---------- 第 1 级：Better BibTeX JSON-RPC ----------

def bbt_rpc(method, params, timeout=15):
    req = urllib.request.Request(
        BBT_RPC,
        data=json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode(),
        headers={"Content-Type": "application/json"},
    )
    resp = json.load(urllib.request.urlopen(req, timeout=timeout))
    if "error" in resp:
        raise RuntimeError(resp["error"].get("message", "BBT error"))
    return resp.get("result")


def bbt_attachments(citekey):
    out = []
    for a in bbt_rpc("item.attachments", [citekey]) or []:
        p = a.get("path")
        # path 可能为 false（链接型附件），必须做类型判断
        if isinstance(p, str) and p.lower().endswith(".pdf") and Path(p).exists():
            out.append({"path": p, "source": "zotero", "citekey": citekey,
                        "open": a.get("open"), "score": 1.0})
    return out


def tier1_zotero(kind, value, query):
    try:
        if kind == "citekey":
            return bbt_attachments(value)
        if kind == "doi":
            # 快速搜索不搜 DOI 字段，须用高级检索元组（2026-07-28 实测）
            hits = bbt_rpc("item.search", [[["DOI", "is", value]]]) or []
        else:
            # arXiv / 标题走快速搜索（Title Creator Year）
            hits = bbt_rpc("item.search", [value]) or []
        if kind == "title" and hits:
            hits.sort(key=lambda h: title_sim(h.get("title", ""), value), reverse=True)
            hits = [h for h in hits if title_sim(h.get("title", ""), value) >= 0.5]
        results = []
        for h in hits[:5]:
            ck = h.get("citekey")
            if not ck:
                continue
            for att in bbt_attachments(ck):
                att["title"] = h.get("title")
                att["score"] = title_sim(h.get("title", ""), query) if kind == "title" else 1.0
                results.append(att)
        return results
    except (urllib.error.URLError, OSError):
        print("[locate_pdf] Zotero 未运行，跳过 BBT 查询", file=sys.stderr)
        return []
    except RuntimeError as e:
        print("[locate_pdf] BBT: {}".format(e), file=sys.stderr)
        return []


# ---------- 第 2 级：札记索引翻译 ----------

def tier2_index_lookup(kind, value):
    """从 literature_index.json 找到条目，返回可用于文件搜索的别名（标题/arXiv 号）。"""
    if not INDEX_JSON.exists():
        return None
    try:
        papers = json.load(open(INDEX_JSON)).get("papers", [])
    except (json.JSONDecodeError, OSError):
        return None
    for p in papers:
        if kind == "citekey" and p.get("citekey") == value:
            return p
        if kind == "doi" and norm(p.get("doi", "")) == norm(value):
            return p
        if kind == "arxiv" and (p.get("arxiv_id") or "").split("v")[0] == value:
            return p
    if kind == "title":
        best = max(papers, key=lambda p: title_sim(p.get("title", ""), value), default=None)
        if best and title_sim(best.get("title", ""), value) >= 0.6:
            return best
    return None


# ---------- 第 3 级：Spotlight mdfind 兜底 ----------

def mdfind(query_str):
    paths = []
    for d in SEARCH_DIRS:
        if not d.exists():
            continue
        try:
            out = subprocess.run(["mdfind", "-onlyin", str(d), query_str],
                                 capture_output=True, text=True, timeout=20).stdout
            paths += [l for l in out.splitlines() if l.lower().endswith(".pdf")]
        except (subprocess.TimeoutExpired, OSError):
            continue
    return paths


def tier3_spotlight(kind, value, title=None, arxiv_id=None):
    candidates = {}

    def add(paths, base_score, ref_title):
        for p in paths:
            name = Path(p).stem
            score = base_score + 0.5 * (title_sim(name, ref_title) if ref_title else 0)
            if p not in candidates or candidates[p]["score"] < score:
                candidates[p] = {"path": p, "source": "spotlight", "score": round(min(score, 0.99), 3)}

    if arxiv_id or kind == "arxiv":
        aid = arxiv_id or value
        add(mdfind('"{}"'.format(aid)), 0.5, title)
    if kind == "doi":
        add(mdfind('"{}"'.format(value)), 0.5, title)  # 全文里通常印着 DOI
        suffix = value.split("/")[-1]
        if len(suffix) >= 6:
            add(mdfind('"{}"'.format(suffix)), 0.3, title)
    ref = title or (value if kind == "title" else None)
    if ref:
        add(mdfind('"{}"'.format(ref)), 0.4, ref)  # 短语（文件名或全文）
        tokens = [t for t in norm(ref).split() if len(t) > 3][:6]
        if len(tokens) >= 2:
            add(mdfind(" ".join('"{}"'.format(t) for t in tokens)), 0.2, ref)
    if kind == "citekey":  # citekey 有时就是文件名（导出的 bundle 等）
        add(mdfind('"{}"'.format(value)), 0.4, title)
    return sorted(candidates.values(), key=lambda c: c["score"], reverse=True)


def tier0_citekey_filename(value):
    """手动精读批次的 PDF 按 <citekey>.pdf 归档，直接命中即可，无需搜索。

    加这一级是因为 tier3 的 Spotlight 兜底按「文件名 vs 论文标题」的 difflib 相似度排序，
    而 citekey 文件名与标题字面差得远：实测 erickson2025Tabarena.pdf 只得 0.49，
    反被同目录树下另一篇正文提到 TabArena 的 PDF（0.53）压过去，返回错的文件。
    """
    hits = []
    for d in SEARCH_DIRS:
        if not d.is_dir():
            continue
        for p in d.rglob(value + ".pdf"):
            if p.is_file():
                hits.append({"path": str(p), "score": 1.0, "source": "citekey-filename"})
    return hits


def locate(query):
    kind, value = classify(query)
    print("[locate_pdf] 识别为 {}: {}".format(kind, value), file=sys.stderr)

    if kind == "citekey":
        results = tier0_citekey_filename(value)
        if results:
            print("[locate_pdf] 文件名直接命中 citekey", file=sys.stderr)
            return kind, results

    results = tier1_zotero(kind, value, query)
    if results:
        return kind, results

    meta = tier2_index_lookup(kind, value)
    title = meta.get("title") if meta else None
    arxiv_id = (meta.get("arxiv_id") or "").split("v")[0] if meta else None
    if meta:
        print("[locate_pdf] 索引命中: {} ({})".format(meta.get("citekey"), title), file=sys.stderr)
        # 索引翻译出的标题再回打一次 Zotero（citekey 可能对不上但条目在库里）
        if kind != "title" and title:
            results = tier1_zotero("title", title, title)
            if results:
                return kind, results

    return kind, tier3_spotlight(kind, value, title=title, arxiv_id=arxiv_id)


def main():
    ap = argparse.ArgumentParser(description="定位本地 PDF：citekey/DOI/arXiv/标题 → 路径")
    ap.add_argument("query", help="citekey、DOI、arXiv ID 或标题")
    ap.add_argument("--all", action="store_true", help="列出全部候选（含来源与分数）")
    ap.add_argument("--json", action="store_true", dest="as_json", help="JSON 输出")
    args = ap.parse_args()

    kind, results = locate(args.query)
    confident = [r for r in results if r["score"] >= CONF_THRESHOLD]
    if args.as_json:
        print(json.dumps({"query": args.query, "kind": kind,
                          "results": confident, "low_confidence": results[len(confident):]},
                         ensure_ascii=False, indent=2))
        sys.exit(0 if confident else 1)
    if args.all:
        for r in results:
            print("{:.2f}  [{}]  {}".format(r["score"], r["source"], r["path"]))
        sys.exit(0 if results else 1)
    if not confident:
        msg = "[locate_pdf] 未找到高置信匹配"
        if results:
            msg += "；有 {} 个低置信候选（--all 查看）".format(len(results))
        print(msg, file=sys.stderr)
        sys.exit(1)
    print(confident[0]["path"])
    if len(confident) > 1:
        print("[locate_pdf] 另有 {} 个候选（--all 查看）".format(len(confident) - 1), file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
