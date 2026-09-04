# -*- coding: utf-8 -*-
"""
pandoc-ready 科研札记生成。

把入库拿到 citekey 的论文渲染成：
  - 每篇一份 markdown 札记（YAML front matter + 正文 `[@citekey]` pandoc 引用 + 裁决/摘要/AI归纳 + 留白手记）；
  - 一份 references.json（CSL-JSON），使札记在未配置 BBT 自动导出时也能被 pandoc 渲染（自包含兜底）。

真正的引用解析可由 Zotero + Better BibTeX 的 references.bib/json（自动更新）承担；
本模块的 CSL-JSON 只是「即使没接 BBT 导出也能 pandoc」的保险，二者可任选其一。

渲染示例：
  pandoc note.md --citeproc --bibliography=note.references.json --csl=nature.csl -o note.docx
"""
import json
from pathlib import Path
from typing import List, Dict, Any, Optional

from .schema import PaperSegment, PaperMetadata
from ._citekey_utils import (_fallback_citekey, _priority_tier, _suffix_seq,
                             entry_from_segment, date_parts, build_csl_common,
                             render_tag_line, TAG_MARK)
from ..utils.logger import get_logger

logger = get_logger(__name__)

# 句级角色标记（渲染为〔〕marker；docx 对应三色）。收新旧六类：
#   新（换轴后）：可引用证据 / 可反驳观点 / 方法论借鉴
#   旧（历史 bundle，原样保留以诚实反映当时标注）：方法学创新 / 重要发现 / 研究背景
# 语法（含可选页码锚）由 _citekey_utils 单点持有，渲染走 render_tag_line，解析走 TAG_LINE_RE。
_TAG_MARK = set(TAG_MARK)


def _book_line(meta) -> Optional[str]:
    """书籍/章条目的容器元数据行；非书条目返回 None（文章渲染逐字节不变）。

    格式契约见 notes_index._parse_book_line —— 两侧必须同改。
    """
    if not getattr(meta, "entry_type", None):
        return None
    parts: List[str] = []
    container = meta.container_title or (meta.title if meta.entry_type == "book" else None)
    if container:
        parts.append(container)
    if meta.isbn:
        parts.append("ISBN {}".format(meta.isbn))
    if meta.chapter_number is not None:
        parts.append("第{}章".format(meta.chapter_number))
    if meta.page_range:
        parts.append("pp.{}".format(meta.page_range))
    if meta.publisher:
        parts.append("出版 {}".format(meta.publisher))
    if meta.edition:
        parts.append("{} 版".format(meta.edition))
    if meta.editors:
        parts.append("编者 {}".format("; ".join(meta.editors)))
    if meta.book_key:
        parts.append("[@{}]".format(meta.book_key))
    return "**所属书籍**: {}".format(" · ".join(parts)) if parts else None


def _slug(text: str, maxlen: int = 60) -> str:
    """文件名安全的 slug。"""
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in (text or "").strip()]
    s = "".join(keep).strip("-")
    while "--" in s:
        s = s.replace("--", "-")
    return (s or "untitled")[:maxlen]


def _yaml_list(items: List[str]) -> str:
    return "[" + ", ".join(json.dumps(i, ensure_ascii=False) for i in items) + "]"


