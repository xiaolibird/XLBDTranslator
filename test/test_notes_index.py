# -*- coding: utf-8 -*-
"""文献索引回归：md 格式契约锁（round-trip）/ sidecar 优先 / 去重与撞键 / 增量幂等。

test_roundtrip_md_parse 是 md 格式契约：以后改 notes._paper_section 的输出格式必挂此测试，
提醒同步更新 notes_index 的解析正则。
"""
import json
import os
from datetime import date
from pathlib import Path

import pytest

from src.scholar.schema import (
    PaperSegment, PaperMetadata, FilterDecision,
    CloseReading, CloseReadSection, CloseReadSentence,
)
from src.scholar.notes import write_notes
from src.scholar import notes_index as ni


# 人工裁决来源的隔离夹具 _isolate_repo_overrides 已提到 test/conftest.py（对整个 test/
# 目录生效）——原先只挂在本文件上，test_vault.py / test_pdf_ingest.py 仍读仓库真文件。


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


def test_sidecar_duplicate_citekey_gets_distinct_note_lines(tmp_path):
    """回归 notes_index.py:230 — 同一 citekey 在一份 md 里出现多次(误配撞键)时,
    _locate_headings 若用 dict 单值会让后写的覆盖先写的,sidecar 每条条目的
    note_line 就全指向最后一节。修复后须按出现顺序逐条认领,各自指向自己的小节。"""
    _write_month(tmp_path, citekeys={"pa": "shared2025Key", "pb": "shared2025Key", "pc": None},
                 sidecar=True)
    stem = "科研札记_2025-03_全文精读"
    entries = ni.build_month_entries("2025-03", tmp_path / (stem + ".md"),
                                     ref_path=tmp_path / (stem + ".references.json"),
                                     sidecar_path=tmp_path / (stem + ".index.json"))
    same_key = [e for e in entries if e["citekey"] == "shared2025Key"]
    assert len(same_key) == 2
    lines = [e["note_line"] for e in same_key]
    assert len(set(lines)) == 2, "两条同 citekey 条目的 note_line 不能撞到同一行"
    # 各自的 note_heading 都必须真的含 [@shared2025Key]（自证落点在正确的小节标题上）
    for e in same_key:
        assert e["note_heading"] and "[@shared2025Key]" in e["note_heading"]
    # priority_score 更高的 pa 排在前面，其 note_line 应更小（更靠文件前部）
    by_title = {e["title"]: e for e in same_key}
    a = by_title["Deep | EHR [Models] under MNAR"]
    b = by_title["Graph Transformers for Missingness"]
    assert a["note_line"] < b["note_line"]


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


def test_rename_citekey_in_note_skips_on_note_line_mismatch(tmp_path):
    """回归 notes_index.py:687 — note_line 没有真实命中 [@old] 时必须跳过改键,
    不能退化成「全文首个命中」瞎猜；否则同 citekey 出现多次时会改错别的那一条。"""
    _write_month(tmp_path, citekeys={"pa": "shared2025Key", "pb": "shared2025Key", "pc": None})
    stem = "科研札记_2025-03_全文精读"
    md = tmp_path / (stem + ".md")
    entries = ni.build_month_entries("2025-03", md,
                                     ref_path=tmp_path / (stem + ".references.json"),
                                     sidecar_path=None)
    b = next(e for e in entries if e["title"] == "Graph Transformers for Missingness")
    entry_bad = dict(b)
    entry_bad["note_line"] = 1     # 伪造一个不含 [@shared2025Key] 的行号（文件顶端 front matter）
    ok = ni._rename_citekey_in_note(tmp_path, entry_bad, "shared2025Key", "shared2025Keyb")
    assert ok == ni.RENAME_REFUSED
    text = md.read_text(encoding="utf-8")
    assert text.count("[@shared2025Key]") == 2   # 两条都原样保留，谁也没被误改


def test_rename_citekey_targets_own_row_when_dup_has_no_doi(tmp_path):
    """回归 _pick_rename_row — 撞键修复场景：同文件两篇论文共享 citekey，被改的 dup
    （rank 靠后）无 DOI。旧实现 refs/sidecar 侧退到 cand[0]，命中的是 keeper 那行 →
    md 改 dup、sidecar 改 keeper，两侧身份互换。修复后须按 dedup_key/标题精确定位
    dup 自己的行，keeper 的行一个字节不动。"""
    _write_month(tmp_path, citekeys={"pa": "shared2025Key", "pb": "shared2025Key", "pc": None},
                 sidecar=True)
    stem = "科研札记_2025-03_全文精读"
    entries = ni.build_month_entries("2025-03", tmp_path / (stem + ".md"),
                                     ref_path=tmp_path / (stem + ".references.json"),
                                     sidecar_path=tmp_path / (stem + ".index.json"))
    # dup = 无 DOI 的 pb（Graph Transformers），keeper = 有 DOI 的 pa
    dup = next(e for e in entries if e["title"] == "Graph Transformers for Missingness")
    assert not dup.get("doi")
    dup = dict(dup, references_json=stem + ".references.json", note_file=stem + ".md")
    ok = ni._rename_citekey_in_note(tmp_path, dup, "shared2025Key", "shared2025Keyb")
    assert ok == ni.RENAME_OK
    # sidecar：dup 行改新键，keeper 行保持旧键（身份不互换）
    rows = json.loads((tmp_path / (stem + ".index.json")).read_text(encoding="utf-8"))["papers"]
    by_title = {r["title"]: r for r in rows}
    assert by_title["Graph Transformers for Missingness"]["citekey"] == "shared2025Keyb"
    assert by_title["Deep | EHR [Models] under MNAR"]["citekey"] == "shared2025Key"
    # references.json：同样只有 dup 的 CSL 条目换 id
    items = json.loads((tmp_path / (stem + ".references.json")).read_text(encoding="utf-8"))
    ids = {it.get("title"): it["id"] for it in items if isinstance(it, dict)}
    assert ids.get("Graph Transformers for Missingness") == "shared2025Keyb"
    assert ids.get("Deep | EHR [Models] under MNAR") == "shared2025Key"


