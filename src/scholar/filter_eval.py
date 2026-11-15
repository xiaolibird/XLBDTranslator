# -*- coding: utf-8 -*-
"""筛选召回评测集（金标准）构造与指标纯函数。设计：docs/decisions/trust_hardening_design_2026-10-13.md §5。

本模块**不调用任何 LLM、不联网**，只读已有产物（札记索引、sidecar、digest、稿件 bib），
产出五类东西：

1. 金标准 `gold_v{N}.jsonl`（分层：P1-in / P1-out / M-core / M-side / F-override / H-stream / S-stream / R-stable；
   S-stream = 入库确认弹窗持续产生的标签，WO-8b，按确认日期滚动窗口并入）；
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
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ._citekey_utils import _norm_title, dedup_key_fields

# ---------------- 常量 ----------------

SEED = 20261013                  # 抽样/混洗的固定种子（可由 CLI 覆盖，覆盖值会写进 meta）
TEST_FRACTION_PCT = 30           # sha256(item_id) % 100 < 30 → test，其余 dev；不随 gold 版本变
ABSTRACT_MAX_CHARS = 800         # 与生产 prompt 一致；prod 口径的 400 截断由评测执行器（WO-7）负责
FILTER_VERSION_PREFIX = "filter-v3"

STRATA = ("P1-in", "P1-out", "M-core", "M-side", "F-override", "H-stream", "S-stream", "R-stable")
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


# ---------------- S-stream：入库确认标签（WO-8b，设计 §12.2）----------------

def stream_gold_items(labels: Sequence[Dict[str, Any]], *, as_of: Optional[datetime] = None,
                      window_weeks: int = 12, include_default_accepted: bool = False) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """把 stream_labels.jsonl 的行变成 S-stream 层 gold 条目。纯函数。

    标签语义：user_decision=keep → positive，drop → negative。
    窗口：confirmed_at ∈ (as_of - window_weeks 周, as_of]；as_of 缺省取标签里最晚的确认时间
    （由数据决定，同一份标签文件重建得同一份 gold）。研究兴趣在漂移，旧标签会过时，所以滚动。

    抽样权重还原漏判：lowconf_exclude 是整池全取（权重 1），audit 是从高置信 EXCLUDE 池随机抽
    （权重 = 池/抽中），二者都带在 sample.weight 里，compute_metrics 的 s_stream 据此还原
    「生产 EXCLUDE 中其实该收」的总体比例。

    user_decision_source=default_accepted 的行（用户在汇总核对里没改过、照单全收默认勾选）**默认排除**：
    它不是用户的主动判断，混进来会把筛选器自己的默认当成「人也同意」。include_default_accepted=True 可纳入；
    旧行（无该字段）按主动判断处理。

    ⚠️ 锚定偏倚：INCLUDE/MAYBE 类条目默认勾选，用户「没取消」不等于「认真确认过」，所以
    s_stream 里「筛选器收了但用户丢掉」的比例是**下界**；EXCLUDE 类默认不勾，用户勾上是主动行为，
    漏判估计不受此影响。条目上记 anchored=True 以便分开看。
    """
    rows = [r for r in labels if r.get("user_decision") in ("keep", "drop") and r.get("dedup_key")]
    n_default = sum(1 for r in rows if r.get("user_decision_source") in ("default_accepted", "auto_agent"))
    if not include_default_accepted:
        # auto_agent（自动筛选 LLM 代勾）同样不是人的判断：让筛选器拿 LLM 的口径给自己判分是循环论证
        rows = [r for r in rows if r.get("user_decision_source") not in ("default_accepted", "auto_agent")]
    stamps = {}
    for r in rows:
        t = _parse_ts(r.get("confirmed_at"))
        if t is not None:
            stamps[id(r)] = t
    if as_of is None and stamps:
        as_of = max(stamps.values())
    lo = (as_of - timedelta(weeks=window_weeks)) if as_of else None
    items, dropped_window = [], 0
    for r in rows:
        t = stamps.get(id(r))
        if t is None or (lo is not None and t <= lo) or (as_of is not None and t > as_of):
            dropped_window += 1
            continue
        fd = r.get("filter_decision") or "INCLUDE"
        conf = r.get("filter_confidence")
        items.append(_item(
            "S-stream", r["dedup_key"], r.get("title"), r.get("abstract"), "digest_original",
            "positive" if r["user_decision"] == "keep" else "negative", "user_confirm",
            labeled_at=r.get("confirmed_at"),
            notes="category={}".format(r.get("category")),
            category=r.get("category"), anchored=bool(r.get("default_checked")),
            override=bool(r.get("override")), prompt_version=r.get("prompt_version"),
            sample={"pool": fd, "conf_bin": conf_bin(conf) if fd == "EXCLUDE" else "-",
                    "weight": float(r.get("sample_weight") or 1.0), "prod_verdict": fd,
                    "prod_confidence": conf, "category": r.get("category")}))
    items.sort(key=lambda it: it["item_id"])         # 与输入顺序无关
    stats = {"n_labels": len(rows), "n_default_accepted": n_default,
             "default_accepted_included": bool(include_default_accepted), "n_in_window": len(items), "n_out_of_window": dropped_window,
             "window_weeks": window_weeks, "as_of": as_of.isoformat(timespec="seconds") if as_of else None}
    return items, stats


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
               seed: int = SEED, labeled_at: Optional[str] = None,
               stream_labels: Optional[Sequence[Dict[str, Any]]] = None,
               stream_as_of: Optional[datetime] = None, stream_window_weeks: int = 12,
               stream_include_default: bool = False
               ) -> Tuple[List[Dict[str, Any]], Dict[str, Any], List[Dict[str, Any]]]:
    """纯函数：把各路输入装配成 (gold 条目, 构造统计, bib 审阅记录)。同输入同输出。

    层间去重优先级 P1 > M > F > H > S > R：同一 dedup_key 只在最先命中的层出现（落选者计数写进统计）。
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

    # S（入库确认标签；不给 stream_labels 则整层缺席，旧版本 gold 不受影响）
    if stream_labels:
        s_items, s_stats = stream_gold_items(stream_labels, as_of=stream_as_of, window_weeks=stream_window_weeks,
                                           include_default_accepted=stream_include_default)
        stats["s_stream"] = s_stats
        for it in s_items:
            add(it)

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


