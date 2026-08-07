# -*- coding: utf-8 -*-
"""期刊全量筛选：PubMed 检索 → 标题级黑名单预筛 → filter-v3 LLM 裁决 → 候选导出。

与 ScholarWorkflow 的三点关键区别：
1. 无 Gmail 依赖——纯 PubMed 检索，按期刊名 + 日期区间捞全量
2. 黑名单只打标题（不打摘要）——因为 npj DM 里一半是医学影像，但标题含
   "Handling missingness in multimodal EHR" 这种要留；摘要里提到 "including
   imaging data" 不该被砍
3. 两阶段可中断——`--stage search` 只搜+黑名单就落盘，用户看了降量效果再
   `--stage filter` 续跑 LLM，避免一次性全量 LLM 花钱但不确定黑名单是否合理
"""
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .academic_search import AcademicSearchClient, item_to_segment
from .research_profile import (
    get_covid_safeguard_keywords,
    get_journal_blacklist_categories,
    get_omics_ehr_signal_keywords,
)
from .schema import DigestStatus, FilterDecision, PaperMetadata, PaperSegment, ScholarSettings
from ..utils.logger import get_logger

logger = get_logger("journal_screen")

# 近邻相似度 ≥ 此值视为"疑似已在札记库"（in_library 标记）。bge-m3 短文本余弦分布窄：
# 库内真实近邻 0.63-0.74、同篇自身 >0.9，0.85 取"几乎同文"档；与 workflow._library_neighbors
# 的召回门槛 min_sim=0.65 是两回事——那是"值得展示"，这是"疑似重复"。
IN_LIBRARY_SIM_THRESHOLD = 0.85

# ---------------------------------------------------------------- 黑名单词表
# 分类组织，每类有一个前缀注释说明口径。
# 匹配时大小写不敏感，按整词边界 + 复数容忍（复用 _match_title_only 的规则）。
# 词表来自 config/research_profile.yaml（journal_blacklist_categories）：
# A 类 neuroimaging 是复合词，整词匹配不命中→显式加；D 类用词干而非完整词，
# genom 可命中 genomic/genomics/genome/radiogenomic/pharmacogenomic。
JOURNAL_BLACKLIST_CATEGORIES = get_journal_blacklist_categories()

# COVID 专项：标题含 COVID/COVID-19/SARS-CoV-2 且不含以下任一词 → 排除
# 词表来自 config/research_profile.yaml（journal_screen.covid_safeguard_keywords）
COVID_SAFEGUARD_KEYWORDS = get_covid_safeguard_keywords()

# ---------------------------------------------------------------- 匹配逻辑


def _esc_cell(text: Any) -> str:
    """候选列表 Markdown 表格列消毒：裸 `|` 会提前闭合当前列、裸换行会把整行拆成
    多行——原来只给 title 做了这层转义，one_line（LLM 自由文本，最容易带 `|`）/
    bucket/role/decision/flags 这些同样拼进表格单元格的列没做，含 `|` 时会把
    后面所有列错列漂移，实测 filter-v3 的一句话用途偶尔会写成 `A|B 两种情形`
    这类带竖线的短语。所有拼进表格单元格的自由文本列统一走这一个函数。"""
    return str(text or "").replace("|", "\\|").replace("\n", " ")


def _match_title_only(title: str, keywords: List[str]) -> Optional[str]:
    """在标题中匹配关键词列表，返回命中的第一个词，无命中返回 None。

    规则：
    - ASCII 词按整词边界 + 常见后缀容忍（\\b{word}(?:e?s|y|ical|istic|ing)?\\b）
      → 例如 "radiolog" 可命中 radiology/radiological, "histolog" 可命中 histology
    - 中文词按包含匹配
    - 空关键词静默跳过
    """
    title_lower = (title or "").lower()
    for word in keywords:
        w = word.lower().strip()
        if not w:
            continue
        if re.fullmatch(r'[a-z0-9 \-]+', w):
            if re.search(r'\b{}(?:e?s|y|ical|ic|istic|ing)?\b'.format(re.escape(w)), title_lower):
                return word
        elif w in title_lower:
            return word
    return None