def _paper_section(seg: PaperSegment, citekey: Optional[str], index: Optional[int] = None,
                   level: int = 2, tier: str = "") -> List[str]:
    """渲染单篇论文的 markdown 段落（不含 YAML front matter）。

    level=1 用于单篇文档的一级标题；level=2 用于聚合文档里每篇一节。
    index 非空时在标题前加序号；tier 非空时在标题前加优先级着色标记。citekey 为 None 时用占位键。
    """
    meta = seg.metadata
    fd = seg.filter_decision
    cite = citekey or (f"MISSING-KEY-{meta.doi or meta.paper_id[:8]}")

    h = "#" * level
    sub = "#" * (level + 1)
    prefix = "{} ".format(tier) if tier else ""
    num = "{}. ".format(index) if index else ""
    body: List[str] = ["{} {}{}{} [@{}]".format(h, prefix, num, meta.title or "(无标题)", cite), ""]
    if seg.priority_score:
        body.append("**优先级**: `{:.2f}`{}".format(
            seg.priority_score, " · {}".format(meta.priority_reason) if meta.priority_reason else ""))

    # 裁决徽章
    if fd:
        parts = []
        if fd.decision:
            parts.append("`{}`".format(fd.decision))
        if fd.bucket:
            parts.append("维度 {}".format("/".join(fd.bucket)))
        if fd.flags:
            parts.append("⚑ " + "/".join(fd.flags))
        if fd.role and fd.role != "NONE":
            parts.append("角色 {}".format(fd.role))
        if fd.confidence is not None:
            parts.append("conf {:.2f}".format(fd.confidence))
        if parts:
            body.append("**裁决**: {}".format(" · ".join(parts)))
        if fd.one_line:
            body.append("**一句话用处**: {}".format(fd.one_line))

    # 元信息
    if meta.authors:
        body.append("**作者**: {}{}".format(", ".join(meta.authors[:5]),
                                            " et al." if len(meta.authors) > 5 else ""))
    if meta.journal:
        body.append("**期刊/来源**: {}".format(meta.journal))
    book_line = _book_line(meta)
    if book_line:
        body.append(book_line)
    if meta.doi:
        body.append("**DOI**: [{0}](https://doi.org/{0})".format(meta.doi))
    elif meta.url:
        body.append("**链接**: {}".format(meta.url))

    # 摘要
    body.extend(["", "{} 摘要".format(sub), ""])
    body.append(seg.translated_abstract or seg.original_abstract or "*摘要暂无*")

    # 全文精读（句级三色联想）——有则优先展示，替代摘要级 AI 归纳
    cr = seg.close_reading
    if cr and cr.sections:
        # 标题两态，且**必须**保持两态：它是 notes_index._CLOSEREAD_RE 的机读锚点，
        # 往里塞括号后缀会让「· 来源」那个可选组匹配空，静默丢掉 reading_source
        # （2026-08-28 实测踩过，回归测试见 test_closeread_chunk_coupling.py）。
        label = "全文精读" if cr.from_full_text else "精读（仅摘要降级）"
        src = " · 来源 `{}`".format(cr.source) if cr.source else ""
        body.extend(["", "{} {}{}".format(sub, label, src), ""])
        for sec in cr.sections:
            body.append("**【{}】**".format(sec.heading))
            for st in sec.sentences:
                body.append(render_tag_line(st.tag, st.text, getattr(st, "page", None)))
            body.append("")
    elif seg.summary:
        # 无精读时退回摘要级 AI 归纳
        body.extend(["", "{} AI 归纳".format(sub), "", seg.summary])

    body.append("")
    return body


def build_note(seg: PaperSegment, citekey: Optional[str], instruction: str = "") -> str:
    """渲染单篇 pandoc-ready 札记 markdown（含 YAML front matter）。"""
    meta = seg.metadata
    fd = seg.filter_decision
    cite = citekey or (f"MISSING-KEY-{meta.doi or meta.paper_id[:8]}")

    fm: List[str] = ["---"]
    fm.append("title: {}".format(json.dumps(meta.title or "", ensure_ascii=False)))
    fm.append("citekey: {}".format(json.dumps(cite, ensure_ascii=False)))
    if meta.doi:
        fm.append("doi: {}".format(json.dumps(meta.doi, ensure_ascii=False)))
    if fd:
        if fd.decision:
            fm.append("decision: {}".format(fd.decision))
        if fd.bucket:
            fm.append("bucket: {}".format(_yaml_list(fd.bucket)))
        if fd.flags:
            fm.append("flags: {}".format(_yaml_list(fd.flags)))
        if fd.role and fd.role != "NONE":
            fm.append("role: {}".format(fd.role))
    fm.append("---")
    fm.append("")

    body = _paper_section(seg, citekey, index=None, level=1)
    body.extend(["## 我的札记", ""])
    if instruction:
        body.append("<!-- 归纳指令: {} -->".format(instruction))
    body.append("")
    return "\n".join(fm + body) + "\n"


