# -*- coding: utf-8 -*-
"""筛选召回评测集（金标准）构造与指标纯函数。设计：docs/decisions/trust_hardening_design_2026-10-13.md §5。

本模块**不调用任何 LLM、不联网**，只读已有产物（札记索引、sidecar、digest、稿件 bib），
产出五类东西：

1. 金标准 `gold_v{N}.jsonl`（分层：P1-in / P1-out / M-core / M-side / F-override / H-stream / R-stable）；
2. manual 批次登记表（批次级 + 按篇级），登记时间戳写进 gold 元数据——**必须先于第一次评测运行**，
   否则用户会不自觉把漏判的篇目归为「支线」，召回率被事后修饰；
3. H-stream 盲标表：只含 标题 + 生产口径摘要 + 空白标签列，**不含**任何 LLM 判定、置信度、层别、近邻；
   对应的抽样钥匙（带 LLM 判定与抽样权重）另存 `label_key_*.json`，标完之前别打开；
4. 指标纯函数（Wilson 区间、加权漏判率、配对回归、翻转率、dev/test 切分）；
5. 数据快照：输入文件的 sha256 与计数写进 meta，构造可复现。

所有产物落在 `<notes_dir>/_eval/filter/`（gitignore 的数据目录；稿件引用清单属未发表信息，不进 git）。
"""
import csv
import hashlib
import json
import math
import re
import subprocess
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ._citekey_utils import _norm_title, dedup_key_fields

# ---------------- 常量 ----------------

SEED = 20261013                  # 抽样/混洗的固定种子（可由 CLI 覆盖，覆盖值会写进 meta）
TEST_FRACTION_PCT = 30           # sha256(item_id) % 100 < 30 → test，其余 dev；不随 gold 版本变
ABSTRACT_MAX_CHARS = 800         # 与生产 prompt 一致；prod 口径的 400 截断由评测执行器（WO-7）负责
FILTER_VERSION_PREFIX = "filter-v3"

STRATA = ("P1-in", "P1-out", "M-core", "M-side", "F-override", "H-stream", "R-stable")
REG_CLASSES = ("core", "side", "mixed")
PAPER_CLASSES = ("core", "side")

# H-stream 抽样规模（设计 5.3：EXCLUDE 100 / INCLUDE 40 / MAYBE 30）
H_EXCLUDE_N = 100
H_INCLUDE_N = 40
H_MAYBE_N = 30
H_LOW_CAP = 40                   # 低置信 EXCLUDE 段（<=0.7）上限；见 sample_stream 注释

# 盲标表列名白名单：任何诱导判断的字段（LLM 原判、置信度、层别、近邻、bucket、期刊）都不许出现
SHEET_COLUMNS = ("row_id", "title", "abstract", "label", "comment")
HUMAN_LABELS = {"应收": "positive", "边缘": "borderline", "无关": "negative"}

BIB_KEEP_TYPES = ("article", "inproceedings", "conference", "incollection")


# ---------------- 通用小工具 ----------------

def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha256_file(p: Path) -> Optional[str]:
    try:
        return _sha256_bytes(Path(p).read_bytes())
    except OSError:
        return None


def item_id_of(dedup_key: str) -> str:
    """稳定条目 id：dedup_key 的 sha256 前 12 位（跨 gold 版本不变，切分据此）。"""
    return _sha256_bytes((dedup_key or "").encode("utf-8"))[:12]


def split_of(item_id: str) -> str:
    """dev/test 固定切分：只取决于 item_id，不随 gold 版本、抽样种子变。"""
    h = int(_sha256_bytes(item_id.encode("utf-8"))[:8], 16)
    return "test" if (h % 100) < TEST_FRACTION_PCT else "dev"


def _rank_key(seed: int, tag: str, item_id: str) -> str:
    """确定性「随机」排序键：同种子同输入永远同序，且与输入顺序无关。"""
    return _sha256_bytes("{}|{}|{}".format(seed, tag, item_id).encode("utf-8"))


