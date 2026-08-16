# -*- coding: utf-8 -*-
"""
科研札记文献索引：把 output/scholar_notes/ 的月度札记聚合成机器可读的 literature_index.json
（+人读 INDEX.md + 部署 AGENTS.md），供论文项目的 agent 检索可用文献。

数据源两路（build_month_entries）：
  - sidecar `{slug}.index.json`（write_notes 顺手写出，无损：含 arxiv_id/priority_score 等）——优先；
  - 存量札记无 sidecar 时解析 md（行首锚定正则）+ references.json（CSL-JSON）合并。

去重与 scripts/backfill_notes.py 同源同规则（doi: > arxiv: > title: 规范化，最早月优先）；
本模块即权威实现，backfill delegate 到这里。重复条目不删除，标 `duplicate_of` 供消费方过滤。

判重三层（_global_pass，并查集成簇保传递性）：精确身份键 → dedup_overrides.json 人工确认对
→ 标题相似度（IDF 加权余弦 ≥ AUTO_MERGE_SIM 且无身份冲突）。中间带 [REVIEW_SIM,
AUTO_MERGE_SIM) 只报候选到 index["title_near_duplicates"]，不合并。
"""
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ._citekey_utils import (
    _suffix_seq, _priority_tier, _TIER_MAP, _reading_depth,
    _collect_highlights, dedup_key_fields, entry_from_segment, _norm_title,
    recompute_entry_key,
)

# 向后兼容：旧公开 API
norm_title = _norm_title
from ..utils.logger import get_logger

logger = get_logger(__name__)

# v4：条目加阅读深度量尺 fulltext_chars / fulltext_chars_raw / fulltext_truncated / reading_depth。
# reading_depth 四态（与 AGENTS.md 逐字一致）：'chunked' = manual 全部 + 开关打开后的 auto；
# 'single-call' = auto 单跳；'unknown-legacy' = 仅 auto 存量条目（由回填写入）；
# 键缺失或 null 只可能出现在 has_full_text_reading == false 的非精读条目上。
# fulltext_truncated：缺失 = 未知，false = 确认未截断——下游禁止把「缺失」当作「未截断」。
SCHEMA_VERSION = 4
INDEX_JSON = "literature_index.json"
INDEX_MD = "INDEX.md"
AGENTS_MD = "AGENTS.md"
ALL_REFS_JSON = "all_references.json"
# 人工确认的合并/不合并裁决（标题相似度无法安全自动判定时的通道），格式见 _load_dedup_overrides
DEDUP_OVERRIDES_JSON = "dedup_overrides.json"
# 仓库内那份裁决文件（受版本控制）。提成模块级常量而非写死在 _read_override_files 里：
# 单测得以把 override 来源完全限定在 tmp_path 内（monkeypatch 掉本常量），否则每个
# _global_pass 用例都会连带读进这份真实文件，测试结果跟着仓库状态飘。
# ⚠️ 生产行为不变：仍与 notes_dir 那份**取并集**（理由见 _read_override_files 文档）。
REPO_OVERRIDES_PATH = Path(__file__).resolve().parents[2] / "config" / DEDUP_OVERRIDES_JSON

# 成品札记 md 命名：_全文精读=自动流水线；_手动精读=手动 PDF 深度精读
# （天然排除 demo/ideal/validate/digest_* 等杂档）
# 月份桶允许 YYYY-MM、YYYY-MM-DD，或 YYYY-MM-DD-<批次名>（后两者用于同月内另起的专题批次：
# 前者如按论文攻防立场组织的深读，后者如按作者语料通读的 2026-07-27-HuiyingLiang）。
# 批次名不含下划线——`_` 是与系列后缀（全文精读/手动精读）的分隔符，让开不会有歧义。
# vault.month_key 取前 7 位折回 YYYY-MM，专题批次因此不会在图谱里劈出多余的月度页。
NOTE_MD_RE = re.compile(r"^科研札记_(\d{4}-\d{2}(?:-\d{2})?(?:-[^_]+)?)_(全文精读|手动精读)\.md$")
_SERIES_MAP = {"全文精读": "auto", "手动精读": "manual"}


def validate_note_label(label: str) -> str:
    """校验札记标签（月份桶/周标签/专题批次）能被 NOTE_MD_RE 认出；合法返回原值，否则抛 ValueError。

    为什么必须在 CLI 入口拦（read_pdf --month / ingest_notes --label）：畸形值
    （如 "2026-7"）会照常拼进文件名，md/references/sidecar 全部落盘、退出 0，
    但 _note_files 按 NOTE_MD_RE 静默跳过——这篇札记从此对 literature_index/
    seen/向量库/vault 全部不可见，且 seen 缺键会让自动链路下月把同批论文当
    新论文重复精读（静默数据丢失 + 重复烧 LLM）。落盘前一次校验换掉这一整类坑。

    合法形状与 NOTE_MD_RE 的月份桶注释一致：YYYY-MM、YYYY-MM-DD、YYYY-MM[-DD]-批次名
    （manual 的 2026-07-28-TFM、周札记的 2026-08-11 都是存量在用的形状，不能收紧成纯月份）。
    """
    # 以「拼出的文件名能被 NOTE_MD_RE 认出」为准——不另抄一份正则，保证校验口径
    # 与索引口径永不漂移。group 比对 + 空白检查挡住换行/斜杠等正则字符类（[^_]）
    # 拦不住、却会破坏文件名的字符。
    m = NOTE_MD_RE.match("科研札记_{}_全文精读.md".format(label))
    if (not m or m.group(1) != label
            or re.search(r"\s", label) or "/" in label or "\\" in label):
        raise ValueError(
            "札记标签应为 YYYY-MM[-DD][-批次名]（两位月份；批次名不含下划线/斜杠/空白，"
            "如 2026-07-28-TFM），收到 {!r}".format(label))
    try:
        datetime.strptime(label[:7], "%Y-%m")   # 首段年月还得是真日历月份（挡 2026-13）
    except ValueError:
        raise ValueError("札记标签的年月段不合法：{!r}（月份应为 01-12）".format(label))
    return label

# 每篇论文小节标题行（notes._paper_section 第 92 行的格式契约）：
#   ## 🔴 高 2. Title ... [@citekey]
_SECTION_RE = re.compile(
    r"^## (🔴 高|🟠 中|🟢 低|🔴)\s+(\d+)\.\s+(.*)\s+\[@([^\[\]\s]+)\]\s*$")
_PRIORITY_RE = re.compile(r"^\*\*优先级\*\*: `([\d.]+)`")
_DECISION_RE = re.compile(r"`(INCLUDE|MAYBE|EXCLUDE)`")
_BUCKET_RE = re.compile(r"维度 ([A-G](?:/[A-G])*)")
_ROLE_RE = re.compile(r"角色 (\S+)")
_CONF_RE = re.compile(r"conf ([\d.]+)")
_FLAGS_RE = re.compile(r"⚑ (\S+)")
_DOI_RE = re.compile(r"^\*\*DOI\*\*: \[([^\]]+)\]")
_URL_RE = re.compile(r"^\*\*链接\*\*: (\S+)")
_CLOSEREAD_RE = re.compile(r"^### (全文精读|精读（仅摘要降级）)(?: · 来源 `(.+?)`)?")
_CR_SECTION_RE = re.compile(r"^\*\*【(.+?)】\*\*\s*$")   # 精读分节标题（供 highlights 溯源 section）
# 句级角色标记行：捕获 tag（新旧六类）+ 句子文本（供 highlights 从 md 无损回填）
_TAG_LINE_RE = re.compile(
    r"^- 〔(可引用证据|可反驳观点|方法论借鉴|方法学创新|重要发现|研究背景)〕(.*)$")
_ARXIV_URL_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5}|[a-z\-]+/\d{7})")


# ---------------- 存量札记：md 解析 + CSL 合并 ----------------