def build_digest_note(segments: List[PaperSegment], citekeys: Dict[str, Optional[str]],
                      title: str = "Scholar Digest", instruction: str = "") -> str:
    """把一个时间窗的所有论文聚合成一篇 pandoc-ready 札记。

    按优先级降序排列；顶部「优先级速览」表分级着色（🔴高/🟠中/🟢低）；每篇一节，末尾统一参考文献。
    """
    ordered = sorted(segments, key=lambda s: s.priority_score, reverse=True)
    total = len(ordered)
    tiers = [_priority_tier(i, total) for i in range(total)]

    fm = ["---", "title: {}".format(json.dumps(title, ensure_ascii=False)),
          'lang: zh', "---", ""]
    lines: List[str] = ["# {}".format(title), "", "共 {} 篇（按优先级降序）。".format(total), "",
                        "> 句级角色标记：〔可引用证据〕〔可反驳观点〕〔方法论借鉴〕（按对后续工作流的用途；docx 版对应墨绿/紫/蓝三色）。历史札记可能保留旧标记〔方法学创新〕〔重要发现〕〔研究背景〕。", ""]
    if instruction:
        lines.append("<!-- 归纳指令: {} -->".format(instruction))
        lines.append("")

    # 优先级速览表（着色 + 排序，一眼看清轻重）
    lines.extend(["## 优先级速览", "",
                  "| 优先级 | # | 裁决 | 标题 | 一句话用处 |",
                  "|:---:|:---:|:---:|---|---|"])
    for i, seg in enumerate(ordered):
        fd = seg.filter_decision
        decision = "`{}`".format(fd.decision) if fd and fd.decision else ""
        one_line = (fd.one_line if fd and fd.one_line else "").replace("|", "/")
        t = (seg.metadata.title or "").replace("|", "/")
        lines.append("| {} | {} | {} | {} | {} |".format(tiers[i], i + 1, decision, t, one_line))
    lines.append("")
    lines.append("---")
    lines.append("")

    # 逐篇（同一排序）
    for i, seg in enumerate(ordered):
        lines.extend(_paper_section(seg, citekeys.get(seg.paper_id), index=i + 1, level=2,
                                    tier=tiers[i]))
        lines.append("---")
        lines.append("")

    lines.extend(["# 参考文献", "", "::: {#refs}", ":::", ""])
    return "\n".join(fm + lines) + "\n"


def build_csl_item(meta: PaperMetadata, citekey: str) -> Dict[str, Any]:
    """把 PaperMetadata 映射为 CSL-JSON 条目（id=citekey），用于 pandoc 自包含兜底。"""
    issued = None
    if meta.publication_date:
        # issued = CSL 的**出版日期**（与 issue 期号无关）。按 date_precision 截断，
        # 别把补出来的占位月日当真——否则参考文献会渲染出论文并不存在的月份。
        issued = {"date-parts": date_parts(meta.publication_date,
                                           getattr(meta, "date_precision", None))}
    return build_csl_common(
        citekey=citekey, title=meta.title, authors=meta.authors,
        entry_type=getattr(meta, "entry_type", None), journal=meta.journal,
        doi=meta.doi, url=meta.url, arxiv_id=meta.arxiv_id,
        volume=meta.volume, issue=meta.issue, pages=meta.pages,
        isbn=getattr(meta, "isbn", None), publisher=getattr(meta, "publisher", None),
        edition=getattr(meta, "edition", None), editors=getattr(meta, "editors", None),
        container_title=getattr(meta, "container_title", None),
        page_range=getattr(meta, "page_range", None), issued=issued)


