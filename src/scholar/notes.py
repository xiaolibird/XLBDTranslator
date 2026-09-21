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
import re
from pathlib import Path
from typing import List, Dict, Any, Optional

from .schema import PaperSegment, PaperMetadata
from ._citekey_utils import (_fallback_citekey, _priority_tier, _suffix_seq,
                             entry_from_segment, date_parts, build_csl_common,
                             render_tag_line, TAG_MARK, dedup_key_fields, _norm_title)
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


# 覆盖既有札记前的备份目录（与 backfill_deepread 的 .backfill_deepread_backup 同一思路：
# output/ 全部在 .gitignore 内，回滚不能靠 git）。
NOTE_OVERWRITE_BACKUP_DIR = ".digest_overwrite_backup"


def note_file_paths(out_dir, filename: str) -> Dict[str, Path]:
    """write_notes(filename=…) 会落的四件套路径（md / references / sidecar / docx），与
    write_notes 内部的 slug 规则**同源**——调用方要判「目标已存在」必须经此取路径，别自己拼。"""
    out_dir = Path(out_dir)
    slug = _slug(filename, 80) or "scholar_digest"
    return {
        "md": out_dir / "{}.md".format(slug),
        "references": out_dir / "{}.references.json".format(slug),
        "sidecar": out_dir / "{}.index.json".format(slug),
        "docx": out_dir / "{}.docx".format(slug),
    }


def backup_note_files(out_dir, filename: str, stamp: Optional[str] = None) -> Optional[Path]:
    """把既有四件套（凡存在者）复制到 out_dir/.digest_overwrite_backup/<时间戳>/，返回备份目录。
    一件都不存在返回 None（无需备份）。复制失败向外抛——备份不成功就不该继续覆盖。"""
    import shutil
    from datetime import datetime
    out_dir = Path(out_dir)
    existing = [p for p in note_file_paths(out_dir, filename).values() if p.exists()]
    if not existing:
        return None
    bdir = out_dir / NOTE_OVERWRITE_BACKUP_DIR / (stamp or datetime.now().strftime("%Y%m%dT%H%M%S%f"))
    bdir.mkdir(parents=True, exist_ok=True)
    for p in existing:
        shutil.copy2(p, bdir / p.name)
    return bdir


def _segment_dedup_key(meta) -> str:
    """与 notes_index.recompute_entry_key 同一套键梯（doi → arxiv → isbn/章 → 场地 id → 标题）。
    不 import ingest：ingest 在函数内 import 本模块，模块级反向 import 会成环。
    书/章条目要带 isbn/chapter_number，否则章的键退化成标题键、永远对不上索引侧的 `isbn:…:chNN`。"""
    return dedup_key_fields(meta.doi, meta.arxiv_id, meta.title, fallback=meta.paper_id,
                            url=getattr(meta, "url", None),
                            isbn=getattr(meta, "isbn", None),
                            chapter_number=getattr(meta, "chapter_number", None))


def _may_inherit(owner, meta) -> bool:
    """占着基键的那条，能不能判定为「与本篇是同一篇、且**索引最终会把两条合并**」？

    判据直接照抄索引自己的合并判据（`notes_index._entry_keys`）：条目的身份键集合 =
    `dedup_key` **加上规范标题二级键**（二级键无条件生成）。所以两条只要满足其一，
    就必然被并查集 union 进同一簇、其中一条成为 duplicate：
      1. `dedup_key` 逐字相等；
      2. **规范标题相等**。

    为什么不按「dedup_key 家族相等」（arXiv 剥不剥 vN、DataCite DOI 与 arxiv 档）判：
    家族相等**不等于**索引会合并——那正是 `config/dedup_overrides.json` 要写死人工裁决的原因。
    一旦没合并，两条 live 条目共用一个 citekey → `build_all_references` 把该键整键剔除 →
    **两篇一起**从全局书目消失，比原来那个「基键消失」的缺陷更糟（第 2 轮压测 CONFIRMED）。
    改用标题判据后：那三种 arXiv 形态照样继承（同一篇论文标题相同），「家族相同但标题已漂」
    这个真正危险的组合被挡住，还顺带覆盖了一种此前漏掉的情形——auto 侧是 arXiv DOI、
    manual 侧被 Crossref 补成正刊 DOI（一级键完全不同、标题相同，索引照样合并）。
    """
    if not owner:
        return False
    owner_dk, owner_title = owner if isinstance(owner, tuple) else (owner, None)
    if not owner_dk:
        return False                       # ("", "") = 两个 keeper 撞键，不许继承
    if owner_dk == _segment_dedup_key(meta):
        return True                        # 一级键逐字相等
    return bool(owner_title) and owner_title == _norm_title(meta.title)


