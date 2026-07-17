# -*- coding: utf-8 -*-
"""文献索引回归：md 格式契约锁（round-trip）/ sidecar 优先 / 去重与撞键 / 增量幂等。

test_roundtrip_md_parse 是 md 格式契约：以后改 notes._paper_section 的输出格式必挂此测试，
提醒同步更新 notes_index 的解析正则。
"""
import json
from datetime import date
from pathlib import Path

from src.scholar.schema import (
    PaperSegment, PaperMetadata, FilterDecision,
    CloseReading, CloseReadSection, CloseReadSentence,
)
from src.scholar.notes import write_notes
from src.scholar import notes_index as ni


def _fd(pid, decision="INCLUDE", **kw):
    d = dict(paper_id=pid, title="t", verdict="included", stage="llm_judge",
             decision=decision, one_line="对MNAR建模有直接借鉴", bucket=["A", "D"],
             role="CITE_SUPPORT", confidence=0.7, flags=[])
    d.update(kw)
    return FilterDecision(**d)


def _segments():
    """三篇：全字段+精读 / arXiv-only无DOI / 极简无裁决。标题含 | 和 [ 的刁钻字符。"""
    cr = CloseReading(from_full_text=True, source="arxiv", sections=[
        CloseReadSection(heading="方法与数据", sentences=[
            CloseReadSentence(text="可学习掩码嵌入。", tag="方法论借鉴"),
            CloseReadSentence(text="普通句子。", tag=None)]),
        CloseReadSection(heading="关键结论", sentences=[
            CloseReadSentence(text="AUPRC 提升。", tag="可引用证据")])])
    a = PaperSegment(
        segment_id=1, paper_id="pa", priority_score=0.9, translated_title="深度EHR模型",
        metadata=PaperMetadata(paper_id="pa", title="Deep | EHR [Models] under MNAR",
                               authors=["Jane Public", "Wei Chen"], doi="10.1/aaa",
                               journal="NEJM AI", publication_date=date(2025, 3, 2)),
        filter_decision=_fd("pa", flags=["THREAT"]), close_reading=cr)
    b = PaperSegment(
        segment_id=2, paper_id="pb", priority_score=0.6,
        metadata=PaperMetadata(paper_id="pb", title="Graph Transformers for Missingness",
                               authors=["Ann Lee"], arxiv_id="2501.01234",
                               url="https://arxiv.org/abs/2501.01234",
                               publication_date=date(2025, 1, 10)),
        filter_decision=_fd("pb", decision="MAYBE", bucket=[], role="NONE",
                            confidence=None, one_line="图变换器处理缺失"))
    c = PaperSegment(
        segment_id=3, paper_id="pc", priority_score=0.3,
        metadata=PaperMetadata(paper_id="pc", title="Minimal Paper"))
    return [a, b, c]


def _write_month(tmp_path, month="2025-03", citekeys=None, sidecar=False):
    segs = _segments()
    keys = citekeys or {"pa": "public2025Deep", "pb": "lee2025Graph", "pc": None}
    return write_notes(segs, keys, out_dir=tmp_path,
                       digest_title="科研札记 · {}（全文精读）".format(month),
                       filename="科研札记_{}_全文精读".format(month),
                       fallback_citekeys=True, emit_index_sidecar=sidecar), segs


# ---------------- md 格式契约（round-trip） ----------------