def write_notes(
    segments: List[PaperSegment],
    citekeys: Dict[str, Optional[str]],
    out_dir: Path,
    instruction: str = "",
    digest_title: str = "Scholar Digest",
    filename: str = "scholar_digest",
    emit_docx: bool = False,
    cjk_font: str = "",
    fallback_citekeys: bool = False,
    emit_index_sidecar: bool = True,
    index_series: str = "auto",
    existing_citekeys: Optional[set] = None,
    explicit_citekey_source: str = "zotero",
) -> Dict[str, Any]:
    """把一个时间窗的论文聚合成【单篇】pandoc-ready 札记 + 一份 references.json（CSL-JSON）。

    Args:
        segments: 入选论文（同一时间窗）
        citekeys: paper_id -> citekey（None 表示未解析到，正文用占位键）
        out_dir: 输出目录
        digest_title: 聚合札记标题
        filename: 输出文件名（不含扩展名）
        explicit_citekey_source: 显式传入的（非 None）citekey 在 sidecar 里标成哪种来源。
            默认 "zotero"——自动链路传进来的确实是 Zotero/BBT 权威键。
            但手动精读的 regen 会把**上一轮自己生成的兜底键**显式传回来沿用（见
            read_pdf._rebuild_month），那些键并非权威，必须传 "fallback"，
            否则 sidecar 会把兜底键冒充成 Zotero 键，下游按 citekey_source 判权威时会误判。
    Returns: 摘要 dict（聚合札记路径、references 路径、缺 key 数）
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # citekey 与文献元数据以 Zotero（BBT）为权威来源；未入库者用占位键并计数
    missing = sum(1 for seg in segments if not citekeys.get(seg.paper_id))
    zotero_keyed = {pid for pid, v in citekeys.items() if v}  # 兜底填充前的权威键集合

    # headless 回填：为无 Zotero key 的论文补人读临时键（去重防撞），令札记不出现 MISSING-KEY
    if fallback_citekeys:
        citekeys = dict(citekeys)
        # used 初始化为"本批已用键 + 索引已有 citekey 全集"：确保新生成的 fallback 键
        # 不与库内任何条目重名，杜绝"同一论文 auto/manual 各收一次生成同名 citekey"
        used = {v for v in citekeys.values() if v} | (existing_citekeys or set())
        for seg in segments:
            if citekeys.get(seg.paper_id):
                continue
            base = _fallback_citekey(seg.metadata)
            key = base
            if key in used:
                for suf in _suffix_seq():
                    cand = "{}{}".format(base, suf)
                    if cand not in used:
                        key = cand
                        break
            used.add(key)
            citekeys[seg.paper_id] = key

    csl_items = [build_csl_item(seg.metadata, citekeys[seg.paper_id])
                 for seg in segments if citekeys.get(seg.paper_id)]

    slug = _slug(filename, 80) or "scholar_digest"
    note_path = out_dir / "{}.md".format(slug)
    # 三件套逐文件原子落盘（复用 notes_index._atomic_write 的 tmp+os.replace，勿另写第二套），
    # 并把顺序调成 references → sidecar → md：裸 open(path, "w") 先截断再写，过夜回填中途
    # 被 SIGKILL 会留下 0 字节/半截 md——md 仍 exists()，backfill 重跑判「该月已完成」永久
    # 跳过，被截论文既不进索引/seen 也不会再被捞回（静默丢失）。md 是各调用方的完成判定锚
    # （backfill/ingest 都看它 exists()），放最后落 = 事务提交标记：中断后要么整月旧态完整、
    # 要么配套先到位而 md 未落，重跑都能完整恢复。
    from .notes_index import _atomic_write
    md_content = build_digest_note(segments, citekeys, title=digest_title, instruction=instruction)

    # 每篇札记配套独立的 references.json（按文件名区分），避免按月回填时互相覆盖
    ref_path = out_dir / "{}.references.json".format(slug)
    _atomic_write(ref_path, json.dumps(csl_items, ensure_ascii=False, indent=2))

    # 索引 sidecar：从内存对象无损导出结构化条目（含 arxiv_id/priority_score/三色计数），
    # 供 notes_index 聚合——新札记不再依赖 md 反向解析。排序与 build_digest_note 一致。
    sidecar_path = None
    if emit_index_sidecar:
        try:
            ordered = sorted(segments, key=lambda s: s.priority_score, reverse=True)
            entries = []
            for i, seg in enumerate(ordered):
                key = citekeys.get(seg.paper_id) or \
                    "MISSING-KEY-{}".format(seg.metadata.doi or seg.paper_id[:8])
                # 占位键（未开兜底且无 Zotero key）标 "missing"，消费方据此过滤
                src = (explicit_citekey_source if seg.paper_id in zotero_keyed
                       else "missing" if key.startswith("MISSING-KEY-") else "fallback")
                entries.append(entry_from_segment(seg, key, rank=i, total=len(ordered),
                                                  citekey_source=src, series=index_series))
            sidecar_path = out_dir / "{}.index.json".format(slug)
            _atomic_write(sidecar_path, json.dumps(
                {"schema_version": 1, "papers": entries}, ensure_ascii=False, indent=2))
        except Exception as e:
            sidecar_path = None
            logger.warning("  ⚠️ 写索引 sidecar 失败（不影响札记）: {}".format(e))

    # md 最后落盘（见上）：此行成功 = 本次 write_notes 事务提交
    _atomic_write(note_path, md_content)

    logger.info("  📝 聚合札记（{} 篇）→ {}".format(len(segments), note_path))
    logger.info("  📚 references.json（CSL-JSON）{} 条".format(len(csl_items)))
    if missing:
        logger.info("  ℹ️ {} 篇未从 Zotero 拿到 citekey，已用「作者+年份」兜底键（札记自包含可渲染）".format(missing))

    result = {
        "notes_dir": str(out_dir),
        "note_path": str(note_path),
        "references_json": str(ref_path),
        "csl_count": len(csl_items),
        "missing_citekey": missing,
    }
    if sidecar_path:
        result["index_sidecar"] = str(sidecar_path)

    # 样式化 docx（单元格着色/句级三色/字体区分）；延迟导入避免与 docx_builder 循环依赖
    if emit_docx:
        try:
            from .docx_builder import build_digest_docx
            docx_path = out_dir / "{}.docx".format(_slug(filename, 80) or "scholar_digest")
            build_digest_docx(segments, citekeys, docx_path, title=digest_title,
                              csl_items=csl_items, cjk_font=cjk_font, instruction=instruction)
            result["docx_path"] = str(docx_path)
        except Exception as e:
            logger.warning("  ⚠️ 生成样式化 docx 失败: {}".format(e))

    return result