def _yaml_list(items: List[str]) -> str:
    return "[" + ", ".join(json.dumps(i, ensure_ascii=False) for i in items) + "]"


def _shot_lines_after(text: str, shots: Optional[Dict[str, Any]], shown: set) -> List[str]:
    """一句证据后面要跟的图表截图行（每张图只在**第一次被提到**时出现一次）。

    行首两个空格 = 挂在上一条列表项之下；它不以 `- 〔` 开头，notes_index 的 TAG_LINE_RE
    不会把它当成证据句（截图是派生物，不进 highlights）。
    """
    if not shots:
        return []
    from .page_shots import labels_in, norm_label, sentence_key
    by_label = {norm_label(v["label"]): (rel, v) for rel, v in shots.items()
                if isinstance(v, dict) and v.get("kind") == "figure"}
    out = []
    hit = (shots.get("_passages") or {}).get(sentence_key(text))
    if hit:                                   # 这句话在原文里的出处段落（连文字一起裁下来）
        out.append("  ![原文出处 p.{}]({}/{})".format(hit["printed"], _SHOT_DIR, hit["rel"]))
    for lab in labels_in(text):
        key = norm_label(lab)
        if key in by_label and key not in shown:
            shown.add(key)
            rel, v = by_label[key]
            note = "（札记写作 {}，按图注宽松匹配，请核对）".format(lab) if "lenient" in (v.get("how") or "") else ""
            out.append("  ![{} · 原文截图{}]({}/{})".format(v["label"], note, _SHOT_DIR, rel))
    return out


def _shot_pages_block(shots: Optional[Dict[str, Any]]) -> List[str]:
    """节末的「原文页面」：整页截图以链接给出（内嵌会让月度 md/docx 膨胀到不可用）。"""
    if not shots:
        return []
    pages = sorted(((v.get("printed") or v.get("pdf_page") or 0, rel, v) for rel, v in shots.items()
                    if isinstance(v, dict) and v.get("kind") == "page"), key=lambda t: t[0])
    if not pages:
        return []
    links = []
    m0 = shots.get("_map") or {}
    for _, rel, v in pages:
        if v.get("how") == "figure-fallback":
            name = "{}所在页".format("/".join(v.get("labels") or []))
        elif m0.get("how") == "label" and v.get("printed") == v.get("pdf_page"):
            # 罗马页码页在 PDF 页标签里不是数字，to_printed 回退成了页序——别以「印刷页」名义显示（F13）
            name = "PDF 第 {} 页".format(v["pdf_page"])
        else:
            name = "p.{}".format(v["printed"])
        links.append("[{}]({}/{})".format(name, _SHOT_DIR, rel))
    m = shots.get("_map") or {}
    how = {"label": "页码为原文印刷页（PDF 自带页标签）", "header": "页码为原文印刷页（由页眉/页脚推得，PDF 第 1 页 = p.{}）"
           .format((m.get("offset") or 0) + 1), "pdf-order": "页码为 PDF 页序",
           "pdf-order(header-ignored)": "页码为 PDF 页序（页眉印的是期刊页码，札记标的不是）"}.get(m.get("how"), "")
    # 标题**不能**写成 `**【原文页面】**`：那正是 notes_index._CR_SECTION_RE 认的精读分节标题格式，
    # 会被当成一个新分节（ingest 的 md 还原路径据此多还原出一个空节）。用不同的写法避开（F12）。
    out = ["**原文页面**", "", "{}：{}".format(how or "原文页面", " · ".join(links))]
    miss = shots.get("_missing") or []
    if miss:
        out.append("")
        out.append("> 未能截到：{}（页码落不到本 PDF 内，或全文找不到该图注）".format("、".join(miss)))
    out.append("")
    return out