def test_roundtrip_md_parse(tmp_path):
    _write_month(tmp_path, sidecar=False)
    md = tmp_path / "科研札记_2025-03_全文精读.md"
    entries = ni.build_month_entries("2025-03", md,
                                     ref_path=tmp_path / "科研札记_2025-03_全文精读.references.json",
                                     sidecar_path=None)
    assert len(entries) == 3 and all(e["_source"] == "md-parse" for e in entries)
    by = {e["citekey"]: e for e in entries}

    a = by["public2025Deep"]
    assert a["title"] == "Deep | EHR [Models] under MNAR"      # 刁钻标题完整还原
    assert a["priority_tier"] == "high" and a["priority_rank"] == 1
    assert a["priority_score"] == 0.9
    assert a["decision"] == "INCLUDE" and a["one_line"] == "对MNAR建模有直接借鉴"
    assert a["bucket"] == ["A", "D"] and a["role"] == "CITE_SUPPORT"
    assert a["confidence"] == 0.7 and a["flags"] == ["THREAT"]
    assert a["doi"] == "10.1/aaa" and a["journal"] == "NEJM AI"
    assert a["authors"] == ["Jane Public", "Wei Chen"]
    assert a["year"] == 2025                                    # CSL issued 合并
    assert a["has_full_text_reading"] and a["reading_source"] == "arxiv"
    assert a["tag_counts"] == {"method": 1, "citable": 1}   # 口径归一到 role slug
    # highlights 从 md 句级标记无损回填（含 section 溯源 + 文本）
    hl = {h["role"]: h for h in a["highlights"]}
    assert set(hl) == {"method", "citable"}
    assert hl["method"]["section"] == "方法与数据" and hl["method"]["text"] == "可学习掩码嵌入。"
    assert hl["citable"]["tag"] == "可引用证据"
    assert a["dedup_key"] == "doi:10.1/aaa"
    assert a["note_line"] and "[@public2025Deep]" in a["note_heading"]

    bseg = by["lee2025Graph"]
    assert bseg["doi"] is None and bseg["arxiv_id"] == "2501.01234"  # 从 URL 提取
    assert bseg["dedup_key"] == "arxiv:2501.01234"
    assert bseg["decision"] == "MAYBE" and not bseg["has_full_text_reading"]

    cseg = next(e for e in entries if e["title"] == "Minimal Paper")
    assert cseg["citekey"].startswith("anon")                    # 兜底键
    assert cseg["dedup_key"] == "title:" + ni.norm_title("Minimal Paper")


# ---------------- sidecar 优先 ----------------

def test_sidecar_preferred_over_md(tmp_path):
    res, _ = _write_month(tmp_path, sidecar=True)
    assert "index_sidecar" in res
    stem = "科研札记_2025-03_全文精读"
    entries = ni.build_month_entries("2025-03", tmp_path / (stem + ".md"),
                                     ref_path=tmp_path / (stem + ".references.json"),
                                     sidecar_path=tmp_path / (stem + ".index.json"))
    assert all(e["_source"] == "sidecar" for e in entries)
    by = {e["citekey"]: e for e in entries}
    # sidecar 独有信息：citekey_source 区分权威/兜底（md 只能给 unknown）
    assert by["public2025Deep"]["citekey_source"] == "zotero"
    assert next(e for e in entries if e["title"] == "Minimal Paper")["citekey_source"] == "fallback"
    assert by["lee2025Graph"]["arxiv_id"] == "2501.01234"       # 无损来自 metadata
    assert by["public2025Deep"]["title_zh"] == "深度EHR模型"     # md 里没有译名
    assert by["public2025Deep"]["note_line"]                     # 落盘上下文仍从 md 定位


# ---------------- 去重 / 撞键 ----------------

def test_dedup_earliest_month_wins():
    papers = [
        {"month": "2023-02", "priority_rank": 1, "citekey": "x", "dedup_key": "doi:10.1/z"},
        {"month": "2023-01", "priority_rank": 2, "citekey": "y", "dedup_key": "doi:10.1/z"},
        {"month": "2023-03", "priority_rank": 1, "citekey": "z", "dedup_key": "doi:10.1/q"},
    ]
    out = ni._global_pass(papers)
    keeper = next(e for e in out if e["month"] == "2023-01")
    dup = next(e for e in out if e["month"] == "2023-02")
    assert keeper["duplicate_of"] is None and keeper["duplicate_months"] == ["2023-02"]
    assert dup["duplicate_of"] == "doi:10.1/z@2023-01"
    assert next(e for e in out if e["month"] == "2023-03")["duplicate_of"] is None


def test_same_month_same_citekey_keeps_own_doi(tmp_path):
    """同月两篇不同论文共用 citekey(BBT 误配)时,CSL 匹配须按 DOI 优先,
    不得把第一篇的 DOI 灌给第二篇(假重复+元数据污染)。"""
    _write_month(tmp_path, citekeys={"pa": "shared2025Key", "pb": "shared2025Key", "pc": None},
                 sidecar=False)
    stem = "科研札记_2025-03_全文精读"
    entries = ni.build_month_entries("2025-03", tmp_path / (stem + ".md"),
                                     ref_path=tmp_path / (stem + ".references.json"),
                                     sidecar_path=None)
    by_title = {e["title"]: e for e in entries}
    assert by_title["Deep | EHR [Models] under MNAR"]["doi"] == "10.1/aaa"
    b = by_title["Graph Transformers for Missingness"]
    assert b["doi"] is None and b["arxiv_id"] == "2501.01234"   # 没被灌入 10.1/aaa
    assert b["dedup_key"] == "arxiv:2501.01234"


