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
from ..utils.logger import get_logger

logger = get_logger(__name__)

# 句级联想标记（与 docx 三色一致）：方法学创新=墨绿 / 重要发现=紫 / 研究背景=蓝
_TAG_MARK = {"方法学创新", "重要发现", "研究背景"}

_STOP_WORDS = {"the", "a", "an", "of", "for", "and", "with", "using", "based",
               "from", "into", "via", "toward", "towards", "study", "novel"}


def _fallback_citekey(meta: PaperMetadata) -> str:
    """headless 回填无 Zotero key 时，生成人读的临时引用键（作者姓+年+标题实词，仿 BBT 动态键）。"""
    author = ""
    if meta.authors:
        first = (meta.authors[0] or "").strip()
        surname = first.split(",")[0].split()[-1] if first else ""
        author = "".join(c for c in surname if c.isalpha()).lower()
    year = ""
    if getattr(meta, "publication_date", None):
        year = str(meta.publication_date.year)
    elif getattr(meta, "email_received_at", None):
        year = str(meta.email_received_at.year)
    word = ""
    for w in (meta.title or "").split():
        cw = "".join(c for c in w if c.isalnum())
        if len(cw) >= 4 and cw.lower() not in _STOP_WORDS:
            word = cw[:1].upper() + cw[1:].lower()
            break
    key = "{}{}{}".format(author or "anon", year, word)
    return key or "ref{}".format((meta.doi or meta.paper_id or "x")[:8])


def _slug(text: str, maxlen: int = 60) -> str:
    """文件名安全的 slug。"""
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in (text or "").strip()]
    s = "".join(keep).strip("-")
    while "--" in s:
        s = s.replace("--", "-")
    return (s or "untitled")[:maxlen]


def _yaml_list(items: List[str]) -> str:
    return "[" + ", ".join(json.dumps(i, ensure_ascii=False) for i in items) + "]"


def _priority_tier(rank: int, total: int) -> str:
    """按排名分三级着色标记（Word 里 emoji 可见，跨格式稳定）。"""
    if total <= 1:
        return "🔴"
    if rank < max(1, total // 3):
        return "🔴 高"
    if rank < max(2, 2 * total // 3):
        return "🟠 中"
    return "🟢 低"


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
        label = "全文精读" if cr.from_full_text else "精读（仅摘要降级）"
        src = " · 来源 `{}`".format(cr.source) if cr.source else ""
        body.extend(["", "{} {}{}".format(sub, label, src), ""])
        for sec in cr.sections:
            body.append("**【{}】**".format(sec.heading))
            for st in sec.sentences:
                marker = "〔{}〕".format(st.tag) if st.tag in _TAG_MARK else ""
                body.append("- {}{}".format(marker, st.text))
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
                        "> 句级联想标记：〔方法学创新〕〔重要发现〕〔研究背景〕（关联研究主线；docx 版对应墨绿/紫/蓝三色）。", ""]
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
    item: Dict[str, Any] = {
        "id": citekey,
        "type": "article-journal" if not meta.arxiv_id or meta.doi else "article",
        "title": meta.title or "",
    }
    authors = []
    for a in (meta.authors or []):
        n = a.strip()
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
    if meta.journal:
        item["container-title"] = meta.journal
    if meta.doi:
        item["DOI"] = meta.doi
    if meta.url:
        item["URL"] = meta.url
    if meta.volume:
        item["volume"] = meta.volume
    if meta.issue:
        item["issue"] = meta.issue
    if meta.pages:
        item["page"] = meta.pages
    if meta.publication_date:
        d = meta.publication_date
        item["issued"] = {"date-parts": [[d.year, d.month, d.day]]}
    return item


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
) -> Dict[str, Any]:
    """把一个时间窗的论文聚合成【单篇】pandoc-ready 札记 + 一份 references.json（CSL-JSON）。

    Args:
        segments: 入选论文（同一时间窗）
        citekeys: paper_id -> citekey（None 表示未解析到，正文用占位键）
        out_dir: 输出目录
        digest_title: 聚合札记标题
        filename: 输出文件名（不含扩展名）
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
        used = {v for v in citekeys.values() if v}
        for seg in segments:
            if citekeys.get(seg.paper_id):
                continue
            base = _fallback_citekey(seg.metadata)
            key, suf = base, ord("a")
            while key in used:
                key = "{}{}".format(base, chr(suf))
                suf += 1
            used.add(key)
            citekeys[seg.paper_id] = key

    csl_items = [build_csl_item(seg.metadata, citekeys[seg.paper_id])
                 for seg in segments if citekeys.get(seg.paper_id)]

    slug = _slug(filename, 80) or "scholar_digest"
    note_path = out_dir / "{}.md".format(slug)
    with open(note_path, "w", encoding="utf-8") as f:
        f.write(build_digest_note(segments, citekeys, title=digest_title, instruction=instruction))

    # 每篇札记配套独立的 references.json（按文件名区分），避免按月回填时互相覆盖
    ref_path = out_dir / "{}.references.json".format(slug)
    with open(ref_path, "w", encoding="utf-8") as f:
        json.dump(csl_items, f, ensure_ascii=False, indent=2)

    # 索引 sidecar：从内存对象无损导出结构化条目（含 arxiv_id/priority_score/三色计数），
    # 供 notes_index 聚合——新札记不再依赖 md 反向解析。排序与 build_digest_note 一致。
    sidecar_path = None
    if emit_index_sidecar:
        try:
            from .notes_index import entry_from_segment  # 延迟导入，避免循环依赖
            ordered = sorted(segments, key=lambda s: s.priority_score, reverse=True)
            entries = []
            for i, seg in enumerate(ordered):
                key = citekeys.get(seg.paper_id) or \
                    "MISSING-KEY-{}".format(seg.metadata.doi or seg.paper_id[:8])
                # 占位键（未开兜底且无 Zotero key）标 "missing"，消费方据此过滤
                src = ("zotero" if seg.paper_id in zotero_keyed
                       else "missing" if key.startswith("MISSING-KEY-") else "fallback")
                entries.append(entry_from_segment(seg, key, rank=i, total=len(ordered),
                                                  citekey_source=src))
            sidecar_path = out_dir / "{}.index.json".format(slug)
            with open(sidecar_path, "w", encoding="utf-8") as f:
                json.dump({"schema_version": 1, "papers": entries}, f,
                          ensure_ascii=False, indent=2)
        except Exception as e:
            sidecar_path = None
            logger.warning("  ⚠️ 写索引 sidecar 失败（不影响札记）: {}".format(e))

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
