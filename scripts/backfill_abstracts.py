# -*- coding: utf-8 -*-
"""摘要回填：给 literature_index.json 的主条目抓英文摘要，产出 abstracts.json sidecar。

背景（2026-08-21 批，roadmap v2 P0）：paper 级向量文本只有 title+one_line，概念换述
检索 @1 仅 46.7%（见 docs/decisions/rag_bench_baseline_2026-08.md）。摘要在
literature_index / all_references 两处真理源覆盖率都是 0，必须回填。产物定位为
**不进 git 的可再生 sidecar**（output/ 整目录 gitignore，与向量库同级口径）：
    output/scholar_notes/abstracts.json
      {"generated_at": ..., "abstracts": {dedup_key: {abstract, source, fetched_at, ...}},
       "failures": {dedup_key: {reason, class, attempted_at}}}
消费方唯一入口是 embed_store.sync_store（自动发现同目录 sidecar，为每篇生成独立的
`ab:<citekey>` 厚召回 chunk：title+one_line+摘要[:800]；`p:` 瘦 chunk 不受影响），
本脚本只负责抓取落盘，不碰向量库。

数据源与顺序（2026-08-21 全库 census 实测：OpenAlex 1493 / +Crossref 30 / +PubMed 144
/ arXiv 63 / title.search 117，联合 ≈81.8%）：
  doi:        OpenAlex 批量(40/批) → Crossref 逐条 → PubMed 逐条
  arxiv:      arXiv API 批量
  title:/pmlr:/openreview:  OpenAlex title.search，命中标题相似度 ≥0.9 才收（守门防错配）

断点续跑：abstracts 段已有键默认跳过（--force 重抓）；failures 键每次重试。
启动时先做 dedup_key 漂移对账（_reconcile_drift）：索引重建会按当前键梯重算键，
enrich 后旧键（title:xxx）会漂成新键（doi:yyy），孤儿摘要按低层级候选键迁移回主条目。
喂厚生效不靠人工：com.xlbd.scholar-embed watcher 同时监视本产物，落盘即触发增量重嵌。
撤稿条目不抓（本就被踢出向量库，喂了也测不到）。
礼貌参数：Crossref/OpenAlex 带 mailto（external_email），NCBI 限 3 req/s。

用法（仓库根）：
    python scripts/backfill_abstracts.py --limit 20     # 试跑
    python scripts/backfill_abstracts.py                # 全量（约 10-20 分钟）
    python scripts/backfill_abstracts.py --stats        # 只看当前覆盖统计

退出码：0=完成；1=完成但有失败键（详见 failures）；2=索引/配置不可用。
"""
import argparse
import difflib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.paths import repo_path                      # noqa: E402
from src.scholar.settings import ScholarSettings             # noqa: E402
from src.scholar.notes_index import is_retracted             # noqa: E402
from src.scholar._citekey_utils import dedup_key_fields      # noqa: E402
from src.scholar.embed_store import ABSTRACTS_NAME, INDEX_NAME  # noqa: E402
from src.utils.logger import get_logger                      # noqa: E402

logger = get_logger(__name__)

UA = "XLBD-scholar-notes/1.0 (abstracts backfill)"
OPENALEX_BATCH = 40
ARXIV_BATCH = 40
TITLE_SIM_GATE = 0.9          # title.search 守门：抓错论文的摘要照样"非空"，覆盖率数字辨不出
NCBI_SLEEP = 0.35             # 无 key 3 req/s
POLITE_SLEEP = 0.15

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean(text: str) -> str:
    """剥 JATS/XML 标签 + 压空白（Crossref abstract 是 JATS，OpenAlex 重构无标签）。"""
    text = _TAG_RE.sub(" ", text or "")
    return _WS_RE.sub(" ", text).strip()


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9一-鿿]+", "", (t or "").lower())


def _fetch_json(url: str, tries: int = 4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 429 or e.code >= 500:
                time.sleep(3 * (i + 1))
                continue
            raise
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))
    return None


def _fetch_text(url: str, tries: int = 4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))
    return None


def _invert(inv) -> str:
    """OpenAlex abstract_inverted_index -> 原文顺序摘要。"""
    if not isinstance(inv, dict) or not inv:
        return ""
    n = max((max(pos) for pos in inv.values() if pos), default=-1) + 1
    words = [""] * n
    for w, poss in inv.items():
        for p in poss:
            if 0 <= p < n:
                words[p] = w
    return _WS_RE.sub(" ", " ".join(words)).strip()


# ---------------------------------------------------------------------------
# 各数据源
# ---------------------------------------------------------------------------