# ======================================================================
# 评测执行器（WO-7，设计 §5.5/§5.6）
#
# 复用生产的 ScholarWorkflow._filter_by_llm（不另写 prompt 组装），把 gold 条目装成 PaperSegment
# 混洗后按生产批大小送审；重复 N 次取更差；按「实际应答的 provider:served_model」拆分统计。
# 本段本身不联网、不调 LLM：LLM 调用全部发生在传入的 workflow 实例里（测试用桩替换 _call_llm）。
# ======================================================================

# 生产口径摘要长度：D11 实测近 6 次运行 1233 条送审摘要中位数 407 字符，取 400。
# （prompt 本身截 800；full 模式即 gold 里存的 800 截断版，作对照臂）
PROD_ABSTRACT_CHARS = 400
ABSTRACT_MODES = ("prod", "full")
DEFAULT_REPEATS = 2
SMOKE_PER_GROUP = 20             # --smoke：每组固定抽 20 条（3 组共 60 条 = 3 批）
SMOKE_GROUPS = (("P1", ("P1-in", "P1-out")), ("M-core", ("M-core",)), ("H-stream", ("H-stream",)))
# 默认入跑的层：M-side/R-stable 无标签、只做观察，要看须显式 --strata 指定
DEFAULT_RUN_STRATA = ("P1-in", "P1-out", "M-core", "F-override", "H-stream", "S-stream")
# --split 只作用于大层（M-core 的 test 是主门槛；P1/H/F 规模小，一律全收）
SPLIT_STRATA = ("M-core", "M-side")
# 这些层的「标签/判定」来自生产 LLM 裁决；decision_source 为 manual 系列的条目其裁决不是 LLM 给的，剔除
SKIP_DECISION_SOURCES = ("manual", "manual_pick")
SKIP_SOURCE_STRATA = ("H-stream", "R-stable")
F_OVERRIDE_MIN_N = 10            # F-override 累积到此数才进门槛（设计 §5.3）
VERDICT_ORDER = {"EXCLUDE": 0, "FALLBACK": 0, "MAYBE": 1, "INCLUDE": 2}
# 设计 §5.6 的建议门槛，**仅供报告对照**；正式常量由 WO-8 迁入 thresholds.py，那之前此处不具放行效力
REFERENCE_GATES = {
    "p1_keep_min": 0.95, "core_keep_min": 0.92, "core_keep_wilson_min": 0.88,
    "false_include_max": 0.15, "stream_miss_max": 0.05, "regression_max": 0.02, "flip_max": 0.15,
}
RUN_SCHEMA = 1


def truncate_abstract(text: Optional[str], mode: str) -> str:
    """送审摘要：prod 截 PROD_ABSTRACT_CHARS（对齐生产实测中位数），full 保持 gold 里的 800。"""
    if mode not in ABSTRACT_MODES:
        raise ValueError("abstract-mode 只能是 {}，收到 {!r}".format(ABSTRACT_MODES, mode))
    t = (text or "")
    return t[:PROD_ABSTRACT_CHARS] if mode == "prod" else t[:ABSTRACT_MAX_CHARS]


def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s)).replace(tzinfo=None)
    except ValueError:
        return None


def load_gold(edir, version: int) -> Tuple[List[Dict[str, Any]], Dict[str, Any], str]:
    """读 gold_v{N}.jsonl 与 meta，返回 (条目, meta, 文件 sha256)。meta 缺失直接报错——没有登记时间戳就不许跑。"""
    gp = Path(edir) / "gold_v{}.jsonl".format(version)
    mp = Path(edir) / "gold_v{}.meta.json".format(version)
    if not gp.exists() or not mp.exists():
        raise ValueError("找不到 gold v{}（{} / {}）；先 build-gold".format(version, gp.name, mp.name))
    blob = gp.read_bytes()
    items = [json.loads(l) for l in blob.decode("utf-8").splitlines() if l.strip()]
    return items, json.loads(mp.read_text(encoding="utf-8")), _sha256_bytes(blob)