def _trim_abstract(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()[:ABSTRACT_MAX_CHARS]


def eval_dir(notes_dir) -> Path:
    return Path(notes_dir) / "_eval" / "filter"


def assert_ignored(path) -> None:
    """金标准目录必须被 gitignore（含未发表稿件的引用清单）。不在任何 git 仓库内则放行。

    退出码约定：git check-ignore → 0 被忽略 / 1 未被忽略 / 128 不在仓库或出错。
    只有明确的 1 才拒绝。
    """
    p = Path(path)
    probe = p if p.exists() else p.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        r = subprocess.run(["git", "-C", str(probe), "check-ignore", "-q", str(p / "_probe")],
                           capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return
    if r.returncode == 1:
        raise RuntimeError("目录未被 gitignore，拒绝写入金标准产物：{}".format(p))


# ---------------- 指标纯函数 ----------------

def wilson_interval(k: int, n: int, z: float = 1.96) -> Tuple[float, float, float]:
    """比例的 Wilson 区间，返回 (点估计, 下界, 上界)。n=0 时全 NaN 之外返回 (0,0,1) 以免除零。"""
    if n <= 0:
        return (0.0, 0.0, 1.0)
    p = k / n
    z2 = z * z
    den = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / den
    return (p, max(0.0, centre - half), min(1.0, centre + half))


def keep_rate(verdicts: Sequence[str]) -> Tuple[int, int]:
    """保留率分子/分母：INCLUDE 或 MAYBE 算保留（进库），EXCLUDE 才是丢论文。"""
    n = len(verdicts)
    return (sum(1 for v in verdicts if v in ("INCLUDE", "MAYBE")), n)


def weighted_rate(flags: Sequence[bool], weights: Sequence[float]) -> float:
    """加权比例 Σw·flag / Σw。按抽样权重还原总体（分层抽样的漏判率用它）。"""
    tw = sum(weights)
    if tw <= 0:
        return 0.0
    return sum(w for f, w in zip(flags, weights) if f) / tw


def worse_of_repeats(runs: Sequence[Sequence[str]]) -> List[str]:
    """多次重复里每条取「更差」的一次（EXCLUDE 最差，MAYBE 次之，INCLUDE 最好）。"""
    order = {"EXCLUDE": 0, "MAYBE": 1, "INCLUDE": 2}
    return [min(col, key=lambda v: order.get(v, 0)) for col in zip(*runs)]


def flip_rate(runs: Sequence[Sequence[str]]) -> float:
    """翻转率：同一条在各次重复里三态结论不一致的比例。"""
    cols = list(zip(*runs))
    if not cols:
        return 0.0
    return sum(1 for c in cols if len(set(c)) > 1) / len(cols)


def paired_regression(base: Sequence[str], new: Sequence[str]) -> Dict[str, float]:
    """同一批条目上新旧配置的配对比较：保留率之差（new - base）与双向翻转计数。"""
    if len(base) != len(new):
        raise ValueError("配对比较要求两侧条目一一对应")
    kb, n = keep_rate(base)
    kn, _ = keep_rate(new)
    lost = sum(1 for b, c in zip(base, new) if b != "EXCLUDE" and c == "EXCLUDE")
    gained = sum(1 for b, c in zip(base, new) if b == "EXCLUDE" and c != "EXCLUDE")
    return {"n": n, "keep_base": kb / n if n else 0.0, "keep_new": kn / n if n else 0.0,
            "delta": (kn - kb) / n if n else 0.0, "lost": lost, "gained": gained}


# ---------------- bib 解析（无第三方依赖） ----------------

def _read_braced(s: str, i: int) -> Tuple[str, int]:
    """s[i] == '{'：返回 (花括号内文本, 闭合括号之后的下标)。"""
    depth, j = 0, i
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[i + 1:j], j + 1
        j += 1
    return s[i + 1:], len(s)


def parse_bib(text: str) -> List[Dict[str, Any]]:
    """极简 BibTeX 解析：返回 [{key, type, fields{小写字段名: 原文}}]；跳过 @comment/@string/@preamble。"""
    out: List[Dict[str, Any]] = []
    pos = 0
    head = re.compile(r"@(\w+)\s*([{(])")
    while True:
        m = head.search(text, pos)
        if not m:
            break
        etype = m.group(1).lower()
        if m.group(2) == "(":                       # @type( … ) 形式极少见，按未识别跳过
            pos = m.end()
            continue
        body, end = _read_braced(text, m.end() - 1)
        pos = end
        if etype in ("comment", "string", "preamble"):
            continue
        km = re.match(r"\s*([^,\s]+)\s*,", body)
        if not km:
            continue
        fields: Dict[str, str] = {}
        i = km.end()
        while i < len(body):
            fm = re.compile(r"\s*([A-Za-z_][\w\-]*)\s*=\s*").match(body, i)
            if not fm:
                nxt = body.find(",", i)
                if nxt < 0:
                    break
                i = nxt + 1
                continue
            name = fm.group(1).lower()
            i = fm.end()
            if i < len(body) and body[i] == "{":
                val, i = _read_braced(body, i)
            elif i < len(body) and body[i] == '"':
                j = i + 1
                while j < len(body) and not (body[j] == '"' and body[j - 1] != "\\"):
                    j += 1
                val, i = body[i + 1:j], j + 1
            else:
                j = i
                while j < len(body) and body[j] not in ",\n":
                    j += 1
                val, i = body[i:j].strip(), j
            fields[name] = val
            c = body.find(",", i)
            i = c + 1 if c >= 0 else len(body)
        out.append({"key": km.group(1), "type": etype, "fields": fields})
    return out


def clean_bib_text(s: Optional[str]) -> str:
    """去花括号与 LaTeX 控制序列，压缩空白。"""
    s = re.sub(r"\\[a-zA-Z]+\s*", "", s or "")
    s = s.replace("\\", "").replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", s).strip()


_ARXIV_URL_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([^\s?#]+?)(?:v\d+)?(?:\.pdf)?(?:[?#]|$)", re.I)
_ARXIV_DOI_RE = re.compile(r"10\.48550/arxiv\.(.+)$", re.I)


def bib_identifiers(entry: Dict[str, Any]) -> Dict[str, str]:
    """从 bib 条目抽 doi / arxiv_id（无版本号）/ 规范化标题 / url。"""
    f = entry["fields"]
    doi = (f.get("doi") or "").strip()
    url = (f.get("url") or "").strip()
    if not doi:
        dm = re.search(r"doi\.org/(10\.\S+)", url, re.I)
        doi = dm.group(1) if dm else ""
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I).strip().lower()
    arxiv = ""
    m = _ARXIV_URL_RE.search(url)
    if m:
        arxiv = m.group(1)
    elif (f.get("archiveprefix", "").lower() == "arxiv") and f.get("eprint"):
        arxiv = f["eprint"]
    elif _ARXIV_DOI_RE.search(doi):
        arxiv = _ARXIV_DOI_RE.search(doi).group(1)
    arxiv = re.sub(r"v\d+$", "", arxiv.strip().lower())
    title = clean_bib_text(f.get("title"))
    return {"doi": doi, "arxiv_id": arxiv, "title": title, "title_norm": _norm_title(title), "url": url}


def classify_bib_entry(entry: Dict[str, Any], extra_exclude: Iterable[str] = (),
                       extra_include: Iterable[str] = ()) -> Tuple[bool, str]:
    """是否按「论文」收：article/inproceedings 收；misc 只有带 arXiv/DOI 才收（其余多为软件/网页）。

    教科书（book）、报告、软件、数据集、网页一律剔除；剔除清单落盘供用户过目，
    用户可再用 extra_exclude（citekey 集合）追加剔除（例如以 article 形式发表的数据集论文、
    明显画像外的通用论文），或用 extra_include 捞回被默认规则剔掉的（例如缺 url 的 arXiv misc）。
    """
    if entry["key"] in set(extra_exclude):
        return False, "用户追加剔除"
    if entry["key"] in set(extra_include):
        return True, "用户追加纳入"
    t = entry["type"]
    ids = bib_identifiers(entry)
    if t in BIB_KEEP_TYPES:
        return True, "类型 {}".format(t)
    if t == "misc":
        if ids["arxiv_id"] or ids["doi"]:
            return True, "misc 但带 arXiv/DOI"
        return False, "misc 且无 arXiv/DOI（疑软件/网页/数据集）"
    return False, "类型 {}（非论文）".format(t)


# ---------------- 数据加载（全部只读） ----------------

def load_library(index_path) -> List[Dict[str, Any]]:
    """札记索引中的 keeper 条目（duplicate_of 为空）。"""
    d = json.loads(Path(index_path).read_text(encoding="utf-8"))
    return [e for e in d.get("papers", []) if not e.get("duplicate_of")]


def load_abstract_cache(path) -> Dict[str, str]:
    """abstracts.json → {dedup_key: abstract}；文件缺失/损坏返回空。"""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    ab = d.get("abstracts", d) if isinstance(d, dict) else {}
    out = {}
    for k, v in ab.items():
        a = v.get("abstract") if isinstance(v, dict) else v
        if isinstance(a, str) and a.strip():
            out[k] = a
    return out


def _segment_dedup_key(meta: Dict[str, Any], fallback: str) -> str:
    return dedup_key_fields(meta.get("doi"), meta.get("arxiv_id"), meta.get("title"), fallback=fallback,
                            url=meta.get("url"), isbn=meta.get("isbn"),
                            chapter_number=meta.get("chapter_number"))


def load_manual_bundles(manual_dir) -> List[Dict[str, Any]]:
    """manual/*/*.paper.json → [{dedup_key, title, abstract, batch, paper_id}]；批次 = 所在目录名。"""
    out = []
    for f in sorted(Path(manual_dir).glob("*/*.paper.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        seg = d.get("segment") or {}
        meta = seg.get("metadata") or {}
        pid = d.get("paper_id") or seg.get("paper_id") or f.stem
        out.append({"dedup_key": _segment_dedup_key(meta, pid), "title": meta.get("title") or "",
                    "abstract": seg.get("original_abstract") or "", "batch": f.parent.name,
                    "paper_id": pid})
    return out


def load_stream_decisions(excluded_dir, digest_dir) -> List[Dict[str, Any]]:
    """生产流里 filter-v3 的 LLM 裁决：每个 paper_id 取最晚一条，再按 dedup_key 取最晚一条。

    来源：excluded sidecar（EXCLUDE，带送审时的 original_abstract）+ digest_*.json（INCLUDE/MAYBE）。
    只收 stage=llm_judge 且 prompt_version 以 filter-v3 开头的（manual 的 INCLUDE 名为 llm_judge 实为
    人选，但 manual 不在这两处目录，不会混入）。
    """
    rows: Dict[str, Dict[str, Any]] = {}

    def offer(pid, dec, meta, abstract):
        if not isinstance(dec, dict) or dec.get("stage") != "llm_judge":
            return
        if not str(dec.get("prompt_version") or "").startswith(FILTER_VERSION_PREFIX):
            return
        if dec.get("decision") not in ("INCLUDE", "MAYBE", "EXCLUDE"):
            return
        rec = {"paper_id": pid, "dedup_key": _segment_dedup_key(meta or {}, pid),
               "title": (meta or {}).get("title") or dec.get("title") or "",
               "abstract": abstract or "", "verdict": dec["decision"],
               "confidence": dec.get("confidence"), "decided_at": dec.get("decided_at") or "",
               "prompt_version": dec.get("prompt_version")}
        old = rows.get(pid)
        if old is None or rec["decided_at"] > old["decided_at"]:
            rows[pid] = rec

    for f in sorted(Path(excluded_dir).glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for p in d.get("papers", []):
            offer(p.get("paper_id"), p.get("decision"), p.get("metadata"), p.get("original_abstract"))
    for f in sorted(Path(digest_dir).glob("digest_*[0-9].json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for s in d.get("segments", []):
            offer(s.get("paper_id"), s.get("filter_decision"), s.get("metadata"), s.get("original_abstract"))
    by_key: Dict[str, Dict[str, Any]] = {}
    for r in rows.values():
        o = by_key.get(r["dedup_key"])
        if o is None or r["decided_at"] > o["decided_at"]:
            by_key[r["dedup_key"]] = r
    return sorted(by_key.values(), key=lambda r: r["dedup_key"])


def load_stable_audit(path) -> List[Dict[str, Any]]:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return d if isinstance(d, list) else []


# ---------------- P1：稿件引用 ----------------

def build_p1(bibs: Dict[str, str], lib: List[Dict[str, Any]], extra_exclude: Iterable[str] = (),
             extra_include: Iterable[str] = ()) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """两份稿件 bib → (P1 条目草稿, 逐条审阅记录)。

    bibs: {来源名: bib 文本}。匹配次序 citekey → DOI → arXiv id → 规范化标题（对 keeper 条目）。
    返回的草稿：{stratum: P1-in|P1-out, dedup_key, title, citekey, doi, arxiv_id, origin, sources, match_by}；
    同一论文在两份 bib 里重复出现只出一条（sources 合并）。审阅记录含被剔除的与保留的全部 bib 条目。
    """
    by_ck = {e["citekey"]: e for e in lib if e.get("citekey")}
    by_doi = {(e.get("doi") or "").lower(): e for e in lib if e.get("doi")}
    by_arx = {re.sub(r"v\d+$", "", (e.get("arxiv_id") or "").lower()): e for e in lib if e.get("arxiv_id")}
    by_title: Dict[str, Dict[str, Any]] = {}
    for e in lib:
        t = _norm_title(e.get("title"))
        if t:
            by_title.setdefault(t, e)
    extra = set(extra_exclude)
    drafts: Dict[str, Dict[str, Any]] = {}
    review: List[Dict[str, Any]] = []
    for src in sorted(bibs):
        for ent in parse_bib(bibs[src]):
            ids = bib_identifiers(ent)
            keep, why = classify_bib_entry(ent, extra, extra_include)
            rec = {"source": src, "bib_key": ent["key"], "type": ent["type"], "title": ids["title"],
                   "keep": keep, "reason": why, "stratum": "", "match_by": ""}
            if not keep:
                review.append(rec)
                continue
            hit, how = None, ""
            if ent["key"] in by_ck:
                hit, how = by_ck[ent["key"]], "citekey"
            elif ids["doi"] and ids["doi"] in by_doi:
                hit, how = by_doi[ids["doi"]], "doi"
            elif ids["arxiv_id"] and ids["arxiv_id"] in by_arx:
                hit, how = by_arx[ids["arxiv_id"]], "arxiv"
            elif ids["title_norm"] and ids["title_norm"] in by_title:
                hit, how = by_title[ids["title_norm"]], "title"
            if hit is not None:
                dk, title, ck = hit["dedup_key"], hit.get("title") or ids["title"], hit.get("citekey")
                origin, stratum = hit.get("series") or "auto", "P1-in"
            else:
                dk = dedup_key_fields(ids["doi"] or None, ids["arxiv_id"] or None, ids["title"],
                                      fallback=ent["key"], url=ids["url"])
                title, ck, origin, stratum = ids["title"], ent["key"], "bib", "P1-out"
            rec.update(stratum=stratum, match_by=how)
            review.append(rec)
            d = drafts.get(dk)
            if d is None:
                drafts[dk] = {"stratum": stratum, "dedup_key": dk, "title": title, "citekey": ck,
                              "doi": ids["doi"], "arxiv_id": ids["arxiv_id"], "origin": origin,
                              "sources": [src], "match_by": how, "bib_key": ent["key"]}
            elif src not in d["sources"]:
                d["sources"].append(src)
    return sorted(drafts.values(), key=lambda d: d["dedup_key"]), review


# ---------------- 登记表 ----------------

def _write_csv(path: Path, header: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _read_csv(path: Path) -> List[Dict[str, str]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]


def registry_paths(edir: Path, version: int) -> Dict[str, Path]:
    return {"batches": edir / "registry_batches_v{}.csv".format(version),
            "papers": edir / "registry_papers_v{}.csv".format(version),
            "state": edir / "registry_v{}.json".format(version)}


def _load_state(p: Path) -> Dict[str, Any]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def registry_export(edir: Path, version: int, manual: List[Dict[str, Any]], kind: str) -> Path:
    """导出登记表（空白待填）。已登记过的版本拒绝覆盖——改登记必须新开版本。"""
    paths = registry_paths(edir, version)
    state = _load_state(paths["state"])
    if state.get("{}_registered_at".format(kind)):
        raise RuntimeError("v{} 的 {} 登记已于 {} 完成；改登记须新开版本（--version {}）".format(
            version, kind, state["{}_registered_at".format(kind)], version + 1))
    if kind == "batches":
        cnt: Dict[str, int] = defaultdict(int)
        for m in manual:
            cnt[m["batch"]] += 1
        _write_csv(paths["batches"], ("batch", "n_papers", "class", "note"),
                   [(b, cnt[b], "", "") for b in sorted(cnt)])
        return paths["batches"]
    if kind == "papers":
        if not state.get("batches_registered_at"):
            raise RuntimeError("先完成并导入批次级登记（registry-import --kind batches）")
        mixed = {b for b, c in state["batches"].items() if c == "mixed"}
        rows = [(m["dedup_key"], m["batch"], m["title"], "") for m in
                sorted(manual, key=lambda x: (x["batch"], x["dedup_key"])) if m["batch"] in mixed]
        _write_csv(paths["papers"], ("dedup_key", "batch", "title", "class"), rows)
        return paths["papers"]
    raise ValueError("kind 只能是 batches / papers")


def registry_import(edir: Path, version: int, manual: List[Dict[str, Any]], kind: str,
                    now: Optional[str] = None) -> Dict[str, Any]:
    """校验已填写的登记表并盖登记时间戳（写入 registry_v{N}.json）。校验不过则不落状态。"""
    paths = registry_paths(edir, version)
    state = _load_state(paths["state"])
    if state.get("{}_registered_at".format(kind)):
        raise RuntimeError("v{} 的 {} 登记已完成，不可重复导入；请新开版本".format(version, kind))
    stamp = now or datetime.now().isoformat(timespec="seconds")
    if kind == "batches":
        have = {m["batch"] for m in manual}
        rows = _read_csv(paths["batches"])
        got = {r["batch"]: r["class"] for r in rows}
        bad = {b: c for b, c in got.items() if c not in REG_CLASSES}
        if bad:
            raise ValueError("登记值只能是 core/side/mixed，非法或为空：{}".format(sorted(bad)[:10]))
        missing = sorted(have - set(got))
        if missing:
            raise ValueError("以下批次未出现在登记表里：{}".format(missing))
        state.update(version=version, batches=got, batches_registered_at=stamp,
                     batches_sha256=_sha256_file(paths["batches"]))
    elif kind == "papers":
        if not state.get("batches_registered_at"):
            raise RuntimeError("先导入批次级登记")
        mixed = {b for b, c in state["batches"].items() if c == "mixed"}
        need = {m["dedup_key"] for m in manual if m["batch"] in mixed}
        rows = _read_csv(paths["papers"])
        got = {r["dedup_key"]: r["class"] for r in rows}
        bad = {k: c for k, c in got.items() if c not in PAPER_CLASSES}
        if bad:
            raise ValueError("按篇登记值只能是 core/side，非法或为空 {} 条".format(len(bad)))
        missing = sorted(need - set(got))
        if missing:
            raise ValueError("mixed 批次中有 {} 篇未登记".format(len(missing)))
        state.update(papers=got, papers_registered_at=stamp, papers_sha256=_sha256_file(paths["papers"]))
    else:
        raise ValueError("kind 只能是 batches / papers")
    paths["state"].write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return state


def check_registry_complete(state: Dict[str, Any], manual: List[Dict[str, Any]]) -> None:
    """build-gold 的前置闸：登记缺失即拒绝（不允许事后找补）。"""
    if not state.get("batches_registered_at"):
        raise RuntimeError("未做 manual 批次登记，拒绝构造 gold：先 registry-export / 填写 / registry-import")
    reg = state.get("batches", {})
    new = sorted({m["batch"] for m in manual} - set(reg))
    if new:
        raise RuntimeError("登记之后出现了新批次 {}；须新开登记版本".format(new))
    if any(c == "mixed" for c in reg.values()):
        if not state.get("papers_registered_at"):
            raise RuntimeError("存在 mixed 批次但未做按篇登记，拒绝构造 gold")
        mixed = {b for b, c in reg.items() if c == "mixed"}
        miss = [m for m in manual if m["batch"] in mixed and m["dedup_key"] not in state.get("papers", {})]
        if miss:
            raise RuntimeError("登记之后 mixed 批次新增了 {} 篇；须新开登记版本".format(len(miss)))


def manual_class(state: Dict[str, Any], m: Dict[str, Any]) -> str:
    c = state["batches"].get(m["batch"])
    return state.get("papers", {}).get(m["dedup_key"], "core") if c == "mixed" else c


# ---------------- H-stream 抽样与盲标表 ----------------

def conf_bin(conf: Optional[float]) -> str:
    c = 0.0 if conf is None else float(conf)
    if c < 0.75:
        return "low"        # 0.5–0.7 段
    if c < 0.85:
        return "0.8"
    if c < 0.95:
        return "0.9"
    return "1.0"


def allocate(sizes: Dict[str, int], total: int, min_each: int = 1) -> Dict[str, int]:
    """按池大小比例分配名额（最大余数法），每池至少 min_each（不超过池大小），总数不超过 total。"""
    live = {k: v for k, v in sizes.items() if v > 0}
    if not live or total <= 0:
        return {k: 0 for k in sizes}
    base = {k: min(v, min_each) for k, v in live.items()}
    while sum(base.values()) > total:              # 名额不够兜底线时，从最大的一池扣
        base[max(base, key=lambda k: (base[k], k))] -= 1
    left = total - sum(base.values())
    if left > 0:
        cap = {k: live[k] - base[k] for k in live}
        tot = sum(cap.values()) or 1
        raw = {k: left * cap[k] / tot for k in live}
        add = {k: min(cap[k], int(raw[k])) for k in live}
        rem = left - sum(add.values())
        for k in sorted(live, key=lambda k: (-(raw[k] - int(raw[k])), k)):
            if rem <= 0:
                break
            if add[k] < cap[k]:
                add[k] += 1
                rem -= 1
        base = {k: base[k] + add[k] for k in live}
    return {k: base.get(k, 0) for k in sizes}


def sample_stream(cands: List[Dict[str, Any]], seed: int = SEED, n_exclude: int = H_EXCLUDE_N,
                  n_include: int = H_INCLUDE_N, n_maybe: int = H_MAYBE_N, low_cap: int = H_LOW_CAP
                  ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """从生产流 filter-v3 裁决里分层抽样，每条记 pool / conf_bin / weight（=池大小/抽中数）。

    EXCLUDE：低置信段（<=0.7）优先，设计原为「全取」。实测去重后该段 131 条已超过 100 的预算，
    故以 low_cap 封顶（默认 40）；余下名额按池大小比例分给 0.8/0.9/1.0 三段，各段至少 3 条。
    低置信段若不超过 low_cap 则全取（回到设计原意）。INCLUDE/MAYBE 各自随机抽。
    选择由 sha256(seed|池|item_id) 排序决定，与输入顺序无关，可复现。
    """
    for c in cands:
        c.setdefault("item_id", item_id_of(c["dedup_key"]))
    pools: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for c in cands:
        pools[c["verdict"]].append(c)
    chosen: List[Dict[str, Any]] = []
    plan: Dict[str, Any] = {"seed": seed}

    def take(pool_name, items, n):
        items = sorted(items, key=lambda c: _rank_key(seed, pool_name, c["item_id"]))[:n]
        return items

    ex = pools.get("EXCLUDE", [])
    bins: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for c in ex:
        bins[conf_bin(c.get("confidence"))].append(c)
    low = bins.get("low", [])
    n_low = len(low) if len(low) <= low_cap else low_cap
    n_low = min(n_low, n_exclude)
    hi_names = ("0.8", "0.9", "1.0")
    alloc = allocate({b: len(bins.get(b, [])) for b in hi_names}, n_exclude - n_low, min_each=3)
    quotas = {"low": n_low, **alloc}
    plan["EXCLUDE"] = {}
    for b in ("low",) + hi_names:
        pool = bins.get(b, [])
        got = take("EXCLUDE|" + b, pool, quotas.get(b, 0))
        w = len(pool) / len(got) if got else 0.0
        plan["EXCLUDE"][b] = {"pool": len(pool), "taken": len(got), "weight": round(w, 6)}
        for c in got:
            chosen.append({**c, "pool": "EXCLUDE", "conf_bin": b, "weight": w})
    for name, n in (("INCLUDE", n_include), ("MAYBE", n_maybe)):
        pool = pools.get(name, [])
        got = take(name, pool, n)
        w = len(pool) / len(got) if got else 0.0
        plan[name] = {"pool": len(pool), "taken": len(got), "weight": round(w, 6)}
        for c in got:
            chosen.append({**c, "pool": name, "conf_bin": "-", "weight": w})
    return chosen, plan


def label_sheet_rows(sample: List[Dict[str, Any]], seed: int = SEED) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    """混洗并生成盲标表行 + 钥匙。盲标表只有 SHEET_COLUMNS 里的列；钥匙含 LLM 判定与权重（标完再看）。"""
    order = sorted(sample, key=lambda c: _rank_key(seed, "sheet", c["item_id"]))
    rows, key = [], {}
    for i, c in enumerate(order, 1):
        rid = "H{:03d}".format(i)
        rows.append({"row_id": rid, "title": c["title"], "abstract": _trim_abstract(c["abstract"]),
                     "label": "", "comment": ""})
        key[rid] = {"item_id": c["item_id"], "dedup_key": c["dedup_key"], "pool": c["pool"],
                    "conf_bin": c["conf_bin"], "weight": c["weight"], "prod_verdict": c["verdict"],
                    "prod_confidence": c.get("confidence")}
    return rows, key


def write_label_sheet(path: Path, rows: List[Dict[str, str]]) -> None:
    _write_csv(path, SHEET_COLUMNS, [[r[c] for c in SHEET_COLUMNS] for r in rows])


def import_labels(sheet_path: Path, key: Dict[str, Any], allow_partial: bool = False
                  ) -> List[Dict[str, str]]:
    """回收盲标表：校验标签取值与完整性，返回 [{row_id, item_id, label, comment}]。"""
    rows = _read_csv(sheet_path)
    out, bad, empty = [], [], []
    for r in rows:
        rid = r.get("row_id", "")
        if rid not in key:
            raise ValueError("标注表含钥匙里没有的 row_id：{}".format(rid))
        lab = r.get("label", "")
        if not lab:
            empty.append(rid)
            continue
        if lab not in HUMAN_LABELS:
            bad.append((rid, lab))
            continue
        out.append({"row_id": rid, "item_id": key[rid]["item_id"], "label": lab,
                    "comment": r.get("comment", "")})
    if bad:
        raise ValueError("标签只能是 应收/边缘/无关；非法：{}".format(bad[:10]))
    if empty and not allow_partial:
        raise ValueError("还有 {} 条未标注（如 {}）；确需部分回收请加 --allow-partial".format(len(empty), empty[:5]))
    missing = sorted(set(key) - {r.get("row_id") for r in rows})
    if missing and not allow_partial:
        raise ValueError("标注表缺行：{}".format(missing[:5]))
    return out


# ---------------- gold 构造 ----------------

def _item(stratum, dedup_key, title, abstract, abstract_source, label, label_source, citekey=None,
          labeled_at=None, notes="", **extra) -> Dict[str, Any]:
    iid = item_id_of(dedup_key)
    d = {"item_id": iid, "title": title or "", "abstract": _trim_abstract(abstract),
         "abstract_source": abstract_source if (abstract or "").strip() else "none",
         "stratum": stratum, "label": label, "split": split_of(iid), "dedup_key": dedup_key,
         "citekey": citekey, "label_source": label_source, "labeled_at": labeled_at, "notes": notes}
    d.update(extra)
    return d


def build_gold(*, lib: List[Dict[str, Any]], manual: List[Dict[str, Any]], abstracts: Dict[str, str],
               bibs: Dict[str, str], reg_state: Dict[str, Any], stream: List[Dict[str, Any]],
               stable: List[Dict[str, Any]], p1_abstracts: Optional[Dict[str, Dict[str, str]]] = None,
               labels: Optional[List[Dict[str, str]]] = None, label_key: Optional[Dict[str, Any]] = None,
               f_override: Optional[List[Dict[str, Any]]] = None, bib_exclude: Iterable[str] = (),
               bib_include: Iterable[str] = (),
               seed: int = SEED, labeled_at: Optional[str] = None
               ) -> Tuple[List[Dict[str, Any]], Dict[str, Any], List[Dict[str, Any]]]:
    """纯函数：把各路输入装配成 (gold 条目, 构造统计, bib 审阅记录)。同输入同输出。

    层间去重优先级 P1 > M > F > H > R：同一 dedup_key 只在最先命中的层出现（落选者计数写进统计）。
    """
    check_registry_complete(reg_state, manual)
    p1_abstracts = p1_abstracts or {}
    items: Dict[str, Dict[str, Any]] = {}
    stats: Dict[str, Any] = {"dropped_dup": defaultdict(int)}
    man_by_key = {m["dedup_key"]: m for m in manual}

    def add(it):
        if it["dedup_key"] in items:
            stats["dropped_dup"][it["stratum"]] += 1
            return
        items[it["dedup_key"]] = it

    # P1
    drafts, review = build_p1(bibs, lib, bib_exclude, bib_include)
    todo = []
    for d in drafts:
        dk = d["dedup_key"]
        if d["stratum"] == "P1-in":
            m = man_by_key.get(dk)
            if m and m["abstract"].strip():
                ab, src = m["abstract"], "bundle"
            elif abstracts.get(dk):
                ab, src = abstracts[dk], "abstracts_cache"
            else:
                ab, src = "", "none"
            note = "来自 {}；origin={}；match={}".format("+".join(d["sources"]), d["origin"], d["match_by"])
            if d["origin"] == "auto":
                note += "；幸存者偏倚，单列不进门槛"
        else:
            c = p1_abstracts.get(dk) or p1_abstracts.get(d["bib_key"]) or {}
            ab, src = c.get("abstract", ""), c.get("source", "none") if c.get("abstract") else "none"
            note = "来自 {}；库外，从未被过滤器看过".format("+".join(d["sources"]))
            if not ab:
                todo.append({"dedup_key": dk, "bib_key": d["bib_key"], "doi": d["doi"],
                             "arxiv_id": d["arxiv_id"], "title": d["title"]})
        add(_item(d["stratum"], dk, d["title"], ab, src, "positive", "manuscript_citation",
                  citekey=d["citekey"], notes=note, origin=d["origin"], sources=d["sources"]))
    stats["p1_abstract_todo"] = todo

    # M
    reg_at = reg_state.get("batches_registered_at")
    for m in sorted(manual, key=lambda x: x["dedup_key"]):
        cls = manual_class(reg_state, m)
        ab = m["abstract"] if m["abstract"].strip() else abstracts.get(m["dedup_key"], "")
        src = "bundle" if m["abstract"].strip() else ("abstracts_cache" if ab else "none")
        add(_item("M-core" if cls == "core" else "M-side", m["dedup_key"], m["title"], ab, src,
                  "positive" if cls == "core" else None, "manual_pick" if cls == "core" else "manual_side",
                  labeled_at=reg_at, notes="batch={}".format(m["batch"]), batch=m["batch"]))

    # F（WO-2 起才有持久记录；此前只收调用方给的）
    for f in (f_override or []):
        add(_item("F-override", f["dedup_key"], f.get("title"), f.get("abstract"), f.get("abstract_source", "digest"),
                  "positive", "manual_override", citekey=f.get("citekey"),
                  notes=f.get("notes", "")))

    # H
    if labels and label_key:
        lab_by_item = {l["item_id"]: l for l in labels}
        meta_by_item = {v["item_id"]: v for v in label_key["key"].values()}
        cand_by_id = {item_id_of(c["dedup_key"]): c for c in stream}
        for iid in sorted(lab_by_item):
            k, c = meta_by_item.get(iid), cand_by_id.get(iid)
            if not k or not c:
                stats["dropped_dup"]["H-stream(标签找不到候选)"] += 1
                continue
            lab = lab_by_item[iid]
            add(_item("H-stream", c["dedup_key"], c["title"], c["abstract"], "digest_original",
                      HUMAN_LABELS[lab["label"]], "blind_label", labeled_at=label_key.get("exported_at"),
                      notes=lab.get("comment", ""),
                      sample={"pool": k["pool"], "conf_bin": k["conf_bin"], "weight": k["weight"],
                              "prod_verdict": k["prod_verdict"], "prod_confidence": k["prod_confidence"]}))

    # R
    for r in stable:
        lib_e = next((e for e in lib if e.get("citekey") == r.get("citekey")), None)
        if not lib_e:
            stats["dropped_dup"]["R-stable(库内无此 citekey)"] += 1
            continue
        dk = lib_e["dedup_key"]
        man = man_by_key.get(dk)
        ab = (man["abstract"] if man and man["abstract"].strip() else abstracts.get(dk, ""))
        add(_item("R-stable", dk, lib_e.get("title") or r.get("title"), ab,
                  "bundle" if (man and man["abstract"].strip()) else "abstracts_cache", None, "llm_audit",
                  citekey=lib_e.get("citekey"),
                  notes="两轮均拒；仅回归观察，不当金标准", old=r.get("old"), reason=r.get("reason")))

    order = {s: i for i, s in enumerate(STRATA)}
    out = sorted(items.values(), key=lambda it: (order[it["stratum"]], it["item_id"]))
    stats["dropped_dup"] = dict(stats["dropped_dup"])
    return out, stats, review


def gold_bytes(items: List[Dict[str, Any]]) -> bytes:
    """gold 的规范字节串：键排序、一行一条、末尾换行——sha256 据此计算，保证可复现。"""
    return "".join(json.dumps(it, ensure_ascii=False, sort_keys=True) + "\n" for it in items).encode("utf-8")


def gold_counts(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    c: Dict[str, Any] = {s: {"n": 0, "dev": 0, "test": 0, "no_abstract": 0} for s in STRATA}
    for it in items:
        d = c[it["stratum"]]
        d["n"] += 1
        d[it["split"]] += 1
        d["no_abstract"] += it["abstract_source"] == "none"
    return c