def test_title_secondary_dedup_catches_missed_duplicate():
    """同一论文一月缺 DOI(title 键)、另一月补出 DOI(doi 键):一级键不同,
    须由规范化标题二级键判为重复,而非留成 citekey 撞键。"""
    papers = [
        {"month": "2025-03", "priority_rank": 1, "citekey": "dayan2021Fed",
         "title": "Federated Learning for Predicting Outcomes",
         "dedup_key": "title:federatedlearningforpredictingoutcomes"},
        {"month": "2025-05", "priority_rank": 2, "citekey": "dayan2021Fed",
         "title": "Federated Learning for Predicting Outcomes",
         "dedup_key": "doi:10.1038/xyz"},
    ]
    out = ni._global_pass(papers)
    dup = next(e for e in out if e["month"] == "2025-05")
    assert dup["duplicate_of"] == "title:federatedlearningforpredictingoutcomes@2025-03"
    assert ni._citekey_collisions(out) == []          # 不再报撞键


def test_fix_citekey_collisions_renames_later_month(tmp_path):
    """真撞键(不同论文同键):最早月保留,后月加 b 后缀,md 与 references.json 同步改。"""
    _write_month(tmp_path, month="2024-01",
                 citekeys={"pa": "wang2024Same", "pb": "lee2025Graph", "pc": None})
    _write_month(tmp_path, month="2024-05",
                 citekeys={"pa": "x2024A", "pb": "x2024B", "pc": None})
    # 手工制造撞键:把 2024-05 的 pa(不同 DOI 论文)改成同键 wang2024Same
    stem5 = "科研札记_2024-05_全文精读"
    md5 = tmp_path / (stem5 + ".md")
    md5.write_text(md5.read_text(encoding="utf-8").replace("[@x2024A]", "[@wang2024Same]"),
                   encoding="utf-8")
    rp5 = tmp_path / (stem5 + ".references.json")
    items = json.loads(rp5.read_text(encoding="utf-8"))
    for it in items:
        if it["id"] == "x2024A":
            it["id"] = "wang2024Same"
            it["DOI"] = "10.9/other"           # 不同论文(不同 DOI、不同标题则太重,改 DOI 即可)
            it["title"] = "Another Different Paper"
    rp5.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    # 标题也要不同,否则会被二级标题键判成重复而非撞键
    md5.write_text(md5.read_text(encoding="utf-8").replace(
        "Deep | EHR [Models] under MNAR", "Another Different Paper"), encoding="utf-8")

    renamed = ni.fix_citekey_collisions(tmp_path)
    assert renamed == 1
    idx = ni.update_index(tmp_path, full=True)
    assert idx["citekey_collisions"] == []
    md_txt = md5.read_text(encoding="utf-8")
    assert "[@wang2024Sameb]" in md_txt and md_txt.count("[@wang2024Same]") == 0
    items = json.loads(rp5.read_text(encoding="utf-8"))
    assert any(it["id"] == "wang2024Sameb" for it in items)
    # 最早月原键不动
    md1 = (tmp_path / "科研札记_2024-01_全文精读.md").read_text(encoding="utf-8")
    assert "[@wang2024Same]" in md1


def test_citekey_collision_detected():
    papers = [
        {"month": "2024-01", "citekey": "wang2024Missing", "dedup_key": "doi:10.1/a",
         "duplicate_of": None},
        {"month": "2024-05", "citekey": "wang2024Missing", "dedup_key": "doi:10.1/b",
         "duplicate_of": None},
        {"month": "2024-02", "citekey": "ok2024Key", "dedup_key": "doi:10.1/c",
         "duplicate_of": None},
    ]
    cols = ni._citekey_collisions(papers)
    assert cols == [{"citekey": "wang2024Missing", "months": ["2024-01", "2024-05"]}]


# ---------------- 目录扫描过滤 ----------------