def check_run_preconditions(meta: Dict[str, Any], gold_sha256: str, items: Sequence[Dict[str, Any]],
                            now: Optional[datetime] = None) -> None:
    """运行前闸：gold 内容与 meta 一致；manual 登记时间戳（及盲标时间）早于本次运行。违反即 ValueError。

    先登记后评测是防事后找补的核心约束（设计 §5.3）：登记晚于运行 = 看过结果才分支线，数字作废。
    """
    now = now or datetime.now()
    if meta.get("gold_sha256") != gold_sha256:
        raise ValueError("gold 文件内容与 meta 记录的 sha256 不一致（被改过？），拒绝运行")
    reg = meta.get("registry") or {}
    b_at = _parse_ts(reg.get("batches_registered_at"))
    if b_at is None:
        raise ValueError("gold meta 没有 manual 批次登记时间戳（registry.batches_registered_at），拒绝运行")
    stamps = [("批次登记", b_at)]
    if reg.get("papers_registered_at"):
        p_at = _parse_ts(reg["papers_registered_at"])
        if p_at is None:
            raise ValueError("按篇登记时间戳无法解析：{!r}".format(reg["papers_registered_at"]))
        stamps.append(("按篇登记", p_at))
    for it in items:
        if it.get("stratum") == "H-stream" and it.get("labeled_at"):
            t = _parse_ts(it["labeled_at"])
            if t is not None:
                stamps.append(("盲标导出", t))
    for name, t in stamps:
        if t >= now:
            raise ValueError("{}时间 {} 不早于本次运行 {}，拒绝运行（先登记/标注，后评测）".format(
                name, t.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")))


def select_items(items: Sequence[Dict[str, Any]], *, split: str = "all",
                 strata: Optional[Sequence[str]] = None, smoke: bool = False,
                 seed: int = SEED) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """按层/切分/冒烟挑出本次要跑的条目，返回 (条目, 选择统计)。纯函数，同输入同输出。

    - strata 缺省 DEFAULT_RUN_STRATA；split 只作用于 SPLIT_STRATA 中的层；
    - decision_source 为 manual/manual_pick 的 H/R 层条目剔除（其裁决不是生产 LLM 给的）；
    - smoke：每组按 _rank_key 固定抽 SMOKE_PER_GROUP 条（与输入顺序、批次无关），组内再按 strata 过滤。
    """
    if split not in ("test", "dev", "all"):
        raise ValueError("split 只能是 test/dev/all")
    use = tuple(strata) if strata else DEFAULT_RUN_STRATA
    bad = [s for s in use if s not in STRATA]
    if bad:
        raise ValueError("未知层：{}".format(bad))
    stat = {"skipped_manual_source": 0, "skipped_split": 0}
    pool = []
    for it in items:
        if it["stratum"] not in use:
            continue
        if it["stratum"] in SKIP_SOURCE_STRATA and it.get("decision_source") in SKIP_DECISION_SOURCES:
            stat["skipped_manual_source"] += 1
            continue
        if split != "all" and it["stratum"] in SPLIT_STRATA and it["split"] != split:
            stat["skipped_split"] += 1
            continue
        pool.append(it)
    if smoke:
        picked = []
        for tag, members in SMOKE_GROUPS:
            grp = sorted((i for i in pool if i["stratum"] in members),
                         key=lambda i: _rank_key(seed, "smoke:" + tag, i["item_id"]))
            picked.extend(grp[:SMOKE_PER_GROUP])
        pool = picked
    pool = sorted(pool, key=lambda i: i["item_id"])
    stat["n"] = len(pool)
    return pool, stat


def shuffled_order(items: Sequence[Dict[str, Any]], seed: int) -> List[Dict[str, Any]]:
    """确定性混洗：阳性与阴性打散，避免整批全是同一类（批内对比效应会扭曲判定）。"""
    return sorted(items, key=lambda i: _rank_key(seed, "shuffle", i["item_id"]))


def build_segment(item: Dict[str, Any], segment_id: int, abstract_mode: str):
    """gold 条目 → PaperSegment（生产同口径：标题 + 英文原始摘要；gold 没存 journal，置空）。"""
    from .schema import PaperMetadata, PaperSegment
    pid = "eval_{}".format(item["item_id"])
    return PaperSegment(
        segment_id=segment_id, paper_id=pid,
        original_abstract=truncate_abstract(item.get("abstract"), abstract_mode),
        metadata=PaperMetadata(paper_id=pid, title=item.get("title") or "", source_email_id="filter_eval"))


def verdict_of(seg) -> Dict[str, Any]:
    """从 _filter_by_llm 处理后的 segment 取本条裁决。关键词回退/无裁决记 FALLBACK（按丢弃计，且使本次运行不可用于认证）。"""
    d = seg.filter_decision
    if d is None or d.stage != "llm_judge" or d.decision not in ("INCLUDE", "MAYBE", "EXCLUDE"):
        return {"verdict": "FALLBACK", "confidence": None, "stage": getattr(d, "stage", None),
                "provider": getattr(d, "provider", None), "served_model": getattr(d, "served_model", None),
                "fell_back": getattr(d, "fell_back", None)}
    return {"verdict": d.decision, "confidence": d.confidence, "stage": d.stage, "provider": d.provider,
            "served_model": d.served_model, "fell_back": d.fell_back}


def model_key(provider: Optional[str], served_model: Optional[str]) -> str:
    """按实际应答者记账的键。served_model 为 None（未认证）记 unknown，不退回请求值。"""
    return "{}:{}".format(provider or "unknown", served_model or "unknown")


def neighbor_exclusion(item: Dict[str, Any], cit_by_dedup: Optional[Dict[str, str]] = None) -> set:
    """近邻自排除集：被测条目自己的 dedup_key 与 citekey（gold 里的 + 库索引里按 dedup_key 查到的）。"""
    out = {item.get("dedup_key"), item.get("citekey"), (cit_by_dedup or {}).get(item.get("dedup_key"))}
    return {x for x in out if x}


def run_once(wf, items: Sequence[Dict[str, Any]], *, seed: int, abstract_mode: str, rag_on: bool,
             id_base: int = 0, cit_by_dedup: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """跑一遍：混洗 → 装 segment → wf._filter_by_llm → 逐条取裁决。返回与 items 顺序无关的逐条记录列表。

    每遍重置 wf 的裁决累积状态与近邻缓存，segment_id 用独立号段（id_base 起），两遍互不串扰。
    rag_on=False 时置 _library_neighbors_unavailable，近邻恒为空（RAG 关闭对照臂）；
    rag_on=True 时给每条设近邻自排除集（D4：库内论文重审不得被判「与自己重复」）。
    """
    order = shuffled_order(items, seed)
    wf.excluded, wf.included_decisions, wf.undecided_segments = [], [], []
    wf._filter_fallback_count = 0
    wf._filter_retry_recovered = 0
    wf._filter_served_models = {}
    wf._library_neighbors_cache = {}
    wf._library_neighbors_unavailable = not rag_on
    segs, by_seg = [], {}
    wf._neighbor_exclude = {}
    for k, it in enumerate(order):
        seg = build_segment(it, id_base + k, abstract_mode)
        segs.append(seg)
        by_seg[seg.segment_id] = (k, it)
        if rag_on:
            wf._neighbor_exclude[seg.segment_id] = neighbor_exclusion(it, cit_by_dedup)
    try:
        wf._filter_by_llm(segs)
    finally:
        wf._neighbor_exclude = {}
    from .workflow import FILTER_BATCH_SIZE
    # 想开 RAG 却中途降级（向量库缺失/陈旧/Ollama 不可达）：这一遍其实是 RAG 关闭臂，必须在记录里留痕
    degraded = bool(rag_on and wf._library_neighbors_unavailable)
    out = []
    for seg in segs:
        k, it = by_seg[seg.segment_id]
        v = verdict_of(seg)
        v.update(item_id=it["item_id"], batch_index=k // FILTER_BATCH_SIZE, rag_degraded=degraded)
        out.append(v)
    return out


def _worst(vs: Sequence[str]) -> str:
    return worse_of_repeats([[v] for v in vs])[0]


def build_entries(items: Sequence[Dict[str, Any]], repeat_results: Sequence[Sequence[Dict[str, Any]]]
                  ) -> List[Dict[str, Any]]:
    """把各遍逐条记录并成每条目一行：verdicts 按遍排列，model 为各遍应答者的并（不同则标 mixed）。"""
    by_rep = [{r["item_id"]: r for r in rep} for rep in repeat_results]
    entries = []
    for it in sorted(items, key=lambda i: i["item_id"]):
        reps = [m[it["item_id"]] for m in by_rep if it["item_id"] in m]
        keys = {model_key(r["provider"], r["served_model"]) for r in reps if r["verdict"] != "FALLBACK"}
        e = {"item_id": it["item_id"], "stratum": it["stratum"], "label": it.get("label"), "split": it.get("split"),
             "title": it.get("title"), "abstract_source": it.get("abstract_source"), "origin": it.get("origin"),
             "sample": it.get("sample"), "batch": it.get("batch"), "anchored": it.get("anchored"),
             "verdicts": [r["verdict"] for r in reps],
             "confidence": [r["confidence"] for r in reps],
             "served_model": [model_key(r["provider"], r["served_model"]) if r["verdict"] != "FALLBACK" else None
                              for r in reps],
             "verified": [bool(r["served_model"]) for r in reps],
             "fell_back": [r["fell_back"] for r in reps],
             "rag_degraded": any(r.get("rag_degraded") for r in reps),
             "worst": _worst([r["verdict"] for r in reps]) if reps else "FALLBACK"}
        e["model"] = (next(iter(keys)) if len(keys) == 1 else ("mixed" if keys else "none"))
        entries.append(e)
    return entries


# ---------------- 指标汇总 ----------------

def _rate(k: int, n: int) -> Dict[str, Any]:
    p, lo, hi = wilson_interval(k, n)
    return {"k": k, "n": n, "p": (k / n if n else None), "lo": (lo if n else None), "hi": (hi if n else None)}


def _keep(entries: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    k, n = keep_rate([e["worst"] for e in entries])
    return _rate(k, n)


def _flip(entries: Sequence[Dict[str, Any]]) -> Optional[float]:
    es = [e for e in entries if len(e["verdicts"]) > 1]
    if not es:
        return None
    n_rep = min(len(e["verdicts"]) for e in es)
    return flip_rate([[e["verdicts"][r] for e in es] for r in range(n_rep)])


def _stream_metrics(s: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """S-stream（入库确认标签）的指标。n=0 时只返回 {"n": 0}。

    prod_miss：生产 EXCLUDE 的样本里用户勾上（=该收）的比例，按抽样权重还原总体
               （lowconf 全取权重 1，audit 权重 = 池/抽中）。EXCLUDE 默认不勾，勾上是主动行为，无锚定偏倚。
    prod_false_keep：生产收了（INCLUDE/MAYBE）而用户取消的比例——**下界**（默认勾选的锚定偏倚，见
               filter_eval.stream_gold_items 的注释）。
    now_keep_pos / now_false_include：本次复跑（当前 prompt/画像/模型）对用户 keep/drop 标签的表现。
    """
    if not s:
        return {"n": 0}
    sx = [e for e in s if (e.get("sample") or {}).get("prod_verdict") == "EXCLUDE"]
    wts = [float((e["sample"] or {}).get("weight") or 1.0) for e in sx]
    flags = [e["label"] == "positive" for e in sx]
    by_cat: Dict[str, List[bool]] = defaultdict(list)
    for e, f in zip(sx, flags):
        by_cat[(e["sample"] or {}).get("category") or "?"].append(f)
    sk = [e for e in s if (e.get("sample") or {}).get("prod_verdict") in ("INCLUDE", "MAYBE")]
    pos = [e for e in s if e["label"] == "positive"]
    neg = [e for e in s if e["label"] == "negative"]
    return {
        "n": len(s),
        "n_positive": len(pos), "n_negative": len(neg),
        "prod_miss": dict(_rate(sum(flags), len(sx)), weighted=(weighted_rate(flags, wts) if sx else None)),
        "prod_miss_by_category": {c: {"k": sum(v), "n": len(v)} for c, v in sorted(by_cat.items())},
        "prod_false_keep": _rate(sum(1 for e in sk if e["label"] == "negative"), len(sk)),
        "now_keep_pos": _keep(pos),
        "now_false_include": _rate(sum(1 for e in neg if e["worst"] == "INCLUDE"), len(neg)),
        "n_anchored": sum(1 for e in s if (e.get("anchored"))),
    }


def compute_metrics(entries: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """设计 §5.2/§5.6 的指标。每条取各遍中更差者（worst）；所有比例带 Wilson 95% 区间。

    只有有人类/行为标签的层进门槛类指标；M-side 与 R-stable 只给裁决分布。
    """
    E = list(entries)
    positive = [e for e in E if e["label"] == "positive"]
    p1 = [e for e in E if e["stratum"] in ("P1-in", "P1-out") and e.get("origin") != "auto"]
    p1_auto = [e for e in E if e["stratum"] == "P1-in" and e.get("origin") == "auto"]
    core = [e for e in E if e["stratum"] == "M-core"]
    f_over = [e for e in E if e["stratum"] == "F-override"]
    h = [e for e in E if e["stratum"] == "H-stream"]
    m: Dict[str, Any] = {"n_items": len(E)}
    m["p1_keep"] = _keep(p1)
    m["p1_out_keep"] = _keep([e for e in p1 if e["stratum"] == "P1-out"])
    m["p1_dropped"] = [{"item_id": e["item_id"], "title": e["title"], "stratum": e["stratum"],
                        "verdicts": e["verdicts"]} for e in p1 if e["worst"] == "EXCLUDE" or e["worst"] == "FALLBACK"]
    m["p1_auto_keep"] = _keep(p1_auto)                 # 幸存者偏倚，单列不进门槛
    m["core_test_keep"] = _keep([e for e in core if e["split"] == "test"])
    m["core_dev_keep"] = _keep([e for e in core if e["split"] == "dev"])
    m["core_dropped_test"] = [{"item_id": e["item_id"], "title": e["title"]}
                              for e in core if e["split"] == "test" and e["worst"] in ("EXCLUDE", "FALLBACK")]
    k_inc = sum(1 for e in positive if e["worst"] == "INCLUDE")
    m["include_recall"] = _rate(k_inc, len(positive))
    m["f_override_keep"] = dict(_keep(f_over), gated=len(f_over) >= F_OVERRIDE_MIN_N)
    neg = [e for e in h if e["label"] == "negative"]
    m["h_false_include"] = _rate(sum(1 for e in neg if e["worst"] == "INCLUDE"), len(neg))
    m["h_false_keep"] = _rate(sum(1 for e in neg if e["worst"] in ("INCLUDE", "MAYBE")), len(neg))
    # 生产漏判率：生产 EXCLUDE 样本里人判「应收」的比例，按抽样权重还原总体；边缘不进分母
    hx = [e for e in h if (e.get("sample") or {}).get("prod_verdict") == "EXCLUDE"
          and e["label"] in ("positive", "negative")]
    wts = [float((e["sample"] or {}).get("weight") or 1.0) for e in hx]
    flags = [e["label"] == "positive" for e in hx]
    m["h_stream_miss"] = dict(_rate(sum(flags), len(hx)), weighted=(weighted_rate(flags, wts) if hx else None))
    bins: Dict[str, List[bool]] = defaultdict(list)
    for e, f in zip(hx, flags):
        bins[(e["sample"] or {}).get("conf_bin") or "?"].append(f)
    m["h_stream_miss_by_conf_bin"] = {b: {"k": sum(v), "n": len(v)} for b, v in sorted(bins.items())}
    m["h_borderline_n"] = sum(1 for e in h if e["label"] == "borderline")
    # 本次复跑与生产裁决的一致度（观察用）：生产 EXCLUDE 而本次保留 / 生产保留而本次 EXCLUDE
    m["h_vs_prod"] = {
        "prod_exclude_now_keep": sum(1 for e in h if (e.get("sample") or {}).get("prod_verdict") == "EXCLUDE"
                                      and e["worst"] in ("INCLUDE", "MAYBE")),
        "prod_keep_now_exclude": sum(1 for e in h if (e.get("sample") or {}).get("prod_verdict") in ("INCLUDE", "MAYBE")
                                      and e["worst"] == "EXCLUDE")}
    m["s_stream"] = _stream_metrics([e for e in E if e["stratum"] == "S-stream"])
    m["no_abstract_keep"] = _keep([e for e in positive if e.get("abstract_source") == "none"])
    m["flip"] = {"all": _flip(E), **{s: _flip([e for e in E if e["stratum"] == s]) for s in STRATA
                                     if any(e["stratum"] == s for e in E)}}
    m["verdict_counts"] = {s: dict(Counter(e["worst"] for e in E if e["stratum"] == s))
                           for s in STRATA if any(e["stratum"] == s for e in E)}
    m["n_fallback_items"] = sum(1 for e in E if "FALLBACK" in e["verdicts"])
    return m


def reference_check(m: Dict[str, Any]) -> Dict[str, Optional[bool]]:
    """对照 REFERENCE_GATES 的逐项达标情况；n=0 记 None。**仅供报告，不是放行判定**（WO-8 才有）。"""
    G = REFERENCE_GATES

    def le(v, lim):
        return None if v is None else v <= lim

    def ge(v, lim):
        return None if v is None else v >= lim
    return {
        "p1_keep": ge(m["p1_keep"]["p"], G["p1_keep_min"]),
        "core_keep_point": ge(m["core_test_keep"]["p"], G["core_keep_min"]),
        "core_keep_wilson": ge(m["core_test_keep"]["lo"], G["core_keep_wilson_min"]),
        "false_include": le(m["h_false_include"]["p"], G["false_include_max"]),
        "stream_miss": le(m["h_stream_miss"]["weighted"], G["stream_miss_max"]),
        "flip(仅告警)": le(m["flip"]["all"], G["flip_max"]),
    }


def summarize(entries: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """整体 + 按实际应答模型拆分的汇总，并判定本次运行能否用于认证（必须单一已验证模型、无回退、无关键词兜底）。"""
    E = list(entries)
    models = sorted({e["model"] for e in E})
    by_model = {mk: compute_metrics([e for e in E if e["model"] == mk]) for mk in models}
    reasons = []
    real = [mk for mk in models if mk not in ("none",)]
    if len(real) > 1:
        reasons.append("多于一个应答模型（{}）：回退发生，需按模型拆分，认证无效".format("、".join(models)))
    if "mixed" in models:
        reasons.append("同一条目两遍由不同模型应答（mixed）")
    if any(mk.endswith(":unknown") for mk in real):
        reasons.append("存在 served_model 未知（未验证）的应答")
    if any(not all(e["verified"]) for e in E if e["model"] != "none"):
        reasons.append("存在 decision_model_verified=false 的应答")
    nf = sum(1 for e in E if "FALLBACK" in e["verdicts"])
    if nf:
        reasons.append("{} 条出现关键词回退/无裁决（FALLBACK，按丢弃计）".format(nf))
    if any(e.get("rag_degraded") for e in E):
        reasons.append("RAG 近邻检索中途降级（向量库不可用），实际是 RAG 关闭臂，与 rag_mode=on 的认证键不符")
    return {"overall": compute_metrics(E), "reference": reference_check(compute_metrics(E)),
            "models": models, "by_model": by_model, "not_certifiable_reasons": reasons}


def paired_between_runs(base: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """两次运行在**共同条目**上的配对比较（回归检查 / 两条对照臂的差异）。用各自的 worst 裁决。"""
    b = {e["item_id"]: e for e in base["entries"]}
    n = {e["item_id"]: e for e in new["entries"]}
    common = sorted(set(b) & set(n))
    res = paired_regression([b[i]["worst"] for i in common], [n[i]["worst"] for i in common])
    res["lost_items"] = [{"item_id": i, "title": n[i]["title"]} for i in common
                         if b[i]["worst"] != "EXCLUDE" and n[i]["worst"] == "EXCLUDE"]
    res["n_only_base"], res["n_only_new"] = len(set(b) - set(n)), len(set(n) - set(b))
    return res


def run_eval(wf, items: Sequence[Dict[str, Any]], *, repeats: int = DEFAULT_REPEATS, seed: int = SEED,
             abstract_mode: str = "prod", rag_on: bool = True,
             cit_by_dedup: Optional[Dict[str, str]] = None, on_repeat=None) -> List[Dict[str, Any]]:
    """跑 repeats 遍（每遍种子 seed+r，混洗不同），返回每遍逐条记录。on_repeat(r, 记录) 供落盘断点。"""
    if repeats < 1:
        raise ValueError("repeats 至少 1")
    reps = []
    for r in range(repeats):
        rep = run_once(wf, items, seed=seed + r, abstract_mode=abstract_mode, rag_on=rag_on,
                       id_base=(r + 1) * 1_000_000, cit_by_dedup=cit_by_dedup)
        reps.append(rep)
        if on_repeat:
            on_repeat(r, reps)
    return reps


# ---------------- 评测覆盖告警（WO-8b：只告警，不拦截）----------------

def fingerprint_coverage(edir, fingerprint: str) -> Dict[str, Any]:
    """当前筛选器指纹（prompt 版本 + 模板/维度/画像哈希）有没有至少一次**完整、非冒烟**的评测记录。

    只读 runs/*.json 的 config；不调 LLM、不联网。runs 目录不存在（CI / 新机器——评测数据 gitignore，
    不随仓库走）记 runs_dir_exists=False，调用方据此跳过而不是误报。
    """
    rd = Path(edir) / "runs"
    out: Dict[str, Any] = {"fingerprint": fingerprint, "runs_dir_exists": rd.is_dir(),
                           "n_runs_total": 0, "run_ids": [], "has_record": False}
    if not rd.is_dir():
        return out
    for f in sorted(rd.glob("run_*.json")):
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out["n_runs_total"] += 1
        cfg = r.get("config") or {}
        if cfg.get("prompt_fingerprint") == fingerprint and r.get("status") == "complete" and not cfg.get("smoke"):
            out["run_ids"].append(r.get("run_id") or f.stem)
    out["has_record"] = bool(out["run_ids"])
    return out


# ---------------- 报告 ----------------

def _fmt_rate(r: Dict[str, Any]) -> str:
    if not r["n"]:
        return "n=0（无样本）"
    return "{:.1%} ({}/{})  Wilson95% [{:.1%}, {:.1%}]".format(r["p"], r["k"], r["n"], r["lo"], r["hi"])


def format_report(run: Dict[str, Any], baseline: Optional[Dict[str, Any]] = None,
                  show_verdicts: bool = False) -> str:
    """把 runs/{run_id}.json 渲染成文本。show_verdicts 打印逐条裁决（盲标完成前不得用；由 CLI 把关）。"""
    cfg, S = run["config"], run["summary"]
    L = ["运行 {}  状态={}".format(run["run_id"], run.get("status")),
         "gold={}  split={}  strata={}  smoke={}".format(cfg["gold_version"], cfg["split"], ",".join(cfg["strata"]),
                                                       cfg["smoke"]),
         "指纹={}  请求模型={}  rag={}  摘要口径={}  批大小={}  重复={}  种子={}".format(
             cfg["prompt_fingerprint"], cfg["requested_model"], "on(自排除)" if cfg["rag_on"] else "off",
             cfg["abstract_mode"], cfg["batch_size"], cfg["repeats"], cfg["seed"]),
         "条目 {}；跳过 {}".format(run["n_items"], json.dumps(run["selection"], ensure_ascii=False))]
    certifiable = not S["not_certifiable_reasons"] and not cfg["smoke"] and cfg["repeats"] >= 2 \
        and run.get("status") == "complete"
    L.append("可用于认证：{}".format("是" if certifiable else "否"))
    for why in S["not_certifiable_reasons"] + (["冒烟运行"] if cfg["smoke"] else []) + \
            (["重复次数 <2"] if cfg["repeats"] < 2 else []) + \
            ([] if run.get("status") == "complete" else ["运行未完成（断点）"]):
        L.append("  - 不可认证原因：{}".format(why))
    L.append("应答模型分布：{}".format(", ".join(
        "{}={}".format(mk, sum(1 for e in run["entries"] if e["model"] == mk)) for mk in S["models"])))

    def block(title, m, ref=None):
        L.append("")
        L.append("== {} ==".format(title))
        L.append("P1 保留率（不含 auto 来源）  {}".format(_fmt_rate(m["p1_keep"])))
        for d in m["p1_dropped"]:
            L.append("    被丢 P1：{}  [{}] {}".format(d["title"], d["stratum"], d["verdicts"]))
        L.append("P1-in(auto 来源，幸存者偏倚)  {}".format(_fmt_rate(m["p1_auto_keep"])))
        L.append("M-core test 保留率        {}".format(_fmt_rate(m["core_test_keep"])))
        L.append("M-core dev 保留率         {}".format(_fmt_rate(m["core_dev_keep"])))
        for d in m["core_dropped_test"]:
            L.append("    被丢 M-core(test)：{}".format(d["title"]))
        L.append("INCLUDE 召回(全部阳性)    {}".format(_fmt_rate(m["include_recall"])))
        f = m["f_override_keep"]
        L.append("F-override 保留率         {}{}".format(_fmt_rate(f), "" if f["gated"] else "  （n<10，不进门槛）"))
        L.append("H 过度纳入(无关→INCLUDE)  {}".format(_fmt_rate(m["h_false_include"])))
        L.append("H 过度纳入(无关→INCL|MAYBE) {}".format(_fmt_rate(m["h_false_keep"])))
        hm = m["h_stream_miss"]
        L.append("H 生产漏判率(EXCLUDE 中应收) {}  加权还原={}".format(
            _fmt_rate(hm), "n/a" if hm["weighted"] is None else "{:.1%}".format(hm["weighted"])))
        L.append("    按置信度段（应收/样本）：{}".format(
            ", ".join("{}={}/{}".format(b, v["k"], v["n"]) for b, v in m["h_stream_miss_by_conf_bin"].items()) or "无"))
        L.append("    H 边缘 {} 条不进分母；复跑 vs 生产：{}".format(m["h_borderline_n"], m["h_vs_prod"]))
        sm = m.get("s_stream") or {"n": 0}
        if sm["n"]:
            pm = sm["prod_miss"]
            L.append("S 入库确认标签 {} 条（keep {} / drop {}；锚定 {} 条）".format(
                sm["n"], sm["n_positive"], sm["n_negative"], sm["n_anchored"]))
            L.append("    生产漏判率(EXCLUDE 中用户勾上) {}  加权还原={}".format(
                _fmt_rate(pm), "n/a" if pm["weighted"] is None else "{:.1%}".format(pm["weighted"])))
            L.append("      按类别（勾上/样本）：{}".format(", ".join(
                "{}={}/{}".format(c, v["k"], v["n"]) for c, v in sm["prod_miss_by_category"].items()) or "无"))
            L.append("    生产收了而用户取消（下界，默认勾选有锚定偏倚） {}".format(_fmt_rate(sm["prod_false_keep"])))
            L.append("    本次保留用户 keep 的比例 {}".format(_fmt_rate(sm["now_keep_pos"])))
            L.append("    本次把用户 drop 的判成 INCLUDE {}".format(_fmt_rate(sm["now_false_include"])))
        L.append("无摘要阳性保留率          {}".format(_fmt_rate(m["no_abstract_keep"])))
        L.append("翻转率：{}".format(", ".join(
            "{}={}".format(k, "n/a" if v is None else "{:.1%}".format(v)) for k, v in m["flip"].items())))
        for s, c in m["verdict_counts"].items():
            L.append("  {:<11}裁决分布（worst）：{}".format(s, c))
        if ref:
            L.append("对照建议门槛（仅参考，非放行判定）：{}".format(
                ", ".join("{}={}".format(k, {True: "达标", False: "未达标", None: "n/a"}[v]) for k, v in ref.items())))
    block("整体", S["overall"], S["reference"])
    if len(S["models"]) > 1:
        for mk, m in S["by_model"].items():
            block("按应答模型 {}".format(mk), m)
    if baseline is not None:
        pr = paired_between_runs(baseline, run)
        L.append("")
        L.append("== 配对比较：{} → {} ==".format(baseline["run_id"], run["run_id"]))
        L.append("共同条目 {}；保留率 {:.1%} → {:.1%}（差 {:+.1%}，参考上限 {:.0%}）；新增丢弃 {}，新增保留 {}".format(
            pr["n"], pr["keep_base"], pr["keep_new"], pr["delta"], REFERENCE_GATES["regression_max"],
            pr["lost"], pr["gained"]))
        for d in pr["lost_items"]:
            L.append("    新增丢弃：{}".format(d["title"]))
    if show_verdicts:
        L.append("")
        L.append("== 逐条裁决 ==")
        for e in run["entries"]:
            L.append("{:<10}{:<9}{:<11}{}  {}  {}".format(e["stratum"], e["label"] or "-", e["worst"], e["verdicts"],
                                                     e["model"], e["title"]))
    return "\n".join(L)


def assemble_run(run_id: str, config: Dict[str, Any], selection: Dict[str, Any],
                 items: Sequence[Dict[str, Any]], repeat_results: Sequence[Sequence[Dict[str, Any]]],
                 *, status: str, started_at: str, finished_at: Optional[str] = None) -> Dict[str, Any]:
    """把配置 + 逐遍结果装成 runs/{run_id}.json 的内容（逐条 + 汇总 + 配置键）。纯函数。

    断点（status=partial）时 repeat_results 可能少于 config.repeats 遍，汇总只基于已完成的遍。
    """
    entries = build_entries(items, repeat_results)
    return {"schema": RUN_SCHEMA, "run_id": run_id, "status": status, "started_at": started_at,
            "finished_at": finished_at, "config": config, "selection": selection, "n_items": len(items),
            "n_repeats_done": len(repeat_results), "entries": entries, "summary": summarize(entries),
            "llm_batches_ok": None}