def _hit_blacklist(title: str) -> Optional[Tuple[str, str]]:
    """返回 (category_label, hit_keyword)，或 None。"""
    title_lower = (title or "").lower()
    for cat, words in JOURNAL_BLACKLIST_CATEGORIES.items():
        hit = _match_title_only(title, words)
        if hit is not None:
            # D-分类有特殊规则：如果标题同时出现 EHR/electronic health record
            # 或 clinical prediction 等临床词，不算命中（组学+EHR 可能是相关论文）
            if cat == "D-纯组学测序":
                # 词表来自 config/research_profile.yaml（journal_screen.omics_ehr_signal_keywords）
                ehr_signal = get_omics_ehr_signal_keywords()
                if any(s.lower() in title_lower for s in ehr_signal):
                    continue  # 不排除：组学 + EHR 组合可能是相关的
            return (cat, hit)
    return None


def _hit_covid(title: str) -> bool:
    """COVID 专项：标题含 COVID/SARS-CoV-2 且不含保护词 → 排除。"""
    title_lower = (title or "").lower()
    if not re.search(r'\b(?:covid-?19|sars-cov-?2|coronavirus)\b', title_lower):
        return False
    for kw in COVID_SAFEGUARD_KEYWORDS:
        if kw.lower() in title_lower:
            return False  # 保护词命中 → 不排除
    return True


# ---------------------------------------------------------------- 筛选器