_SHOT_DIR = "assets/shots"


def _paper_section(seg: PaperSegment, citekey: Optional[str], index: Optional[int] = None,
                   level: int = 2, tier: str = "",
                   shots: Optional[Dict[str, Any]] = None) -> List[str]:
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
        shown: set = set()
        for sec in cr.sections:
            body.append("**【{}】**".format(sec.heading))
            for st in sec.sentences:
                body.append(render_tag_line(st.tag, st.text, getattr(st, "page", None)))
                body.extend(_shot_lines_after(st.text, shots, shown))
            body.append("")
        body.extend(_shot_pages_block(shots))
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
                      title: str = "Scholar Digest", instruction: str = "",
                      shots: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
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
                                    tier=tiers[i], shots=(shots or {}).get(seg.paper_id)))
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


def _make_shots(segments, citekeys, pdf_paths, out_dir, embed_fn=None) -> Dict[str, Dict[str, Any]]:
    """为给了本地 PDF 的论文生成原文截图，返回 paper_id -> 截图清单。

    逐篇 best-effort：截图是派生物，PDF 坏了/加密了/PyMuPDF 没装都只告警，绝不拦归档。
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not pdf_paths:
        return out
    try:
        from . import page_shots
    except Exception as e:                                   # noqa: BLE001
        logger.warning("  ⚠️ 原文截图不可用（{}），本轮跳过".format(e))
        return out
    root = Path(out_dir) / _SHOT_DIR
    n_pass = 0
    state = {"embed_fn": embed_fn, "fails_in_a_row": 0}
    for seg in segments:
        ck = citekeys.get(seg.paper_id)
        cr = seg.close_reading
        if not (ck and cr and cr.sections):
            continue
        sents = [{"text": st.text, "page": getattr(st, "page", None), "section": sec.heading}
                 for sec in cr.sections for st in sec.sentences]
        got = shots_for_one(ck, pdf_paths.get(seg.paper_id), sents, root, state)
        if got is not None:
            out[seg.paper_id] = got
            n_pass += len(got.get("_passages") or {})
    n = sum(1 for m in out.values() for v in m.values() if isinstance(v, dict) and v.get("kind"))
    if n:
        logger.info("  🖼 原文截图 {} 篇 / {} 张 · 逐句出处 {} 句 → {}".format(len(out), n, n_pass, root))
    return out


def shots_for_one(ck: str, spec, sents: List[Dict[str, Any]], root: Path,
                  state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """单篇论文的原文截图清单（整页 + 图表 + 逐句出处 `_passages`）。没有可用 PDF 返回 None。

    `write_notes` 与「给已归档 md 补截图」（`attach_shots_to_md`）共用这一个函数，
    **不许**另写第二套：两套出图逻辑一旦口径漂移，同一篇论文在不同札记里会配出不同的图。

    spec：路径字符串，或 {"path", "offset"(印刷页 = PDF 页序 + offset), "ranges"[(a,b)…]}
    ——书籍链路用后者：整本 600 页只切精读过的那几章，页码口径直接取 manifest 的权威偏移。
    sents：[{"text", "page", "section"}]，即精读里的每一句（带分节标题，供「无出处分节」判断）。
    state：跨篇共享的 {"embed_fn", "fails_in_a_row"}——连续两篇定位失败就判定向量服务挂了，
    余下论文只读缓存（单篇的确定性错误不该让后面所有论文都没有出处图）。
    """
    from . import page_shots
    spec = {"path": spec} if isinstance(spec, str) else (spec or {})
    pdf, ranges = spec.get("path"), spec.get("ranges")
    if not pdf:
        return None
    if not Path(pdf).is_file():                              # 死链：此前无声跳过，md 里这篇的图全部消失
        logger.warning("  ⚠️ {} 的 pdf_path 不存在，本轮无原文截图: {}".format(ck, pdf))
        return None
    pmap = None
    if spec.get("offset") is not None:
        import fitz
        with fitz.open(str(pdf)) as _d:
            pmap = page_shots.PageMap(_d.page_count, int(spec["offset"]), "manifest")
    hl = [{"text": s["text"], "page": s.get("page")} for s in sents]
    try:
        out = page_shots.shots_for_paper(Path(pdf), ck, hl, root, page_map=pmap)
    except Exception as e:                                   # noqa: BLE001
        logger.warning("  ⚠️ 原文截图失败 {}（不影响归档）: {}: {}".format(ck, type(e).__name__, e))
        return None
    got = None
    if state.get("embed_fn") is not None:
        try:
            got = page_shots.passages_for_paper(Path(pdf), ck, sents, root, state["embed_fn"],
                                                page_map=pmap, ranges=ranges)
            state["fails_in_a_row"] = 0
        except Exception as e:                               # noqa: BLE001
            logger.warning("  ⚠️ 逐句出处定位失败 {}（不影响归档）: {}: {}".format(ck, type(e).__name__, str(e)[:120]))
            state["fails_in_a_row"] = state.get("fails_in_a_row", 0) + 1
            if state["fails_in_a_row"] >= 2:
                logger.warning("  ⚠️ 连续 {} 篇失败，判定向量服务不可用：本轮余下论文只读缓存"
                               .format(state["fails_in_a_row"]))
                state["embed_fn"] = None
    if got is None:                                          # 没有向量服务 / 这一篇失败：已算过的图照出
        try:
            got = page_shots.passages_for_paper(Path(pdf), ck, sents, root, None,
                                                page_map=pmap, ranges=ranges)
        except Exception as e:                               # noqa: BLE001
            logger.warning("  ⚠️ 读逐句出处缓存失败 {}: {}: {}".format(ck, type(e).__name__, str(e)[:120]))
            got = {}
    out["_passages"] = got
    return out


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
    existing_key_owners: Optional[Dict[str, str]] = None,
    pdf_paths: Optional[Dict[str, str]] = None,
    embed_fn=None,
    docx_shots: bool = True,
) -> Dict[str, Any]:
    """把一个时间窗的论文聚合成【单篇】pandoc-ready 札记 + 一份 references.json（CSL-JSON）。

    pdf_paths: paper_id -> 本地 PDF 路径，或 {"path", "offset", "ranges"}（书籍链路按章限定页段、
        页码口径取 manifest 的权威偏移）。给了就在 citekey 定稿之后、渲染之前为每篇生成
        原文截图（证据页码对应的整页 + 证据句提到的图/表），落 `<out_dir>/assets/shots/<citekey>/`，
        md 与 docx 里都会带上。再给了 embed_fn（texts -> ndarray，多语种向量）就为**每一句**
        证据定位它在原文里的出处段落并裁图，紧跟在那句下面；没给就只出整页与图表。
        向量函数由调用方注入而不在这里自建：测试不许连网络，Ollama 没起也不该拦归档。
        必须在这里做而不是事后补：finalize 是整月重建，而截图目录名
        用的 citekey 要到本函数内部才定下来——事后补意味着每篇新论文都要重建两遍。
    docx_shots: docx 里要不要内嵌图表截图（md 不受影响）。月度 docx 是不断累积的大文件，内嵌后
        2026-08 一份就 85 MB，周备份从 56 MB 涨到 310 MB——调用方只对篇数有限的专题桶打开。

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
        existing_key_owners: 库内已占用键 → (占有者 dedup_key, 占有者规范标题)
            （notes_index.existing_citekey_owners；也接受老形态的裸 dedup_key 字符串）。
            给了它，兜底键才能做「同一篇论文继承基键」：一篇论文先被月度流水线浅读入库
            （auto，0 条取证句），后来被手动全文精读——去重层会把手动深读判为 keeper、auto
            判为 duplicate，但 citekey 是在去重**之前**分配的，只看 `used` 集合只能盲目加
            后缀，于是 keeper 拿到 `<基键>b`、干净基键留在那条 0 条取证句的 duplicate 上，
            而 all_references 只收 keeper → **基键从全局书目里消失**，此前所有 `[@基键]`
            引用悬空（已波及 P4 主稿 8 处，见 docs/bugs/2026-09-04-manual-upgrade-citekey-suffix.md）。
            规则：基键被占，但占有者与本篇 dedup_key 相同、且**本批**尚未有人用它 → 直接
            继承基键（keeper 与 duplicate 同键，_global_pass 的陈旧键守卫不再触发）。
            本批内两篇同 dedup_key 仍各得不同键（与 read_pdf._reuse_citekeys 的口径一致）；
            不同论文同基键照旧加后缀。不传（None）= 旧行为。
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
        batch_used = {v for v in citekeys.values() if v}
        used = set(batch_used) | (existing_citekeys or set())
        owners = existing_key_owners or {}
        inherited = 0
        for seg in segments:
            if citekeys.get(seg.paper_id):
                continue
            base = _fallback_citekey(seg.metadata)
            key = base
            if key in used:
                if owners and base not in batch_used and _may_inherit(owners.get(base), seg.metadata):
                    # 占着基键的是**同一篇论文**的既有条目（跨月重复 / auto→manual 升级）：
                    # 继承基键，让 keeper 与 duplicate 同键（见 existing_key_owners 的说明）
                    inherited += 1
                else:
                    for suf in _suffix_seq():
                        cand = "{}{}".format(base, suf)
                        if cand not in used:
                            key = cand
                            break
            used.add(key)
            batch_used.add(key)
            citekeys[seg.paper_id] = key
        if inherited:
            logger.info("  citekey：{} 篇继承了库内同一论文既有条目的基键（不加后缀）".format(inherited))

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
    shots = _make_shots(segments, citekeys, pdf_paths, out_dir, embed_fn)
    md_content = build_digest_note(segments, citekeys, title=digest_title, instruction=instruction,
                                   shots=shots)

    # 每篇札记配套独立的 references.json（按文件名区分），避免按月回填时互相覆盖
    ref_path = out_dir / "{}.references.json".format(slug)
    _atomic_write(ref_path, json.dumps(csl_items, ensure_ascii=False, indent=2))

    # 索引 sidecar：从内存对象无损导出结构化条目（含 arxiv_id/priority_score/三色计数），
    # 供 notes_index 聚合——新札记不再依赖 md 反向解析。排序与 build_digest_note 一致。
    sidecar_path = None
    sidecar_error = None
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
            sidecar_error = "{}: {}".format(type(e).__name__, e)
            # 不是「不影响札记」：md 不存阅读深度量尺（fulltext_chars/_raw/truncated、
            # reading_source 只能从标题两态半猜），sidecar 一旦没写出来这些字段**永久
            # 不可恢复**——全库 76 个 auto 月里 43 个没有 sidecar 正是这样悄悄积出来的
            # （见 docs/bugs/2026-09-04-auto-sidecar-missing.md）。所以走 error 级，
            # 并在返回值里带 sidecar_ok=False 让调用方能判定/告警，而不是把它当成功。
            logger.error("  ❌ 写索引 sidecar 失败——本月条目的阅读深度量尺将无法从 md 回读，"
                         "索引只能按老规则推断（reading_depth=unknown-legacy）: {}".format(sidecar_error))

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
    if emit_index_sidecar:
        # 显式两态：调用方（backfill/ingest/read_pdf）据此判定「本月量尺有没有落盘」。
        # 只看 index_sidecar 键在不在会把"没要求写"与"要求了但失败"混成一态。
        result["sidecar_ok"] = sidecar_path is not None
        if sidecar_error:
            result["sidecar_error"] = sidecar_error

    # 样式化 docx（单元格着色/句级三色/字体区分）；延迟导入避免与 docx_builder 循环依赖
    if emit_docx:
        try:
            from .docx_builder import build_digest_docx
            docx_path = out_dir / "{}.docx".format(_slug(filename, 80) or "scholar_digest")
            build_digest_docx(segments, citekeys, docx_path, title=digest_title,
                              csl_items=csl_items, cjk_font=cjk_font, instruction=instruction,
                              shots=shots if docx_shots else None, assets_root=out_dir / _SHOT_DIR)
            result["docx_path"] = str(docx_path)
        except Exception as e:
            logger.warning("  ⚠️ 生成样式化 docx 失败: {}".format(e))

    return result