def parse_note_md(md_path: Path) -> List[Dict[str, Any]]:
    """逐节解析札记 md（行首锚定），返回条目列表（字段有损：无 priority_score 之外的原始分等）。"""
    entries: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    lines = Path(md_path).read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines, 1):
        m = _SECTION_RE.match(line)
        if m:
            tier, num, title, citekey = m.groups()
            cur = {
                "citekey": citekey,
                "citekey_source": "unknown",  # md 无法区分 Zotero 键/兜底键
                "series": "auto",             # 由 build_month_entries 按文件名权威覆盖
                "doi": None, "arxiv_id": None,
                "title": title.strip(), "title_zh": None,
                "authors": [], "year": None, "journal": None, "url": None,
                "priority_tier": _TIER_MAP.get(tier, "low"),
                "priority_rank": int(num), "priority_score": None,
                "decision": None, "one_line": "", "bucket": [], "role": None,
                "confidence": None, "flags": [],
                "has_full_text_reading": False, "reading_source": None,
                "tag_counts": {}, "highlights": [],
                "note_heading": line, "note_line": i, "_cur_section": "",
            }
            ym = re.match(r"[a-z]*?(\d{4})", citekey)
            if ym:
                cur["year"] = int(ym.group(1))
            entries.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("# "):          # 参考文献等一级节，论文区结束
            cur = None
            continue
        pm = _PRIORITY_RE.match(line)
        if pm:
            cur["priority_score"] = float(pm.group(1))
            continue
        if line.startswith("**裁决**:"):
            dm = _DECISION_RE.search(line)
            if dm:
                cur["decision"] = dm.group(1)
            bm = _BUCKET_RE.search(line)
            if bm:
                cur["bucket"] = bm.group(1).split("/")
            rm = _ROLE_RE.search(line)
            if rm:
                cur["role"] = rm.group(1)
            cm = _CONF_RE.search(line)
            if cm:
                cur["confidence"] = float(cm.group(1))
            fm = _FLAGS_RE.search(line)
            if fm:
                cur["flags"] = fm.group(1).split("/")
            continue
        if line.startswith("**一句话用处**: "):
            cur["one_line"] = line[len("**一句话用处**: "):].strip()
            continue
        if line.startswith("**作者**: "):
            raw = line[len("**作者**: "):].strip()
            raw = raw[:-len(" et al.")] if raw.endswith(" et al.") else raw
            cur["authors"] = [a.strip() for a in raw.split(",") if a.strip()]
            continue
        if line.startswith("**期刊/来源**: "):
            cur["journal"] = line[len("**期刊/来源**: "):].strip()
            continue
        dm = _DOI_RE.match(line)
        if dm:
            cur["doi"] = dm.group(1).strip()
            continue
        um = _URL_RE.match(line)
        if um:
            cur["url"] = um.group(1).strip()
            am = _ARXIV_URL_RE.search(cur["url"])
            if am:
                cur["arxiv_id"] = am.group(1)
            continue
        crm = _CLOSEREAD_RE.match(line)
        if crm:
            cur["has_full_text_reading"] = crm.group(1) == "全文精读"
            cur["reading_source"] = crm.group(2)
            continue
        sm = _CR_SECTION_RE.match(line)
        if sm:
            cur["_cur_section"] = sm.group(1).strip()
            continue
        tm = _TAG_LINE_RE.match(line)
        if tm:
            hl, tc = _collect_highlights([(cur.get("_cur_section", ""),
                                           tm.group(1), tm.group(2))])
            for h in hl:
                cur["highlights"].append(h)
                cur["tag_counts"][h["role"]] = cur["tag_counts"].get(h["role"], 0) + 1
    for e in entries:
        e.pop("_cur_section", None)
        e["dedup_key"] = dedup_key_fields(e["doi"], e["arxiv_id"], e["title"],
                                          fallback=e["citekey"], url=e.get("url"))
    return entries


def load_csl_items(ref_path: Path) -> List[Dict[str, Any]]:
    """references.json（CSL-JSON 数组）→ item 列表。缺文件/坏文件返回空。"""
    try:
        items = json.loads(Path(ref_path).read_text(encoding="utf-8"))
        return [it for it in items if isinstance(it, dict)]
    except Exception:
        return []


def _match_csl(entry: Dict[str, Any], items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """给 md 条目挑对应 CSL item：先按 DOI 精确，citekey 仅在文件内唯一时才用。

    同月多篇论文可能共用同一 citekey（BBT 误配所致）——若盲按 citekey 匹配，
    第二篇会被灌入第一篇的 DOI/作者（曾致索引里两篇不同论文同 DOI 的假重复）。
    """
    doi = (entry.get("doi") or "").strip().lower()
    if doi:
        for it in items:
            if (it.get("DOI") or "").strip().lower() == doi:
                return it
    cand = [it for it in items if it.get("id") == entry.get("citekey")]
    return cand[0] if len(cand) == 1 else None


def _merge_csl(entry: Dict[str, Any], item: Dict[str, Any]) -> None:
    """作者/DOI/年份/期刊以 CSL 为准（结构化、无 et al. 截断），md 值兜底。"""
    if item.get("DOI"):
        entry["doi"] = item["DOI"]
    if item.get("URL") and not entry.get("url"):
        entry["url"] = item["URL"]
    if entry.get("url") and not entry.get("arxiv_id"):
        am = _ARXIV_URL_RE.search(entry["url"])
        if am:
            entry["arxiv_id"] = am.group(1)
    authors = []
    for a in item.get("author", []) or []:
        name = " ".join(x for x in (a.get("given"), a.get("family")) if x)
        if name:
            authors.append(name)
    if authors:
        entry["authors"] = authors
    try:
        entry["year"] = item["issued"]["date-parts"][0][0]
    except Exception:
        pass
    if item.get("container-title"):
        entry["journal"] = item["container-title"]
    entry["dedup_key"] = dedup_key_fields(entry["doi"], entry["arxiv_id"], entry["title"],
                                          fallback=entry.get("citekey", ""),
                                          url=entry.get("url"))


def _locate_headings(md_path: Path) -> Dict[str, List[Any]]:
    """citekey -> [(行号, 标题行), ...]（按文件内出现顺序），供 sidecar 条目按序回填
    note_line/note_heading。

    同一 citekey 在一份 md 里出现多次（近重复文献分别精读）时，各次出现都要保留——
    dict 单值会让后写的覆盖先写的，导致所有该 citekey 的 sidecar 条目都指向最后一节。
    """
    out: Dict[str, List[Any]] = {}
    try:
        for i, line in enumerate(Path(md_path).read_text(encoding="utf-8").splitlines(), 1):
            m = _SECTION_RE.match(line)
            if m:
                out.setdefault(m.group(4), []).append((i, line))
    except Exception:
        pass
    return out


def build_month_entries(month: str, md_path: Path,
                        ref_path: Optional[Path],
                        sidecar_path: Optional[Path],
                        series: str = "auto") -> List[Dict[str, Any]]:
    """单月条目：sidecar 优先（无损）；否则 md 解析 + CSL 合并。补齐落盘上下文字段。

    series 按文件名权威决定（_全文精读=auto / _手动精读=manual），覆盖 sidecar 里的值。
    """
    entries: List[Dict[str, Any]] = []
    source = "md-parse"
    if sidecar_path and Path(sidecar_path).exists():
        try:
            data = json.loads(Path(sidecar_path).read_text(encoding="utf-8"))
            entries = list(data.get("papers", []))
            source = "sidecar"
        except Exception as e:
            logger.warning("  ⚠️ sidecar 损坏，退回 md 解析（{}）: {}".format(sidecar_path, e))
    if not entries:
        entries = parse_note_md(md_path)
        csl = load_csl_items(ref_path) if ref_path and Path(ref_path).exists() else []
        for e in entries:
            item = _match_csl(e, csl)
            if item:
                _merge_csl(e, item)
        source = "md-parse"
    else:
        locs = _locate_headings(md_path)
        # 历史 sidecar（v2 及更早）无 highlights/新口径 tag_counts —— 从 md 句级标记回填（近似，
        # 按 citekey 匹配）。新 sidecar（v3+，含 highlights）直接沿用，不触碰。
        hl_map = ({m["citekey"]: m for m in parse_note_md(md_path)}
                  if any("highlights" not in e for e in entries) else {})
        # sidecar 条目已按 priority_rank 排好，与 build_digest_note 落盘顺序一致——
        # 同 citekey 出现多次时（近重复文献各自精读）按出现次序逐个认领，不能整批复用
        # 同一个 (行号, 标题)，否则第二条以后全部指向最后一节。
        _loc_cursor: Dict[str, int] = {}
        for e in entries:
            ck = e.get("citekey")
            candidates = locs.get(ck) or []
            i = _loc_cursor.get(ck, 0)
            loc = candidates[i] if i < len(candidates) else None
            _loc_cursor[ck] = i + 1
            e["note_line"] = loc[0] if loc else None
            e["note_heading"] = loc[1] if loc else None
            if "highlights" not in e:
                src_e = hl_map.get(e.get("citekey"))
                e["highlights"] = src_e.get("highlights", []) if src_e else []
                e["tag_counts"] = src_e.get("tag_counts", {}) if src_e else {}
    has_refs = bool(ref_path and Path(ref_path).exists())
    for e in entries:
        e["month"] = month
        e["series"] = series          # 文件名权威（覆盖 sidecar/md 默认）
        # dedup_key 一律**按当前规则重算**，不沿用 sidecar 里落盘时的值。
        # sidecar 是 write_notes 当时写下的快照，dedup_key 也被冻在那一刻；键梯规则一改
        # （如 2026-08-15 加 pmlr:/openreview: 层），md 解析那条路生效、sidecar 这条路却
        # 纹丝不动——同一篇论文两条来源拿到两个键，且**毫无迹象**。实测踩中：手动精读的
        # fani26a 有 proceedings.mlr.press 的 url 却仍持 title: 键，人工合并裁决因此失配。
        # 例外：重算落到 "id:"（doi/arxiv/场地 id/标题全空）时保留原值——sidecar 用
        # paper_id 兜底比这里能拿到的 citekey 更稳，重算反而更差。
        # ⚠️ 重算规则收在 _citekey_utils.recompute_entry_key 一处：ingest 的整篇覆盖守卫
        #    （_existing_note_dedup_keys）读同一批 sidecar，两处各写各的就会键梯漂移。
        e["dedup_key"] = recompute_entry_key(e)
        # 存量精读条目回填 reading_depth（两条并列的对称规则；series 已由文件名权威定死）。
        # 不重跑任何存量精读——只在量尺上标出「这批读到什么程度」，让下游能显式区分两代札记。
        # (a) auto 存量：既没有 reading_depth 又确实做过精读的，只可能是加分块开关之前跑的单跳，
        #     且当时的正文上限会把长文砍在前 40k 字符 —— 深度不可考，标 'unknown-legacy'。
        # (b) manual：pdf_ingest 的 synthesize_deep_read 只写 from_full_text/model/source，
        #     从不写 reading_depth，但它按构造就是 chunk_text + deep_read_chunks 的分块深读；
        #     不兜的话全库读得最深的这批会和 auto 存量一起沉在「无值」里，与 has_full_text_reading
        #     直接打架。与 entry_from_segment 的 _reading_depth() 同一口径。
        # 两条规则都只补 reading_depth：fulltext_chars / fulltext_chars_raw / fulltext_truncated
        # 一律保持缺失（缺失=未知）——猜填 false 会让「确认未截断」和「不知道」混为一谈。
        if "reading_depth" not in e:
            if series == "manual":
                e["reading_depth"] = "chunked"
            elif series == "auto" and e.get("has_full_text_reading"):
                e["reading_depth"] = "unknown-legacy"
        e["note_file"] = Path(md_path).name
        e["references_json"] = Path(ref_path).name if has_refs else None
        e["_source"] = source
        e.setdefault("duplicate_months", [])
        e.setdefault("duplicate_of", None)
    return entries


# ---------------- 索引构建（增量/全量/区间） ----------------

def _note_files(notes_dir: Path) -> Dict[str, tuple]:
    """文件 stem -> (month, series, md 路径)（只认成品命名）。

    键改用 stem（而非 month）：同月 `_全文精读` 与 `_手动精读` 两系列可共存。
    """
    out = {}
    for p in sorted(Path(notes_dir).glob("*.md")):
        m = NOTE_MD_RE.match(p.name)
        if m:
            out[p.name[:-3]] = (m.group(1), _SERIES_MAP.get(m.group(2), "auto"), p)
    return out


def _entry_keys(e: Dict[str, Any]) -> List[str]:
    """条目的身份键集合：dedup_key + 规范化标题键（二级）。

    二级标题键捕获「同一论文、不同 dedup_key」的漏网重复——典型场景是
    某月该篇缺 DOI（title 键）、另一月经 Crossref 补出 DOI（doi 键），
    一级键不同但实为同文（如预印本/正刊双收）。
    """
    keys = [e["dedup_key"]]
    t = _norm_title(e.get("title"))
    if t:
        tk = "title:" + t
        if tk != e["dedup_key"]:
            keys.append(tk)
    return keys


def _keeper_rank(e: Dict[str, Any]) -> tuple:
    """keeper 优先级（越小越优先当权威）：手动深读 > 书目更全 > 最早月份 > 更高优先级排名。

    手动 PDF 深度精读是论文 agent 应优先读到的权威版本，即使月份晚于自动浅读，
    故 series 始终是第一顺位——正文内容比元数据完整度重要。

    「书目更全」是**同系列内**的次级顺位：keeper 是下游唯一可见的记录（其余按
    duplicate_of 过滤），若它是个作者/DOI/期刊全空的残缺条目，合并等于把完好记录
    换成了残条。实测本库 76 个重复簇里有 6 组踩中，典型是预印本被解析成 `anon*`
    无作者条目、却因月份更早而压过带作者的正刊记录（如 Research Square 版
    《Federated Learning used for predicting outcomes in SARS-COV-2 patients》
    压过 Nature Medicine 版）。月份只在完整度相同时才决定胜负。
    """
    completeness = sum(1 for f in ("authors", "doi", "journal") if e.get(f))
    return (0 if e.get("series") == "manual" else 1,
            -completeness, e["month"], e.get("priority_rank") or 9999)


# ---------------- 标题相似度层（捕获改写题名的漏网重复） ----------------
#
# 精确标题键（_entry_keys 二级键）只认逐字相同的题名，预印本→正刊常改写标题而两侧
# 又都无 DOI 时会漏网（实测：arXiv《Volatility-Aware Masking Improves…》与 ML4H
# 正刊《Coefficient of Variation Masking: A Volatility-Aware Strategy…》）。
#
# 度量选型（在本库 2264 篇真实标题上标定，见下）：IDF 加权余弦。
#   - 用 IDF 而非裸 Jaccard：领域高频词（clinical/prediction/model）不该撑起相似度，
#     真正的信号是稀有词共现（volatility/masking）。
#   - 不用 containment（交集/较短者）：短标题与截断标题会退化成 1.0（实测
#     「Healthcare Analytics」对任意含该词的长标题都是 1.0），假阳性极高。
# 阈值标定（加下述守卫后，全库跨簇对的人工判读结果）：
#   cos≥0.85 → 3 对，全部真重复；0.70–0.85 → 2 对，全部真重复；
#   0.60–0.70 → 4 对，已混入 2 对不同论文；0.50–0.60 → 8 对，多数是不同论文。
# 故 0.70 以上自动合并，0.45–0.70 只报候选（title_near_duplicates）交人工确认——
# **误合并会静默吞掉一篇论文**（下游一律按 duplicate_of 过滤），代价远高于漏合并。
AUTO_MERGE_SIM = 0.70
REVIEW_SIM = 0.45

_TITLE_STOP = {
    "the", "and", "for", "with", "from", "using", "via", "into", "over", "under",
    "that", "this", "these", "those", "are", "was", "were", "its", "their",
    "based", "toward", "towards", "through", "between", "among", "study",
    "approach", "novel", "new", "use", "used", "can", "does", "what", "how", "why",
    "when", "not",
}


def _title_tokens(title: Optional[str]) -> Set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (title or "").lower())
            if len(w) >= 3 and w not in _TITLE_STOP}