def test_note_files_filter(tmp_path):
    for name in ["科研札记_2025-03_全文精读.md", "科研札记_2025-03_手动精读.md",
                 "demo_yahei.md", "digest_20260712_154534.md",
                 "科研札记_2025-03_全文精读_validate.md", "INDEX.md"]:
        (tmp_path / name).write_text("x", encoding="utf-8")
    files = ni._note_files(tmp_path)
    # 键为文件 stem；同月 auto/manual 双系列共存
    assert set(files) == {"科研札记_2025-03_全文精读", "科研札记_2025-03_手动精读"}
    assert files["科研札记_2025-03_全文精读"][:2] == ("2025-03", "auto")
    assert files["科研札记_2025-03_手动精读"][:2] == ("2025-03", "manual")


# ---------------- 增量 + 幂等 ----------------

def test_incremental_and_idempotent(tmp_path):
    _write_month(tmp_path, month="2025-03", sidecar=True)
    _write_month(tmp_path, month="2025-04", sidecar=False,
                 citekeys={"pa": "other2025Key", "pb": None, "pc": None})

    s3, s4 = "科研札记_2025-03_全文精读", "科研札记_2025-04_全文精读"
    idx1 = ni.update_index(tmp_path, full=True)
    assert set(idx1["months"]) == {s3, s4}          # v2：months 按文件 stem 键
    assert idx1["months"][s3]["source"] == "sidecar"
    assert idx1["months"][s4]["source"] == "md-parse"
    # 2025-04 与 2025-03 同一批论文 → 全部标为 2025-03 的重复
    dups = [e for e in idx1["papers"] if e["duplicate_of"]]
    assert len(dups) == 3 and all(e["month"] == "2025-04" for e in dups)

    wrote1 = ni.write_outputs(idx1, tmp_path)
    assert wrote1["index_json"] and (tmp_path / "literature_index.json").exists()
    mtime = (tmp_path / "literature_index.json").stat().st_mtime

    # 无变化重跑：不重解析、不写盘、mtime 不抖
    idx2 = ni.update_index(tmp_path)
    assert {m: v for m, v in idx2["months"].items()} == idx1["months"]
    wrote2 = ni.write_outputs(idx2, tmp_path)
    assert not wrote2["index_json"] and not wrote2["index_md"]
    assert (tmp_path / "literature_index.json").stat().st_mtime == mtime

    # 区间语义：区间外沿用旧条目（即使 md 变了），区间内强制重扫
    md4 = tmp_path / "科研札记_2025-04_全文精读.md"
    md4.write_text(md4.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    idx3 = ni.update_index(tmp_path, since="2025-03", until="2025-03")
    assert idx3["months"][s4] == idx1["months"][s4]   # 区间外：变化不采集
    idx4 = ni.update_index(tmp_path, since="2025-04", until="2025-04")
    assert idx4["months"][s4] != idx1["months"][s4]   # 区间内：重扫到新 mtime

    # 月份 md 删除 → 条目随之消失（months 以磁盘为准）
    md4.unlink()
    idx5 = ni.update_index(tmp_path)
    assert set(idx5["months"]) == {s3}
    assert all(e["month"] == "2025-03" for e in idx5["papers"])


# ---------------- backfill 去重集恢复 ----------------

def test_load_seen_keys_and_exclude(tmp_path):
    idx = {"papers": [
        {"month": "2023-01", "dedup_key": "doi:10.1/a"},
        {"month": "2023-02", "dedup_key": "arxiv:2401.9"},
        {"month": "2023-02", "dedup_key": "title:foo"},
    ]}
    p = tmp_path / "literature_index.json"
    p.write_text(json.dumps(idx), encoding="utf-8")
    assert ni.load_seen_keys(p) == {"doi:10.1/a", "arxiv:2401.9", "title:foo"}
    assert ni.load_seen_keys(p, exclude_months={"2023-02"}) == {"doi:10.1/a"}
    assert ni.load_seen_keys(tmp_path / "nope.json") == set()


def test_load_seen_keys_excludes_cross_month_duplicate_keys(tmp_path):
    """--force 重跑 keeper 月：同键在别的月还有 duplicate_of 条目时，须整键剔除,
    否则该键残留 seen → 重跑月把这篇论文 dedup 掉（丢篇）。"""
    idx = {"papers": [
        {"month": "2023-06", "dedup_key": "doi:10.1/z", "duplicate_of": None},
        {"month": "2024-06", "dedup_key": "doi:10.1/z", "duplicate_of": "doi:10.1/z@2023-06"},
        {"month": "2024-06", "dedup_key": "doi:10.1/q", "duplicate_of": None},
    ]}
    p = tmp_path / "literature_index.json"
    p.write_text(json.dumps(idx), encoding="utf-8")
    assert ni.load_seen_keys(p, exclude_months={"2023-06"}) == {"doi:10.1/q"}


def test_dedup_key_empty_title_does_not_collide():
    """两篇「三无」论文（无 DOI/arXiv/标题）不得共享空键 "title:" 被误判同一篇。"""
    k1 = ni.dedup_key_fields(None, None, "", fallback="paperA")
    k2 = ni.dedup_key_fields(None, None, None, fallback="paperB")
    assert k1 != k2 and k1 == "id:paperA"
    assert ni.dedup_key_fields(None, None, "", fallback="") == "id:unknown"


def test_sidecar_marks_missing_citekey(tmp_path):
    """未开兜底键且无 Zotero key：占位键进 sidecar 时 citekey_source 须为 missing。"""
    segs = _segments()
    res = write_notes(segs, {"pa": "public2025Deep", "pb": None, "pc": None},
                      out_dir=tmp_path, filename="科研札记_2025-05_全文精读",
                      fallback_citekeys=False, emit_index_sidecar=True)
    data = json.loads(Path(res["index_sidecar"]).read_text(encoding="utf-8"))
    by_title = {e["title"]: e for e in data["papers"]}
    assert by_title["Deep | EHR [Models] under MNAR"]["citekey_source"] == "zotero"
    miss = by_title["Minimal Paper"]
    assert miss["citekey"].startswith("MISSING-KEY-") and miss["citekey_source"] == "missing"


def test_references_json_field_none_when_file_absent(tmp_path):
    """索引的 references_json 字段不得指向不存在的文件（下游 pandoc 会找不到书目）。"""
    _write_month(tmp_path, sidecar=False)
    stem = "科研札记_2025-03_全文精读"
    (tmp_path / (stem + ".references.json")).unlink()
    entries = ni.build_month_entries("2025-03", tmp_path / (stem + ".md"),
                                     ref_path=tmp_path / (stem + ".references.json"),
                                     sidecar_path=None)
    assert entries and all(e["references_json"] is None for e in entries)


# ---------------- 手动精读系列（series）+ keeper 规则 ----------------

def _write_manual_month(tmp_path, month="2026-07", segs=None):
    """写一篇手动精读札记（series=manual）。默认复用 pa（与自动系列同 DOI，触发 keeper 竞争）。"""
    segs = segs if segs is not None else [_segments()[0]]
    keys = {s.paper_id: None for s in segs}
    return write_notes(segs, keys, out_dir=tmp_path,
                       digest_title="科研札记 · {}（手动深度精读）".format(month),
                       filename="科研札记_{}_手动精读".format(month),
                       fallback_citekeys=True, emit_index_sidecar=True,
                       index_series="manual")


def test_note_md_re_accepts_both_series_rejects_others():
    assert ni.NOTE_MD_RE.match("科研札记_2026-07_全文精读.md")
    assert ni.NOTE_MD_RE.match("科研札记_2026-07_手动精读.md")
    assert ni.NOTE_MD_RE.match("科研札记_2026-07_全文精读_validate.md") is None
    assert ni.NOTE_MD_RE.match("digest_20260712.md") is None
    # 专题批次:YYYY-MM-DD 桶（同月另起文件）
    m = ni.NOTE_MD_RE.match("科研札记_2026-07-17_手动精读.md")
    assert m and m.group(1) == "2026-07-17" and m.group(2) == "手动精读"


def test_both_series_coexist_same_month(tmp_path):
    """同月 auto + manual 双系列均入索引，各成一条 note file。"""
    _write_month(tmp_path, month="2026-07", sidecar=True)
    # 手动系列换一篇不同论文，避免与自动版去重（本测试只验证共存）
    solo = PaperSegment(segment_id=1, paper_id="pm", priority_score=1.0,
                        metadata=PaperMetadata(paper_id="pm", title="Manual Only Paper",
                                               doi="10.9/manual"))
    _write_manual_month(tmp_path, month="2026-07", segs=[solo])
    idx = ni.update_index(tmp_path, full=True)
    stems = set(idx["months"])
    assert "科研札记_2026-07_全文精读" in stems and "科研札记_2026-07_手动精读" in stems
    series = {e["series"] for e in idx["papers"]}
    assert series == {"auto", "manual"}
    manual = [e for e in idx["papers"] if e["series"] == "manual"]
    assert manual and manual[0]["note_file"] == "科研札记_2026-07_手动精读.md"


def test_manual_is_keeper_over_auto_even_if_later(tmp_path):
    """同一论文（同 DOI）自动版在早月、手动深读在晚月：手动为 keeper，自动被标 duplicate。"""
    _write_month(tmp_path, month="2025-03", sidecar=True)          # pa DOI 10.1/aaa
    _write_manual_month(tmp_path, month="2026-07")                 # 同 pa，同 DOI
    idx = ni.update_index(tmp_path, full=True)
    pa = [e for e in idx["papers"] if e.get("doi") == "10.1/aaa"]
    keeper = [e for e in pa if not e.get("duplicate_of")]
    dup = [e for e in pa if e.get("duplicate_of")]
    assert len(keeper) == 1 and keeper[0]["series"] == "manual" and keeper[0]["month"] == "2026-07"
    assert len(dup) == 1 and dup[0]["series"] == "auto" and dup[0]["month"] == "2025-03"
    assert dup[0]["duplicate_of"].endswith("@2026-07")
    assert "2025-03" in keeper[0]["duplicate_months"]


def test_load_seen_keys_excludes_only_auto(tmp_path):
    """--force 重跑某月：只剔除该月 auto 键；同月 manual 键恒留 seen（自动回填应跳过已手动深读的论文）。"""
    idx = {"papers": [
        {"month": "2026-07", "dedup_key": "doi:10.1/auto", "series": "auto"},
        {"month": "2026-07", "dedup_key": "doi:10.1/manual", "series": "manual"},
        {"month": "2026-06", "dedup_key": "doi:10.1/old", "series": "auto"},
    ]}
    p = tmp_path / "literature_index.json"
    p.write_text(json.dumps(idx), encoding="utf-8")
    # 全量 seen 含全部键
    assert ni.load_seen_keys(p) == {"doi:10.1/auto", "doi:10.1/manual", "doi:10.1/old"}
    # --force 剔 2026-07：auto 键去掉，manual 键仍在
    got = ni.load_seen_keys(p, exclude_months={"2026-07"})
    assert got == {"doi:10.1/manual", "doi:10.1/old"}


def test_schema_version_is_v3():
    assert ni.SCHEMA_VERSION == 3


def test_legacy_tags_map_to_role_highlights():
    """历史 bundle 的旧三色 tag 经 entry_from_segment → 映射为 role highlights（不重跑 LLM）。"""
    cr = CloseReading(from_full_text=True, source="manual-pdf", sections=[
        CloseReadSection(heading="方法", sentences=[
            CloseReadSentence(text="用图变换器聚合。", tag="方法学创新")]),
        CloseReadSection(heading="结论", sentences=[
            CloseReadSentence(text="AUC 0.9。", tag="重要发现")]),
        CloseReadSection(heading="背景", sentences=[
            CloseReadSentence(text="EHR 常缺失。", tag="研究背景")])])  # 研究背景→丢弃
    seg = PaperSegment(
        segment_id=1, paper_id="lg", priority_score=0.5,
        metadata=PaperMetadata(paper_id="lg", title="Legacy Tagged Paper"),
        close_reading=cr)
    e = ni.entry_from_segment(seg, "leg2020Paper", rank=0, total=1)
    assert e["tag_counts"] == {"method": 1, "citable": 1}   # 研究背景不计
    roles = sorted(h["role"] for h in e["highlights"])
    assert roles == ["citable", "method"]
    assert all(h["role"] != "refutable" for h in e["highlights"])  # 浅读无 refutable


def test_collect_highlights_drops_null_and_background():
    hl, tc = ni._collect_highlights([
        ("结果", "可引用证据", "P<0.05"),
        ("方法", None, "普通句"),
        ("背景", "研究背景", "综述"),          # legacy→None，丢弃
        ("质疑", "可反驳观点", "作者高估效应"),
    ])
    assert [h["role"] for h in hl] == ["citable", "refutable"]
    assert tc == {"citable": 1, "refutable": 1}