def openalex_doi_batch(dois, mailto):
    """{doi(小写): abstract}，一次 40 个 DOI。"""
    out = {}
    fil = "doi:" + "|".join(dois)
    url = ("https://api.openalex.org/works?per-page=50"
           "&select=doi,abstract_inverted_index&mailto={}&filter={}".format(
               urllib.parse.quote(mailto), urllib.parse.quote(fil, safe=":|.%/()<>-")))
    j = _fetch_json(url)
    for w in (j or {}).get("results", []):
        d = (w.get("doi") or "").replace("https://doi.org/", "").lower()
        abstract = _invert(w.get("abstract_inverted_index"))
        if d and abstract:
            out[d] = abstract
    return out


def crossref_doi(doi, mailto):
    url = "https://api.crossref.org/works/{}?mailto={}".format(
        urllib.parse.quote(doi, safe=""), urllib.parse.quote(mailto))
    j = _fetch_json(url)
    msg = (j or {}).get("message") or {}
    return _clean(msg.get("abstract") or "")


def pubmed_doi(doi):
    """esearch [AID] -> pmid -> efetch abstract。两次请求，NCBI 限速由调用方 sleep。"""
    q = urllib.parse.quote('"{}"[AID]'.format(doi))
    j = _fetch_json("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
                    "?db=pubmed&retmode=json&term=" + q)
    ids = ((j or {}).get("esearchresult") or {}).get("idlist") or []
    if len(ids) != 1:
        return ""       # 0 或多命中都不可信
    time.sleep(NCBI_SLEEP)
    xml = _fetch_text("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
                      "?db=pubmed&retmode=xml&id=" + ids[0])
    if not xml:
        return ""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return ""
    parts = []
    for ab in root.iter("AbstractText"):
        label = ab.get("Label")
        text = "".join(ab.itertext()).strip()
        if text:
            parts.append("{}: {}".format(label, text) if label else text)
    return _WS_RE.sub(" ", " ".join(parts)).strip()


def arxiv_batch(ids):
    """{arxiv_id(去版本): abstract}。export API 一次 40 个。"""
    out = {}
    url = ("http://export.arxiv.org/api/query?max_results={}&id_list={}".format(
        len(ids), ",".join(ids)))
    xml = _fetch_text(url)
    if not xml:
        return out
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return out
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for entry in root.findall("a:entry", ns):
        eid = (entry.findtext("a:id", "", ns) or "")
        m = re.search(r"abs/([^v]+(?:v\d+)?)", eid)
        summary = _WS_RE.sub(" ", entry.findtext("a:summary", "", ns) or "").strip()
        if m and summary:
            out[re.sub(r"v\d+$", "", m.group(1))] = summary
    return out