def _first_surname(e: Dict[str, Any]) -> str:
    """首作者姓氏（小写）；取不到返回 ""（视作「未知」，不参与冲突判定）。

    authors 为空时退回 citekey 前缀，但 `anon`（元数据解析失败的兜底键）要当未知，
    否则两篇互不相干的 anon 条目会被误判为「同一作者」。
    """
    authors = e.get("authors") or []
    if authors:
        parts = str(authors[0]).replace(",", " ").split()
        if parts:
            s = re.sub(r"[^a-z]", "", parts[-1].lower())
            if s:
                return s
    m = re.match(r"([a-z]+)\d{4}", e.get("citekey") or "")
    s = m.group(1) if m else ""
    return "" if s == "anon" else s


def _identity_conflict(a: Dict[str, Any], b: Dict[str, Any]) -> str:
    """两条目是否有「铁证不同篇」的冲突；返回冲突原因，无冲突返回 ""。

    只在**双方都有**该字段时才判冲突：一方缺失是常态（PMLR 无 DOI、anon 无作者），
    缺失不能当作证据。守卫存在的意义是把标题相似度的阈值压低到可用区间而不误吞——
    实测姊妹篇（Static vs Time-varying Feature Settings）正是靠 arXiv id 冲突拦下的。
    """
    da, db = (a.get("doi") or "").strip().lower(), (b.get("doi") or "").strip().lower()
    if da and db and da != db:
        return "doi"
    xa, xb = (a.get("arxiv_id") or "").strip().lower(), (b.get("arxiv_id") or "").strip().lower()
    if xa and xb and xa.split("v")[0] != xb.split("v")[0]:
        return "arxiv"
    sa, sb = _first_surname(a), _first_surname(b)
    if sa and sb and sa != sb:
        return "author"
    ya, yb = a.get("year"), b.get("year")
    if isinstance(ya, int) and isinstance(yb, int) and abs(ya - yb) > 3:
        return "year"
    return ""


def _title_sim_pairs(papers: List[Dict[str, Any]],
                     cluster_of) -> List[Dict[str, Any]]:
    """跨簇标题相似对（已过守卫），按相似度降序。cluster_of(i) 给出条目 i 的现有簇。

    倒排索引分块：只比较共享至少一个非高频词的对，避免 O(n²) 全比。
    """
    idxs = [i for i, e in enumerate(papers) if _title_tokens(e.get("title"))]
    toks = {i: _title_tokens(papers[i].get("title")) for i in idxs}
    df: Dict[str, int] = {}
    for i in idxs:
        for w in toks[i]:
            df[w] = df.get(w, 0) + 1
    # n 取下限 50：语料太小时 IDF 会反转——共享词必然 df≥2、在 n=2 时权重反而最低，
    # 相似度恒塌成 0，特征在小札记库（或单元测试）里静默失效。加下限后小语料退化成
    # 近似均匀权重（即纯 token 重叠度），大语料（本库 2260 篇）不受影响。
    n = max(len(idxs), 50)
    idf = {w: math.log((n + 1) / (c + 0.5)) for w, c in df.items()}
    norms = {i: math.sqrt(sum(idf[w] ** 2 for w in toks[i])) for i in idxs}

    inv: Dict[str, List[int]] = {}
    for i in idxs:
        for w in toks[i]:
            if df[w] <= 300:                     # 高频词不作分块键
                inv.setdefault(w, []).append(i)

    seen: Set[tuple] = set()
    out: List[Dict[str, Any]] = []
    for w, ids in inv.items():
        if len(ids) > 500:
            continue
        for x in range(len(ids)):
            for y in range(x + 1, len(ids)):
                i, j = (ids[x], ids[y]) if ids[x] < ids[y] else (ids[y], ids[x])
                if (i, j) in seen:
                    continue
                seen.add((i, j))
                if cluster_of(i) == cluster_of(j):        # 精确键已合并
                    continue
                if not norms[i] or not norms[j]:
                    continue
                inter = toks[i] & toks[j]
                if not inter:
                    continue
                sim = sum(idf[w2] ** 2 for w2 in inter) / (norms[i] * norms[j])
                if sim < REVIEW_SIM:
                    continue
                conflict = _identity_conflict(papers[i], papers[j])
                if conflict:
                    continue
                out.append({"i": i, "j": j, "similarity": round(sim, 4)})
    out.sort(key=lambda r: -r["similarity"])
    return out