# ---------------------------------------------------------------------------
# 给已归档的 md 补原文截图（自动月度/周札记：digest JSON 不存精读分节，证据句只在 md 里）
# ---------------------------------------------------------------------------

_SHOT_LINE_RE = re.compile(r"^  !\[.*\]\(" + re.escape(_SHOT_DIR) + r"/")
_PAGES_HEAD = "**原文页面**"
_ANCHOR_TAIL = re.compile(r"\s*〔p\.([0-9][0-9\-–,\s]*)〕\s*$")


def _strip_shot_lines(region: List[str]) -> List[str]:
    """去掉 region 里上一轮插进来的截图行与「原文页面」块（别的行一字不动）。"""
    out: List[str] = []
    i = 0
    while i < len(region):
        ln = region[i]
        if _SHOT_LINE_RE.match(ln):
            i += 1
            continue
        if ln.strip() == _PAGES_HEAD:
            # 块的形状见 _shot_pages_block：标题 / 空行 / 链接行 /（空行 / > 未能截到）/ 空行
            j = i + 1
            while j < len(region) and (not region[j].strip()
                                       or "(" + _SHOT_DIR + "/" in region[j]
                                       or region[j].startswith("> 未能截到")):
                j += 1
            # 块尾那一个空行归块所有，其余空行（区段收尾的）留给调用方按原样保留
            k = j
            while k > i + 1 and not region[k - 1].strip():
                k -= 1
            i = k + 1 if k < j else j
            continue
        out.append(ln)
        i += 1
    return out