def test_update_index_range_covers_finer_granularity_months(tmp_path):
    """回归 update_index 区间比较 — month 可比边界更细（周札记 "2025-03-17"）。
    旧实现按字典序整串比较，"2025-03-17" <= "2025-03" 为假 → 周札记被当区间外，
    且区间模式下区间外文件改了也不重扫，改动永远进不了索引。"""
    _write_month(tmp_path, month="2025-03")
    _write_month(tmp_path, month="2025-03-17",
                 citekeys={"pa": "weekly2025A", "pb": "weekly2025B", "pc": None})
    weekly = "科研札记_2025-03-17_全文精读"
    idx1 = ni.update_index(tmp_path, full=True)
    assert weekly in idx1["months"]
    # 改动周札记后按月区间重扫：周札记必须落在 2025-03 区间内被强制重解析
    md = tmp_path / (weekly + ".md")
    md.write_text(md.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    idx2 = ni.update_index(tmp_path, since="2025-03", until="2025-03")
    assert idx2["months"][weekly]["md_size"] != idx1["months"][weekly]["md_size"]


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


# ---------------- 标题相似度层（改写题名的漏网重复） ----------------

def _pe(month, title, key, **kw):
    e = {"month": month, "priority_rank": 1, "title": title, "dedup_key": key,
         "citekey": kw.pop("citekey", "k" + month.replace("-", "")), "series": kw.pop("series", "auto")}
    e.update(kw)
    return e


def test_title_similarity_merges_rewritten_title():
    """同一论文预印本→正刊改写题名、两侧都无 DOI：精确标题键漏网，相似度层须合并。"""
    papers = [
        _pe("2025-03", "Recurrent neural network models (CovRNN) for predicting outcomes of "
                      "patients with COVID-19 on admission", "title:covrnnA", authors=["Laila Rasmy"]),
        _pe("2025-06", "Recurrent Neural Network Models (CovRNN) for Predicting Outcomes of "
                      "Patients With COVID-19 on Admission to Hospital", "doi:10.1/s2589",
           authors=["Laila Rasmy"], doi="10.1/s2589"),
    ]
    out = ni._global_pass(papers)
    dups = [e for e in out if e["duplicate_of"]]
    # keeper 取书目更全的正刊版（带 DOI），而非月份更早的预印本——见 _keeper_rank
    assert len(dups) == 1 and dups[0]["month"] == "2025-03"
    assert [e for e in out if not e["duplicate_of"]][0]["doi"] == "10.1/s2589"


def test_title_similarity_blocked_by_identity_conflict():
    """标题高度相似但身份铁证冲突（不同 DOI / 不同首作者）：绝不合并——
    误合并会静默吞掉一篇（下游按 duplicate_of 过滤），代价高于漏合并。"""
    base = ("Evaluation of Active Feature Acquisition Methods for {} Feature Settings")
    doi_conflict = [
        _pe("2025-03", base.format("Static"), "doi:10.48550/arxiv.2312.03619",
           doi="10.48550/arXiv.2312.03619", authors=["Henrik von Kleist"]),
        _pe("2025-04", base.format("Time-varying"), "doi:10.48550/arxiv.2312.01530",
           doi="10.48550/arXiv.2312.01530", authors=["Henrik von Kleist"]),
    ]
    assert all(e["duplicate_of"] is None for e in ni._global_pass(doi_conflict))

    author_conflict = [
        _pe("2025-03", "Improving Generalizability of Extracting Social Determinants of Health",
           "title:improvingsdoh", authors=["Bo Peng"]),
        _pe("2025-04", "Enhancing Generalizability in Social Determinants of Health Extraction",
           "title:enhancingsdoh", authors=["Ivana Ciganic"]),
    ]
    assert all(e["duplicate_of"] is None for e in ni._global_pass(author_conflict))


def test_anon_citekey_is_not_an_author_match():
    """两篇互不相干的 anon 条目不得因 citekey 前缀同为 anon 而被当成同作者。"""
    a = {"citekey": "anon2025Foo", "authors": []}
    b = {"citekey": "anon2025Bar", "authors": []}
    assert ni._first_surname(a) == "" and ni._first_surname(b) == ""
    assert ni._identity_conflict(a, b) == ""          # 未知 ≠ 冲突，但也不构成佐证


def test_mid_band_pairs_go_to_review_not_merged():
    """相似度落在 [REVIEW_SIM, AUTO_MERGE_SIM) 的对只报候选、不合并。"""
    papers = [
        _pe("2025-03", "Volatility-Aware Masking Improves Performance and Efficiency of "
                      "Pretrained EHR Foundation Models", "title:volatilityaware"),
        _pe("2025-08", "Coefficient of Variation Masking: A Volatility-Aware Strategy for "
                      "EHR Foundation Models", "title:coefficientvariation", series="manual"),
    ]
    review = []
    out = ni._global_pass(papers, review_out=review)
    assert all(e["duplicate_of"] is None for e in out)          # 未自动合并
    assert len(review) == 1
    assert ni.REVIEW_SIM <= review[0]["similarity"] < ni.AUTO_MERGE_SIM
    assert {review[0]["a"]["dedup_key"], review[0]["b"]["dedup_key"]} == {
        "title:volatilityaware", "title:coefficientvariation"}


def test_manual_override_merges_and_keeper_is_manual(tmp_path):
    """人工确认对无条件合并，且 keeper 规则照旧（手动深读胜过自动浅读）。"""
    (tmp_path / ni.DEDUP_OVERRIDES_JSON).write_text(json.dumps(
        {"merge": [["title:volatilityaware", "title:coefficientvariation"]]}), encoding="utf-8")
    papers = [
        _pe("2025-03", "Volatility-Aware Masking Improves Performance", "title:volatilityaware"),
        _pe("2025-08", "Coefficient of Variation Masking", "title:coefficientvariation",
           series="manual"),
    ]
    review = []
    out = ni._global_pass(papers, notes_dir=tmp_path, review_out=review)
    keeper = [e for e in out if not e["duplicate_of"]]
    dup = [e for e in out if e["duplicate_of"]]
    assert len(keeper) == 1 and keeper[0]["series"] == "manual"     # 手动版当权威
    assert len(dup) == 1 and dup[0]["month"] == "2025-03"
    assert dup[0]["duplicate_of"] == "title:coefficientvariation@2025-08"
    assert review == []                                             # 已合并即不再待确认


def test_override_transitivity_and_bad_file_is_tolerated(tmp_path):
    """A≈B、B≈C 时三者同簇同 keeper；覆盖文件损坏只告警不炸索引。"""
    (tmp_path / ni.DEDUP_OVERRIDES_JSON).write_text(json.dumps(
        {"merge": [["title:a", "title:b"], ["title:b", "title:c"]]}), encoding="utf-8")
    papers = [_pe("2025-01", "Alpha", "title:a"), _pe("2025-02", "Beta", "title:b"),
              _pe("2025-03", "Gamma", "title:c")]
    out = ni._global_pass(papers, notes_dir=tmp_path)
    assert sum(1 for e in out if not e["duplicate_of"]) == 1
    assert all(e["duplicate_of"] == "title:a@2025-01" for e in out if e["duplicate_of"])

    (tmp_path / ni.DEDUP_OVERRIDES_JSON).write_text("{ 半个 JSON", encoding="utf-8")
    papers2 = [_pe("2025-01", "Alpha", "title:a"), _pe("2025-02", "Beta", "title:b")]
    out2 = ni._global_pass(papers2, notes_dir=tmp_path)
    assert all(e["duplicate_of"] is None for e in out2)      # 未合并但也未抛异常


def test_distinct_override_suppresses_review_pair_without_merging(tmp_path):
    """人工判「非同文」的对不再上报待确认，且不因此被合并（distinct 只影响报告）。"""
    pair = ["title:volatilityaware", "title:coefficientvariation"]
    papers = lambda: [
        _pe("2025-03", "Volatility-Aware Masking Improves Performance and Efficiency of "
                       "Pretrained EHR Foundation Models", pair[0]),
        _pe("2025-08", "Coefficient of Variation Masking: A Volatility-Aware Strategy for "
                       "EHR Foundation Models", pair[1], series="manual"),
    ]
    review = []
    ni._global_pass(papers(), review_out=review)
    assert len(review) == 1                                  # 无覆盖文件时照旧上报

    (tmp_path / ni.DEDUP_OVERRIDES_JSON).write_text(
        json.dumps({"distinct": [pair]}), encoding="utf-8")
    review2 = []
    out = ni._global_pass(papers(), notes_dir=tmp_path, review_out=review2)
    assert review2 == []                                     # 已判非同文 → 不再上报
    assert all(e["duplicate_of"] is None for e in out)        # 且绝不因此被合并


def test_distinct_does_not_override_merge(tmp_path):
    """同一对同时写进 merge 与 distinct 时，合并优先——distinct 压制的只是「请人看一眼」。"""
    pair = ["title:volatilityaware", "title:coefficientvariation"]
    (tmp_path / ni.DEDUP_OVERRIDES_JSON).write_text(
        json.dumps({"merge": [pair], "distinct": [pair]}), encoding="utf-8")
    papers = [_pe("2025-03", "Volatility-Aware Masking Improves Performance", pair[0]),
              _pe("2025-08", "Coefficient of Variation Masking", pair[1], series="manual")]
    review = []
    out = ni._global_pass(papers, notes_dir=tmp_path, review_out=review)
    assert sum(1 for e in out if not e["duplicate_of"]) == 1  # 仍然合并成一簇
    assert review == []


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


def test_validate_note_label_accepts_index_visible_shapes():
    """入口校验的合法集 = NOTE_MD_RE 认得出的月份桶：纯月/周日期/专题批次都是存量在用的形状。"""
    for lab in ["2026-07", "2026-08-11", "2026-07-28-TFM", "2026-07-27-HuiyingLiang"]:
        assert ni.validate_note_label(lab) == lab


def test_validate_note_label_rejects_index_invisible_labels():
    """回归：畸形 --month/--label 落盘后 _note_files 会静默跳过（论文对索引/seen/向量库
    全部不可见，下月被当新论文重读）→ 这些值必须在入口抛 ValueError，而非落盘退出 0。"""
    for lab in ["2026-7",            # 一位月份——本缺陷的原始触发值
                "2026-13",           # 正则拦不住的假月份
                "202607", "2026/07", "test",
                "2026-07_x",         # 下划线是系列后缀分隔符
                "2026-07-a/b", "2026-07 ", "2026-07\n", ""]:
        with pytest.raises(ValueError):
            ni.validate_note_label(lab)


def test_note_md_re_accepts_all_series_rejects_others():
    assert ni.NOTE_MD_RE.match("科研札记_2026-07_全文精读.md")
    assert ni.NOTE_MD_RE.match("科研札记_2026-07_手动精读.md")
    assert ni.NOTE_MD_RE.match("科研札记_2026-07_全文精读_validate.md") is None
    assert ni.NOTE_MD_RE.match("digest_20260712.md") is None
    # 专题批次:YYYY-MM-DD 桶（同月另起文件）
    m = ni.NOTE_MD_RE.match("科研札记_2026-07-17_手动精读.md")
    assert m and m.group(1) == "2026-07-17" and m.group(2) == "手动精读"
    # 书籍系列：一书一文件，标签为 YYYY-MM-DD-<BookSlug>
    b = ni.NOTE_MD_RE.match("科研札记_2026-08-25-LittleRubin2020_书籍精读.md")
    assert b and b.group(1) == "2026-08-25-LittleRubin2020"
    assert ni._SERIES_MAP[b.group(2)] == "book"


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


def test_schema_version_is_v5():
    assert ni.SCHEMA_VERSION == 5


# ---------------- 阅读深度量尺四键 ----------------

_DEPTH_KEYS = ("fulltext_chars", "fulltext_chars_raw", "fulltext_truncated", "reading_depth")


def _seg_with_cr(cr=None, pid="dp"):
    return PaperSegment(segment_id=1, paper_id=pid, priority_score=0.5,
                        metadata=PaperMetadata(paper_id=pid, title="Depth Probe"),
                        close_reading=cr)


def test_depth_keys_none_when_no_close_reading():
    e = ni.entry_from_segment(_seg_with_cr(None), "no2020Read", rank=0, total=1)
    assert [e[k] for k in _DEPTH_KEYS] == [None, None, None, None]


def test_depth_keys_from_auto_close_reading():
    cr = CloseReading(from_full_text=True, source="arxiv", body_chars=40000,
                      body_chars_raw=221101, truncated=True, n_chunks=1,
                      reading_depth="single-call",
                      sections=[CloseReadSection(heading="结论", sentences=[
                          CloseReadSentence(text="AUC 0.9。", tag="可引用证据")])])
    e = ni.entry_from_segment(_seg_with_cr(cr), "au2025Paper", rank=0, total=1)
    assert e["fulltext_chars"] == 40000
    assert e["fulltext_chars_raw"] == 221101
    assert e["fulltext_truncated"] is True
    assert e["reading_depth"] == "single-call"


def test_manual_series_reading_depth_defaults_to_chunked():
    """pdf_ingest 不写 reading_depth，而 manual 正是读得最深的一批——不兜会落成 null。"""
    cr = CloseReading(from_full_text=True, source="manual-pdf", sections=[
        CloseReadSection(heading="结论", sentences=[
            CloseReadSentence(text="AUC 0.9。", tag="可引用证据")])])
    assert cr.reading_depth is None
    by_series = ni.entry_from_segment(_seg_with_cr(cr), "m2025A", rank=0, total=1,
                                      series="manual")
    assert by_series["reading_depth"] == "chunked"
    # series 仍是 auto、但 source 标了 manual-pdf 的（relink 链路）同样兜住
    by_source = ni.entry_from_segment(_seg_with_cr(cr), "m2025B", rank=0, total=1)
    assert by_source["reading_depth"] == "chunked"


def test_legacy_auto_entries_get_unknown_legacy_depth(tmp_path):
    """存量 auto 精读（无 reading_depth 键）回填 'unknown-legacy'：不重跑，只在量尺上标出深度不可考。

    非精读条目（has_full_text_reading=false）不写键——四态里「缺失」专属于非精读。
    另三键（fulltext_chars/_raw/_truncated）保持缺失：猜填 false 会把「确认未截断」和「不知道」混同。
    """
    _write_month(tmp_path, sidecar=False)
    stem = "科研札记_2025-03_全文精读"
    entries = ni.build_month_entries("2025-03", tmp_path / (stem + ".md"),
                                     ref_path=tmp_path / (stem + ".references.json"),
                                     sidecar_path=None)
    by = {e["citekey"]: e for e in entries}
    read = by["public2025Deep"]
    assert read["has_full_text_reading"] and read["reading_depth"] == "unknown-legacy"
    assert not any(k in read for k in
                   ("fulltext_chars", "fulltext_chars_raw", "fulltext_truncated"))
    unread = by["lee2025Graph"]
    assert unread["has_full_text_reading"] is False
    assert "reading_depth" not in unread


def test_legacy_manual_entries_get_chunked_depth(tmp_path):
    """存量 manual（同样无 reading_depth 键）回填 'chunked'：按构造就是逐块深读，
    全库最深的一批不能和 auto 存量一起沉在「无值」里。与 entry_from_segment 同口径。"""
    _write_month(tmp_path, sidecar=False)
    stem = "科研札记_2025-03_全文精读"
    entries = ni.build_month_entries("2025-03", tmp_path / (stem + ".md"),
                                     ref_path=tmp_path / (stem + ".references.json"),
                                     sidecar_path=None, series="manual")
    assert {e["reading_depth"] for e in entries} == {"chunked"}   # 含无精读节的那两篇


def test_existing_reading_depth_not_overwritten(tmp_path):
    """新 sidecar 已带 reading_depth（含 None）的条目不被回填覆盖——回填只补「键缺失」。"""
    stem = "科研札记_2025-03_全文精读"
    _write_month(tmp_path, sidecar=True)
    p = tmp_path / (stem + ".index.json")
    data = json.loads(p.read_text(encoding="utf-8"))
    for e in data["papers"]:                      # 模拟切换后新产出的月份：键已就位
        e["reading_depth"] = "single-call" if e["has_full_text_reading"] else None
    p.write_text(json.dumps(data), encoding="utf-8")
    entries = ni.build_month_entries("2025-03", tmp_path / (stem + ".md"),
                                     ref_path=tmp_path / (stem + ".references.json"),
                                     sidecar_path=p)
    by = {e["citekey"]: e for e in entries}
    assert by["public2025Deep"]["reading_depth"] == "single-call"   # 来自 sidecar，未被改写
    assert by["lee2025Graph"]["reading_depth"] is None              # 显式 None 也不回填


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


# ---------------- 全局书目合并（build_all_references） ----------------

def _idx(papers, collisions=()):
    return {"schema_version": ni.SCHEMA_VERSION, "papers": papers,
            "citekey_collisions": list(collisions)}


def _p(citekey, *, doi=None, title="T", ref="r.references.json", **kw):
    e = {"citekey": citekey, "doi": doi, "title": title, "authors": ["Ann Lee"],
         "year": 2025, "journal": "J", "url": None, "arxiv_id": None,
         "references_json": ref, "duplicate_of": None, "month": "2025-03"}
    e.update(kw)
    return e


def test_all_references_matches_by_doi_not_blind_id(tmp_path):
    """references.json 里两条目 id 被历史 --fix-collisions 改岔时，必须按 DOI 匹对。

    P0 回归：盲按 id 取会让 pandoc 安静地引到另一篇论文（曾致 9 条 DOI/11 条标题错配）。
    """
    (tmp_path / "r.references.json").write_text(json.dumps([
        {"id": "keyB", "title": "Paper A", "DOI": "10.1/aaa"},   # id 与 DOI 对调
        {"id": "keyA", "title": "Paper B", "DOI": "10.1/bbb"},
    ]), encoding="utf-8")
    refs = ni.build_all_references(
        _idx([_p("keyA", doi="10.1/aaa", title="Paper A"),
              _p("keyB", doi="10.1/bbb", title="Paper B")]), tmp_path)
    by = {r["id"]: r for r in refs}
    assert by["keyA"]["DOI"] == "10.1/aaa" and by["keyA"]["title"] == "Paper A"
    assert by["keyB"]["DOI"] == "10.1/bbb" and by["keyB"]["title"] == "Paper B"


def test_all_references_drops_collided_keys(tmp_path):
    """撞键整键剔除：宁可 pandoc 输出 ???，也不要静默引成另一篇。"""
    (tmp_path / "r.references.json").write_text(json.dumps(
        [{"id": "dup2020Key", "title": "X"}, {"id": "ok2021Key", "title": "Y"}]), encoding="utf-8")
    refs = ni.build_all_references(
        _idx([_p("dup2020Key"), _p("ok2021Key")],
             collisions=[{"citekey": "dup2020Key", "months": ["2020-01", "2021-02"]}]), tmp_path)
    assert [r["id"] for r in refs] == ["ok2021Key"]


def test_all_references_keeper_only_sorted_and_fallback(tmp_path):
    """只收 keeper、按 id 升序；月度文件缺条目时兜底且字段与 build_csl_item 对齐。"""
    (tmp_path / "r.references.json").write_text(json.dumps(
        [{"id": "zzz2020Have", "title": "Has CSL"}]), encoding="utf-8")
    refs = ni.build_all_references(_idx([
        _p("zzz2020Have"),
        _p("aaa2021Miss", doi="10.1/miss", title="No CSL entry"),
        _p("dup2019Skip", duplicate_of="zzz2020Have@2020-01"),
    ]), tmp_path)
    assert [r["id"] for r in refs] == ["aaa2021Miss", "zzz2020Have"]   # 去重复 + 排序
    fb = refs[0]
    assert fb["type"] == "article-journal" and fb["DOI"] == "10.1/miss"
    assert fb["author"] == [{"family": "Lee", "given": "Ann"}]
    assert fb["issued"] == {"date-parts": [[2025]]}


def test_all_references_skips_missing_key_placeholders(tmp_path):
    """MISSING-KEY 占位键不得进全局书目：[@MISSING-KEY-...] 是渲染后无处可指的死引用。

    两种判定都要生效：键名前缀（sidecar 缺失时 md 解析出的占位键）与
    citekey_source=="missing"（键被后续流程改名但源头仍是占位的情形）。
    """
    (tmp_path / "r.references.json").write_text(json.dumps(
        [{"id": "ok2025Key", "title": "Real"}]), encoding="utf-8")
    refs = ni.build_all_references(_idx([
        _p("ok2025Key"),
        _p("MISSING-KEY-10.1/lost", doi="10.1/lost"),          # 仅前缀可辨（sidecar 缺失）
        _p("odd2024Renamed", citekey_source="missing"),        # 仅来源可辨
    ]), tmp_path)
    assert [r["id"] for r in refs] == ["ok2025Key"]


def test_all_references_accepts_str_notes_dir(tmp_path):
    """notes_dir 传 str 不得静默全量降级为兜底（曾因 str/Path 相除抛错被 except 吞掉）。"""
    (tmp_path / "r.references.json").write_text(json.dumps(
        [{"id": "s2025Key", "title": "From CSL", "volume": "12"}]), encoding="utf-8")
    refs = ni.build_all_references(_idx([_p("s2025Key")]), str(tmp_path))
    assert refs[0]["title"] == "From CSL" and refs[0].get("volume") == "12"


def test_all_references_written_by_write_outputs(tmp_path):
    _write_month(tmp_path, sidecar=True)
    index = ni.update_index(tmp_path)
    wrote = ni.write_outputs(index, tmp_path)
    assert wrote["all_references"] is True
    refs = json.loads((tmp_path / ni.ALL_REFS_JSON).read_text(encoding="utf-8"))
    assert {r["id"] for r in refs} >= {"public2025Deep", "lee2025Graph"}
    assert ni.write_outputs(index, tmp_path)["all_references"] is False    # 幂等


def test_suffix_seq_is_always_alphabetic():
    """消歧后缀恒为纯字母：曾用 chr(ord('b')+n) 递增，'z' 后落到 '{' '|'，
    而 pandoc 会在这些字符处截断 citekey，把引用静默指到基键那篇论文。"""
    import re as _re
    seq = list(ni._suffix_seq())
    first = seq[:30]
    assert first[:3] == ["b", "c", "d"] and "z" in first
    assert first[first.index("z") + 1] == "bb"        # z 之后进两字母，不是 '{'
    assert all(_re.fullmatch(r"[b-z]+", s) for s in seq)
    assert len(seq) == len(set(seq)) and len(seq) > 600


def test_fix_collisions_generates_parsable_key(tmp_path):
    """撞键改名产出的新键不含 pandoc 会截断的字符。"""
    _write_month(tmp_path, month="2025-03")
    _write_month(tmp_path, month="2025-04", citekeys={"pa": "public2025Deep",   # 同键不同文
                                                      "pb": "other2025Key", "pc": None})
    # 让 2025-04 的 pa 变成另一篇论文：改其 DOI 使 dedup_key 不同
    md2 = tmp_path / "科研札记_2025-04_全文精读.md"
    md2.write_text(md2.read_text(encoding="utf-8").replace("10.1/aaa", "10.1/zzz"),
                   encoding="utf-8")
    ni.fix_citekey_collisions(tmp_path)
    keys = {e["citekey"] for e in ni.update_index(tmp_path)["papers"] if e.get("citekey")}
    assert not any(set(k) & set("{|}[]() ") for k in keys)


# ---------------- 原子写 + 索引损坏 fail-fast（FIX-5） ----------------


def test_load_seen_keys_corrupt_index_raises(tmp_path):
    """索引存在但损坏（半写 JSON）：静默空集会让整窗论文重复入库，须 fail-fast。"""
    import pytest
    p = tmp_path / "literature_index.json"
    p.write_text('{"papers": [{"month": "2023-01", "dedup', encoding="utf-8")   # 截断
    with pytest.raises(RuntimeError):
        ni.load_seen_keys(p)
    # 文件不存在仍是"首次运行"语义，返回空集不抛
    assert ni.load_seen_keys(tmp_path / "nope.json") == set()


def test_existing_citekeys_excludes_own_note_file(tmp_path):
    """exclude_note_files 剔除本次要整篇重写的札记自己的旧条目——否则重跑时兜底键
    重算出同样的 base 会被判「库内已占用」而加消歧后缀，来回改名（citekey 抖动）。"""
    p = tmp_path / "literature_index.json"
    p.write_text(json.dumps({"papers": [
        {"citekey": "wang2024Missing", "note_file": "科研札记_2024-03_全文精读.md"},
        {"citekey": "other2023Key", "note_file": "科研札记_2023-11_全文精读.md"},
    ]}, ensure_ascii=False), encoding="utf-8")

    all_keys = ni.existing_citekeys(p)
    assert all_keys == {"wang2024Missing", "other2023Key"}

    excl = ni.existing_citekeys(p, exclude_note_files={"科研札记_2024-03_全文精读.md"})
    assert excl == {"other2023Key"}


def test_existing_citekeys_missing_or_corrupt_index_returns_empty(tmp_path):
    """索引不存在或损坏时退化为空集（只影响消歧判断，不影响去重正确性——
    与 load_seen_keys 的 fail-fast 语义不同，这里没有"重复入库"风险）。"""
    assert ni.existing_citekeys(tmp_path / "nope.json") == set()
    p = tmp_path / "literature_index.json"
    p.write_text('{"papers": [{"citekey": "a2024', encoding="utf-8")   # 截断
    assert ni.existing_citekeys(p) == set()


def test_write_if_changed_atomic_no_tmp_residue(tmp_path):
    """写后目标 JSON 可解析且无 .tmp 残留；内容未变仍不写盘（mtime 不抖语义不变）。"""
    p = tmp_path / "all_references.json"
    content = json.dumps([{"id": "a2025Key", "title": "T"}], ensure_ascii=False)
    assert ni.write_if_changed(p, content) is True
    assert json.loads(p.read_text(encoding="utf-8"))[0]["id"] == "a2025Key"
    assert not list(tmp_path.glob("*.tmp"))
    assert ni.write_if_changed(p, content) is False


def test_write_outputs_index_json_atomic(tmp_path):
    """索引写盘走 tmp+replace：落盘后无 .tmp 残留且 JSON 完整可解析。"""
    _write_month(tmp_path, sidecar=True)
    wrote = ni.write_outputs(ni.update_index(tmp_path), tmp_path)
    assert wrote["index_json"] is True
    index = json.loads((tmp_path / ni.INDEX_JSON).read_text(encoding="utf-8"))
    assert index["papers"]
    assert not list(tmp_path.glob("*.tmp"))


def test_digest_output_save_to_file_roundtrip(tmp_path):
    """save_to_file 改原子写后 round-trip 不变：load 回来的内容与保存前一致、无 .tmp 残留。"""
    from src.scholar.schema import DigestOutput
    digest = DigestOutput(digest_id="d-2025-07", segments=_segments())
    out = digest.save_to_file(tmp_path / "digest_test.json")
    loaded = DigestOutput.load_from_file(out)
    assert loaded.digest_id == "d-2025-07"
    assert [s.paper_id for s in loaded.segments] == ["pa", "pb", "pc"]
    assert loaded.total_papers == 3
    assert not list(tmp_path.glob("*.tmp"))


def test_keeper_prefers_complete_bibliography_but_manual_still_wins():
    """同系列内书目更全者当 keeper；但手动深读始终压过自动浅读，哪怕元数据更薄。"""
    # 同为 auto：无作者无 DOI 的早月残条不该压过带作者的正刊记录
    out = ni._global_pass([
        _pe("2021-01", "Federated Learning used for predicting outcomes in SARS-COV-2 patients",
            "title:flsars", citekey="anon2021Federated"),
        _pe("2021-09", "Federated Learning used for predicting outcomes in SARS-COV-2 patients",
            "doi:10.1038/s41591-021-01506-3", citekey="dayan2021Federated",
            authors=["I Dayan"], doi="10.1038/s41591-021-01506-3", journal="Nature Medicine"),
    ])
    keeper = [e for e in out if not e["duplicate_of"]]
    assert len(keeper) == 1 and keeper[0]["citekey"] == "dayan2021Federated"

    # 手动 vs 自动：series 仍是第一顺位，元数据完整度不得反超
    out2 = ni._global_pass([
        _pe("2025-01", "Same Paper", "title:same", series="manual"),
        _pe("2025-02", "Same Paper", "title:same", authors=["A B"], doi="10.1/x",
            journal="J", citekey="rich"),
    ])
    keeper2 = [e for e in out2 if not e["duplicate_of"]]
    assert len(keeper2) == 1 and keeper2[0]["series"] == "manual"


def test_stale_override_key_warns_instead_of_silently_skipping(tmp_path):
    """人工裁决的键在库中找不到时必须告警——沉默会让裁决永久失效且无迹象。

    日志走 loguru（caplog 收不到它绑定的 sink），临时挂一个自己的 sink 来收。
    """
    from loguru import logger as _lg
    lines = []
    sink = _lg.add(lines.append, level="WARNING")
    try:
        (tmp_path / ni.DEDUP_OVERRIDES_JSON).write_text(json.dumps(
            {"merge": [["title:a", "title:键已漂走"]]}), encoding="utf-8")
        papers = [_pe("2025-01", "Alpha", "title:a"), _pe("2025-02", "Beta", "title:b")]
        out = ni._global_pass(papers, notes_dir=tmp_path)
    finally:
        _lg.remove(sink)
    assert all(e["duplicate_of"] is None for e in out)       # 确实没合并
    assert any("键已漂走" in s for s in lines)                # 且逐条点名了失配的键
    assert any("未生效" in s for s in lines)


# ---------------- 场地原生 id 键层（无 DOI 的会议论文） ----------------

def test_venue_native_id_extraction():
    from src.scholar._citekey_utils import venue_native_id as v
    assert v("https://proceedings.mlr.press/v287/elsharief25a.html") == "pmlr:v287/elsharief25a"
    assert v("https://proceedings.mlr.press/v225/ren23a/ren23a.pdf") == "pmlr:v225/ren23a"
    assert v("http://proceedings.mlr.press/v146/kalmady21a") == "pmlr:v146/kalmady21a"
    assert v("https://proceedings.mlr.press/v297/torres-fuertes26a.html") == \
        "pmlr:v297/torres-fuertes26a"          # 复姓 slug 带连字符，不能被切
    # OpenReview id 区分大小写，原样保留（折叠会静默吞篇，见下方专门用例）
    assert v("https://openreview.net/forum?id=x4UK4GadLd") == "openreview:x4UK4GadLd"
    assert v("https://openreview.net/pdf?id=F3G2udCF3Q") == "openreview:F3G2udCF3Q"
    assert v("https://arxiv.org/abs/2412.07712") is None
    assert v(None) is None


def test_dedup_key_ladder_puts_venue_id_above_title_below_arxiv():
    from src.scholar._citekey_utils import dedup_key_fields as k
    url = "https://proceedings.mlr.press/v287/elsharief25a.html"
    # 无 DOI 无 arXiv：场地 id 顶掉标题键
    assert k(None, None, "MedMod: Multimodal Benchmark", url=url) == "pmlr:v287/elsharief25a"
    # 标题被解析截断也不影响身份——这正是这一层要解决的问题
    assert k(None, None, "Healthcare Analytics", url=url) == "pmlr:v287/elsharief25a"
    # DOI / arXiv 仍优先
    assert k("10.1/x", None, "T", url=url) == "doi:10.1/x"
    assert k(None, "2501.01234", "T", url=url) == "arxiv:2501.01234"
    # 无 url 时行为不变
    assert k(None, None, "Some Title") == "title:" + ni.norm_title("Some Title")


def test_sidecar_dedup_key_is_recomputed_not_frozen(tmp_path):
    """sidecar 里冻结的旧 dedup_key 必须按当前规则重算——否则键梯改了它纹丝不动。"""
    stem = "科研札记_2025-03_全文精读"
    (tmp_path / (stem + ".md")).write_text(
        "## 🔴 高 1. Paper [@x2025Paper]\n", encoding="utf-8")
    (tmp_path / (stem + ".index.json")).write_text(json.dumps({"papers": [{
        "citekey": "x2025Paper", "title": "Paper", "doi": None, "arxiv_id": None,
        "url": "https://proceedings.mlr.press/v287/elsharief25a.html",
        "dedup_key": "title:paper",          # 落盘时（旧规则）冻下来的键
        "highlights": [], "tag_counts": {},
    }]}), encoding="utf-8")
    entries = ni.build_month_entries("2025-03", tmp_path / (stem + ".md"),
                                     ref_path=None,
                                     sidecar_path=tmp_path / (stem + ".index.json"))
    assert entries[0]["dedup_key"] == "pmlr:v287/elsharief25a"


def test_sidecar_id_fallback_key_is_preserved(tmp_path):
    """重算会落到 id: 的「三无」条目保留 sidecar 原键（paper_id 比 citekey 更稳）。"""
    stem = "科研札记_2025-04_全文精读"
    (tmp_path / (stem + ".md")).write_text("## 🔴 高 1.  [@anon2025X]\n", encoding="utf-8")
    (tmp_path / (stem + ".index.json")).write_text(json.dumps({"papers": [{
        "citekey": "anon2025X", "title": "", "doi": None, "arxiv_id": None, "url": None,
        "dedup_key": "id:pdf-abc123", "highlights": [], "tag_counts": {},
    }]}), encoding="utf-8")
    entries = ni.build_month_entries("2025-04", tmp_path / (stem + ".md"),
                                     ref_path=None,
                                     sidecar_path=tmp_path / (stem + ".index.json"))
    assert entries[0]["dedup_key"] == "id:pdf-abc123"


# ---------------- R1 对抗审查回归（2026-08-15） ----------------

def test_openreview_id_is_case_sensitive_pmlr_is_folded():
    """R1-6：OpenReview forum id 区分大小写，.lower() 会把两个不同 id 折成同一个键。

    折叠 → 并查集判成同一篇 → 落败方标 duplicate_of 后被下游一律过滤 = 静默吞篇。
    PMLR slug 本身全小写，继续归一化无害。
    """
    from src.scholar._citekey_utils import venue_native_id as v
    assert v("https://openreview.net/forum?id=x4UK4GadLd") == "openreview:x4UK4GadLd"
    assert v("https://openreview.net/forum?id=x4UK4GadLd") != \
        v("https://openreview.net/forum?id=X4uk4gAdLD")
    # PMLR 仍小写归一（大小写域名/大写 slug 都折到同一个键）
    assert v("https://PROCEEDINGS.MLR.PRESS/v287/ELSHARIEF25A.html") == "pmlr:v287/elsharief25a"


def test_venue_native_id_handles_real_url_variants():
    """R1-7：带 query 无扩展名、参数换序、attachment/references 链接、GitHub 镜像都要认得。

    抽不出时无任何日志、直接退回 title: 键——标题被解析截断身份就跟着漂，正是这一层
    存在的全部理由。
    """
    from src.scholar._citekey_utils import venue_native_id as v
    assert v("https://proceedings.mlr.press/v287/elsharief25a?x=1") == "pmlr:v287/elsharief25a"
    assert v("https://proceedings.mlr.press/v287/elsharief25a/elsharief25a-supp.pdf") == \
        "pmlr:v287/elsharief25a"          # 附件后缀不得被当成 slug 的一部分
    assert v("https://openreview.net/forum?noteId=abc&id=x4UK4GadLd") == "openreview:x4UK4GadLd"
    assert v("https://openreview.net/attachment?id=x4UK4GadLd&name=pdf") == \
        "openreview:x4UK4GadLd"
    assert v("https://openreview.net/references/pdf?id=x4UK4GadLd") == "openreview:x4UK4GadLd"
    # PMLR 官方 GitHub 镜像（本库真实存在一条）
    assert v("https://raw.githubusercontent.com/mlresearch/v281/main/assets/"
             "noshin25a/noshin25a.pdf") == "pmlr:v281/noshin25a"
    # 反例：profile 链接的 ~id 不是 forum id；非场地 url 照旧 None
    assert v("https://openreview.net/profile?id=~John_Doe1") is None
    assert v("https://raw.githubusercontent.com/someone/v281/main/x.pdf") is None
    assert v("https://arxiv.org/abs/2412.07712") is None


def test_openreview_id_only_from_paper_paths():
    """R2-2：OpenReview 的 `?id=` 只有长在论文端点上才是论文身份。

    group/search/venue 页的 `?id=` 装的是**会场名/检索词**，抽成身份键会让两篇不相干的
    论文拿到同一个 dedup_key → 并查集判同篇 → 落败方标 duplicate_of 被下游一律过滤，
    即「代价远高于漏合并」的静默吞篇。
    """
    from src.scholar._citekey_utils import venue_native_id as v
    # 非论文端点：一律不抽
    assert v("https://openreview.net/group?id=NeurIPS") is None
    assert v("https://openreview.net/group?id=MIDL") is None
    assert v("https://openreview.net/search?term=ehr&id=abc") is None
    assert v("https://openreview.net/venue?id=ICLR2024") is None
    assert v("https://openreview.net/?id=x4UK4GadLd") is None          # 首页 query 不承载身份
    # 论文端点：五种都要照常抽出（白名单不能把真实变体一起挡掉）
    for u in ("https://openreview.net/forum?id=x4UK4GadLd",
              "https://openreview.net/pdf?id=x4UK4GadLd",
              "https://openreview.net/attachment?id=x4UK4GadLd&name=pdf",
              "https://openreview.net/references?id=x4UK4GadLd",
              "https://openreview.net/revisions?id=x4UK4GadLd"):
        assert v(u) == "openreview:x4UK4GadLd", u


def test_existing_note_dedup_keys_recomputed_like_index(tmp_path):
    """R1-2：整篇覆盖守卫读 sidecar 时必须按当前键梯重算，否则同 label 重跑被误判丢数据。

    sidecar 里冻结的是落盘那一刻的键；run_ingest 那侧走 dedup_key(seg.metadata) 是新键。
    两侧不等 → existing - new 非空 → RuntimeError 拒写，提示换 --label（换了会真的产生
    重复札记）。
    """
    from src.scholar.ingest import _existing_note_dedup_keys, dedup_key
    from src.scholar.schema import PaperMetadata
    stem = "科研札记_2026-08_全文精读"
    (tmp_path / (stem + ".md")).write_text("x\n", encoding="utf-8")
    (tmp_path / (stem + ".index.json")).write_text(json.dumps({"papers": [{
        "citekey": "anon2021Automation", "title": "Towards Automation of Knowledge Graph",
        "doi": None, "arxiv_id": None,
        "url": "https://openreview.net/pdf?id=N4cz2jRFFlp",
        "dedup_key": "title:towardsautomationofknowledgegraph",      # 旧规则冻结值
    }]}), encoding="utf-8")
    existing = _existing_note_dedup_keys(tmp_path, stem)
    new = dedup_key(PaperMetadata(paper_id="p1", title="Towards Automation of Knowledge Graph",
                                  url="https://openreview.net/pdf?id=N4cz2jRFFlp"))
    assert existing == {new} == {"openreview:N4cz2jRFFlp"}
    assert existing - {new} == set()          # 守卫不会再误报「本批未覆盖」
    # 与索引侧同一函数、同一结果（杜绝两处漂移）
    entries = ni.build_month_entries("2026-08", tmp_path / (stem + ".md"), ref_path=None,
                                     sidecar_path=tmp_path / (stem + ".index.json"))
    assert entries[0]["dedup_key"] == new


def test_existing_note_dedup_keys_keeps_id_fallback(tmp_path):
    """R1-2 边界：重算落到 id: 的「三无」条目仍用 sidecar 冻结值（paper_id 比 citekey 稳）。"""
    from src.scholar.ingest import _existing_note_dedup_keys
    stem = "科研札记_2026-09_全文精读"
    (tmp_path / (stem + ".md")).write_text("x\n", encoding="utf-8")
    (tmp_path / (stem + ".index.json")).write_text(json.dumps({"papers": [{
        "citekey": "anon2025X", "title": "", "doi": None, "arxiv_id": None, "url": None,
        "dedup_key": "id:pdf-abc123",
    }]}), encoding="utf-8")
    assert _existing_note_dedup_keys(tmp_path, stem) == {"id:pdf-abc123"}


def test_rename_citekey_refuses_when_references_json_is_broken(tmp_path):
    """R1-3：refs 不可解析时不得「md 已改却返回 True」——那会让 pandoc 静默解析不到条目。"""
    stem = "科研札记_2026-05_全文精读"
    md = tmp_path / (stem + ".md")
    md.write_text("## 🔴 高 1. Some Paper [@torres2026Uncertainty]\n", encoding="utf-8")
    rp = tmp_path / (stem + ".references.json")
    rp.write_text("{ 这不是合法 JSON", encoding="utf-8")
    entry = {"note_file": md.name, "note_line": 1,
             "references_json": rp.name, "doi": None}
    ok = ni._rename_citekey_in_note(tmp_path, entry, "torres2026Uncertainty",
                                    "torresfuertes2026Uncertainty")
    assert ok == ni.RENAME_REFUSED
    # 关键：md 一个字都不许动（预检不过 = 磁盘零改动）
    assert md.read_text(encoding="utf-8") == \
        "## 🔴 高 1. Some Paper [@torres2026Uncertainty]\n"
    assert rp.read_text(encoding="utf-8") == "{ 这不是合法 JSON"


def test_rename_citekey_refuses_when_sidecar_lacks_the_key(tmp_path):
    """R1-3：sidecar 里找不到旧 citekey 时也算失败——下次重建会把 md 顶回旧值。"""
    stem = "科研札记_2026-06_全文精读"
    md = tmp_path / (stem + ".md")
    md.write_text("## 🔴 高 1. P [@a2026X]\n", encoding="utf-8")
    (tmp_path / (stem + ".index.json")).write_text(
        json.dumps({"papers": [{"citekey": "别的键", "doi": None}]}), encoding="utf-8")
    entry = {"note_file": md.name, "note_line": 1, "references_json": None, "doi": None}
    assert ni._rename_citekey_in_note(tmp_path, entry, "a2026X", "b2026X") == ni.RENAME_REFUSED
    assert "[@a2026X]" in md.read_text(encoding="utf-8")


def test_rename_citekey_writes_all_three_when_preflight_passes(tmp_path):
    """R1-3 正面：三处预检都过时，md + references.json + sidecar 一起改。"""
    stem = "科研札记_2026-07_全文精读"
    md = tmp_path / (stem + ".md")
    md.write_text("## 🔴 高 1. P [@a2026X]\n", encoding="utf-8")
    rp = tmp_path / (stem + ".references.json")
    rp.write_text(json.dumps([{"id": "a2026X", "title": "P"}]), encoding="utf-8")
    sc = tmp_path / (stem + ".index.json")
    sc.write_text(json.dumps({"papers": [{"citekey": "a2026X", "doi": None}]}),
                  encoding="utf-8")
    entry = {"note_file": md.name, "note_line": 1, "references_json": rp.name, "doi": None}
    assert ni._rename_citekey_in_note(tmp_path, entry, "a2026X", "b2026X") == ni.RENAME_OK
    assert "[@b2026X]" in md.read_text(encoding="utf-8")
    assert json.loads(rp.read_text(encoding="utf-8"))[0]["id"] == "b2026X"
    assert json.loads(sc.read_text(encoding="utf-8"))["papers"][0]["citekey"] == "b2026X"
    assert not list(tmp_path.glob("*.tmp-*"))


# ---------------- R2-1：写盘阶段的事务性（回滚 + 三态） ----------------

_COLLIDE_TITLES = {
    "2026-01": "Alpha Retrieval Of Cardiac Waveforms",
    "2026-02": "Beta Federated Graph Kernels For Sepsis",
    "2026-03": "Gamma Tensor Phenotyping With Missing Labels",
}


def _write_collide_notes(d, key="dup2026Key"):
    """三个月、三篇**不同**论文共用一个 citekey（无 sidecar）：撞键修复的最小现场。"""
    for month, title in _COLLIDE_TITLES.items():
        stem = "科研札记_{}_全文精读".format(month)
        (d / (stem + ".md")).write_text(
            "# 论文\n## 🔴 高 1. {} [@{}]\n**优先级**: `0.5`\n".format(title, key),
            encoding="utf-8")
        (d / (stem + ".references.json")).write_text(
            json.dumps([{"id": key, "title": title}], ensure_ascii=False), encoding="utf-8")


def _md_key(d, month):
    stem = "科研札记_{}_全文精读".format(month)
    line = next(l for l in (d / (stem + ".md")).read_text(encoding="utf-8").splitlines()
                if "[@" in l)
    return line.split("[@")[-1].rstrip("]")


def _refs_id(d, month):
    stem = "科研札记_{}_全文精读".format(month)
    return json.loads((d / (stem + ".references.json")).read_text(encoding="utf-8"))[0]["id"]


def test_rename_citekey_rolls_back_when_a_later_write_fails(tmp_path):
    """R2-1：md 已落盘、references.json 写失败 → 必须把 md 回滚，返回 refused（磁盘零改动）。

    预检全过之后写盘循环本身也要有事务性：pending 顺序是 [md, refs, sidecar]，md 先落盘，
    第 2/3 个文件写失败（磁盘满、卷只读、tmp 路径被占）时旧写法直接 return False，
    而 md 已是新键 —— 调用方汇报「磁盘未改动」，重跑必然再失败（md 里已无 [@old]），
    札记永久停在半改状态且不再有任何新信号。
    """
    stem = "科研札记_2026-04_全文精读"
    md = tmp_path / (stem + ".md")
    md.write_text("## 🔴 高 1. P [@a2026X]\n", encoding="utf-8")
    rp = tmp_path / (stem + ".references.json")
    rp.write_text(json.dumps([{"id": "a2026X", "title": "P"}]), encoding="utf-8")
    md_before, rp_before = md.read_text(encoding="utf-8"), rp.read_text(encoding="utf-8")
    # 自然触发写失败：占住 _atomic_write 的 tmp 路径（等价于磁盘满/只读卷，不打桩）
    (tmp_path / (rp.name + ".tmp-{}".format(os.getpid()))).mkdir()
    entry = {"note_file": md.name, "note_line": 1, "references_json": rp.name, "doi": None}
    assert ni._rename_citekey_in_note(tmp_path, entry, "a2026X", "b2026X") == ni.RENAME_REFUSED
    # 关键：md 必须被回滚成原样，逐字节相同
    assert md.read_text(encoding="utf-8") == md_before
    assert rp.read_text(encoding="utf-8") == rp_before


def test_rename_citekey_reports_partial_when_rollback_also_fails(tmp_path):
    """R2-1：写盘失败**且回滚也失败** → 返回 partial（不是 refused），磁盘确实是半改的。"""
    stem = "科研札记_2026-04_全文精读"
    md = tmp_path / (stem + ".md")
    md.write_text("## 🔴 高 1. P [@a2026X]\n", encoding="utf-8")
    rp = tmp_path / (stem + ".references.json")
    rp.write_text(json.dumps([{"id": "a2026X", "title": "P"}]), encoding="utf-8")
    real = ni._atomic_write

    def flaky(path, content):
        if path.name == rp.name:
            raise OSError("模拟磁盘满")
        if path.name == md.name and "[@a2026X]" in content:
            raise OSError("模拟回滚也失败")      # 只在写回原内容（回滚）时炸
        return real(path, content)

    entry = {"note_file": md.name, "note_line": 1, "references_json": rp.name, "doi": None}
    try:
        ni._atomic_write = flaky
        assert ni._rename_citekey_in_note(tmp_path, entry, "a2026X",
                                          "b2026X") == ni.RENAME_PARTIAL
    finally:
        ni._atomic_write = real
    assert "[@b2026X]" in md.read_text(encoding="utf-8")        # md 半改，如实汇报
    assert _load_json_id(rp) == "a2026X"


def _load_json_id(p):
    return json.loads(p.read_text(encoding="utf-8"))[0]["id"]


def test_fix_citekey_collisions_disk_matches_report_when_write_fails(tmp_path):
    """R2-1 端到端①：写盘中途失败并回滚成功 → 磁盘要么全改要么全没改，汇总口径与磁盘一致。"""
    from loguru import logger as _lg
    _write_collide_notes(tmp_path)
    md2_before = (tmp_path / "科研札记_2026-02_全文精读.md").read_text(encoding="utf-8")
    rp2 = tmp_path / "科研札记_2026-02_全文精读.references.json"
    rp2_before = rp2.read_text(encoding="utf-8")
    (tmp_path / (rp2.name + ".tmp-{}".format(os.getpid()))).mkdir()     # 堵死 2026-02 的写路
    lines = []
    sink = _lg.add(lines.append, level="WARNING")
    try:
        renamed = ni.fix_citekey_collisions(tmp_path)
    finally:
        _lg.remove(sink)
    blob = "\n".join(lines)
    assert renamed == 1
    # 2026-02：三处一处没改（不是「md 已改、refs 还是旧 id」的半改态）
    assert (tmp_path / "科研札记_2026-02_全文精读.md").read_text(encoding="utf-8") == md2_before
    assert rp2.read_text(encoding="utf-8") == rp2_before
    # 汇总说「磁盘未改动」时，磁盘必须真的没动（旧实现在这里自相矛盾）
    assert "磁盘未改动" in blob and "dup2026Key → dup2026Keyb" in blob
    assert "半改" not in blob
    # 2026-03 正常改成 b（2026-02 的键没落盘，后缀本就该让给它）
    assert _md_key(tmp_path, "2026-03") == "dup2026Keyb"
    assert _refs_id(tmp_path, "2026-03") == "dup2026Keyb"
    assert _md_key(tmp_path, "2026-01") == "dup2026Key"


def test_fix_citekey_collisions_never_reuses_a_key_left_on_disk(tmp_path):
    """R2-1 端到端②：半改（回滚也失败）时新键**已在 md 上**，绝不能再发给同组下一条。

    旧实现只在成功时 all_keys.add(new)，于是 2026-02 与 2026-03 会拿到同一个 dup2026Keyb
    —— 修撞键的工具在磁盘上新造一个撞键，而报告里只说「1 条未能修复、磁盘未改动」。
    """
    from loguru import logger as _lg
    _write_collide_notes(tmp_path)
    real = ni._atomic_write
    bad_md = "科研札记_2026-02_全文精读.md"
    bad_rp = "科研札记_2026-02_全文精读.references.json"

    def flaky(path, content):
        if path.name == bad_rp:
            raise OSError("模拟磁盘满")
        if path.name == bad_md and "[@dup2026Key]" in content:
            raise OSError("模拟回滚也失败")
        return real(path, content)

    lines = []
    sink = _lg.add(lines.append, level="WARNING")
    try:
        ni._atomic_write = flaky
        renamed = ni.fix_citekey_collisions(tmp_path)
    finally:
        ni._atomic_write = real
        _lg.remove(sink)
    assert renamed == 1                                   # 只有 2026-03 算成功
    assert _md_key(tmp_path, "2026-02") == "dup2026Keyb"   # 半改：键已落在 md 上
    assert _refs_id(tmp_path, "2026-02") == "dup2026Key"
    # 核心断言：下一条不得复用那个已落盘的键，否则磁盘上凭空多出一组撞键
    assert _md_key(tmp_path, "2026-03") != "dup2026Keyb"
    assert _md_key(tmp_path, "2026-03") == "dup2026Keyc"
    assert _refs_id(tmp_path, "2026-03") == "dup2026Keyc"
    assert len({_md_key(tmp_path, m) for m in _COLLIDE_TITLES}) == 3
    blob = "\n".join(lines)
    assert "半改" in blob and bad_md in blob               # 单列成半改清单，不说「磁盘未改动」


def test_stale_distinct_key_warns_and_wording_distinguishes_both_missing(tmp_path):
    """R1-5：distinct 漂键也要告警（原先只有 merge 有）；两侧都缺时不得只说「缺 前者」。"""
    from loguru import logger as _lg
    lines = []
    sink = _lg.add(lines.append, level="WARNING")
    try:
        (tmp_path / ni.DEDUP_OVERRIDES_JSON).write_text(json.dumps({
            "merge": [["title:两边都没有a", "title:两边都没有b"],
                      ["title:a", "title:merge后者漂走"]],
            "distinct": [["title:distinct键已漂走", "title:b"]],
        }), encoding="utf-8")
        papers = [_pe("2025-01", "Alpha", "title:a"), _pe("2025-02", "Beta", "title:b")]
        ni._global_pass(papers, notes_dir=tmp_path, review_out=[])
    finally:
        _lg.remove(sink)
    blob = "\n".join(lines)
    assert "distinct键已漂走" in blob and "非同文" in blob     # distinct 那半不再沉默
    # 措辞按 (缺前者 / 缺后者 / 两侧都缺) 三分，不再一律写成「缺 前者」
    def _line(frag):
        return next(s for s in lines if frag in s)
    assert "两侧都缺" in _line("title:两边都没有a")
    assert "缺 后者" in _line("merge后者漂走")
    assert "缺 前者" in _line("distinct键已漂走")


def test_read_override_files_honors_repo_path_constant(tmp_path, monkeypatch):
    """R1-9：仓库那份裁决文件的路径可被 monkeypatch，测试得以完全隔离。

    生产语义保持不变：仓库 config 与 notes_dir 两处**取并集**。
    """
    repo_fake = tmp_path / "repo" / ni.DEDUP_OVERRIDES_JSON
    repo_fake.parent.mkdir()
    repo_fake.write_text(json.dumps({"merge": [["title:r1", "title:r2"]]}), encoding="utf-8")
    monkeypatch.setattr(ni, "REPO_OVERRIDES_PATH", repo_fake)
    nd = tmp_path / "notes"
    nd.mkdir()
    (nd / ni.DEDUP_OVERRIDES_JSON).write_text(
        json.dumps({"merge": [["title:n1", "title:n2"]]}), encoding="utf-8")
    pairs = ni._pairs_from(ni._read_override_files(nd), "merge")
    assert [["title:r1", "title:r2"], ["title:n1", "title:n2"]] == pairs   # 并集，不是择一
    # 指向不存在的路径 = 仓库那侧完全不参与（本文件 autouse fixture 用的就是这一招）
    monkeypatch.setattr(ni, "REPO_OVERRIDES_PATH", tmp_path / "nope.json")
    assert ni._read_override_files(None) == []


# ---------------- scripts/ 侧回归（脚本无独立测试文件，收在此处） ----------------

def _load_script(name):
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "scripts" / name
    spec = importlib.util.spec_from_file_location("script_" + name[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_audit_citekey_tail_refuses_unparseable_keys():
    """R1-1：拆不出「姓+4 位年」的 citekey 必须返回 None（跳过），不能把整串当标题实词拼回去。

    旧写法 re.sub 不命中时原样返回，新键 = 姓+年+整个旧键，产物如
    torresfuertes2026куксенко2024Аналіз —— 含西里尔字符，pandoc citekey 语法不接受。
    这类键还必然被判为不一致（citekey_parts 对它们返回 year=None），--apply 一定会去改。
    """
    aud = _load_script("audit_citekeys_vs_pmlr.py")
    assert aud.citekey_tail("thakur2024Federated") == "Federated"
    assert aud.citekey_tail("guillen-ramirez2025Prediction") == "Prediction"
    for bad in ["molaeiFederated", "anonProceedings", "", None]:
        assert aud.citekey_tail(bad) is None
    # citekey_parts 与 citekey_tail 用同一个正则：连字符姓不再被切
    assert aud.citekey_parts("guillen-ramirez2025Prediction") == ("guillenramirez", 2025)
    # 新键合法性闸：西里尔尾巴过不去
    assert aud.VALID_CITEKEY_RE.match("torresfuertes2026Uncertainty")
    assert not aud.VALID_CITEKEY_RE.match("torresfuertes2026Аналіз")


def test_audit_slug_re_accepts_double_letter_suffix_and_digit_surname():
    """R1-4：PMLR 真实存在 chen22aa / zhang22ab（双字母后缀），窄正则会让它们漏审无声。"""
    aud = _load_script("audit_citekeys_vs_pmlr.py")
    assert aud.slug_parts("pmlr:v287/elsharief25a") == ("elsharief", 2025)
    assert aud.slug_parts("pmlr:v202/o-neill23a") == ("o-neill", 2023)
    assert aud.slug_parts("pmlr:v162/chen22aa") == ("chen", 2022)
    assert aud.slug_parts("pmlr:v139/zhang21ab") == ("zhang", 2021)
    assert aud.slug_parts("pmlr:v235/3dgs24a") == ("3dgs", 2024)
    assert aud.slug_parts("doi:10.1/x") is None


def test_backfill_volume_of_scans_all_path_segments_and_rejects_ambiguity():
    """R1-8：批次目录可能不在紧邻文件那一层；同时命中两个已登记关键词时必须拒绝而非任选。"""
    bf = _load_script("backfill_pmlr_metadata.py")
    assert bf.volume_of("/x/ML4H2025/ji25a.pdf") == "297"
    assert bf.volume_of("/x/CHIL2025/sub/ji25a.pdf") == "287"       # 多一层子目录也认得
    assert bf.volume_of("/x/CHIL2025_CHIL2026/ji25a.pdf") is None   # 歧义 → 拒绝
    assert bf.volume_candidates("/x/CHIL2025_CHIL2026/ji25a.pdf") == {"287", "333"}
    assert bf.volume_of("/x/ML4H2025/CHIL2026/ji25a.pdf") is None   # 两段各命中一个 → 拒绝
    assert bf.volume_of("/x/nothing/ji25a.pdf") is None
    assert bf.volume_candidates("/x/nothing/ji25a.pdf") == set()
    # 文件名不参与匹配（免得 ML4H2025.pdf 这种命名误判）
    assert bf.volume_of("/x/misc/ML4H2025.pdf") is None


def _build_audit_sandbox(d):
    """一份含「citekey 与 PMLR slug 不一致」条目的假札记库，供 audit 脚本 --apply 跑。"""
    ck, title = "torres2026Uncertaintyaware", "Uncertainty Aware Logistic Regression"
    url = "https://proceedings.mlr.press/v297/torres-fuertes26a.html"
    stem = "科研札记_2026-08_全文精读"
    (d / (stem + ".md")).write_text(
        "# 论文\n## 🔴 高 1. {} [@{}]\n**优先级**: `0.5`\n**链接**: {}\n".format(title, ck, url),
        encoding="utf-8")
    (d / (stem + ".references.json")).write_text(
        json.dumps([{"id": ck, "title": title}], ensure_ascii=False), encoding="utf-8")
    (d / (stem + ".index.json")).write_text(json.dumps({"papers": [
        {"citekey": ck, "title": title, "url": url, "doi": None, "arxiv_id": None,
         "priority_rank": 1, "highlights": [], "tag_counts": {}}]},
        ensure_ascii=False), encoding="utf-8")
    return ck


def test_audit_apply_separates_partial_from_refused(tmp_path, monkeypatch, capsys):
    """R2-1（审计脚本这一侧）：改键返回三态时必须分流，别拿返回值当 bool。

    旧写法 `if _rename_citekey_in_note(...)` 会把 "partial" 这个**真值**当成成功：
    磁盘半改却打印 🔧、rc=0，运维完全收不到信号。
    """
    aud = _load_script("audit_citekeys_vs_pmlr.py")
    _build_audit_sandbox(tmp_path)
    argv = ["audit", "--notes-dir", str(tmp_path), "--apply"]

    monkeypatch.setattr(aud, "_rename_citekey_in_note", lambda *a, **k: ni.RENAME_PARTIAL)
    monkeypatch.setattr(aud.sys, "argv", argv)
    assert aud.main() == 1                       # 半改必须非零退出
    out = capsys.readouterr().out
    assert "半改" in out and "🔧" not in out
    assert "磁盘未改动" not in out               # 半改绝不能说成「磁盘未改动」

    monkeypatch.setattr(aud, "_rename_citekey_in_note", lambda *a, **k: ni.RENAME_REFUSED)
    monkeypatch.setattr(aud.sys, "argv", argv)
    assert aud.main() == 1
    out = capsys.readouterr().out
    assert "磁盘未改动" in out and "半改" not in out


# ---------------- R3：改键对派生物（向量库 / docx）的告知面 ----------------

def _loguru_lines():
    """loguru 的 sink 收不进 caplog，临时挂一个 list sink（同 test_stale_override_key_warns）。"""
    from loguru import logger as _lg
    lines = []
    return lines, _lg, _lg.add(lines.append, level="INFO")


def test_announce_rekey_side_effects_names_stale_docx_and_vector_rebuild(tmp_path):
    """R3-1/R3-4：改完 citekey 必须把两个派生物的失效讲出来，否则运维零信号。

    改键只落在 md + references.json + sidecar 三处。向量库的 chunk 上内嵌 citekey/year，
    已渲染的 docx 正文里也写死了 citekey——两者都不会自己跟上，且原先没有任何一行日志
    提到它们。缺了这一步的现实后果：notes_search --cite 吐出磁盘上已不存在的键，
    pandoc 渲染成 (key?)；传阅出去的 docx 里的键读者回查不到。
    """
    (tmp_path / "科研札记_2026-08_手动精读.docx").write_bytes(b"PK\x03\x04fake")
    entry = {"citekey": "fani2026Coefficient", "month": "2026-08",
             "note_file": "科研札记_2026-08_手动精读.md"}

    lines, _lg, sink = _loguru_lines()
    try:
        out = ni.announce_rekey_side_effects(tmp_path, [entry])
    finally:
        _lg.remove(sink)
    blob = "\n".join(lines)

    assert len(out["stale_docx"]) == 1
    assert out["stale_docx"][0]["month"] == "2026-08"
    assert "科研札记_2026-08_手动精读.docx" in blob      # 点名到具体文件
    assert "2026-08" in blob                             # 受影响月份
    assert "notes_embed.py" in blob                      # 向量库重建命令
    # 库文件不存在 → 只提示不同步，绝不因此报错或连 Ollama
    assert out["synced"] is False and out["error"] is None


def test_announce_rekey_side_effects_is_silent_when_nothing_renamed(tmp_path):
    """没改任何键就不该刷屏——告警刷成噪音等于没有告警。"""
    (tmp_path / "科研札记_2026-08_手动精读.docx").write_bytes(b"PK\x03\x04fake")
    lines, _lg, sink = _loguru_lines()
    try:
        out = ni.announce_rekey_side_effects(tmp_path, [])
    finally:
        _lg.remove(sink)
    assert out == {"stale_docx": [], "synced": False, "error": None}
    assert not [s for s in lines if "notes_embed" in s or "docx" in s]


def test_fix_citekey_collisions_announces_derived_artifacts(tmp_path):
    """R3-1/R3-4 端到端：撞键自动改键这条路径也必须走收尾告知（原先改完就静默返回）。"""
    _write_month(tmp_path, month="2024-01",
                 citekeys={"pa": "wang2024Same", "pb": "lee2025Graph", "pc": None})
    _write_month(tmp_path, month="2024-05",
                 citekeys={"pa": "x2024A", "pb": "x2024B", "pc": None})
    stem5 = "科研札记_2024-05_全文精读"
    md5 = tmp_path / (stem5 + ".md")
    md5.write_text(md5.read_text(encoding="utf-8").replace("[@x2024A]", "[@wang2024Same]"),
                   encoding="utf-8")
    rp5 = tmp_path / (stem5 + ".references.json")
    items = json.loads(rp5.read_text(encoding="utf-8"))
    for it in items:
        if it["id"] == "x2024A":
            it["id"] = "wang2024Same"
            it["DOI"] = "10.9/other"
            it["title"] = "Another Different Paper"
    rp5.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    md5.write_text(md5.read_text(encoding="utf-8").replace(
        "Deep | EHR [Models] under MNAR", "Another Different Paper"), encoding="utf-8")
    (tmp_path / (stem5 + ".docx")).write_bytes(b"PK\x03\x04fake")   # 该月已渲染过

    lines, _lg, sink = _loguru_lines()
    try:
        renamed = ni.fix_citekey_collisions(tmp_path)
    finally:
        _lg.remove(sink)
    blob = "\n".join(lines)

    assert renamed == 1
    assert "notes_embed.py" in blob                       # 向量库失效必须讲出来
    assert stem5 + ".docx" in blob                        # 且点名被改月份的过期 docx
    # 没被改键的那个月的 docx 不该被拉进清单
    assert "科研札记_2024-01_全文精读.docx" not in blob


def test_audit_apply_announces_derived_artifacts(tmp_path, monkeypatch, capsys):
    """R3-1：audit --apply 这条改键路径同样要收尾告知（它此前也是改完直接退出）。"""
    aud = _load_script("audit_citekeys_vs_pmlr.py")
    _build_audit_sandbox(tmp_path)
    (tmp_path / "科研札记_2026-08_全文精读.docx").write_bytes(b"PK\x03\x04fake")
    monkeypatch.setattr(aud, "_rename_citekey_in_note", lambda *a, **k: ni.RENAME_OK)
    monkeypatch.setattr(aud.sys, "argv", ["audit", "--notes-dir", str(tmp_path), "--apply"])

    lines, _lg, sink = _loguru_lines()
    try:
        rc = aud.main()
    finally:
        _lg.remove(sink)
    blob = "\n".join(lines)

    assert rc == 0
    assert "notes_embed.py" in blob
    assert "科研札记_2026-08_全文精读.docx" in blob
    capsys.readouterr()


def test_audit_apply_stays_silent_about_derived_artifacts_when_nothing_changed(
        tmp_path, monkeypatch, capsys):
    """预检不过、磁盘零改动时不该喊「向量库失效」——那是假警报，会稀释真警报。"""
    aud = _load_script("audit_citekeys_vs_pmlr.py")
    _build_audit_sandbox(tmp_path)
    monkeypatch.setattr(aud, "_rename_citekey_in_note", lambda *a, **k: ni.RENAME_REFUSED)
    monkeypatch.setattr(aud.sys, "argv", ["audit", "--notes-dir", str(tmp_path), "--apply"])

    lines, _lg, sink = _loguru_lines()
    try:
        aud.main()
    finally:
        _lg.remove(sink)
    assert "notes_embed.py" not in "\n".join(lines)
    capsys.readouterr()


# ---------------- write_notes 原子落盘（md 最后落 = 完成标记） ----------------

def test_write_notes_crash_keeps_old_md_intact(tmp_path, monkeypatch):
    """模拟 md 落盘瞬间崩溃（os.replace 对 .md 目标抛错）：旧 md 必须逐字节保留——
    裸 open('w') 会先截断出 0 字节/半截 md，骗过 backfill 的 exists() 完成判定，
    重跑记 skipped 后被截论文永久丢失。references/sidecar 须已按新顺序先落齐，
    md 是最后的事务提交标记。"""
    old_content = "# 上一轮完整札记\n"
    old_md = tmp_path / "科研札记_2025-03_全文精读.md"
    old_md.write_text(old_content, encoding="utf-8")

    real_replace = os.replace

    def crash_on_md(src, dst):
        if str(dst).endswith(".md"):
            raise OSError("模拟写 md 时被杀")
        return real_replace(src, dst)

    monkeypatch.setattr(ni.os, "replace", crash_on_md)
    with pytest.raises(OSError):
        _write_month(tmp_path, sidecar=True)

    # 旧 md 未被截断/污染（原子写的全部意义）
    assert old_md.read_text(encoding="utf-8") == old_content
    # 配套两件已完整先落盘且可解析
    refs = json.loads((tmp_path / "科研札记_2025-03_全文精读.references.json")
                      .read_text(encoding="utf-8"))
    assert refs
    side = json.loads((tmp_path / "科研札记_2025-03_全文精读.index.json")
                      .read_text(encoding="utf-8"))
    assert side["papers"]


def test_write_notes_success_no_tmp_leftover(tmp_path):
    """正常落盘后三件套齐活，且目录里不残留 .tmp-* 中间文件。"""
    _write_month(tmp_path, sidecar=True)
    assert not list(tmp_path.glob("*.tmp-*"))
    for ext in (".md", ".references.json", ".index.json"):
        assert (tmp_path / ("科研札记_2025-03_全文精读" + ext)).exists()


# ---------------- R1：md 是 flags 的真相源（撤稿踢库） ----------------

def test_md_retracted_flag_overrides_sidecar(tmp_path):
    """⚑ RETRACTED 是人工事后写进 md 裁决行的，不在 sidecar 快照里。sidecar 全量顶掉
    md 时 flags 一次都没回读 → 撤稿踢库对全部有 sidecar 的月份静默失效（真库 40/83 月、
    1019/2343 篇 = 43%），而 lint 读的也是索引，会永远报「已撤稿且未标记」。"""
    _write_month(tmp_path, sidecar=True)
    stem = "科研札记_2025-03_全文精读"
    md = tmp_path / (stem + ".md")
    text = md.read_text(encoding="utf-8")
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("**裁决**") and "THREAT" not in ln:
            lines[i] = ln + " ⚑ RETRACTED"
            break
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    entries = ni.build_month_entries("2025-03", md,
                                     ref_path=tmp_path / (stem + ".references.json"),
                                     sidecar_path=tmp_path / (stem + ".index.json"))
    assert all(e["_source"] == "sidecar" for e in entries)      # 仍走 sidecar 路径
    assert any("RETRACTED" in (e.get("flags") or []) for e in entries)
    assert any(ni.is_retracted(e) for e in entries)


def test_sidecar_flags_survive_roundtrip(tmp_path):
    """反向保护：sidecar 里已有的 flags（THREAT/BENCHMARK）经 md 渲染 → 回读必须原样还原。
    真库 18 条带 flags 的 sidecar 条目实测 mismatch=0，这条把它钉住。

    单看这一条是**非歧视性**的（不回读 md 时它也绿）——所以额外断言 flags 确实来自 md：
    改掉 md 的裁决行后回读值必须跟着变，否则说明根本没读 md。"""
    _write_month(tmp_path, sidecar=True)
    stem = "科研札记_2025-03_全文精读"
    md = tmp_path / (stem + ".md")
    entries = ni.build_month_entries("2025-03", md,
                                     ref_path=tmp_path / (stem + ".references.json"),
                                     sidecar_path=tmp_path / (stem + ".index.json"))
    by = {e["citekey"]: e for e in entries}
    assert by["public2025Deep"]["flags"] == ["THREAT"]

    text = md.read_text(encoding="utf-8").replace("⚑ THREAT", "⚑ THREAT/RETRACTED")
    md.write_text(text, encoding="utf-8")
    entries2 = ni.build_month_entries("2025-03", md,
                                      ref_path=tmp_path / (stem + ".references.json"),
                                      sidecar_path=tmp_path / (stem + ".index.json"))
    by2 = {e["citekey"]: e for e in entries2}
    assert by2["public2025Deep"]["flags"] == ["THREAT", "RETRACTED"], "flags 必须真的来自 md"


def test_md_row_unmatched_keeps_sidecar_flags(tmp_path):
    """认不到 md 行时必须**保留 sidecar 原值、绝不清空**：RENAME_PARTIAL（md 已是新键、
    sidecar 仍旧键）是显式分流处理的活状态，那时按 citekey 认领会认空——一清空就把
    撤稿论文重新放回向量库，比不修更危险。"""
    _write_month(tmp_path, sidecar=True)
    stem = "科研札记_2025-03_全文精读"
    sc = tmp_path / (stem + ".index.json")
    data = json.loads(sc.read_text(encoding="utf-8"))
    for p in data["papers"]:
        if p.get("citekey") == "public2025Deep":
            p["citekey"] = "public2025DeepRENAMED"    # 模拟 md/sidecar 键失同步
            p["flags"] = ["RETRACTED"]
    sc.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    entries = ni.build_month_entries("2025-03", tmp_path / (stem + ".md"),
                                     ref_path=tmp_path / (stem + ".references.json"),
                                     sidecar_path=sc)
    e = next(x for x in entries if x["citekey"] == "public2025DeepRENAMED")
    assert e["note_line"] is None                       # 确认真的认不到 md 行
    assert e["flags"] == ["RETRACTED"], "认不到就保留，不许清空"


def test_fix_collisions_reports_vector_sync_failure(tmp_path, monkeypatch):
    """R2:改键落盘后向量库没跟上 = 检索会吐已注销的旧键。此前 --fix-collisions 恒 exit 0,
    与同一批刚给 audit_citekeys_vs_pmlr 立的标准打架。用出参带出,不动 int 返回值。"""
    _write_month(tmp_path, citekeys={"pa": "dup2025Key", "pb": "dup2025Key", "pc": None},
                 sidecar=True)
    (tmp_path / ni.DB_NAME if hasattr(ni, "DB_NAME") else tmp_path / "embeddings.sqlite3").write_bytes(b"x")
    monkeypatch.setattr("src.scholar.embed_store.sync_store_best_effort",
                        lambda *a, **k: None)          # 模拟同步失败
    side = {}
    ni.fix_citekey_collisions(tmp_path, side_out=side)
    # 只断真值性是恒绿的:删掉 `if stats is None` 守卫后代码撞进外层 except 照样置 error,
    # 而且会同时留下 synced=True 的自相矛盾态。两条一起断才咬得住。
    assert "向量库同步失败" in (side.get("error") or "")
    assert side.get("synced") is not True, "同步失败时不许同时自称 synced"


# ---------------- 书籍/章节一等公民（2026-08-25） ----------------

def _write_book_note(tmp_path, segs, label="2026-08-25-TestBook"):
    """写一份书籍精读札记（series=book，一书一文件）。"""
    return write_notes(segs, {s.paper_id: None for s in segs}, out_dir=tmp_path,
                       digest_title="科研札记 · {}（书籍精读）".format(label),
                       filename="科研札记_{}_书籍精读".format(label),
                       fallback_citekeys=True, emit_index_sidecar=True,
                       index_series="book")


def _book_meta(**kw):
    d = dict(paper_id="bk", title="Statistical Analysis with Missing Data",
             authors=["Roderick J. A. Little", "Donald B. Rubin"],
             entry_type="book", isbn="9781119482260", publisher="Wiley",
             edition="3rd", publication_date=date(2019, 10, 9), date_precision="day")
    d.update(kw)
    return PaperMetadata(**d)


def _chapter_meta(**kw):
    d = dict(paper_id="ch14", title="Harm (Observational Studies)",
             authors=["Gordon Guyatt"], entry_type="chapter", isbn="9780071790710",
             book_key="guyatt2015users", container_title="Users' Guides to the Medical Literature",
             chapter_number=14, page_range="301-313", publisher="McGraw-Hill",
             edition="3rd", editors=["Gordon Guyatt", "Drummond Rennie"])
    d.update(kw)
    return PaperMetadata(**d)


def test_article_csl_byte_identical_after_book_branch():
    """金标：文章路径的 CSL 输出（键序与取值）不因书籍分支改变。"""
    from src.scholar.notes import build_csl_item
    meta = PaperMetadata(paper_id="pa", title="A Paper", authors=["Jane Doe", "Smith, John"],
                         journal="JMLR", doi="10.1/aaa", url="https://x/y",
                         volume="7", issue="2", pages="1-9",
                         publication_date=date(2025, 3, 1), date_precision="month")
    item = build_csl_item(meta, "doe2025Paper")
    assert list(item.keys()) == ["id", "type", "title", "author", "container-title",
                                 "DOI", "URL", "volume", "issue", "page", "issued"]
    assert item["type"] == "article-journal"
    assert item["author"] == [{"family": "Doe", "given": "Jane"},
                              {"family": "Smith", "given": "John"}]
    assert item["issued"] == {"date-parts": [[2025, 3]]}
    # arXiv-only 仍落 "article"
    arx = build_csl_item(PaperMetadata(paper_id="pb", title="T", arxiv_id="2401.1"), "k")
    assert arx["type"] == "article"


def test_book_and_chapter_csl_types_and_fields():
    from src.scholar.notes import build_csl_item
    bk = build_csl_item(_book_meta(), "little2020rubin")
    assert bk["type"] == "book"
    assert bk["publisher"] == "Wiley" and bk["edition"] == "3rd"
    assert bk["ISBN"] == "9781119482260"
    assert "container-title" not in bk          # 专著没有容器

    ch = build_csl_item(_chapter_meta(), "guyatt2015harm")
    assert ch["type"] == "chapter"
    assert ch["container-title"] == "Users' Guides to the Medical Literature"
    assert ch["editor"] == [{"family": "Guyatt", "given": "Gordon"},
                            {"family": "Rennie", "given": "Drummond"}]
    assert ch["page"] == "301-313"              # 章页码范围进 page
    assert ch["author"] == [{"family": "Guyatt", "given": "Gordon"}]


def test_fallback_csl_matches_notes_csl_for_books():
    """两侧实现已收敛：同一本书经 notes 与 notes_index 两条路产出同型 CSL。"""
    from src.scholar.notes import build_csl_item
    meta = _chapter_meta()
    a = build_csl_item(meta, "guyatt2015harm")
    b = ni._fallback_csl({"citekey": "guyatt2015harm", "title": meta.title,
                          "authors": meta.authors, "entry_type": "chapter",
                          "isbn": meta.isbn, "publisher": meta.publisher,
                          "edition": meta.edition, "editors": meta.editors,
                          "container_title": meta.container_title,
                          "page_range": meta.page_range})
    for k in ("type", "title", "author", "container-title", "editor", "publisher",
              "edition", "ISBN", "page"):
        assert a[k] == b[k], k


def test_page_anchor_round_trips_through_md(tmp_path):
    """页码锚：渲染→解析→highlights 必须原样带回（三处语法共有者的回归锁）。"""
    cr = CloseReading(from_full_text=True, source="manual-pdf", sections=[
        CloseReadSection(heading="Ch.1 · pp.3-28 · Introduction", sentences=[
            CloseReadSentence(text="缺失值背后必须存在有意义的真值。",
                              tag="可引用证据", page="4"),
            CloseReadSentence(text="单调缺失可排序。", tag="方法论借鉴", page="8-12"),
            CloseReadSentence(text="无页码的旧行。", tag="可反驳观点")])])
    seg = PaperSegment(segment_id=1, paper_id="bk", priority_score=1.0,
                       metadata=_book_meta(), close_reading=cr)
    _write_book_note(tmp_path, [seg], label="2026-08-25-LittleRubin2020")
    md = tmp_path / "科研札记_2026-08-25-LittleRubin2020_书籍精读.md"
    assert "〔p.4〕" in md.read_text(encoding="utf-8")
    entries = ni.parse_note_md(md)
    hl = entries[0]["highlights"]
    assert [h.get("pages") for h in hl] == ["4", "8-12", None]
    assert hl[0]["text"] == "缺失值背后必须存在有意义的真值。"    # 锚未混进正文
    assert hl[2]["text"] == "无页码的旧行。"


def test_book_metadata_line_round_trips(tmp_path):
    """`**所属书籍**:` 行：渲染→解析恢复身份关键字段（md 降级路径）。"""
    from src.scholar.notes import _book_line
    line = _book_line(_chapter_meta())
    got = ni._parse_book_line(line[len(ni._BOOK_LINE_PREFIX):])
    assert got["entry_type"] == "chapter"
    assert got["isbn"] == "9780071790710"
    assert got["chapter_number"] == 14
    assert got["page_range"] == "301-313"
    assert got["container_title"] == "Users' Guides to the Medical Literature"
    assert got["book_key"] == "guyatt2015users"
    # 专著：无章号 → book
    assert ni._parse_book_line(
        _book_line(_book_meta())[len(ni._BOOK_LINE_PREFIX):])["entry_type"] == "book"
    assert _book_line(PaperMetadata(paper_id="p", title="T")) is None   # 文章不渲染此行


def test_book_series_enters_index_with_isbn_key(tmp_path):
    """书籍札记进索引：series=book，dedup_key 走 ISBN 档，章条目带 :chNN。"""
    seg = PaperSegment(segment_id=1, paper_id="ch14", priority_score=1.0,
                       metadata=_chapter_meta(),
                       close_reading=CloseReading(from_full_text=True, sections=[
                           CloseReadSection(heading="要点", sentences=[
                               CloseReadSentence(text="观察性研究的偏倚来源。",
                                                 tag="可引用证据", page="303")])]))
    _write_book_note(tmp_path, [seg], label="2026-08-25-JAMAGuide")
    idx = ni.update_index(tmp_path, full=True)
    e = next(x for x in idx["papers"] if x["series"] == "book")
    assert e["dedup_key"] == "isbn:9780071790710:ch14"
    assert e["entry_type"] == "chapter" and e["book_key"] == "guyatt2015users"
    assert e["highlights"][0]["pages"] == "303"


def test_existing_article_entries_keep_no_book_fields(tmp_path):
    """文章条目不得因改造长出书籍键（索引形状回归）。"""
    _write_month(tmp_path, month="2026-07", sidecar=True)
    idx = ni.update_index(tmp_path, full=True)
    from src.scholar._citekey_utils import BOOK_ENTRY_FIELDS
    for e in idx["papers"]:
        assert not (set(e) & set(BOOK_ENTRY_FIELDS)), e.get("citekey")
        for h in e.get("highlights") or []:
            assert "pages" not in h