def _read_override_files(notes_dir: Optional[Path]) -> List[Dict[str, Any]]:
    """读出两处 dedup_overrides.json 的原始内容（解析失败者跳过并告警）。

    两处来源取并集：仓库 `config/`（受版本控制，人工裁决不该随 output/ 被 gitignore
    吞掉——札记库重建或换机后还得靠它）+ `notes_dir/`（该库私有、可选）。取并集而非
    择一，避免本地文件静默遮蔽仓库里已确认的裁决。
    """
    cands = [Path(REPO_OVERRIDES_PATH)]
    if notes_dir:
        cands.append(Path(notes_dir) / DEDUP_OVERRIDES_JSON)
    out: List[Dict[str, Any]] = []
    for p in cands:
        if not p.exists():
            continue
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception as exc:
            logger.warning("  ⚠️ {} 解析失败，本次忽略其中的人工裁决：{}".format(p, exc))
    return out


def _pairs_from(files: List[Dict[str, Any]], field: str) -> List[List[str]]:
    """从已读入的 override 文件里取某个字段的键对列表（去重、忽略残缺行）。"""
    pairs: List[List[str]] = []
    seen: Set[tuple] = set()
    for data in files:
        for row in (data.get(field) or []):
            if isinstance(row, (list, tuple)) and len(row) >= 2 and all(row[:2]):
                a, b = str(row[0]), str(row[1])
                k = (a, b) if a <= b else (b, a)
                if k not in seen:
                    seen.add(k)
                    pairs.append([a, b])
    return pairs


def _report_stale(pairs, by_dedup_key: Dict[str, Any], kind: str) -> int:
    """列出键在库中找不到的人工裁决对，逐条 WARNING。返回失配条数。

    ⚠️ 键失配必须**告警**，不能沉默：dedup_key 会随元数据变动（Crossref 补上 DOI、标题
    修正、键梯升级），一旦某侧键漂了，这条人工裁决就永久失效且无任何迹象——人以为
    「已确认过」，实际每次重建都没生效。札记被删是合理的失配来源，但也该看得见。

    merge 与 distinct 两条通道**同等对待**：distinct 那半原先完全没有这项检查，键一漂
    就永久不再压制那对候选，人工确认过的假阳性每次重建原样重报——正是 _load_dedup_distinct
    文档字符串声称要解决的「永不收敛」问题本身。
    """
    stale: List[str] = []
    for ka, kb in pairs:
        ia, ib = ka in by_dedup_key, kb in by_dedup_key
        if ia and ib:
            continue
        # 三种情况分别措辞：只报「缺 前者」会误导人只去查前一个键
        which = "两侧都缺" if not ia and not ib else ("缺 前者" if not ia else "缺 后者")
        stale.append("{} ↮ {}（{}）".format(ka, kb, which))
    if stale:
        logger.warning("  ⚠️ {} 里有 {} 条人工{}裁决未生效（键在库中找不到，多因元数据变动"
                       "导致 dedup_key 漂移或札记已删）——请核对后更新："
                       .format(DEDUP_OVERRIDES_JSON, len(stale), kind))
        for s in stale:
            logger.warning("      {}".format(s))
    return len(stale)


def _load_dedup_overrides(notes_dir: Optional[Path]) -> List[List[str]]:
    """人工确认的合并对：dedup_overrides.json 的 {"merge": [[keyA, keyB], ...]}。

    标题相似度**无法**安全覆盖所有改写题名（本库实测：真重复只有 cos=0.50，与一堆
    0.5x 的不同论文混在同一区间），故保留人工裁决通道：确认过的对写进此文件，
    无条件合并且不受阈值变动影响。键用 dedup_key（doi:/arxiv:/title:/id:）。
    """
    return _pairs_from(_read_override_files(notes_dir), "merge")


def _load_dedup_distinct(notes_dir: Optional[Path]) -> Set[tuple]:
    """人工确认「**不是**同一篇」的对：dedup_overrides.json 的 {"distinct": [[keyA, keyB], ...]}。

    没有这一半，人工复核通道就**永不收敛**：落在 [REVIEW_SIM, AUTO_MERGE_SIM) 的假阳性
    （短标题、同领域套话、跨语言噪声）每次重建索引都会原样再报一遍，人看过多少次都不减少，
    真正的新增待确认项因此被淹没。判为不同的写进这里，此后不再出现在 title_near_duplicates。
    只影响报告，不影响合并——它压制的是「请人看一眼」，本来就没合并过任何东西。
    """
    return {tuple(sorted(pr)) for pr in _pairs_from(_read_override_files(notes_dir), "distinct")}