def _evidence_of(line: str):
    """md 里一条证据句 → (text, page)。不是证据句返回 None。与 render_tag_line 严格互逆。"""
    from ._citekey_utils import TAG_LINE_RE
    m = TAG_LINE_RE.match(line)
    if m:
        return m.group(2), m.group(3)
    if line.startswith("- ") and not line.startswith("- 〔"):
        body = line[2:]
        a = _ANCHOR_TAIL.search(body)
        return (body[:a.start()] if a else body).rstrip(), (a.group(1) if a else None)
    return None


def attach_shots_to_md(md_path: Path, pdf_for, out_dir: Path, embed_fn=None,
                       apply: bool = True, strip_if_no_shots=None) -> Dict[str, Any]:
    """给一份已归档的札记 md 补原文截图，返回 {"papers", "shot_papers", "passages", "changed"}。

    pdf_for(citekey) -> None | 路径 | {"path","offset","ranges"}。出图用 shots_for_one、插图用
    _shot_lines_after / _shot_pages_block——都是 write_notes 用的那一套，这里只负责「在已有 md 里
    找到每一句、把图片行插回它下面」。**幂等**：先清掉上一轮插的截图行与「原文页面」块再插，
    重跑结果不变；证据句、分节标题、其余行一字不动（notes_index 不认图片行为证据句）。

    strip_if_no_shots：citekey 集合。这些论文的精读节刚被文本手术整节换过（backfill_deepread），
    上一轮的截图行对应的是**旧句子**——这轮出不了图时也要清掉，不能让旧图挂在新句子下面。
    其余论文出不了图时原样保留（PDF 暂时找不到不该抹掉已插好的图）。
    """
    from .notes_index import _SECTION_RE, _CLOSEREAD_RE, _CR_SECTION_RE
    md_path = Path(md_path)
    lines = md_path.read_text(encoding="utf-8").split("\n")
    heads = [i for i, ln in enumerate(lines) if _SECTION_RE.match(ln)]
    root = Path(out_dir) / _SHOT_DIR
    state = {"embed_fn": embed_fn, "fails_in_a_row": 0}
    rep = {"papers": 0, "shot_papers": 0, "passages": 0, "changed": False}
    out: List[str] = []
    cur = 0
    for n, h in enumerate(heads):
        end = heads[n + 1] if n + 1 < len(heads) else len(lines)
        for k in range(h + 1, end):                           # 最后一篇之后是「# 参考文献」
            if lines[k].startswith("# "):
                end = k
                break
        ck = _SECTION_RE.match(lines[h]).group(4)
        cr = next((k for k in range(h + 1, end) if _CLOSEREAD_RE.match(lines[k])), None)
        if cr is None:
            continue
        stop = next((k for k in range(cr + 1, end)
                     if lines[k].startswith("#") or lines[k].strip() == "---"), end)
        region = _strip_shot_lines(lines[cr + 1:stop])
        rep["papers"] += 1
        sents, sec = [], ""
        for ln in region:
            m = _CR_SECTION_RE.match(ln)
            if m:
                sec = m.group(1).strip()
                continue
            ev = _evidence_of(ln)
            if ev and ev[0]:
                sents.append({"text": ev[0], "page": ev[1], "section": sec})
        shots = shots_for_one(ck, pdf_for(ck), sents, root, state) if sents else None
        new_region: List[str] = []
        if shots:
            rep["shot_papers"] += 1
            rep["passages"] += len(shots.get("_passages") or {})
            shown: set = set()
            for ln in region:
                new_region.append(ln)
                ev = _evidence_of(ln)
                if ev and ev[0]:
                    new_region.extend(_shot_lines_after(ev[0], shots, shown))
            block = _shot_pages_block(shots)
            if block:
                t = len(new_region)
                while t > 0 and not new_region[t - 1].strip():
                    t -= 1
                trail = len(new_region) - t
                new_region = new_region[:t] + [""] + block + [""] * max(0, trail - 1)
        elif strip_if_no_shots and ck in strip_if_no_shots:
            new_region = region                              # 精读刚换过：旧图对不上新句子，清掉
        else:
            # 这一篇这轮出不了图（PDF 找不到/坏了）：原样保留，**不**清掉上一轮已插好的截图行
            new_region = lines[cr + 1:stop]
        out.extend(lines[cur:cr + 1])
        out.extend(new_region)
        cur = stop
    out.extend(lines[cur:])
    text = "\n".join(out)
    if text != "\n".join(lines):
        rep["changed"] = True
        if apply:
            from .notes_index import _atomic_write
            _atomic_write(md_path, text)
    return rep


def auto_shot_kwargs(segments, llm_settings, scratch_dir: str = "output/scholar_pdfs") -> Dict[str, Any]:
    """自动链路（周度入库 / 月度回填 / digest / CLI）调 write_notes 时补传的截图参数。

    自动链路精读时 closereading 会把抓到的 PDF 落在 `<scratch_dir>/<paper_id 前 16 位>.pdf`
    （closereading.close_read_segment），此前这份 PDF 只用来抽文本、从没交给 write_notes，
    所以自动札记一张原文截图都没有。有精读、PDF 也在的论文才给；一篇都没有就返回 {}
    （连向量服务都不探活）。向量服务不可用时 embed_fn=None：整页与图表照出，逐句出处跳过。
    """
    from .paths import repo_path
    d = repo_path(scratch_dir)
    paths: Dict[str, str] = {}
    for s in segments:
        cr = getattr(s, "close_reading", None)
        if not (cr and cr.sections and s.paper_id):
            continue
        p = d / "{}.pdf".format(s.paper_id[:16])
        if p.is_file():
            paths[s.paper_id] = str(p)
    if not paths:
        return {}
    from .embeddings import passage_embed_fn
    return {"pdf_paths": paths, "embed_fn": passage_embed_fn(llm_settings)}