class JournalScreener:
    """期刊全量筛选器。

    两阶段工作流：
    1. `search_and_blacklist()` — PubMed 检索 + 标题黑名单 → 落盘中间结果
    2. `run_filter()` — 读中间结果 → classify_segments(filter-v3) → 候选 JSON/MD
    """

    def __init__(self, output_dir: Path, settings: ScholarSettings):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.settings = settings

    # ---- Stage 1: search + blacklist ----

    def search_and_blacklist(
        self,
        journal_name: str,
        date_range: Tuple[date, date],
        max_results: int = 5000,
        email: str = "",
    ) -> Dict[str, Any]:
        """PubMed 检索期刊全量论文 + 标题级黑名单预筛。

        Returns:
            dict with keys:
            - journal, date_range, total_pubmed, blacklist_excluded
            - blacklist_breakdown: {category: count}
            - kept: [{title, abstract, authors, doi, pmid, published, ...}]
            - excluded_blacklist: [{...}] (for audit)
        """
        s, e = date_range
        query = '"{}"[Journal]'.format(journal_name)
        logger.info("PubMed 检索: {}, {} → {}", query, s, e)

        with AcademicSearchClient(email=email) as client:
            items = client.search_pubmed(query, max_results=max_results, date_range=(s, e))

        total = len(items)
        logger.info("PubMed 命中: {} 篇", total)

        kept: List[Dict[str, Any]] = []
        excluded: List[Dict[str, Any]] = []
        breakdown: Dict[str, int] = {}

        for it in items:
            title = it.get("title", "")
            title_lower = (title or "").lower()

            # 检查 COVID
            if _hit_covid(title):
                reason = "COVID 专项（无保护词命中）"
                it["_blacklist_hit"] = reason
                it["_blacklist_category"] = "F-COVID"
                excluded.append(it)
                breakdown["F-COVID"] = breakdown.get("F-COVID", 0) + 1
                continue

            # 检查期刊黑名单
            hit = _hit_blacklist(title)
            if hit is not None:
                cat, kw = hit
                it["_blacklist_hit"] = kw
                it["_blacklist_category"] = cat
                excluded.append(it)
                breakdown[cat] = breakdown.get(cat, 0) + 1
                continue

            kept.append(it)

        logger.info(
            "黑名单排除: {} 篇（{}），保留: {} 篇",
            len(excluded),
            ", ".join("{}:{}".format(k.split("-")[0], v) for k, v in sorted(breakdown.items())),
            len(kept),
        )

        result = {
            "journal": journal_name,
            "date_range": [s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d")],
            "generated_at": datetime.now().isoformat(),
            "total_pubmed": total,
            "blacklist_excluded": len(excluded),
            "blacklist_breakdown": breakdown,
            "kept": kept,
            "excluded_blacklist": excluded,
        }
        return result

    def save_search_results(self, result: Dict[str, Any]) -> Path:
        """保存 Stage 1 结果到 JSON。"""
        out = self.output_dir / "search_results.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        logger.info("Stage 1 结果已保存: {}", out)
        return out

    def load_search_results(self) -> Dict[str, Any]:
        """加载 Stage 1 结果。"""
        out = self.output_dir / "search_results.json"
        if not out.exists():
            raise FileNotFoundError(
                "找不到 search_results.json，请先跑 --stage search。\n"
                "  预期路径: {}".format(out))
        return json.loads(out.read_text(encoding="utf-8"))

    def keyword_prefilter(self, items: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
        """用配置的白名单关键词预筛：标题或摘要命中任一词 → 保留，否则 → 跳过。

        白名单词表来自 ProcessingSettings.whitelist（52 词，覆盖 EHR/missingness/
        因果/跨中心迁移/ICU benchmark 等），与 LLM 失败时的 _fallback_partition
        用同一份词表。注意命中规则**并不完全一致**：这里 ASCII 后缀容忍
        (?:e?s|y|ical|ic|ing)? 比 workflow._match_keyword 的 (?:e?s)? 宽——预筛
        是降量粗筛宁松勿紧，回退分流是入选判定宁紧勿松，方向性差异是有意的。

        Returns: (kept, skipped, skipped_count)
        """
        whitelist = list(self.settings.processing.whitelist)
        if not whitelist:
            logger.warning("白名单为空，跳过关键词预筛")
            return items, [], 0

        kept, skipped = [], []
        for it in items:
            title = it.get("title", "") or ""
            abstract = it.get("abstract", "") or ""
            content = (title + " " + abstract).lower()

            hit = None
            for word in whitelist:
                w = (word or "").lower().strip()
                if not w:
                    continue
                if re.fullmatch(r'[a-z0-9 \-]+', w):
                    if re.search(r'\b{}(?:e?s|y|ical|ic|ing)?\b'.format(re.escape(w)), content):
                        hit = word
                        break
                elif w in content:
                    hit = word
                    break

            if hit:
                it["_whitelist_hit"] = hit
                kept.append(it)
            else:
                skipped.append(it)

        logger.info("关键词预筛: {} → {} 保留, {} 跳过（白名单 {} 词）",
                     len(items), len(kept), len(skipped), len(whitelist))
        return kept, skipped, len(skipped)

    # ---- Stage 2: filter-v3 ----

    def _empty_result(self, search_results: Dict[str, Any], total_input: int,
                      kw_skipped: int = 0) -> Dict[str, Any]:
        """run_filter 两条空结果早退分支与主路径共用的字段骨架。

        早退分支若只给 candidates/total_input 几个字段，export_candidates 拿
        result.get("journal", "journal") 当 slug 落盘成 journal_candidates.json（不是
        {真实期刊}_candidates.json），_write_candidates_md 的降量表也全兜成 0——
        真实原因（PubMed 捞到多少篇、被黑名单/关键词砍掉多少）在产物里完全看不到。
        """
        total = search_results.get("total_pubmed", 0)
        bl_excluded = search_results.get("blacklist_excluded", 0)
        return {
            "journal": search_results.get("journal", "journal"),
            "date_range": search_results.get("date_range"),
            "generated_at": datetime.now().isoformat(),
            "total_pubmed": total,
            "blacklist_excluded": bl_excluded,
            "after_blacklist": total - bl_excluded,
            "keyword_prefilter_skipped": kw_skipped,
            "total_input": total_input,
            "entered_filter": 0,
            "filter_method": "none",
            "candidates_count": 0,
            "filter_stats": {},
            "candidates": [],
        }

    def run_filter(self, search_results: Dict[str, Any],
                   prefilter: bool = True, llm_filter: bool = True) -> Dict[str, Any]:
        """关键词预筛 + filter-v3 LLM 裁决（llm_filter=False 时跳过 LLM）。

        prefilter=True（默认）：先跑白名单关键词预筛。
        llm_filter=False：只做关键词预筛，按命中词生成候选，不调 LLM。"""
        kept = search_results.get("kept", [])
        if not kept:
            logger.warning("Stage 1 无保留论文，跳过 filter")
            return self._empty_result(search_results, total_input=0)

        kw_skipped = 0
        n_input = len(kept)
        if prefilter:
            kept, _, kw_skipped = self.keyword_prefilter(kept)
            if not kept:
                logger.warning("关键词预筛后无保留论文")
                return self._empty_result(search_results, total_input=n_input,
                                          kw_skipped=kw_skipped)

        logger.info("filter-v3 裁决: {} 篇论文{}",
                     len(kept), "" if llm_filter else " (仅关键词)")

        if not llm_filter:
            # 仅关键词模式：每篇命中论文直接成为候选，不调 LLM
            return self._keyword_only_result(kept, search_results, kw_skipped)

        # ---- LLM 路径 ----
        # 构建 PaperSegment 列表
        segments = []
        for i, it in enumerate(kept):
            seg = item_to_segment(it, segment_id=i + 1)
            segments.append(seg)

        # 调 classify_segments（force_include=False：信任 filter-v3 EXCLUDE）
        from .ingest import classify_segments
        classify_segments(segments, self.settings, force_include=False)

        # 收集候选：INCLUDE + MAYBE + UNDECIDED（THREAT 是 flags 旗标，只有真正的
        # EXCLUDE 不进候选）。UNDECIDED 是 workflow._fallback_partition 在 LLM
        # 批次异常/单篇 id 缺失时挂的裁决（verdict="undecided", decision=None）：
        # 语义是「没判过」，不是「判了无关」，必须单列，不能并入 EXCLUDE 静默消失
        # ——那样用户会以为 LLM 已经判定无关，实际上根本没跑到那篇论文的裁决。
        candidates = []
        stats = {"INCLUDE": 0, "MAYBE": 0, "UNDECIDED": 0, "EXCLUDE": 0,
                 "THREAT": 0, "BENCHMARK": 0, "OVERCLAIM_PRECEDENT": 0}
        for seg in segments:
            fd = seg.filter_decision
            if fd is None:
                stats["EXCLUDE"] += 1
                continue

            if fd.verdict == "undecided" or fd.decision is None:
                decision_label = "UNDECIDED"
                stats["UNDECIDED"] += 1
            elif fd.decision == "INCLUDE":
                decision_label = "INCLUDE"
                stats["INCLUDE"] += 1
            elif fd.decision == "MAYBE":
                decision_label = "MAYBE"
                stats["MAYBE"] += 1
            else:
                stats["EXCLUDE"] += 1
                continue

            if fd.flags:
                for flag in fd.flags:
                    if flag in stats:
                        stats[flag] += 1

            candidates.append({
                "title": seg.metadata.title,
                "authors": seg.metadata.authors,
                "journal": seg.metadata.journal,
                "year": (seg.metadata.publication_date.year
                         if seg.metadata.publication_date else None),
                "doi": seg.metadata.doi,
                "pmid": seg.metadata.pmid,
                "abstract": (seg.original_abstract or "")[:800],
                "decision": decision_label,
                "bucket": fd.bucket or [],
                "role": fd.role,
                "one_line": fd.one_line or fd.reason or (
                    "LLM 裁决失败待人工复核" if decision_label == "UNDECIDED" else ""),
                "flags": fd.flags or [],
                "confidence": fd.confidence or 0.0,
                "library_neighbors": fd.library_neighbors or [],
                "in_library": bool(fd.library_neighbors
                                   and any(n.get("sim", 0) >= IN_LIBRARY_SIM_THRESHOLD
                                           for n in fd.library_neighbors)),
            })

        logger.info(
            "filter-v3 完成: INCLUDE={}, MAYBE={}, UNDECIDED={}, EXCLUDE={}, THREAT={}, BENCHMARK={}",
            stats["INCLUDE"], stats["MAYBE"], stats["UNDECIDED"], stats["EXCLUDE"],
            stats["THREAT"], stats["BENCHMARK"])
        if stats["UNDECIDED"]:
            logger.warning(
                "  ⚠️ {} 篇 LLM 裁决失败待人工复核（UNDECIDED，已计入候选而非 EXCLUDE）"
                .format(stats["UNDECIDED"]))

        candidates.sort(key=lambda c: (
            {"INCLUDE": 0, "MAYBE": 1, "UNDECIDED": 2}.get(c["decision"], 3),
            -(c.get("confidence") or 0),
        ))

        result = {
            "journal": search_results["journal"],
            "date_range": search_results["date_range"],
            "generated_at": datetime.now().isoformat(),
            "total_pubmed": search_results["total_pubmed"],
            "blacklist_excluded": search_results["blacklist_excluded"],
            "keyword_prefilter_skipped": kw_skipped,
            "after_blacklist": search_results["total_pubmed"] - search_results["blacklist_excluded"],
            "entered_filter": len(kept),
            "filter_method": "llm",
            "candidates_count": len(candidates),
            "filter_stats": stats,
            "candidates": candidates,
        }
        return result

    def _keyword_only_result(self, kept, search_results, kw_skipped):
        """仅关键词模式：每篇命中论文直接成为候选（decision=KEYWORD）。"""
        candidates = []
        for it in kept:
            hit = it.get("_whitelist_hit", "")
            pub = it.get("published")
            year = None
            if pub:
                try:
                    year = pub.year if hasattr(pub, 'year') else int(str(pub)[:4])
                except Exception:
                    pass
            candidates.append({
                "title": it.get("title", ""),
                "authors": it.get("authors", []),
                "journal": it.get("journal", ""),
                "year": year,
                "doi": it.get("doi"),
                "pmid": it.get("pmid"),
                "abstract": (it.get("abstract") or "")[:800],
                "decision": "KEYWORD",
                "bucket": [],
                "role": "NONE",
                "one_line": "关键词命中: {}".format(hit),
                "flags": [],
                "confidence": 0.5,
                "library_neighbors": [],
                "in_library": False,
                "_whitelist_hit": hit,
            })

        logger.info("仅关键词模式完成: {} 篇候选", len(candidates))

        # 按年份降序排列
        candidates.sort(key=lambda c: -(c.get("year") or 0))

        result = {
            "journal": search_results["journal"],
            "date_range": search_results["date_range"],
            "generated_at": datetime.now().isoformat(),
            "total_pubmed": search_results["total_pubmed"],
            "blacklist_excluded": search_results["blacklist_excluded"],
            "after_blacklist": search_results["total_pubmed"] - search_results["blacklist_excluded"],
            "keyword_prefilter_skipped": kw_skipped,
            "entered_filter": len(kept),
            "filter_method": "keyword_only",
            "candidates_count": len(candidates),
            "filter_stats": {"KEYWORD": len(candidates)},
            "candidates": candidates,
        }
        return result

    def export_candidates(self, result: Dict[str, Any]) -> Tuple[Path, Path]:
        """导出候选 JSON 和人类可读 Markdown。"""
        journal_slug = (result.get("journal", "journal")
                        .lower().replace(" ", "_").replace("-", "_"))

        # JSON
        json_path = self.output_dir / "{}_candidates.json".format(journal_slug)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        logger.info("候选 JSON 已保存: {}", json_path)

        # Markdown
        md_path = self.output_dir / "{}_candidates.md".format(journal_slug)
        self._write_candidates_md(result, md_path)
        logger.info("候选人读列表已保存: {}", md_path)

        return json_path, md_path

    def _write_candidates_md(self, result: Dict[str, Any], path: Path):
        """写人类可读的候选列表 Markdown。"""
        # 计算降量链路
        total = result.get("total_pubmed", 0)
        bl_excluded = result.get("blacklist_excluded", 0)
        kw_skipped = result.get("keyword_prefilter_skipped", 0)
        after_bl = total - bl_excluded
        after_kw = after_bl - kw_skipped

        filter_method = result.get("filter_method", "llm")
        method_label = {"keyword_only": "关键词白名单", "none": "无（提前终止，见下方降量表）"}.get(
            filter_method, "filter-v3 LLM")

        lines = [
            "# {} 文献筛候选列表".format(result.get("journal", "")),
            "",
            "| 项目 | 值 |",
            "|---|---|",
            "| 生成时间 | {} |".format(result.get("generated_at", "")),
            "| 日期范围 | {} 至 {} |".format(
                (result.get("date_range") or ["?", "?"])[0],
                (result.get("date_range") or ["?", "?"])[1],
            ),
            "| 筛选方法 | {} |".format(method_label),
            "| PubMed 总数 | {} |".format(total),
            "| ↓ 标题黑名单排除 | -{} → {} |".format(bl_excluded, after_bl),
            "| ↓ 关键词预筛排除 | -{} → {} |".format(kw_skipped, after_kw),
            "| **候选数** | **{}** |".format(result.get("candidates_count", "?")),
            "",
        ]

        stats = result.get("filter_stats", {})
        if stats:
            lines.append("## 裁决统计")
            lines.append("")
            lines.append("| 裁决 | 数量 |")
            lines.append("|------|------|")
            for k, v in stats.items():
                if v:
                    lines.append("| {} | {} |".format(k, v))
            lines.append("")

        candidates = result.get("candidates", [])
        if not candidates:
            lines.append("⚠️ 无候选论文。")
        else:
            lines.append("## 候选列表（{} 篇）".format(len(candidates)))
            lines.append("")
            lines.append(
                "| # | 标题 | 年份 | 裁决 | 维度 | 角色 | 一句话用途 | 置信度 | 📚 |")
            lines.append(
                "|---|------|------|------|------|------|------------|--------|-----|")
            for i, c in enumerate(candidates, 1):
                title = _esc_cell((c.get("title") or "")[:60])
                year = c.get("year") or "?"
                decision = _esc_cell(c.get("decision", "?"))
                bucket = _esc_cell(",".join(c.get("bucket", [])))
                role = _esc_cell(c.get("role") or "-")
                one_line = _esc_cell((c.get("one_line") or "")[:40])
                conf = c.get("confidence", 0)
                in_lib = "📚" if c.get("in_library") else ""
                flags = ""
                if c.get("flags"):
                    flags = " " + _esc_cell(",".join(c["flags"]))

                lines.append(
                    "| {} | {} | {} | {}{} | {} | {} | {} | {:.0%} | {} |".format(
                        i, title, year, decision, flags, bucket, role,
                        one_line, conf, in_lib))

            lines.append("")
            lines.append("## 入库操作")
            lines.append("")
            lines.append("```bash")
            lines.append("# 勾选后保存 DOI 到 picks.txt（一行一个），然后：")
            lines.append("PYTHONPATH=. python scripts/ingest_notes.py --papers picks.txt")
            lines.append("```")

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