def _global_pass(papers: List[Dict[str, Any]],
                 notes_dir: Optional[Path] = None,
                 review_out: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """全局排序 + 跨月去重标记（keeper 规则见 _keeper_rank）+ 撞键检测的前置排序。

    三层合并，用并查集统一成簇（保证传递性：A≈B、B≈C 时三者同簇同 keeper）：
      1. 精确身份键（dedup_key + 规范标题键）——原有行为，不变；
      2. 人工确认的 dedup_overrides.json 合并对；
      3. 标题相似度 ≥ AUTO_MERGE_SIM 且无身份冲突的跨簇对。
    落在 [REVIEW_SIM, AUTO_MERGE_SIM) 的对不合并，追加到 review_out 供上层报告。
    """
    papers.sort(key=lambda e: (e["month"], e.get("priority_rank") or 9999))
    for e in papers:
        e["duplicate_months"] = []
        e["duplicate_of"] = None

    parent: Dict[int, int] = {i: i for i in range(len(papers))}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # 1) 精确身份键
    owner: Dict[str, int] = {}
    for i, e in enumerate(papers):
        for k in _entry_keys(e):
            if k in owner:
                union(i, owner[k])
            else:
                owner[k] = i

    # 2) 人工确认对（按 dedup_key 找条目）——键失配一律告警，理由见 _report_stale
    by_dedup_key: Dict[str, List[int]] = {}
    for i, e in enumerate(papers):
        by_dedup_key.setdefault(e["dedup_key"], []).append(i)
    override_files = _read_override_files(notes_dir)
    applied_overrides = 0
    merge_pairs = _pairs_from(override_files, "merge")
    for ka, kb in merge_pairs:
        ia, ib = by_dedup_key.get(ka), by_dedup_key.get(kb)
        if ia and ib:
            union(ia[0], ib[0])
            applied_overrides += 1
    if applied_overrides:
        logger.info("  人工合并对（{}）：应用 {} 组".format(DEDUP_OVERRIDES_JSON, applied_overrides))
    _report_stale(merge_pairs, by_dedup_key, "合并")
    # distinct 的漂键检查与 merge 同处执行（不藏在 review_out 分支里）：漂了就永久不再
    # 压制那对候选，与是否要出报告无关。
    distinct_pairs = _pairs_from(override_files, "distinct")
    _report_stale(distinct_pairs, by_dedup_key, "非同文")

    # 3) 标题相似度层
    sim_pairs = _title_sim_pairs(papers, find)
    auto, review = [], []
    for pr in sim_pairs:
        if pr["similarity"] >= AUTO_MERGE_SIM:
            union(pr["i"], pr["j"])
            auto.append(pr)
        else:
            review.append(pr)
    if auto:
        logger.info("  标题相似度自动合并 {} 组（cos≥{}）".format(len(auto), AUTO_MERGE_SIM))

    # 每簇选 keeper，其余标 duplicate_of
    clusters: Dict[int, List[int]] = {}
    for i in range(len(papers)):
        clusters.setdefault(find(i), []).append(i)
    for members in clusters.values():
        if len(members) < 2:
            continue
        keeper = min((papers[i] for i in members), key=_keeper_rank)
        for i in members:
            e = papers[i]
            if e is keeper:
                continue
            if e["month"] not in keeper["duplicate_months"]:
                keeper["duplicate_months"].append(e["month"])
            e["duplicate_of"] = "{}@{}".format(keeper["dedup_key"], keeper["month"])

    if review_out is not None:
        distinct = {tuple(sorted(pr)) for pr in distinct_pairs}
        suppressed = 0
        for pr in review:
            a, b = papers[pr["i"]], papers[pr["j"]]
            if find(pr["i"]) == find(pr["j"]):
                continue                  # 已被别的层归到同簇，无需人工再看
            if tuple(sorted((a["dedup_key"], b["dedup_key"]))) in distinct:
                suppressed += 1           # 人工已判「不是同一篇」，不再反复上报
                continue
            review_out.append({
                "similarity": pr["similarity"],
                "a": {"dedup_key": a["dedup_key"], "month": a["month"],
                      "citekey": a.get("citekey"), "title": a.get("title")},
                "b": {"dedup_key": b["dedup_key"], "month": b["month"],
                      "citekey": b.get("citekey"), "title": b.get("title")},
            })
        if suppressed:
            logger.info("  人工已判非同文（{} 的 distinct）：压制 {} 对不再上报"
                        .format(DEDUP_OVERRIDES_JSON, suppressed))
        review_out.sort(key=lambda r: -r["similarity"])
    return papers


def _citekey_collisions(papers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同 citekey 指向不同论文（不同 dedup_key）→ 撞键告警（合并 bibliography 会吞篇）。"""
    by_key: Dict[str, Set[str]] = {}
    months: Dict[str, List[str]] = {}
    for e in papers:
        if e.get("duplicate_of"):
            continue
        ck = e.get("citekey") or ""
        by_key.setdefault(ck, set()).add(e["dedup_key"])
        months.setdefault(ck, []).append(e["month"])
    return [{"citekey": ck, "months": sorted(set(months[ck]))}
            for ck, keys in sorted(by_key.items()) if len(keys) > 1]


def update_index(notes_dir: Path, *, full: bool = False,
                 since: Optional[str] = None, until: Optional[str] = None) -> Dict[str, Any]:
    """构建/更新索引 dict。

    增量（默认）：只重解析 mtime/size 变化或未入索引的月份；
    since/until：**强制重扫**区间内月份（不看 mtime），区间外沿用旧条目；
    full：全量重建。已删除的月份 md 会连同其索引条目一起消失（months 以磁盘为准）。
    """
    notes_dir = Path(notes_dir)
    index_path = notes_dir / INDEX_JSON
    old: Dict[str, Any] = {}
    if not full and index_path.exists():
        try:
            old = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception:
            old = {}
    old_months = old.get("months", {})
    old_papers = old.get("papers", [])

    files = _note_files(notes_dir)
    months_meta: Dict[str, Any] = {}     # 键为文件 stem（v2）
    papers: List[Dict[str, Any]] = []
    reparsed = kept = 0
    range_mode = since is not None or until is not None
    for stem, (month, series, md_path) in sorted(files.items()):
        # month 允许比边界更细的粒度（周札记 "2026-07-17"、专题批次 "2026-07-27-Xxx"）。
        # 直接字典序比较会把它们排除在 --since/--until 2026-07 之外（"2026-07-17" <= "2026-07"
        # 为假），且区间模式下区间外文件即使变了也不重解析——改动永远进不了索引。
        # 比较前把 month 截到边界自身的粒度：月边界只看 YYYY-MM，日边界看 YYYY-MM-DD。
        in_range = ((since is None or month[:len(since)] >= since)
                    and (until is None or month[:len(until)] <= until))
        st = md_path.stat()
        prev = old_months.get(stem)
        unchanged = (prev and prev.get("md_mtime") == st.st_mtime
                     and prev.get("md_size") == st.st_size)
        force = full or (range_mode and in_range)   # 区间模式：区间内强制重扫
        if (not in_range and prev) or (unchanged and not force):
            # 沿用旧条目（区间外文件即使变化也不动，除非它根本不在旧索引里）
            entries = [e for e in old_papers if e.get("note_file") == md_path.name]
            months_meta[stem] = prev
            kept += 1
        else:
            entries = build_month_entries(
                month, md_path,
                ref_path=notes_dir / "{}.references.json".format(stem),
                sidecar_path=notes_dir / "{}.index.json".format(stem),
                series=series)
            months_meta[stem] = {"month": month, "series": series,
                                 "md_mtime": st.st_mtime, "md_size": st.st_size,
                                 "papers": len(entries),
                                 "source": entries[0]["_source"] if entries else "empty"}
            reparsed += 1
        papers.extend(entries)

    for e in papers:
        e.pop("_source", None)
    review_pairs: List[Dict[str, Any]] = []
    papers = _global_pass(papers, notes_dir=notes_dir, review_out=review_pairs)
    index = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "months": months_meta,
        "citekey_collisions": _citekey_collisions(papers),
        "title_near_duplicates": review_pairs,
        "papers": papers,
    }
    logger.info("  索引：{} 个札记文件（重解析 {}，沿用 {}），共 {} 篇，撞键 {} 组".format(
        len(months_meta), reparsed, kept, len(papers), len(index["citekey_collisions"])))
    if review_pairs:
        logger.info("  ⚠️ 疑似同文待人工确认 {} 对（标题相似 {}–{}）：见 INDEX.md「疑似重复」节，"
                    "确认后写入 {}".format(len(review_pairs), REVIEW_SIM, AUTO_MERGE_SIM,
                                          DEDUP_OVERRIDES_JSON))
    return index


# ---------------- 输出（幂等：内容未变不落盘） ----------------

def _stable(index: Dict[str, Any]) -> str:
    d = dict(index)
    d.pop("generated_at", None)
    return json.dumps(d, ensure_ascii=False, sort_keys=True)


def write_if_changed(path: Path, content: str) -> bool:
    """内容未变则不写盘（mtime 不抖）。vault 生成器复用同一份实现，避免行为漂移。"""
    path = Path(path)
    try:
        if path.exists() and path.read_text(encoding="utf-8") == content:
            return False
    except Exception:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    # tmp+replace 原子写（同 merge_final.py）：all_references.json 等经此落盘，
    # 半写 JSON 会直接毒害 pandoc/vault 消费方；tmp 同目录避免跨设备 replace。
    # tmp 名掺 pid：双写者并发（如 weekly-ingest 与手动 backfill 重叠）各写各的 tmp，
    # 避免互相截断同一 tmp 导致 os.replace 落成半截文件。
    tmp = path.with_suffix(path.suffix + ".tmp-{}".format(os.getpid()))
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return True


_write_if_changed = write_if_changed        # 旧名别名（模块内既有调用点仍在用）


def build_index_md(index: Dict[str, Any]) -> str:
    """人读 INDEX.md：顶部统计 + 撞键告警 + 按月倒序表。"""
    papers = [e for e in index["papers"] if not e.get("duplicate_of")]
    n_inc = sum(1 for e in papers if e.get("decision") == "INCLUDE")
    n_ft = sum(1 for e in papers if e.get("has_full_text_reading"))
    n_manual = sum(1 for e in papers if e.get("series") == "manual")
    all_months = sorted({e["month"] for e in index["papers"]})
    lines = ["# 科研札记文献索引", "",
             "机器可读版：`literature_index.json`（查询配方见 `AGENTS.md`）。", "",
             "- 覆盖月份：**{}**（{} → {}）".format(
                 len(all_months),
                 all_months[0] if all_months else "-",
                 all_months[-1] if all_months else "-"),
             "- 论文：**{}** 篇（INCLUDE {} · 全文精读 {} · 手动深读 {}）".format(
                 len(papers), n_inc, n_ft, n_manual)]
    if index["citekey_collisions"]:
        lines.append("- ⚠️ **citekey 撞键 {} 组**（不同论文同键，合并 bibliography 前必须处理）：{}".format(
            len(index["citekey_collisions"]),
            "; ".join("`{}` ({})".format(c["citekey"], ",".join(c["months"]))
                      for c in index["citekey_collisions"])))
    near = index.get("title_near_duplicates") or []
    if near:
        lines.append("- 🔎 **疑似同文待人工确认 {} 对**（标题相似 {}–{}；≥{} 已自动合并）"
                     .format(len(near), REVIEW_SIM, AUTO_MERGE_SIM, AUTO_MERGE_SIM))
    lines.append("")
    if near:
        lines.extend([
            "## 疑似重复（人工确认后写入 `{}`）".format(DEDUP_OVERRIDES_JSON), "",
            "确认为同一篇 → 把两侧 `dedup_key` 作为一对写进 `{}` 的 `merge` 数组，"
            "下次建索引即永久合并（不受阈值变动影响）；确认为不同论文 → 无需操作。"
            .format(DEDUP_OVERRIDES_JSON), "",
            "| 相似度 | A（月份 / citekey / 标题） | B（月份 / citekey / 标题） | dedup_key 对 |",
            "|:-:|---|---|---|"])
        esc0 = lambda s: (s or "").replace("|", "/")
        for r in near[:40]:
            a, b = r["a"], r["b"]
            lines.append("| {:.2f} | {} `{}` {} | {} `{}` {} | `{}` ↔ `{}` |".format(
                r["similarity"], a["month"], a.get("citekey") or "", esc0(a.get("title"))[:70],
                b["month"], b.get("citekey") or "", esc0(b.get("title"))[:70],
                a["dedup_key"], b["dedup_key"]))
        if len(near) > 40:
            lines.append("| … | 另有 {} 对，见 `literature_index.json` 的 `title_near_duplicates` | | |"
                         .format(len(near) - 40))
        lines.append("")
    esc = lambda s: (s or "").replace("|", "/")
    tier_emoji = {"high": "🔴", "mid": "🟠", "low": "🟢"}
    for month in sorted(all_months, reverse=True):
        rows = [e for e in papers if e["month"] == month]
        if not rows:
            continue
        lines.extend(["## {}".format(month), "",
                      "| # | 优先级 | 系列 | 裁决 | citekey | 标题 | 一句话用处 | DOI |",
                      "|:-:|:-:|:-:|:-:|---|---|---|---|"])
        for e in sorted(rows, key=lambda x: (x.get("series") != "manual",
                                             x.get("priority_rank") or 9999)):
            lines.append("| {} | {} | {} | {} | `{}` | {} | {} | {} |".format(
                e.get("priority_rank") or "", tier_emoji.get(e.get("priority_tier"), ""),
                "📘手动" if e.get("series") == "manual" else "自动",
                e.get("decision") or "", e.get("citekey") or "",
                esc(e.get("title")), esc(e.get("one_line")), e.get("doi") or ""))
        lines.append("")
    lines.append("_generated_at: {}_".format(index.get("generated_at", "")))
    lines.append("")
    return "\n".join(lines)


def _agents_source() -> Optional[Path]:
    p = Path(__file__).resolve().parents[2] / "docs" / "scholar_notes_AGENTS.md"
    return p if p.exists() else None


def _fallback_csl(entry: Dict[str, Any]) -> Dict[str, Any]:
    """月度 CSL 文件缺条目时，从索引字段构造最小 CSL 条目（authors 为字符串列表）。"""
    item: Dict[str, Any] = {
        "id": entry["citekey"],
        "type": "article-journal" if not entry.get("arxiv_id") or entry.get("doi") else "article",
        "title": entry.get("title") or "",
    }
    authors = []
    for n in (entry.get("authors") or []):
        n = (n or "").strip()
        if not n:
            continue
        if "," in n:
            last, _, first = n.partition(",")
            authors.append({"family": last.strip(), "given": first.strip()})
        else:
            parts = n.split()
            if len(parts) == 1:
                authors.append({"family": parts[0]})
            else:
                authors.append({"family": parts[-1], "given": " ".join(parts[:-1])})
    if authors:
        item["author"] = authors
    if entry.get("journal"):
        item["container-title"] = entry["journal"]
    if entry.get("doi"):
        item["DOI"] = entry["doi"]
    if entry.get("url"):
        item["URL"] = entry["url"]
    if entry.get("year"):
        item["issued"] = {"date-parts": [[entry["year"]]]}
    return item


def is_missing_citekey(entry: Dict[str, Any]) -> bool:
    """该条目的 citekey 是否为 MISSING-KEY 占位键（write_notes 未开 fallback_citekeys 的兜底）。

    notes.py 写 sidecar 时承诺「消费方据此过滤」——占位键不对应任何真实文献，
    进全局书目/vault 只会渲染成 [@MISSING-KEY-...] 死引用，消费方必须在此拦截。
    """
    key = entry.get("citekey") or ""
    return key.startswith("MISSING-KEY-") or entry.get("citekey_source") == "missing"


def build_all_references(index: Dict[str, Any], notes_dir: Path) -> List[Dict[str, Any]]:
    """合并全部月度 references.json → 全局 CSL-JSON 书目（按 id 排序）。

    只收 `duplicate_of == null` 的条目（keeper）：跨月重复自然以 keeper 元数据为准，
    但**被判重条目自己的 citekey 不在本书目内**——渲染月度 md 请仍用同名 references.json。

    取 CSL 条目走 `_match_csl`（DOI 优先，citekey 仅在文件内唯一时才用）而非盲按 id 索引：
    历史 --fix-collisions 曾把 md 与 references.json 的键改岔，盲取会安静地引到另一篇论文。
    命中后强制改写 id 为索引的 citekey（DOI 命中时二者可能不同）。

    取不到时用 `_fallback_csl` 兜底（缺卷/期/页、issued 只有年份），逐条 warning 报出。
    citekey 撞键（不同论文同键）时整键剔除：宁可让 pandoc 输出显眼的 `???`，
    也不要静默把某篇的引用渲染成另一篇。
    """
    notes_dir = Path(notes_dir)
    dropped: Set[str] = set()
    for c in (index.get("citekey_collisions") or []):
        if c.get("citekey"):
            dropped.add(c["citekey"])
    if dropped:
        logger.warning("  ⚠️ citekey 撞键 {} 组，这些键已从 all_references.json 整体剔除"
                       "（引用会渲染成 ???）；跑 notes_index.py --fix-collisions 修复后重建：{}".format(
                           len(dropped), ", ".join(sorted(dropped))))
    csl_cache: Dict[str, List[Dict[str, Any]]] = {}
    merged: Dict[str, Dict[str, Any]] = {}
    fallbacks: List[str] = []
    missing: List[str] = []
    for e in index["papers"]:
        if e.get("duplicate_of") or not e.get("citekey"):
            continue
        if is_missing_citekey(e):
            missing.append("{}@{}".format(e["citekey"], e.get("month") or "?"))
            continue
        key = e["citekey"]
        if key in dropped or key in merged:
            continue
        ref_file = e.get("references_json")
        item = None
        if ref_file:
            if ref_file not in csl_cache:
                csl_cache[ref_file] = load_csl_items(notes_dir / ref_file)
            hit = _match_csl(e, csl_cache[ref_file])
            if hit is not None:
                item = dict(hit)        # 浅拷贝：不污染缓存
                item["id"] = key        # DOI 命中的条目 id 可能≠citekey，以索引为准
        if item is None:
            item = _fallback_csl(e)
            fallbacks.append("{}@{}".format(key, e.get("month") or "?"))
        merged[key] = item
    if fallbacks:
        logger.warning("  all_references：{} 条未匹配到月度 CSL 条目，已按索引字段兜底"
                       "（缺卷期页、作者可能被 md 的 et al. 截断）：{}{}".format(
                           len(fallbacks), ", ".join(fallbacks[:8]),
                           " …" if len(fallbacks) > 8 else ""))
    if missing:
        logger.warning("  ⚠️ all_references：{} 条 MISSING-KEY 占位条目已跳过"
                       "（Zotero/BBT 当时未解析出 citekey；补键重跑索引后自动收录）：{}{}".format(
                           len(missing), ", ".join(missing[:8]),
                           " …" if len(missing) > 8 else ""))
    return [merged[k] for k in sorted(merged)]


def write_outputs(index: Dict[str, Any], notes_dir: Path) -> Dict[str, bool]:
    """写 literature_index.json + INDEX.md + 部署 AGENTS.md。内容未变不落盘（mtime 不抖）。"""
    notes_dir = Path(notes_dir)
    wrote = {"index_json": False, "index_md": False, "agents_md": False}

    index_path = notes_dir / INDEX_JSON
    if index_path.exists():
        try:
            if _stable(json.loads(index_path.read_text(encoding="utf-8"))) == _stable(index):
                logger.info("  索引内容未变，跳过写盘")
            else:
                wrote["index_json"] = True
        except Exception:
            wrote["index_json"] = True
    else:
        wrote["index_json"] = True
    if wrote["index_json"]:
        # 索引是跨运行去重的真理源，截断即触发 load_seen_keys fail-fast——原子写掉。
        # tmp 名掺 pid（同 write_if_changed），防双写者互相截断。
        tmp = index_path.with_suffix(index_path.suffix + ".tmp-{}".format(os.getpid()))
        tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, index_path)

    md = build_index_md(index)
    old_md = (notes_dir / INDEX_MD)
    if wrote["index_json"] or not old_md.exists():
        wrote["index_md"] = _write_if_changed(old_md, md)

    src = _agents_source()
    if src:
        wrote["agents_md"] = _write_if_changed(notes_dir / AGENTS_MD,
                                               src.read_text(encoding="utf-8"))

    refs = build_all_references(index, notes_dir)
    wrote["all_references"] = _write_if_changed(
        notes_dir / ALL_REFS_JSON,
        json.dumps(refs, ensure_ascii=False, indent=2) + "\n")
    if wrote["all_references"]:
        logger.info("  📚 all_references.json：全局书目 {} 条".format(len(refs)))
    return wrote


# ---------------- citekey 撞键修复 ----------------

def _atomic_write(path: Path, content: str) -> None:
    """原子写：避免崩溃导致 md/references.json/sidecar 三文件不一致。"""
    tmp = path.with_suffix(path.suffix + ".tmp-{}".format(os.getpid()))
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


RENAME_OK = "ok"              # 三处（凡存在者）都已改到位
RENAME_REFUSED = "refused"    # 一处都没改，磁盘与调用前逐字节相同
RENAME_PARTIAL = "partial"    # 写盘中途失败且回滚也失败——磁盘处于半改状态


def _pick_rename_row(cand: List[Dict[str, Any]], entry: Dict[str, Any], *,
                     doi_field: str, title_field: str,
                     key_field: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """同文件同 id/citekey 多行时挑出 entry 真正对应的那一行；无法唯一定位返回 None。

    撞键修复场景下「同文件同 id 多行」不是罕见防御而是常态：两篇不同论文共享同一
    citekey 才需要修。此时退到 cand[0] 会命中 rank 靠前的 **keeper** 那行——md 侧按
    note_line 改的是 dup 的节，sidecar/references 侧却改了 keeper 的行，两侧身份互换，
    下次重建索引后两条 entry 互指对方小节、CSL 条目挂错键。故按 dedup_key → DOI →
    标题三级精确消歧，全部失配就返回 None 让调用方拒绝改键（宁可不修，不能修错）。
    """
    if len(cand) == 1:
        return cand[0]
    if not cand:
        return None
    if key_field:
        dk = entry.get("dedup_key") or ""
        hits = [c for c in cand if dk and c.get(key_field) == dk]
        if len(hits) == 1:
            return hits[0]
    doi = (entry.get("doi") or "").lower()
    if doi:
        hits = [c for c in cand if (c.get(doi_field) or "").lower() == doi]
        if len(hits) == 1:
            return hits[0]
    tn = _norm_title(entry.get("title"))
    if tn:
        hits = [c for c in cand if _norm_title(c.get(title_field)) == tn]
        if len(hits) == 1:
            return hits[0]
    return None


def _rename_citekey_in_note(notes_dir: Path, entry: Dict[str, Any],
                            old: str, new: str) -> str:
    """把 entry 所在札记里的 [@old] 改为 [@new]，并同步 references.json 与 sidecar。

    返回三态字符串（**不是 bool**，别写 `if _rename_citekey_in_note(...)`——
    "refused" 是真值，会被当成成功）：
      - RENAME_OK      三处（凡存在者）都改到位；
      - RENAME_REFUSED 磁盘零改动（预检不过，或写盘失败但已成功回滚）；
      - RENAME_PARTIAL 写盘失败**且回滚也失败** → 磁盘半改，必须人工核对。

    必须按 entry.note_line 定点替换——同 citekey 在同一份 md 里可以出现多次（近重复
    文献各自精读），"全文首个命中" 回退会在这种情况下改错节：改的是另一条同 key 条目
    的标题行，而当前 entry 真正所在的那一节反而没改。note_line 没命中就跳过并报错，
    不瞎猜。

    ⚠️ **先全量预检、再全量写盘，写盘失败按逆序回滚**（要么三处都改、要么一处不动）。
    最早的写法是「md 先原子写，refs/sidecar 的异常吞成 warning、函数照样返回 True」，
    于是调用方把它当成三处都成功：
      - refs 没同步 → md 里已是 [@new]、references.json 里还是 id: old，pandoc 出稿时
        该引用**解析不到条目**（本仓库最在意的「引用静默失效」），而脚本汇报为成功；
      - sidecar 没同步 → build_month_entries 优先采信 sidecar，下一次索引重建会把 md 里
        改好的键覆盖回旧值，撞键永远修不掉。
    加了预检之后这个状态只是从预检阶段挪到了写盘阶段：pending 顺序是 [md, refs, sidecar]，
    md 先落盘，第 2/3 个文件写失败（磁盘满、卷只读、tmp 路径被占）时 md 已是新键，而函数
    返回 False、调用方汇报「磁盘未改动」——重跑必然再失败（md 里已无 [@old]），札记就永久
    停在半改状态且不再有任何新信号。更糟的是 fix_citekey_collisions 只在成功时占用新键，
    于是同一个 new 会再发给下一条撞键条目：**修撞键的工具在磁盘上新造一个撞键**。
    故写盘阶段留存每个文件的原内容，任一步失败就按已写成功的逆序写回原内容。

    refs 里查无此 id **不算失败**：那说明本来就没有对应条目（引用原就悬空），改不改
    都不会新造出不一致。sidecar 查无此 citekey 则算失败——它会把 md 的改动顶回去。
    """
    md = Path(notes_dir) / entry["note_file"]
    try:
        md_old_text = md.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("  ⚠️ 读札记失败，跳过改键 {}: {}".format(md, e))
        return RENAME_REFUSED
    lines = md_old_text.splitlines()
    tag_old, tag_new = "[@{}]".format(old), "[@{}]".format(new)
    ln = entry.get("note_line")
    if ln and 1 <= ln <= len(lines) and tag_old in lines[ln - 1]:
        hit_line = ln - 1
    else:
        logger.warning(
            "  ⚠️ {} 的 note_line={} 未命中 {}，跳过改键（同 key 多节时拒绝瞎猜首个命中）".format(
                md.name, ln, tag_old))
        return RENAME_REFUSED
    lines[hit_line] = lines[hit_line].replace(tag_old, tag_new)
    # (路径, 新内容, 原内容)：原内容留着给写盘失败时逆序回滚用
    pending: List[Any] = [(md, "\n".join(lines) + "\n", md_old_text)]

    # ---- 预检 references.json ----
    ref_name = entry.get("references_json")
    if ref_name:
        rp = Path(notes_dir) / ref_name
        if not rp.exists():
            logger.warning("  ⚠️ 索引记着 {} 却找不到该文件，拒绝改键 {}（md 未动）"
                           .format(ref_name, old))
            return RENAME_REFUSED
        try:
            rp_old_text = rp.read_text(encoding="utf-8")
            items = json.loads(rp_old_text)
            cand = [it for it in items if isinstance(it, dict) and it.get("id") == old]
            # 撞键场景下同文件同 id 多行是常态，须精确消歧（见 _pick_rename_row）
            tgt = _pick_rename_row(cand, entry, doi_field="DOI", title_field="title")
            if cand and tgt is None:
                logger.warning(
                    "  ⚠️ references.json 中 id={} 有 {} 行且 DOI/标题均无法唯一定位，"
                    "拒绝改键（md 未动；改错行会把 keeper 的 CSL 条目挂到新键上）"
                    .format(old, len(cand)))
                return RENAME_REFUSED
            if tgt is not None:
                tgt["id"] = new
                pending.append((rp, json.dumps(items, ensure_ascii=False, indent=2),
                                rp_old_text))
        except Exception as e:
            logger.warning("  ⚠️ references.json 不可解析（{}）: {}；拒绝改键 {}（md 未动）"
                           .format(ref_name, e, old))
            return RENAME_REFUSED

    # ---- 预检 sidecar `{stem}.index.json` ----
    sc = Path(notes_dir) / "{}.index.json".format(Path(entry["note_file"]).stem)
    if sc.exists():
        try:
            sc_old_text = sc.read_text(encoding="utf-8")
            data = json.loads(sc_old_text)
            rows = data if isinstance(data, list) else data.get("papers", [])
            cand = [r for r in rows if isinstance(r, dict) and r.get("citekey") == old]
            tgt = _pick_rename_row(cand, entry, doi_field="doi", title_field="title",
                                   key_field="dedup_key")
            if tgt is None:
                logger.warning("  ⚠️ sidecar {} 中未找到 {}（或多行且 dedup_key/DOI/标题均"
                               "无法唯一定位），拒绝改键（否则下次重建会把 md 改回旧值，"
                               "或把 keeper 行改成新键造成两侧身份互换）"
                               .format(sc.name, old))
                return RENAME_REFUSED
            tgt["citekey"] = new
            pending.append((sc, json.dumps(data, ensure_ascii=False, indent=2), sc_old_text))
        except Exception as e:
            logger.warning("  ⚠️ sidecar 不可解析（{}）: {}；拒绝改键 {}（md 未动）"
                           .format(sc.name, e, old))
            return RENAME_REFUSED

    # ---- 预检全过，逐个原子写；任一步失败则逆序回滚已写的 ----
    written: List[Any] = []
    for path, content, old_content in pending:
        try:
            _atomic_write(path, content)
        except Exception as e:
            logger.error("  ❌ 写入 {} 失败：{}；{} → {} 开始回滚已改的 {} 个文件"
                         .format(path.name, e, old, new, len(written)))
            rollback_failed = []
            for done_path, done_old in reversed(written):
                try:
                    _atomic_write(done_path, done_old)
                except Exception as e2:
                    rollback_failed.append(done_path.name)
                    logger.error("  ❌ 回滚 {} 失败：{}".format(done_path.name, e2))
            if rollback_failed:
                logger.error("  ⛔ {} → {} 已半改且回滚失败（{}），请人工核对 md/references/"
                             "sidecar 三处".format(old, new, "、".join(rollback_failed)))
                return RENAME_PARTIAL
            logger.warning("  ↩️ {} → {} 写盘失败已全部回滚，磁盘未改动".format(old, new))
            return RENAME_REFUSED
        written.append((path, old_content))
    return RENAME_OK


REKEY_SYNC_HINT = "PYTHONPATH=. python scripts/notes_embed.py"
REKEY_RENDER_HINT = "scripts/render_notes.sh"


def announce_rekey_side_effects(notes_dir: Path,
                                renamed_entries: List[Dict[str, Any]],
                                *, settings: Any = None) -> Dict[str, Any]:
    """改键收尾：把两个**派生物**的失效讲出来，并 best-effort 把向量库同步回去。

    改 citekey 只落在 md + references.json + sidecar 三处。另外两样东西也带着 citekey，
    却没有任何机制跟着变：
      - 向量库 embeddings.sqlite3：chunk id 内嵌 citekey（`p:<citekey>`），chunks 表还有
        citekey / year 两列，是检索结果回传给写作侧的唯一身份来源。库一旧，notes_search
        --cite 就会吐出磁盘上已不存在的死键（pandoc 渲染成 `(key?)`），digest 的近邻注入
        还会把旧键旧年份喂给 LLM 裁决。机制本身是好的——sync_store 的 diff 会让旧 id 消失、
        新 id 出现——缺的纯粹是**触发**：全仓 sync_store 的调用点原本全在 ingest 侧，
        两条改键路径一个都不覆盖，改完就静默退出，运维零信号。
      - 已渲染的 docx：人读/传阅的成品，正文里写死了 citekey，读者据此回查会查不到。
        这里只列清单交给人重渲染（`scripts/render_notes.sh`）。
        注：手动精读那份现在**可以**安全地由 read_pdf.py finalize/regen 重渲染——
        `_reuse_citekeys` 已按 dedup_key 沿用札记侧的现有键（2026-08-15）；
        在那之前 regen 会用 bundle 里的旧元数据重算兜底键、把改过的新键顶回去。

    向量库同步走 best-effort（比照 ingest_notes.py 的挂钩）：Ollama 没起、模型没 pull、
    库被别的进程锁着，都只 log warning 并打出重建命令，绝不改变改键本身的成败。
    库文件不存在时直接跳过同步（没建过向量库的环境/临时目录用不着），只打提示。
    """
    out: Dict[str, Any] = {"stale_docx": [], "synced": False, "error": None}
    if not renamed_entries:
        return out

    notes_dir = Path(notes_dir)
    seen: Set[str] = set()
    for e in renamed_entries:
        nf = (e or {}).get("note_file") or ""
        if not nf.endswith(".md"):
            continue
        docx = notes_dir / (nf[:-3] + ".docx")
        if docx.exists() and str(docx) not in seen:
            seen.add(str(docx))
            out["stale_docx"].append({"month": (e or {}).get("month"), "path": str(docx)})
    out["stale_docx"].sort(key=lambda d: d["path"])

    if out["stale_docx"]:
        logger.warning("  ⚠️ {} 份已渲染 docx 内嵌的是旧 citekey，需重渲染（受影响月份 {}）："
                       .format(len(out["stale_docx"]),
                               ", ".join(sorted({str(d["month"]) for d in out["stale_docx"]}))))
        for d in out["stale_docx"]:
            logger.warning("      {}".format(d["path"]))
        logger.warning("      重渲染：{} <该月 .md>".format(REKEY_RENDER_HINT))

    from .embed_store import DB_NAME
    db_path = notes_dir / DB_NAME
    if not db_path.exists():
        logger.warning("  ⚠️ 已改 {} 个 citekey。向量库 {} 不存在，跳过同步；"
                       "建库后请跑：{}".format(len(renamed_entries), db_path.name, REKEY_SYNC_HINT))
        return out

    logger.warning("  ⚠️ 已改 {} 个 citekey，向量库 {} 即刻失效（里面仍是旧键，"
                   "notes_search --cite 会吐死引用）。现在尝试自动同步；"
                   "若失败请手动跑：{}".format(len(renamed_entries), db_path.name, REKEY_SYNC_HINT))
    try:
        from .embed_store import sync_store
        from .embeddings import EmbeddingClient, resolve_embedding_base_url
        if settings is None:
            from .paths import repo_path
            from .schema import ScholarSettings
            cfg = repo_path("config/scholar.env")
            settings = ScholarSettings.from_env_file(cfg) if cfg.exists() else ScholarSettings()
        # 必须拿**改键之后**的索引去同步：磁盘上那份 literature_index.json 此刻还是旧键，
        # 拿它 diff 等于什么都不改。刷完顺手落盘，调用方紧接着的那次重建会自然变成空跑。
        index_data = update_index(notes_dir)
        write_outputs(index_data, notes_dir)
        client = EmbeddingClient(
            base_url=resolve_embedding_base_url(settings.llm),
            model=settings.llm.embedding_model,
        )
        try:
            stats = sync_store(db_path, index_data, client)
        finally:
            client.close()
        out["synced"] = True
        out["stats"] = {"embedded": stats.embedded, "deleted": stats.deleted,
                        "meta_refreshed": stats.meta_refreshed}
        logger.info("  ✅ 向量库已同步：+{} 嵌入 / -{} 删除 / {} 元数据刷新".format(
            stats.embedded, stats.deleted, stats.meta_refreshed))
    except Exception as e:
        out["error"] = "{}: {}".format(type(e).__name__, e)
        logger.warning("  ⚠️ 向量库自动同步失败（改键本身已完成，不影响退出码）：{}\n"
                       "      向量库现在是**陈旧**的，检索会吐已注销的旧 citekey，"
                       "务必手动跑：{}".format(out["error"], REKEY_SYNC_HINT))
    return out


def fix_citekey_collisions(notes_dir: Path) -> int:
    """自动修复撞键：同 citekey 指向不同论文时，保最早月不动，
    后出现者加 b/c… 后缀（仿 BBT 消歧），就地改 md + references.json。

    返回重命名条数；调用方随后应重建索引（md 已变更）。docx 为人读版不回写。

    _rename_citekey_in_note 的三态必须**分流**处理（别当 bool 用）：
      - RENAME_REFUSED 一处都没改，新键没落到磁盘上 → 不占用 all_keys，下一条撞键条目
        沿用同一个后缀正合语义；汇总里说「磁盘未改动」是实话。
      - RENAME_PARTIAL 新键**已经写在 md 上**了 → 必须 all_keys.add(new)，否则同组的下一条
        会拿到同一个新键，修撞键的工具反而在磁盘上新造一个撞键；且汇总不得说「磁盘未改动」，
        要单列成「已半改」优先展示。
    """
    notes_dir = Path(notes_dir)
    index = update_index(notes_dir)
    live = [e for e in index["papers"] if not e.get("duplicate_of")]
    all_keys = {e.get("citekey") for e in index["papers"]}
    by_key: Dict[str, List[Dict[str, Any]]] = {}
    for e in live:
        by_key.setdefault(e.get("citekey") or "", []).append(e)
    renamed = 0
    failed: List[str] = []
    partial: List[str] = []
    touched: List[Dict[str, Any]] = []      # 键真落到磁盘上的条目（OK + PARTIAL），供收尾告知派生物
    for key, group in sorted(by_key.items()):
        if not key or len(group) <= 1 or len({e["dedup_key"] for e in group}) <= 1:
            continue
        group.sort(key=lambda e: (e["month"], e.get("priority_rank") or 9999))
        for e in group[1:]:                      # 最早月保留原键
            new = None
            for suf in _suffix_seq():
                cand = "{}{}".format(key, suf)
                if cand not in all_keys:
                    new = cand
                    break
            res = _rename_citekey_in_note(notes_dir, e, key, new)
            desc = "{} → {}（{} / {}）".format(key, new, e["month"], e["note_file"])
            if res == RENAME_OK:
                all_keys.add(new)
                renamed += 1
                touched.append(e)
                logger.info("  🔧 改键 {} → {}（{}）".format(key, new, e["month"]))
            elif res == RENAME_PARTIAL:
                all_keys.add(new)                # 键已落在 md 上，绝不能再发给下一条
                partial.append(desc)
                touched.append(e)                # 新键已在磁盘上 → 派生物同样失效，一并告知
            else:
                failed.append(desc)
    if partial:
        logger.error("  ⛔ {} 条改键**已半改且回滚失败**（md/references/sidecar 可能不一致），"
                     "务必优先人工核对：".format(len(partial)))
        for s in partial:
            logger.error("      {}".format(s))
    if failed:
        logger.warning("  ⚠️ {} 条撞键未能修复（md/references/sidecar 预检不过，磁盘未改动）"
                       "，需人工处理：".format(len(failed)))
        for s in failed:
            logger.warning("      {}".format(s))
    # 派生物（向量库 / docx）不会自己跟着改键走，收尾统一告知 + best-effort 同步。
    # 判据用 touched 而非 renamed：半改条目的新键也已经落在磁盘上，派生物照样失效。
    announce_rekey_side_effects(notes_dir, touched)
    return renamed


# ---------------- backfill 去重集 ----------------

def existing_citekeys(index_path: Path,
                      exclude_note_files: Optional[Set[str]] = None) -> Set[str]:
    """从索引读取全部 citekey 全集，供兜底键生成时判断「库内已占用」。

    exclude_note_files：本次要整篇重写的札记文件名集合（如 {"科研札记_2024-03_全文精读.md"}）。
    这些文件里的旧条目必须剔除——否则本批重算出同样的兜底键会被判「已占用」而加消歧
    后缀，下一轮又因为后缀键才是「已占用」而改回原键，来回改名（citekey 抖动）。
    同一坑三处链路共用：backfill_notes.run_month（按月）、ingest.run_ingest（按周）、
    read_pdf._rebuild_month（历史已修，现改调本函数）。
    """
    p = Path(index_path)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return set()   # 索引损坏时退化为空集（仅影响本次消歧判断，不影响去重正确性）
    excl = exclude_note_files or set()
    return {e.get("citekey") for e in data.get("papers", [])
            if e.get("citekey") and e.get("note_file") not in excl}


def load_seen_keys(index_path: Path,
                   exclude_months: Optional[Set[str]] = None) -> Set[str]:
    """从索引恢复全局去重键集合（供 backfill 跨运行去重）。文件不存在返回空集。

    exclude_months：--force 重跑月份时剔除该月涉及的键，避免自 dedup 成空札记。
    按**键**整体剔除而非按条目 month 过滤——同一 dedup_key 可能同时以 keeper 身份
    落在重跑月、又以 duplicate_of 身份落在别的月；只剔本月条目会让该键残留在
    seen 里，重跑时把这篇论文 dedup 掉（丢篇）。

    只剔除 series=="auto" 的条目键：手动深读（manual）的键恒留在 seen 里，令自动回填
    始终跳过已被手动深度精读的论文（避免同一篇又生成一条浅读重复条目）——这是期望行为。
    """
    p = Path(index_path)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        # 索引存在却读不出来时以空集继续会让整窗论文重复入库——比中止更难收拾，
        # 宁可让 backfill/ingest fail-fast，人工修好索引再跑
        raise RuntimeError(
            "文献索引 {} 存在但解析失败（{}），拒绝以空去重集继续入库".format(p, e)
        ) from e
    papers = [e for e in data.get("papers", []) if e.get("dedup_key")]
    excl_months = exclude_months or set()
    # 缺 series 字段的旧条目按 auto 处理（向后兼容）
    excluded_keys = {e["dedup_key"] for e in papers
                     if e.get("month") in excl_months and e.get("series", "auto") == "auto"}
    return {e["dedup_key"] for e in papers if e["dedup_key"] not in excluded_keys}