def openalex_title_search(title, mailto):
    """title.search 前 3 候选，规整标题相似度 ≥ TITLE_SIM_GATE 才收。
    返回 (abstract, matched_title, sim)；没过守门返回 ("", best_title, best_sim)。"""
    # title.search 的过滤值不能含逗号/竖线（OpenAlex filter 语法分隔符）
    safe_title = re.sub(r"[,|]", " ", title)[:300]
    url = ("https://api.openalex.org/works?per-page=3"
           "&select=title,abstract_inverted_index&mailto={}&filter=title.search:{}".format(
               urllib.parse.quote(mailto), urllib.parse.quote(safe_title)))
    j = _fetch_json(url)
    want = _norm_title(title)
    best = ("", "", 0.0)
    for w in (j or {}).get("results", []):
        got = w.get("title") or ""
        sim = difflib.SequenceMatcher(None, want, _norm_title(got)).ratio()
        abstract = _invert(w.get("abstract_inverted_index"))
        if sim > best[2]:
            best = (abstract, got, sim)
    abstract, got, sim = best
    if sim >= TITLE_SIM_GATE and abstract:
        return abstract, got, sim
    return "", got, sim


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp-{}".format(os.getpid()))
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(str(tmp), str(path))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _reconcile_drift(papers, store) -> int:
    """dedup_key 漂移对账，返回迁移/清理的键数（>0 时调用方需落盘）。

    索引每次重建都按当前键梯重算 dedup_key（notes_index.py，recompute_entry_key），
    元数据 enrich 后键会升级（Crossref 补上 DOI：title:xxx → doi:yyy）；而本 sidecar
    的键是抓取时冻结的。不对账则旧键摘要成孤儿、新键在 chunks_from_index 查空 →
    该篇 ab: 厚 chunk 下次同步被静默删掉，且没有任何自动入口按新键补抓。

    迁移判据：孤儿键 == 某**唯一**条目的低层级候选键（按同一把键梯逐层去掉高层
    字段重算出 arxiv:/pmlr:/openreview:/title:）。撞同一候选键视为不可判，
    不迁移——新键不在 abstracts 里，会作为缺失键进本次 todo 正常重抓。撞键判定
    覆盖**全索引**条目（含 duplicate/撤稿：它们不做迁移目标，但孤儿键可能本来就是
    它们的旧键，只看主条目会把「撞车」误判成「唯一」）。
    条目已从索引消失的孤儿键原样保留：留着无害（消费侧按当前键查，查不到它），
    删了反而丢掉断点缓存。

    title: 孤儿键额外过 citekey 闸门：title 键是派生量、非唯一身份——原条目整条
    移出索引后，另一篇规范化标题相同的论文会生成一模一样的候选键，从键上无法区分
    「同一篇漂移」与「另一篇撞名」。只有 sidecar 抓取时冻结的 citekey 与目标条目
    对上才敢迁；对不上（含老记录没存 citekey）原样保留，错留的代价是新键重抓一次
    网络请求，错迁的代价是把别人的摘要喂进向量库。arxiv:/pmlr:/openreview: 是
    出版方分配的唯一 id，同 id 即同篇，不设此闸。
    """
    current = {e.get("dedup_key") for e in papers
               if isinstance(e, dict) and e.get("dedup_key")}
    # 低层级候选键 -> 该条目当前键；撞键记 None（不可判）
    alt, owner = {}, {}
    for e in papers:
        if not (isinstance(e, dict) and e.get("dedup_key")):
            continue
        eligible = not e.get("duplicate_of") and not is_retracted(e)
        if eligible:
            owner[e["dedup_key"]] = e
        cands = {dedup_key_fields(None, e.get("arxiv_id"), e.get("title"), url=e.get("url")),
                 dedup_key_fields(None, None, e.get("title"), url=e.get("url")),
                 dedup_key_fields(None, None, e.get("title"))}
        for k in cands:
            if k == e["dedup_key"] or k.startswith("id:") or k in current:
                continue    # 候选键还被别的条目实占时不算孤儿映射
            if not eligible or (k in alt and alt[k] != e["dedup_key"]):
                alt[k] = None
            else:
                alt[k] = e["dedup_key"]

    changed = 0
    for old in [k for k in store["abstracts"] if k not in current]:
        new = alt.get(old)
        if not new:
            continue
        if old.startswith("title:"):
            ck = store["abstracts"][old].get("citekey")
            if not ck or ck != owner[new].get("citekey"):
                continue    # 身份证据不足，不迁（见 docstring 的 citekey 闸门）
        if new in store["abstracts"]:
            del store["abstracts"][old]     # 新键已单独抓过，旧键纯冗余
        else:
            entry = store["abstracts"].pop(old)
            entry["migrated_from"] = old
            store["abstracts"][new] = entry
            # 新键此前抓取失败留下的陈旧记录：迁移成功即失效。不清则永远留着——
            # 该键已在 abstracts 里，不会再进 todo，flush() 只对本轮抓到的键清 failures
            store["failures"].pop(new, None)
        store["failures"].pop(old, None)
        changed += 1
    # failures 段的孤儿键同理清掉：留着永远不会按旧键重试，新键会自然进 todo
    for old in [k for k in store["failures"] if k not in current and alt.get(k)]:
        del store["failures"][old]
        changed += 1
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description="摘要回填 -> abstracts.json sidecar")
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 个缺失键（0=不限，试跑用）")
    ap.add_argument("--force", action="store_true", help="已有摘要的键也重抓")
    ap.add_argument("--stats", action="store_true", help="只打印当前覆盖统计，不抓取")
    ap.add_argument("--config", default="config/scholar.env")
    args = ap.parse_args()

    cfg = repo_path(args.config)
    settings = ScholarSettings.from_env_file(cfg) if cfg.exists() else ScholarSettings()
    notes_dir = repo_path(settings.processing.notes_dir)
    index_path = notes_dir / INDEX_NAME
    out_path = notes_dir / ABSTRACTS_NAME
    mailto = settings.processing.zotero_email or settings.processing.external_email or ""
    if not mailto:
        logger.warning("未配置 external_email，Crossref/OpenAlex 礼貌参数为空（仍可运行）")
        mailto = "unknown@example.org"

    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception as e:
        print("读索引失败：{}".format(e), file=sys.stderr)
        return 2

    main_entries = [e for e in index.get("papers") or []
                    if isinstance(e, dict) and not e.get("duplicate_of")
                    and e.get("dedup_key") and not is_retracted(e)]

    store = {"generated_at": None, "abstracts": {}, "failures": {}}
    if out_path.exists():
        try:
            store = json.loads(out_path.read_text(encoding="utf-8"))
            store.setdefault("abstracts", {})
            store.setdefault("failures", {})
        except Exception as e:
            print("现有 {} 损坏（{}），拒绝覆盖——请先人工处理".format(out_path, e), file=sys.stderr)
            return 2

    if args.stats:
        total = len(main_entries)
        have = sum(1 for e in main_entries if e["dedup_key"] in store["abstracts"])
        by_src = {}
        for v in store["abstracts"].values():
            by_src[v.get("source", "?")] = by_src.get(v.get("source", "?"), 0) + 1
        print("主条目（不含撤稿）：{}；已有摘要：{}（{:.1%}）".format(total, have, have / total if total else 0))
        print("按来源：{}".format(json.dumps(by_src, ensure_ascii=False)))
        print("失败键：{}".format(len(store["failures"])))
        return 0

    # 漂移对账必须先于 todo 计算：迁移后的键不再算「缺失」，免得白白重抓
    drifted = _reconcile_drift(index.get("papers") or [], store)
    if drifted:
        store["generated_at"] = _now()
        _atomic_write(out_path, store)
        logger.info("dedup_key 漂移对账：迁移/清理 {} 个孤儿键", drifted)

    todo = [e for e in main_entries
            if args.force or e["dedup_key"] not in store["abstracts"]]
    if args.limit > 0:
        todo = todo[:args.limit]
    logger.info("主条目 {} 个，本次待抓 {} 个", len(main_entries), len(todo))

    got, failures = {}, {}
    flushed = {"n": 0}
    # 抓取时冻结 citekey：_reconcile_drift 迁移 title: 孤儿键的唯一身份证据
    ck_by_key = {e["dedup_key"]: e.get("citekey") for e in main_entries}

    def record(key, abstract, source, extra=None):
        # 字面 "\n"（两字符转义序列，arXiv 摘要经 OpenAlex 常见）与真实空白统一压平
        abstract = _WS_RE.sub(" ", abstract.replace("\\n", " ")).strip()
        if len(abstract) < 80:
            # 学位论文/机构库条目在 OpenAlex 的"摘要"常是元数据垃圾串（实测抓到过
            # "International audience"、"Ph.D"、"2025"）——真摘要不可能这么短。
            # 拒收记 failures，别让垃圾进 ab: 向量（污染检索还压低 in_library 自回找分）。
            failures[key] = {"reason": "摘要过短疑似元数据垃圾（{} 字符）：「{}」".format(
                                 len(abstract), abstract[:60]),
                             "class": "junk_abstract", "attempted_at": _now()}
            return
        entry = {"abstract": abstract, "source": source, "fetched_at": _now()}
        if ck_by_key.get(key):
            entry["citekey"] = ck_by_key[key]
        if extra:
            entry.update(extra)
        got[key] = entry
        if len(got) - flushed["n"] >= 100:
            flush()

    def flush():
        """周期落盘：中断/崩溃后重跑从已落盘处续抓（产物本身就是断点缓存）。"""
        store["abstracts"].update(got)
        for key in got:
            store["failures"].pop(key, None)
        store["failures"].update(failures)
        store["generated_at"] = _now()
        _atomic_write(out_path, store)
        flushed["n"] = len(got)

    # -- doi: 三级回退 ------------------------------------------------------
    doi_entries = [(e["dedup_key"], e["dedup_key"].split(":", 1)[1].lower())
                   for e in todo if e["dedup_key"].startswith("doi:")]
    remaining = dict(doi_entries)   # dedup_key -> doi
    if doi_entries:
        dois = [d for _, d in doi_entries]
        for i in range(0, len(dois), OPENALEX_BATCH):
            batch = dois[i:i + OPENALEX_BATCH]
            try:
                found = openalex_doi_batch(batch, mailto)
            except Exception as e:
                logger.warning("OpenAlex 批 {} 失败：{}", i // OPENALEX_BATCH, e)
                found = {}
            for key, doi in list(remaining.items()):
                if doi in found:
                    record(key, found[doi], "openalex")
                    del remaining[key]
            time.sleep(POLITE_SLEEP)
            if (i // OPENALEX_BATCH) % 10 == 0:
                logger.info("  OpenAlex {}/{}（已得 {}）", min(i + OPENALEX_BATCH, len(dois)),
                            len(dois), len(got))
        logger.info("OpenAlex 收官：{}/{}，转 Crossref 补 {} 个",
                    len(got), len(doi_entries), len(remaining))
        for key, doi in list(remaining.items()):
            try:
                abstract = crossref_doi(doi, mailto)
            except Exception:
                abstract = ""
            if abstract:
                record(key, abstract, "crossref")
                del remaining[key]
            time.sleep(POLITE_SLEEP)
        logger.info("Crossref 收官：剩 {} 个转 PubMed", len(remaining))
        for key, doi in list(remaining.items()):
            try:
                abstract = pubmed_doi(doi)
            except Exception:
                abstract = ""
            if abstract:
                record(key, abstract, "pubmed")
                del remaining[key]
            time.sleep(NCBI_SLEEP)
        for key, doi in remaining.items():
            failures[key] = {"reason": "doi 三源（openalex/crossref/pubmed）均无摘要",
                             "class": "no_abstract_any_source", "attempted_at": _now()}

    # -- arxiv: 批量 --------------------------------------------------------
    ax_entries = [(e["dedup_key"], re.sub(r"v\d+$", "", e["dedup_key"].split(":", 1)[1]))
                  for e in todo if e["dedup_key"].startswith("arxiv:")]
    if ax_entries:
        ids = [a for _, a in ax_entries]
        found = {}
        for i in range(0, len(ids), ARXIV_BATCH):
            try:
                found.update(arxiv_batch(ids[i:i + ARXIV_BATCH]))
            except Exception as e:
                logger.warning("arXiv 批失败：{}", e)
            time.sleep(1.0)     # arXiv 官方建议 ≥3s/req，批量请求放宽到 1s
        for key, aid in ax_entries:
            if aid in found:
                record(key, found[aid], "arxiv")
            else:
                failures[key] = {"reason": "arXiv API 未返回摘要",
                                 "class": "arxiv_miss", "attempted_at": _now()}
        logger.info("arXiv 收官：{}/{}", sum(1 for _, a in ax_entries if a in found), len(ax_entries))

    # -- 无 id：title.search + 守门 -----------------------------------------
    ts_entries = [e for e in todo
                  if not e["dedup_key"].startswith(("doi:", "arxiv:"))]
    for n, e in enumerate(ts_entries):
        key, title = e["dedup_key"], (e.get("title") or "").strip()
        if len(_norm_title(title)) < 15:
            # 标题过短（如人名条目）搜出来全是噪声，守门也不可靠
            failures[key] = {"reason": "标题过短，不做 title.search",
                             "class": "title_too_short", "attempted_at": _now()}
            continue
        try:
            abstract, matched, sim = openalex_title_search(title, mailto)
        except Exception as ex:
            failures[key] = {"reason": "title.search 请求失败：{}".format(ex),
                             "class": "network", "attempted_at": _now()}
            continue
        if abstract:
            record(key, abstract, "openalex_title_search",
                   {"matched_title": matched, "title_sim": round(sim, 3)})
        else:
            failures[key] = {
                "reason": "无 ≥{} 相似候选带摘要（最佳 {:.2f}：{}）".format(
                    TITLE_SIM_GATE, sim, (matched or "")[:80]),
                "class": "title_gate_reject" if matched else "title_no_hit",
                "attempted_at": _now()}
        time.sleep(POLITE_SLEEP)
        if n % 50 == 0:
            logger.info("  title.search {}/{}", n, len(ts_entries))

    # -- 终局落盘（原子写；已有键保留，failures 只反映本次尝试结果） --------
    flush()

    total = len(main_entries)
    have = sum(1 for e in main_entries if e["dedup_key"] in store["abstracts"])
    by_src = {}
    for v in store["abstracts"].values():
        by_src[v.get("source", "?")] = by_src.get(v.get("source", "?"), 0) + 1
    print("本次新增 {}，失败 {}；累计覆盖 {}/{}（{:.1%}）".format(
        len(got), len(failures), have, total, have / total if total else 0))
    print("按来源：{}".format(json.dumps(by_src, ensure_ascii=False)))
    if failures:
        cls = {}
        for v in failures.values():
            cls[v["class"]] = cls.get(v["class"], 0) + 1
        print("本次失败分类：{}".format(json.dumps(cls, ensure_ascii=False)))
    print("提示：launchd watcher（com.xlbd.scholar-embed）监视 abstracts.json，会自动增量重嵌；"
          "未安装该 job 时手跑 `python scripts/notes_embed.py` 让喂厚生效")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断（已抓部分未落盘；重跑会从断点续抓）", file=sys.stderr)
        sys.exit(130)
