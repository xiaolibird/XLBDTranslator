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
            CloseReadSentence(text="可学习掩码嵌入。", tag="方法学创新"),
            CloseReadSentence(text="普通句子。", tag=None)]),
        CloseReadSection(heading="关键结论", sentences=[
            CloseReadSentence(text="AUPRC 提升。", tag="重要发现")])])
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
    assert a["tag_counts"] == {"方法学创新": 1, "重要发现": 1}
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
    for name in ["科研札记_2025-03_全文精读.md", "demo_yahei.md", "digest_20260712_154534.md",
                 "科研札记_2025-03_全文精读_validate.md", "INDEX.md"]:
        (tmp_path / name).write_text("x", encoding="utf-8")
    files = ni._note_files(tmp_path)
    assert list(files) == ["2025-03"]


# ---------------- 增量 + 幂等 ----------------

def test_incremental_and_idempotent(tmp_path):
    _write_month(tmp_path, month="2025-03", sidecar=True)
    _write_month(tmp_path, month="2025-04", sidecar=False,
                 citekeys={"pa": "other2025Key", "pb": None, "pc": None})

    idx1 = ni.update_index(tmp_path, full=True)
    assert set(idx1["months"]) == {"2025-03", "2025-04"}
    assert idx1["months"]["2025-03"]["source"] == "sidecar"
    assert idx1["months"]["2025-04"]["source"] == "md-parse"
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
    assert idx3["months"]["2025-04"] == idx1["months"]["2025-04"]   # 区间外：变化不采集
    idx4 = ni.update_index(tmp_path, since="2025-04", until="2025-04")
    assert idx4["months"]["2025-04"] != idx1["months"]["2025-04"]   # 区间内：重扫到新 mtime

    # 月份 md 删除 → 条目随之消失（months 以磁盘为准）
    md4.unlink()
    idx5 = ni.update_index(tmp_path)
    assert set(idx5["months"]) == {"2025-03"}
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
