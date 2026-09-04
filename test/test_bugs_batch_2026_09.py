# -*- coding: utf-8 -*-
"""2026-09-04 docs/bugs 台账清理批的回归测试（按台账条目分节）。

覆盖：digest --month 覆盖护栏（W4）/ keeper 视图（W6）/ sidecar 失败不静默（W7）/
citekey 继承基键 + 陈旧行内键善后工具（W8）/ 摘要回读（W10）/ 渲染↔解析往返（W11）/
Zotero 非 ASCII 键告警（W17）。fulltext 四路见 test_fulltext_extra_routes.py，
topics 子进程见 test_topics.py 末节，ingest 五态见 test_pdf_ingest.py 末节。
"""
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from loguru import logger as _lg

from src.scholar import notes as N
from src.scholar import notes_index as ni
from src.scholar._citekey_utils import TAG_LINE_RE, render_tag_line, TAG_MARK
from src.scholar.schema import (CloseReading, CloseReadSection, CloseReadSentence,
                                FilterDecision, PaperMetadata, PaperSegment)


def _sink():
    lines = []
    hid = _lg.add(lambda m: lines.append(str(m)), level="DEBUG")
    return lines, hid


def _fd(pid, decision="INCLUDE"):
    return FilterDecision(paper_id=pid, title="t", verdict="included", decision=decision,
                          stage="llm_judge", reason="r", one_line="用处", confidence=0.9)


def _cr(n=3):
    return CloseReading(from_full_text=True, source="manual-pdf", sections=[
        CloseReadSection(heading="关键结论", sentences=[
            CloseReadSentence(text="结论 {}。".format(i), tag="可引用证据") for i in range(n)])])


def _seg(pid, title, doi=None, authors=("Jane Public",), abstract="An abstract.", cr=None,
         arxiv_id=None, score=0.9):
    return PaperSegment(
        segment_id=1, paper_id=pid, priority_score=score,
        metadata=PaperMetadata(paper_id=pid, title=title, authors=list(authors), doi=doi,
                               arxiv_id=arxiv_id, journal="J", publication_date=date(2025, 3, 2)),
        original_abstract=abstract, filter_decision=_fd(pid), close_reading=cr)


# =====================================================================================
# W4 digest --month 覆盖护栏（docs/bugs/2026-09-04-digest-month-overwrite.md）
# =====================================================================================

def _proc(tmp_path, closeread=False):
    return SimpleNamespace(
        notes_dir=tmp_path, closeread_enabled=closeread, zotero_base_url="http://z", zotero_email="",
        external_email="", zotero_attach_pdf=False, zotero_enrich_crossref=False, zotero_collection="",
        zotero_translation_server_url="", notes_instruction="", notes_emit_docx=False,
        notes_docx_cjk_font="", research_interests="", closeread_top_n=1, closeread_deep=False,
        closeread_max_chars=1000, closeread_max_chunks=1)


def _workflow(tmp_path, closeread=False, month=(2023, 5)):
    from src.scholar.workflow import ScholarWorkflow
    wf = object.__new__(ScholarWorkflow)
    wf.settings = SimpleNamespace(processing=_proc(tmp_path, closeread),
                                  llm=SimpleNamespace(closeread_model="m", model="m"))
    wf.segments = [_seg("p1", "Paper One", doi="10.1/one")]
    wf.date_range = (date(month[0], month[1], 1), date(month[0], month[1], 28)) if month else None
    wf.allow_note_overwrite = False
    wf.output_dir = tmp_path / "out"
    wf.output_dir.mkdir(exist_ok=True)
    wf.run_id = "run1"
    return wf


def _stub_zotero_and_notes(monkeypatch):
    import src.scholar.zotero_sync as zs
    import src.scholar.workflow as wfm
    calls = {"sync": 0, "write": [], "notify": []}
    monkeypatch.setattr(zs, "sync_segments_to_zotero",
                        lambda segs, **k: calls.__setitem__("sync", calls["sync"] + 1) or {})
    real_write = N.write_notes

    def _write(*a, **k):
        calls["write"].append(k.get("filename"))
        return real_write(*a, **k)
    monkeypatch.setattr(N, "write_notes", _write)
    monkeypatch.setattr(wfm, "notify", lambda t, m: calls["notify"].append((t, m)))
    return calls


def test_planned_note_stem_matches_write_notes_filename(tmp_path):
    wf = _workflow(tmp_path, closeread=True)
    assert wf.planned_note_stem() == "科研札记_2023-05_全文精读"
    assert wf.planned_note_path() == tmp_path / "科研札记_2023-05_全文精读.md"
    wf2 = _workflow(tmp_path, closeread=False, month=None)
    assert wf2.planned_note_stem().startswith("科研札记_") and "全文精读" not in wf2.planned_note_stem()
    assert wf2.planned_note_path().suffix == ".md"


def test_overwrite_guard_refuses_and_touches_nothing(tmp_path, monkeypatch):
    """历史月度札记已存在 → 一字不动、不写 Zotero、不跑精读、notify，返回 skipped_existing。"""
    calls = _stub_zotero_and_notes(monkeypatch)
    wf = _workflow(tmp_path, closeread=True)
    target = tmp_path / "科研札记_2023-05_全文精读.md"
    target.write_text("# 历史札记\n人工修订过的精读句\n", encoding="utf-8")
    refs = tmp_path / "科研札记_2023-05_全文精读.references.json"
    refs.write_text("[]", encoding="utf-8")
    lines, hid = _sink()
    try:
        out = wf._step_sync_zotero()
    finally:
        _lg.remove(hid)
    assert out["skipped_existing"] == str(target)
    assert target.read_text(encoding="utf-8") == "# 历史札记\n人工修订过的精读句\n"
    assert calls["sync"] == 0 and calls["write"] == []
    assert calls["notify"] and "拒绝覆盖" in calls["notify"][0][1]
    assert any("拒绝整篇覆盖" in ln and "--overwrite-notes" in ln for ln in lines)
    assert not (tmp_path / N.NOTE_OVERWRITE_BACKUP_DIR).exists()


def test_overwrite_guard_with_flag_backs_up_then_overwrites(tmp_path, monkeypatch):
    calls = _stub_zotero_and_notes(monkeypatch)
    wf = _workflow(tmp_path, closeread=False)
    wf.allow_note_overwrite = True
    stem = "科研札记_2023-05"
    (tmp_path / (stem + ".md")).write_text("OLD MD", encoding="utf-8")
    (tmp_path / (stem + ".references.json")).write_text("[1]", encoding="utf-8")
    (tmp_path / (stem + ".index.json")).write_text("{}", encoding="utf-8")
    (tmp_path / (stem + ".docx")).write_bytes(b"PK")
    out = wf._step_sync_zotero()
    assert "skipped_existing" not in out and calls["write"] == [stem] and calls["sync"] == 1
    bdirs = list((tmp_path / N.NOTE_OVERWRITE_BACKUP_DIR).iterdir())
    assert len(bdirs) == 1
    names = sorted(p.name for p in bdirs[0].iterdir())
    assert names == sorted([stem + ".md", stem + ".references.json", stem + ".index.json", stem + ".docx"])
    assert (bdirs[0] / (stem + ".md")).read_text(encoding="utf-8") == "OLD MD"
    assert (tmp_path / (stem + ".md")).read_text(encoding="utf-8") != "OLD MD"      # 已被重造
    assert not calls["notify"]


def test_no_existing_note_writes_without_backup(tmp_path, monkeypatch):
    calls = _stub_zotero_and_notes(monkeypatch)
    wf = _workflow(tmp_path, closeread=False)
    out = wf._step_sync_zotero()
    assert calls["write"] == ["科研札记_2023-05"] and Path(out["note_path"]).exists()
    assert not (tmp_path / N.NOTE_OVERWRITE_BACKUP_DIR).exists()


def test_backup_note_files_helpers(tmp_path):
    assert N.backup_note_files(tmp_path, "科研札记_2020-01_全文精读") is None
    p = N.note_file_paths(tmp_path, "科研札记_2020-01_全文精读")
    assert set(p) == {"md", "references", "sidecar", "docx"}
    p["md"].write_text("x", encoding="utf-8")
    b = N.backup_note_files(tmp_path, "科研札记_2020-01_全文精读", stamp="S")
    assert b == tmp_path / N.NOTE_OVERWRITE_BACKUP_DIR / "S"
    assert (b / p["md"].name).read_text(encoding="utf-8") == "x"


def test_cli_has_overwrite_notes_flag():
    import argparse
    from src.scholar.cli import add_digest_arguments
    ap = add_digest_arguments(argparse.ArgumentParser())
    ns = ap.parse_args(["--month", "2023-05", "--zotero", "--overwrite-notes"])
    assert ns.overwrite_notes is True and ns.month == "2023-05"
    assert ap.parse_args([]).overwrite_notes is False


# =====================================================================================
# W6 keeper 视图（docs/bugs/2026-09-04-index-keeper-view-missing.md）
# =====================================================================================

def _idx():
    return {"papers": [
        {"citekey": "a2025X", "title": "A", "duplicate_of": None, "has_full_text_reading": True, "month": "2025-01"},
        {"citekey": "a2025X", "title": "A", "duplicate_of": "doi:1@2025-01", "has_full_text_reading": False,
         "month": "2025-06"},
        {"citekey": "MISSING-KEY-abc", "title": "M", "duplicate_of": None},
        {"citekey": "m2025Y", "title": "M2", "citekey_source": "missing", "duplicate_of": None},
        {"citekey": "r2025Z", "title": "R", "duplicate_of": None, "flags": ["RETRACTED"]},
        {"citekey": None, "title": "N", "duplicate_of": None},
        "not-a-dict",
    ]}


def test_keepers_by_citekey_never_returns_duplicate():
    idx = _idx()
    naive = {e["citekey"]: e for e in idx["papers"] if isinstance(e, dict) and e.get("citekey")}
    assert naive["a2025X"]["has_full_text_reading"] is False        # 天真写法读到的是 duplicate
    by = ni.keepers_by_citekey(idx)
    assert by["a2025X"]["has_full_text_reading"] is True
    assert set(by) == {"a2025X", "r2025Z"}
    assert set(ni.keepers_by_citekey(idx, include_retracted=False)) == {"a2025X"}
    assert [e["citekey"] for e in ni.iter_keepers(idx)] == ["a2025X", "r2025Z"]


def test_backfill_deepread_and_embed_store_delegate_to_iter_keepers():
    import importlib.util
    from src.scholar.paths import REPO_ROOT
    spec = importlib.util.spec_from_file_location("bd_keepers", REPO_ROOT / "scripts" / "backfill_deepread.py")
    bd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bd)
    idx = _idx()
    assert [e["citekey"] for e in bd._keepers(idx)] == ["a2025X", "r2025Z"]   # 撤稿仍在重读目标集
    from src.scholar.embed_store import chunks_from_index
    ids = {c.id for c in chunks_from_index(idx)}
    assert "p:a2025X" in ids and not any("r2025Z" in i for i in ids)          # 向量库口径剔撤稿


# =====================================================================================
# W7 sidecar 写失败不静默（docs/bugs/2026-09-04-auto-sidecar-missing.md）
# =====================================================================================

def test_write_notes_reports_sidecar_failure_loudly(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise ValueError("model_dump exploded")
    monkeypatch.setattr(N, "entry_from_segment", _boom)
    lines, hid = _sink()
    try:
        res = N.write_notes([_seg("p1", "T", doi="10.1/a")], {"p1": "pub2025T"}, out_dir=tmp_path,
                            filename="科研札记_2025-03_全文精读", emit_index_sidecar=True)
    finally:
        _lg.remove(hid)
    assert res["sidecar_ok"] is False and "ValueError" in res["sidecar_error"]
    assert "index_sidecar" not in res
    assert Path(res["note_path"]).exists()                      # md 照常提交
    assert any("| ERROR" in ln and "写索引 sidecar 失败" in ln and "unknown-legacy" in ln for ln in lines)


def test_write_notes_sidecar_ok_flag_two_states(tmp_path):
    res = N.write_notes([_seg("p1", "T", doi="10.1/a")], {"p1": "k"}, out_dir=tmp_path,
                        filename="s", emit_index_sidecar=True)
    assert res["sidecar_ok"] is True and "sidecar_error" not in res
    res2 = N.write_notes([_seg("p1", "T", doi="10.1/a")], {"p1": "k"}, out_dir=tmp_path,
                         filename="s2", emit_index_sidecar=False)
    assert "sidecar_ok" not in res2          # 没要求写 ≠ 写失败


# =====================================================================================
# W8-A 同一论文继承基键（docs/bugs/2026-09-04-manual-upgrade-citekey-suffix.md，方案 A）
# =====================================================================================

def _dk(seg):
    return N._segment_dedup_key(seg.metadata)


def test_fallback_key_inherits_base_when_owner_is_same_paper(tmp_path):
    seg = _seg("m1", "Federated Missing Data", doi="10.1016/j.knosys.2025.114601",
               authors=("Shi Wang",), cr=_cr())
    base = N._fallback_citekey(seg.metadata)
    owners = {base: _dk(seg)}                       # auto 条目占着基键，且是同一篇
    res = N.write_notes([seg], {"m1": None}, out_dir=tmp_path, filename="m", fallback_citekeys=True,
                        existing_citekeys={base}, existing_key_owners=owners)
    md = Path(res["note_path"]).read_text(encoding="utf-8")
    assert "[@{}]".format(base) in md and "[@{}b]".format(base) not in md


def test_fallback_key_suffixes_when_owner_is_different_paper(tmp_path):
    seg = _seg("m1", "Federated Missing Data", doi="10.1/new", authors=("Shi Wang",))
    base = N._fallback_citekey(seg.metadata)
    res = N.write_notes([seg], {"m1": None}, out_dir=tmp_path, filename="m", fallback_citekeys=True,
                        existing_citekeys={base}, existing_key_owners={base: ("doi:10.1/other", ni._norm_title("Some Other Paper"))})
    assert "[@{}b]".format(base) in Path(res["note_path"]).read_text(encoding="utf-8")


def test_fallback_key_without_owners_keeps_legacy_suffix_behaviour(tmp_path):
    seg = _seg("m1", "Federated Missing Data", doi="10.1/x", authors=("Shi Wang",))
    base = N._fallback_citekey(seg.metadata)
    res = N.write_notes([seg], {"m1": None}, out_dir=tmp_path, filename="m", fallback_citekeys=True,
                        existing_citekeys={base})
    assert "[@{}b]".format(base) in Path(res["note_path"]).read_text(encoding="utf-8")


def test_two_segments_same_paper_in_one_batch_never_share_the_inherited_key(tmp_path):
    s1 = _seg("m1", "Federated Missing Data", doi="10.1/x", authors=("Shi Wang",))
    s2 = _seg("m2", "Federated Missing Data", doi="10.1/x", authors=("Shi Wang",))
    base = N._fallback_citekey(s1.metadata)
    res = N.write_notes([s1, s2], {"m1": None, "m2": None}, out_dir=tmp_path, filename="m",
                        fallback_citekeys=True, existing_citekeys={base},
                        existing_key_owners={base: (_dk(s1), ni._norm_title(s1.metadata.title))})
    md = Path(res["note_path"]).read_text(encoding="utf-8")
    assert md.count("[@{}]".format(base)) == 1 and "[@{}b]".format(base) in md


def test_existing_citekey_owners_reads_index_and_marks_collisions(tmp_path):
    idx = tmp_path / "literature_index.json"
    idx.write_text(json.dumps({"papers": [
        {"citekey": "a2025X", "dedup_key": "doi:1", "note_file": "a.md", "title": "T A"},
        {"citekey": "a2025X", "dedup_key": "doi:1", "note_file": "b.md", "title": "T A"},   # 同篇跨月：一致
        {"citekey": "c2025Z", "dedup_key": "doi:2", "note_file": "a.md"},
        {"citekey": "c2025Z", "dedup_key": "doi:3", "note_file": "b.md"},     # 撞键：不许继承
        {"citekey": "own2025", "dedup_key": "doi:4", "note_file": "own.md"},
    ]}), encoding="utf-8")
    owners = ni.existing_citekey_owners(idx)
    assert owners["a2025X"][0] == "doi:1" and owners["c2025Z"] == ("", "") and owners["own2025"][0] == "doi:4"
    assert owners["a2025X"][1] == ni._norm_title("T A")        # 规范标题一并带出，供继承时判「索引会不会合并」
    assert "own2025" not in ni.existing_citekey_owners(idx, exclude_note_files={"own.md"})
    assert ni.existing_citekey_owners(tmp_path / "nope.json") == {}
    idx.write_text("{broken", encoding="utf-8")
    assert ni.existing_citekey_owners(idx) == {}


# =====================================================================================
# W8-B 陈旧行内键：形态判定 + 告警升级 + 善后工具（inline-citekey-mismatch-warn-only）
# =====================================================================================

@pytest.mark.parametrize("base,key,expect", [
    ("shi2025Federated", "shi2025Federatedb", True),
    ("shi2025Federated", "shi2025Federatedzz", True),
    ("shi2025Federated", "shi2025Federatedbbbb", False),      # 后缀最长 3
    ("shi2025Federated", "shi2025FederatedB", False),         # 只认小写
    ("shi2025Federated", "shi2025Federated", False),
    ("ammon2026Comparison", "patel2026Comparison", False),
])
def test_is_suffix_key(base, key, expect):
    assert ni._is_suffix_key(base, key) is expect


def test_find_stale_inline_citekeys_shapes():
    idx = {"papers": [
        {"citekey": "patel2026Comparison", "dedup_key": "doi:p", "month": "2026-01", "duplicate_of": None},
        {"citekey": "ammon2026Comparison", "dedup_key": "title:x", "month": "2026-08",
         "duplicate_of": "doi:p@2026-01"},
        {"citekey": "shi2025Federatedb", "dedup_key": "doi:s", "month": "2026-09", "duplicate_of": None},
        {"citekey": "shi2025Federated", "dedup_key": "doi:s", "month": "2025-10",
         "duplicate_of": "doi:s@2026-09"},
        {"citekey": "same2025", "dedup_key": "doi:q", "month": "2025-01", "duplicate_of": None},
        {"citekey": "same2025", "dedup_key": "doi:q", "month": "2025-02", "duplicate_of": "doi:q@2025-01"},
    ]}
    got = ni.find_stale_inline_citekeys(idx)
    shapes = {(g["entry"]["citekey"], g["keeper"]["citekey"]): g["shape"] for g in got}
    assert shapes == {("ammon2026Comparison", "patel2026Comparison"): "stale-dup",
                      ("shi2025Federated", "shi2025Federatedb"): "suffix-keeper"}


def test_global_pass_stale_key_is_error_with_tool_hint_but_never_notifies(monkeypatch):
    """告警要 error 级（warning 在自动权限 agent 会话里看不见），但**不能在这里 notify**：
    这条件描述的是「库里存在什么」（持久状态），`update_index` 被 4 个 launchd job 调用，
    库里只要有 1 条陈旧键就会每周每月弹同一条，把告警面训练成噪音（第 3 轮审计 CONFIRMED，
    生产库当前正有 1 条）。清单改挂在索引上，由有人在看的入口层报。"""
    calls = []
    monkeypatch.setattr("src.utils.notify.notify", lambda t, m: calls.append((t, m)))
    lines, hid = _sink()
    stale = []
    try:
        ni._global_pass([
            {"month": "2026-09", "priority_rank": 1, "citekey": "shi2025Federatedb", "series": "manual",
             "dedup_key": "doi:s", "note_file": "m.md", "note_line": 1},
            {"month": "2025-10", "priority_rank": 2, "citekey": "shi2025Federated",
             "dedup_key": "doi:s", "note_file": "a.md", "note_line": 9},
        ], stale_out=stale)
    finally:
        _lg.remove(hid)
    joined = "\n".join(lines)
    assert "| ERROR" in joined and "与其 keeper 不一致" in joined
    assert "形态 suffix-keeper" in joined and "--fix-inline-citekeys" in joined
    assert "references.json" in joined and "没有 sidecar" in joined
    assert calls == [], "持久状态不许每次重建都弹窗"
    assert len(stale) == 1 and "shi2025Federated" in stale[0]


def test_update_index_exposes_stale_inline_citekeys(tmp_path):
    """清单要真的挂到索引对象上，入口层才报得出来。"""
    paper = dict(title="Federated Learning with Missing Modalities", doi="10.1016/j.knosys.2025.114601",
                 authors=("Shi Wang",))
    N.write_notes([_seg("a1", **paper)], {"a1": "shi2025Federated"}, out_dir=tmp_path,
                  filename="科研札记_2025-10_全文精读", emit_index_sidecar=True)
    N.write_notes([_seg("m1", cr=_cr(5), **paper)], {"m1": "shi2025Federatedb"}, out_dir=tmp_path,
                  filename="科研札记_2026-09_手动精读", emit_index_sidecar=True,
                  index_series="manual", explicit_citekey_source="fallback")
    idx = ni.update_index(tmp_path)
    assert len(idx["stale_inline_citekeys"]) == 1
    assert "suffix-keeper" in idx["stale_inline_citekeys"][0]
    # 干净库里是空列表（键必须存在，入口层才敢 .get(...) or []）
    N.write_notes([_seg("s1", "Solo Paper", doi="10.9/solo")], {"s1": "solo2025"},
                  out_dir=tmp_path / "clean", filename="科研札记_2025-01_全文精读", emit_index_sidecar=True)
    assert ni.update_index(tmp_path / "clean")["stale_inline_citekeys"] == []


def _build_two_month_library(tmp_path, dup_key, keeper_key, keeper_manual=True):
    """auto 月（2025-10）写 dup_key；manual 月（2026-09）写 keeper_key；同 DOI → 同簇。"""
    paper = dict(title="Federated Learning with Missing Modalities", doi="10.1016/j.knosys.2025.114601",
                 authors=("Shi Wang", "Li Zhao"))
    N.write_notes([_seg("a1", cr=None, **paper)], {"a1": dup_key}, out_dir=tmp_path,
                  filename="科研札记_2025-10_全文精读", emit_index_sidecar=True)
    N.write_notes([_seg("m1", cr=_cr(5), **paper)], {"m1": keeper_key}, out_dir=tmp_path,
                  filename="科研札记_2026-09_手动精读", emit_index_sidecar=True,
                  index_series="manual" if keeper_manual else "auto",
                  explicit_citekey_source="fallback")
    return ni.update_index(tmp_path)


def test_fix_inline_citekeys_suffix_keeper_dry_run_then_apply(tmp_path):
    idx = _build_two_month_library(tmp_path, "shi2025Federated", "shi2025Federatedb")
    stale = ni.find_stale_inline_citekeys(idx)
    assert len(stale) == 1 and stale[0]["shape"] == "suffix-keeper"
    assert stale[0]["keeper"]["series"] == "manual"
    manual_md = tmp_path / "科研札记_2026-09_手动精读.md"
    before = manual_md.read_bytes()

    res = ni.fix_inline_citekeys(tmp_path, apply=False)
    assert len(res["planned"]) == 1 and res["applied"] == 0
    pl = res["planned"][0]
    assert (pl["shape"], pl["old"], pl["new"], pl["month"]) == \
        ("suffix-keeper", "shi2025Federatedb", "shi2025Federated", "2026-09")
    assert manual_md.read_bytes() == before                      # dry-run 零写盘

    res = ni.fix_inline_citekeys(tmp_path, apply=True)
    assert res["applied"] == 1 and not res["refused"] and not res["partial"]
    assert res["remaining"] == 0
    md = manual_md.read_text(encoding="utf-8")
    assert "[@shi2025Federated]" in md and "[@shi2025Federatedb]" not in md
    refs = json.loads((tmp_path / "科研札记_2026-09_手动精读.references.json").read_text(encoding="utf-8"))
    assert [r["id"] for r in refs] == ["shi2025Federated"]
    sc = json.loads((tmp_path / "科研札记_2026-09_手动精读.index.json").read_text(encoding="utf-8"))
    assert sc["papers"][0]["citekey"] == "shi2025Federated"
    # auto 月的 dup 一字未动，且重建后 keeper 是 manual、两条同键、撞键 0、全局书目含基键
    idx2 = json.loads((tmp_path / "literature_index.json").read_text(encoding="utf-8"))
    keepers = ni.keepers_by_citekey(idx2)
    assert keepers["shi2025Federated"]["series"] == "manual"
    assert idx2["citekey_collisions"] == []
    assert ni.find_stale_inline_citekeys(idx2) == []
    ar = json.loads((tmp_path / "all_references.json").read_text(encoding="utf-8"))
    ids = {i["id"] for i in (ar if isinstance(ar, list) else ar["references"])}
    assert "shi2025Federated" in ids and "shi2025Federatedb" not in ids


def test_fix_inline_citekeys_stale_dup_renames_duplicate_side(tmp_path):
    idx = _build_two_month_library(tmp_path, "ammon2026Comparison", "patel2026Comparison")
    stale = ni.find_stale_inline_citekeys(idx)
    assert len(stale) == 1 and stale[0]["shape"] == "stale-dup"
    res = ni.fix_inline_citekeys(tmp_path, apply=True)
    assert res["applied"] == 1 and res["planned"][0]["month"] == "2025-10"
    auto_md = (tmp_path / "科研札记_2025-10_全文精读.md").read_text(encoding="utf-8")
    assert "[@patel2026Comparison]" in auto_md and "[@ammon2026Comparison]" not in auto_md
    refs = json.loads((tmp_path / "科研札记_2025-10_全文精读.references.json").read_text(encoding="utf-8"))
    assert [r["id"] for r in refs] == ["patel2026Comparison"]
    manual_md = (tmp_path / "科研札记_2026-09_手动精读.md").read_text(encoding="utf-8")
    assert "[@patel2026Comparison]" in manual_md                  # keeper 侧不动
    assert res["remaining"] == 0


def test_fix_inline_citekeys_refuses_when_base_owned_by_another_live_paper(tmp_path):
    idx = _build_two_month_library(tmp_path, "shi2025Federated", "shi2025Federatedb")
    # 另一篇不同论文的 live 条目也叫 shi2025Federated → 改回去会撞键，必须跳过
    N.write_notes([_seg("o1", "Other Paper Entirely", doi="10.9/other", authors=("Shi Wang",))],
                  {"o1": "shi2025Federated"}, out_dir=tmp_path,
                  filename="科研札记_2024-01_全文精读", emit_index_sidecar=True)
    res = ni.fix_inline_citekeys(tmp_path, apply=True)
    assert res["planned"] == [] and res["applied"] == 0
    assert res["skipped"] and "被另一篇 live 条目占用" in res["skipped"][0]
    assert "[@shi2025Federatedb]" in (tmp_path / "科研札记_2026-09_手动精读.md").read_text(encoding="utf-8")
    del idx


def test_fix_inline_citekeys_nothing_to_do(tmp_path):
    N.write_notes([_seg("a1", "Solo", doi="10.1/s")], {"a1": "solo2025"}, out_dir=tmp_path,
                  filename="科研札记_2025-01_全文精读", emit_index_sidecar=True)
    res = ni.fix_inline_citekeys(tmp_path, apply=True)
    assert res == {"planned": [], "applied": 0, "refused": [], "partial": [], "skipped": []}


# =====================================================================================
# W10 摘要回读（docs/bugs/2026-09-04-abstract-fallback-dead.md）
# =====================================================================================

def _bd():
    import importlib.util
    from src.scholar.paths import REPO_ROOT
    spec = importlib.util.spec_from_file_location("bd_abs", REPO_ROOT / "scripts" / "backfill_deepread.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_abstract_from_note_md_reads_the_right_paper(tmp_path):
    bd = _bd()
    segs = [_seg("a1", "Paper A", doi="10.1/a", abstract="Abstract of paper A, two sentences."),
            _seg("a2", "Paper B", doi="10.1/b", abstract="Abstract of paper B."),
            _seg("a3", "Paper C", doi="10.1/c", abstract="")]
    N.write_notes(segs, {"a1": "aaa2025", "a2": "bbb2025", "a3": "ccc2025"}, out_dir=tmp_path,
                  filename="科研札记_2025-03_全文精读", emit_index_sidecar=True)
    idx = ni.update_index(tmp_path)
    by = ni.keepers_by_citekey(idx)
    assert bd.abstract_from_note_md(tmp_path, by["aaa2025"]) == "Abstract of paper A, two sentences."
    assert bd.abstract_from_note_md(tmp_path, by["bbb2025"]) == "Abstract of paper B."
    assert bd.abstract_from_note_md(tmp_path, by["ccc2025"]) == ""          # *摘要暂无* 不当摘要
    seg = bd.segment_from_entry(by["bbb2025"], 2, {}, notes_dir=tmp_path)
    assert seg.original_abstract == "Abstract of paper B."
    seg2 = bd.segment_from_entry(by["bbb2025"], 2, {"bbb2025": "from abstracts.json"}, notes_dir=tmp_path)
    assert seg2.original_abstract == "from abstracts.json"                  # abstracts.json 仍优先
    assert bd.segment_from_entry(by["bbb2025"], 2, {}).original_abstract == ""   # 不传 notes_dir = 旧行为
    bogus = dict(by["bbb2025"], note_line=999, citekey="nope2025")
    assert bd.abstract_from_note_md(tmp_path, bogus) == ""                 # 定位失败不抛


# =====================================================================================
# W11 渲染 ↔ 解析往返契约（docs/bugs/2026-09-04-closeread-heading-contract.md 的一般性教训）
# =====================================================================================

@pytest.mark.parametrize("tag", list(TAG_MARK) + [None])
@pytest.mark.parametrize("page", [None, "247", "12-15", "3, 7"])
def test_tag_line_render_parse_roundtrip(tag, page):
    text = "AUC 0.87 (95% CI 0.85–0.89) 〔看似锚点〕的句子。"
    line = render_tag_line(tag, text, page)
    m = TAG_LINE_RE.match(line)
    if tag is None:
        assert m is None                                  # 无 tag 行不是句级标记行
        return
    assert m, line
    assert m.group(1) == tag and m.group(2) == text
    assert m.group(3) == (page if page else None)


def test_book_line_render_parse_roundtrip():
    meta = SimpleNamespace(entry_type="chapter", title="Chapter Title", container_title="Users' Guides",
                           isbn="9780071790710", chapter_number=12, page_range="245-262",
                           publisher="McGraw-Hill", edition="3", editors=["Guyatt G", "Rennie D"],
                           book_key="guyatt2015Users")
    line = N._book_line(meta)
    assert line.startswith(ni._BOOK_LINE_PREFIX)
    parsed = ni._parse_book_line(line[len(ni._BOOK_LINE_PREFIX):].strip())
    assert parsed == {"container_title": "Users' Guides", "isbn": "9780071790710", "chapter_number": 12,
                      "page_range": "245-262", "publisher": "McGraw-Hill", "edition": "3",
                      "editors": ["Guyatt G", "Rennie D"], "book_key": "guyatt2015Users",
                      "entry_type": "chapter"}
    book = SimpleNamespace(entry_type="book", title="Statistical Analysis with Missing Data",
                           container_title=None, isbn="9780470526798", chapter_number=None,
                           page_range=None, publisher="Wiley", edition="3", editors=None, book_key=None)
    line = N._book_line(book)
    parsed = ni._parse_book_line(line[len(ni._BOOK_LINE_PREFIX):].strip())
    assert parsed == {"container_title": "Statistical Analysis with Missing Data", "isbn": "9780470526798",
                      "publisher": "Wiley", "edition": "3", "entry_type": "book"}
    assert N._book_line(SimpleNamespace(entry_type=None)) is None


# =====================================================================================
# W17 Zotero/BBT 非 ASCII citekey 告警（docs/bugs/2026-09-04-zotero-saveitems-target-ignored.md 次生发现）
# =====================================================================================

def test_resolve_citekey_warns_on_non_ascii_key(monkeypatch):
    from src.scholar.zotero_sync import ZoteroConnectorClient
    c = object.__new__(ZoteroConnectorClient)
    monkeypatch.setattr(c, "_bbt_search_pick", lambda term, nd, title: "lado‐baleato2025Testing",
                        raising=False)
    lines, hid = _sink()
    try:
        key = c.resolve_citekey(doi="10.1002/sim.10308", title="Testing Covariates", retries=1, delay=0)
    finally:
        _lg.remove(hid)
    assert key == "lado‐baleato2025Testing"                 # 只告警不改：键是 Zotero 侧权威
    assert any("非 ASCII" in ln and "Extra" in ln for ln in lines)
    lines2, hid2 = _sink()
    monkeypatch.setattr(c, "_bbt_search_pick", lambda term, nd, title: "ladobaleato2025Testing", raising=False)
    try:
        c.resolve_citekey(doi="10.1002/sim.10308", title="Testing Covariates", retries=1, delay=0)
    finally:
        _lg.remove(hid2)
    assert not any("非 ASCII" in ln for ln in lines2)


# =====================================================================================
# 第 1 轮对抗审计/压测的回归（A1/A5/S4/S5/S7/M23/S3/S13/C3/M32）
# =====================================================================================

_TITLE_XIANG = "Dual Masked Autoencoding for EHR"


@pytest.mark.parametrize("owner_dk,seg_kw", [
    ("arxiv:2602.15159", dict(arxiv_id="2602.15159v1")),                 # auto 剥版本 vs 手动保留 vN
    ("doi:10.48550/arxiv.2305.02504", dict(arxiv_id="2305.02504v1")),   # DataCite DOI 形态 vs arxiv 档
    ("arxiv:2305.02504v2", dict(arxiv_id="2305.02504")),                # 反向
])
def test_fallback_key_inherits_across_arxiv_forms(tmp_path, owner_dk, seg_kw):
    """第 1 轮审计 A1：同一篇 arXiv 论文三种 dedup_key 形态互不相等，按原值比较让继承整体失效——
    本批 dedup_overrides.json 新增的两组裁决正是这两种形态。**标题一致**是继承的前提（见下一条）。"""
    seg = _seg("m1", _TITLE_XIANG, authors=("Xiang Li",), **seg_kw)
    base = N._fallback_citekey(seg.metadata)
    owners = {base: (owner_dk, ni._norm_title(_TITLE_XIANG))}
    res = N.write_notes([seg], {"m1": None}, out_dir=tmp_path, filename="m", fallback_citekeys=True,
                        existing_citekeys={base}, existing_key_owners=owners)
    md = Path(res["note_path"]).read_text(encoding="utf-8")
    assert "[@{}]".format(base) in md and "[@{}b]".format(base) not in md


def test_fallback_key_refuses_family_inherit_when_titles_differ(tmp_path):
    """第 2 轮压测 CONFIRMED：只按 dedup_key **家族**相等就继承，会在「同一 arXiv 家族但索引
    判成两篇」时让两条 live 条目共用一个 citekey——`build_all_references` 把该键整键剔除，
    **两篇一起**从全局书目消失，比原来那个「基键消失」的缺陷更糟。索引真正的二级合并键是
    规范标题，所以标题不一致就不许继承。"""
    seg = _seg("m1", _TITLE_XIANG, authors=("Xiang Li",), arxiv_id="2602.15159v1")
    base = N._fallback_citekey(seg.metadata)
    # 家族相同（同一 arXiv id 的两种形态）但标题被解析成了另一个样子 → 索引不会合并
    owners = {base: ("arxiv:2602.15159", ni._norm_title("Healthcare Analytics"))}
    res = N.write_notes([seg], {"m1": None}, out_dir=tmp_path, filename="m", fallback_citekeys=True,
                        existing_citekeys={base}, existing_key_owners=owners)
    assert "[@{}b]".format(base) in Path(res["note_path"]).read_text(encoding="utf-8")
    # 身份键**逐字相等**时不看标题（最强证据，标题漂了也照继承）
    owners2 = {base: ("arxiv:2602.15159v1", ni._norm_title("Healthcare Analytics"))}
    res2 = N.write_notes([seg], {"m1": None}, out_dir=tmp_path, filename="m2", fallback_citekeys=True,
                        existing_citekeys={base}, existing_key_owners=owners2)
    assert "[@{}]".format(base) in Path(res2["note_path"]).read_text(encoding="utf-8")
    # 老形态（裸 dedup_key 字符串）仍被接受：逐字相等即继承
    res3 = N.write_notes([seg], {"m1": None}, out_dir=tmp_path, filename="m3", fallback_citekeys=True,
                        existing_citekeys={base}, existing_key_owners={base: "arxiv:2602.15159v1"})
    assert "[@{}]".format(base) in Path(res3["note_path"]).read_text(encoding="utf-8")


def test_fallback_key_does_not_inherit_when_neither_key_nor_title_matches(tmp_path):
    """既非同一 dedup_key、标题也不同 → 索引不会合并这两条，绝不能共用一个 citekey。"""
    seg = _seg("m1", _TITLE_XIANG, authors=("Xiang Li",), arxiv_id="2602.15159v1")
    base = N._fallback_citekey(seg.metadata)
    res = N.write_notes([seg], {"m1": None}, out_dir=tmp_path, filename="m", fallback_citekeys=True,
                        existing_citekeys={base},
                        existing_key_owners={base: ("arxiv:2602.99999", ni._norm_title("Another Paper"))})
    assert "[@{}b]".format(base) in Path(res["note_path"]).read_text(encoding="utf-8")


def test_fallback_key_inherits_when_only_the_title_matches(tmp_path):
    """标题相等**单独就够**：`notes_index._entry_keys` 无条件生成规范标题二级键，两条必被 union
    进同一簇。这一支还覆盖了家族判据漏掉的情形——auto 侧是 arXiv DOI、manual 侧被 Crossref
    补成正刊 DOI（一级键完全不同、标题相同，索引照样合并）。"""
    seg = _seg("m1", _TITLE_XIANG, authors=("Xiang Li",), doi="10.1016/j.artint.2026.104321")
    base = N._fallback_citekey(seg.metadata)
    owners = {base: ("doi:10.48550/arxiv.2602.15159", ni._norm_title(_TITLE_XIANG))}
    res = N.write_notes([seg], {"m1": None}, out_dir=tmp_path, filename="m", fallback_citekeys=True,
                        existing_citekeys={base}, existing_key_owners=owners)
    md = Path(res["note_path"]).read_text(encoding="utf-8")
    assert "[@{}]".format(base) in md and "[@{}b]".format(base) not in md


def test_fix_inline_dedupes_plans_when_two_dups_point_at_same_keeper(tmp_path):
    """审计 A5 / 压测 S4：同一 keeper 被两个 dup 指向 → 计划出两条相同改键，apply 第二条必 REFUSED、
    打假「未能修复」。按目标行去重。"""
    paper = dict(title="Federated Learning with Missing Modalities", doi="10.1016/j.knosys.2025.114601",
                 authors=("Shi Wang",))
    N.write_notes([_seg("a1", **paper)], {"a1": "shi2025Federated"}, out_dir=tmp_path,
                  filename="科研札记_2025-10_全文精读", emit_index_sidecar=True)
    N.write_notes([_seg("a2", **paper)], {"a2": "shi2025Federated"}, out_dir=tmp_path,
                  filename="科研札记_2025-11_全文精读", emit_index_sidecar=True)
    N.write_notes([_seg("m1", cr=_cr(5), **paper)], {"m1": "shi2025Federatedb"}, out_dir=tmp_path,
                  filename="科研札记_2026-09_手动精读", emit_index_sidecar=True, index_series="manual",
                  explicit_citekey_source="fallback")
    idx = ni.update_index(tmp_path)
    assert len(ni.find_stale_inline_citekeys(idx)) == 2
    res = ni.fix_inline_citekeys(tmp_path, apply=False)
    assert len(res["planned"]) == 1
    res = ni.fix_inline_citekeys(tmp_path, apply=True)
    assert res["applied"] == 1 and res["refused"] == [] and res["remaining"] == 0


def test_fix_inline_mixed_shapes_converge_in_one_round(tmp_path):
    """压测 S5：同簇既有 suffix-keeper（keeper=<基>b、dup1=<基>）又有 stale-dup（dup2=错键）。
    stale-dup 的目标须是 keeper 的**终键**（基键），否则第一轮把 dup2 改成 <基>b、第二轮才收敛。"""
    paper = dict(title="Federated Learning with Missing Modalities", doi="10.1016/j.knosys.2025.114601",
                 authors=("Shi Wang",))
    N.write_notes([_seg("a1", **paper)], {"a1": "shi2025Federated"}, out_dir=tmp_path,
                  filename="科研札记_2025-10_全文精读", emit_index_sidecar=True)
    N.write_notes([_seg("a2", **paper)], {"a2": "wrong2025Key"}, out_dir=tmp_path,
                  filename="科研札记_2025-11_全文精读", emit_index_sidecar=True)
    N.write_notes([_seg("m1", cr=_cr(5), **paper)], {"m1": "shi2025Federatedb"}, out_dir=tmp_path,
                  filename="科研札记_2026-09_手动精读", emit_index_sidecar=True, index_series="manual",
                  explicit_citekey_source="fallback")
    ni.update_index(tmp_path)
    res = ni.fix_inline_citekeys(tmp_path, apply=False)
    plans = {(p["shape"], p["old"], p["new"]) for p in res["planned"]}
    assert plans == {("suffix-keeper", "shi2025Federatedb", "shi2025Federated"),
                     ("stale-dup", "wrong2025Key", "shi2025Federated")}
    res = ni.fix_inline_citekeys(tmp_path, apply=True)
    assert res["applied"] == 2 and res["refused"] == [] and res["remaining"] == 0
    for stem in ("科研札记_2025-10_全文精读", "科研札记_2025-11_全文精读", "科研札记_2026-09_手动精读"):
        md = (tmp_path / (stem + ".md")).read_text(encoding="utf-8")
        assert "[@shi2025Federated]" in md and "shi2025Federatedb" not in md and "wrong2025Key" not in md


def test_overwrite_guard_triggers_on_any_of_the_four_files(tmp_path, monkeypatch):
    """压测 S7：md 缺而 sidecar 在（半态）同样会被整篇覆盖，而 sidecar 是量尺唯一无损源。"""
    calls = _stub_zotero_and_notes(monkeypatch)
    wf = _workflow(tmp_path, closeread=True)
    (tmp_path / "科研札记_2023-05_全文精读.index.json").write_text('{"papers": []}', encoding="utf-8")
    out = wf._step_sync_zotero()
    assert "skipped_existing" in out and calls["write"] == [] and calls["sync"] == 0


def test_cli_preflight_existing_note_helper(tmp_path):
    """变异 M23：开跑前预检此前零测试。"""
    from src.scholar.cli import preflight_existing_note
    wf = _workflow(tmp_path, closeread=True)
    wf.settings.processing.zotero_enabled = True
    rng = (date(2023, 5, 1), date(2023, 5, 31))
    assert preflight_existing_note(wf, rng) is None                          # 目标不存在
    (tmp_path / "科研札记_2023-05_全文精读.references.json").write_text("[]", encoding="utf-8")
    assert preflight_existing_note(wf, rng) == tmp_path / "科研札记_2023-05_全文精读.md"
    wf.allow_note_overwrite = True
    assert preflight_existing_note(wf, rng) is None                          # 授权覆盖
    wf.allow_note_overwrite = False
    assert preflight_existing_note(wf, None) is None                         # --days 不预检
    wf.settings.processing.zotero_enabled = False
    assert preflight_existing_note(wf, rng) is None                          # 不写札记不预检


def test_abstract_from_md_puts_chinese_translation_into_translated_abstract(tmp_path):
    """审计 A7 / 压测 S3：md 摘要节落的是 translated_abstract or original_abstract；中文译文不得冒充
    original_abstract（closereading 用它做数字回查对照）。"""
    bd = _bd()
    seg_zh = _seg("a1", "Paper ZH", doi="10.1/zh", abstract="We report AUC 0.87 in 1200 patients.")
    seg_zh.translated_abstract = "我们在 1200 名患者中报告 AUC 0.78（译错）。"
    seg_en = _seg("a2", "Paper EN", doi="10.1/en", abstract="English only abstract with 中 one glyph.")
    N.write_notes([seg_zh, seg_en], {"a1": "zh2025", "a2": "en2025"}, out_dir=tmp_path,
                  filename="科研札记_2025-03_全文精读", emit_index_sidecar=True)
    by = ni.keepers_by_citekey(ni.update_index(tmp_path))
    s1 = bd.segment_from_entry(by["zh2025"], 1, {}, notes_dir=tmp_path)
    assert s1.original_abstract == "" and "译错" in s1.translated_abstract
    s2 = bd.segment_from_entry(by["en2025"], 2, {}, notes_dir=tmp_path)
    assert s2.original_abstract.startswith("English only") and s2.translated_abstract == ""
    # abstracts.json 里的空白串不算有摘要（压测 S13）
    s3 = bd.segment_from_entry(by["en2025"], 2, {"en2025": "   "}, notes_dir=tmp_path)
    assert s3.original_abstract.startswith("English only")
    # 坏索引里 note_line 是字符串：不抛
    bogus = dict(by["en2025"], note_line="oops")
    assert bd.abstract_from_note_md(tmp_path, bogus) in ("", s2.original_abstract)


def test_backup_snapshot_excludes_digest_overwrite_backup():
    """契约 C3：周快照排除了 .backfill_deepread_backup 却漏了新增的 .digest_overwrite_backup。

    排除是**有意**的（理由写在 EXCLUDE_PATTERNS 上方）：它护的「覆盖历史月札记」场景，
    被上一份周快照本身覆盖；收进快照则 8 周档 + 24 月档各带一份永不收缩的历史备份。"""
    import importlib.util
    from src.scholar.paths import REPO_ROOT
    spec = importlib.util.spec_from_file_location("bs_t", REPO_ROOT / "scripts" / "backup_snapshot.py")
    bs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bs)
    assert N.NOTE_OVERWRITE_BACKUP_DIR in bs.EXCLUDE_PATTERNS
    assert N.NOTE_OVERWRITE_BACKUP_DIR + "/*" in bs.EXCLUDE_PATTERNS


def test_zotero_save_items_calls_update_session_with_same_session_and_target():
    """变异 M32：save_items 的 updateSession 归类此前零断言（台账 zotero-saveitems-target-ignored 的核心修复）。"""
    from src.scholar.zotero_sync import ZoteroConnectorClient

    class _Resp:
        def __init__(self, code, text=""):
            self.status_code, self.text = code, text

    class _Post:
        def __init__(self, codes):
            self.codes, self.calls = list(codes), []

        def post(self, url, json=None):
            self.calls.append((url, json))
            return _Resp(self.codes.pop(0))
    c = object.__new__(ZoteroConnectorClient)
    c.base_url = "http://z"
    c._client = _Post([201, 200])
    assert c.save_items([{"itemType": "journalArticle"}], target="C19") == "ok"
    urls = [u for u, _ in c._client.calls]
    assert urls == ["http://z/connector/saveItems", "http://z/connector/updateSession"]
    sid_save = c._client.calls[0][1]["sessionID"]
    assert c._client.calls[1][1] == {"sessionID": sid_save, "target": "C19", "tags": ""}
    assert "target" not in c._client.calls[0][1]                  # 别把 target 塞进 saveItems（静默忽略）
    # updateSession 失败 → **回报失败**：调用方靠 require_collection 防呆，报 saved=True
    # 等于把防呆静默放宽成没有（改动前「选中项不符」是一个条目都不写）。
    c._client = _Post([201, 500])
    lines, hid = _sink()
    try:
        assert c.save_items([{"itemType": "journalArticle"}], target="C19") == "unfiled"
    finally:
        _lg.remove(hid)
    assert any("updateSession 返回 500" in ln and "未挪入 target=C19" in ln for ln in lines)
    assert any("| ERROR" in ln for ln in lines)
    # 不给 target：只 POST 一次
    c._client = _Post([201])
    assert c.save_items([{"itemType": "journalArticle"}]) == "ok" and len(c._client.calls) == 1


# =====================================================================================
# 第 2 轮变异存活项的守卫补齐（R10 / R28b / R29 / R30 / R31 / R32 / R36）
# 这些都是第 1 轮加的接线，当时没配测试——变异把它们逐条删掉，46 个变异里 17 个因此存活。
# =====================================================================================

def test_book_chapter_inherits_base_key_via_isbn_dedup_key(tmp_path):
    """变异 R10：`_segment_dedup_key` 不传 isbn/chapter_number 时，章条目的键退化成标题键，
    与索引侧的 `isbn:<isbn>:chNN` 永远对不上 → 手动精读升级一章书时拿不到基键。"""
    seg = _seg("b1", "Missing Data in Clinical Trials", authors=("Roderick Little",))
    seg.metadata.entry_type = "chapter"
    seg.metadata.isbn = "978-0-470-52679-8"
    seg.metadata.chapter_number = 3
    seg.metadata.container_title = "Statistical Analysis with Missing Data"
    dk = N._segment_dedup_key(seg.metadata)
    assert dk == "isbn:9780470526798:ch03", dk
    base = N._fallback_citekey(seg.metadata)
    res = N.write_notes([seg], {"b1": None}, out_dir=tmp_path, filename="b", fallback_citekeys=True,
                        existing_citekeys={base}, existing_key_owners={base: (dk, ni._norm_title(seg.metadata.title))})
    md = Path(res["note_path"]).read_text(encoding="utf-8")
    assert "[@{}]".format(base) in md and "[@{}b]".format(base) not in md
    # 专著整本（无章号）走 isbn: 档
    seg.metadata.entry_type = "book"
    seg.metadata.chapter_number = None
    assert N._segment_dedup_key(seg.metadata) == "isbn:9780470526798"


def test_run_digest_actually_calls_the_preflight_and_aborts(tmp_path, monkeypatch):
    """变异 R28b：`run_digest` 不再调 `preflight_existing_note`（接线断）——helper 自己的测试
    照样绿，而「开跑前拦住、省掉一整轮 LLM 额度」这半价值归零。这里盯的是**接线**。"""
    import src.scholar.cli as C
    executed = []

    class _FakeWF:
        def __init__(self, settings):
            self.settings = settings
            self.date_range = None
            self.allow_note_overwrite = False

        def planned_note_stem(self):
            return "科研札记_2023-05_全文精读"

        def planned_note_path(self):
            return tmp_path / "科研札记_2023-05_全文精读.md"

        def execute(self):
            executed.append(True)
            return SimpleNamespace(total_emails=0, total_papers=0, segments=[], fields_distribution={})

    monkeypatch.setattr(C, "ScholarWorkflow", _FakeWF)
    settings = SimpleNamespace(
        processing=_proc(tmp_path, closeread=True), llm=SimpleNamespace(provider="deepseek", model="x", api_key=""),
        log_level="INFO")
    settings.processing.zotero_enabled = True
    settings.processing.days_to_fetch = 8
    settings.processing.max_emails = 10
    settings.processing.batch_size = 5
    settings.processing.filter_mode = "llm"
    settings.processing.blacklist = []
    settings.processing.whitelist = []
    settings.processing.external_sources_enabled = False
    settings.processing.translate_abstracts = False
    settings.processing.generate_summary = False
    settings.processing.auto_mark_read = False
    settings.processing.output_dir = tmp_path / "out"
    args = SimpleNamespace(all=False, days=None, max_emails=None, batch_size=None, provider=None,
                           model=None, no_translate=True, no_summary=True, no_mark_read=True,
                           output_dir=None, debug=False, filter_mode=None, external=False,
                           no_external=True, zotero=True, no_zotero=False, close_read=True,
                           dry_run=False, month="2023-05", since=None, until=None,
                           overwrite_notes=False, export_csv=False)
    (tmp_path / "科研札记_2023-05_全文精读.md").write_text("历史札记", encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        C.run_digest(args, settings)
    assert ei.value.code == 1
    assert executed == [], "预检必须在 execute() 之前拦住，否则白烧一轮 LLM"
    # 加了逃生门就该放行到 execute
    args.overwrite_notes = True
    C.run_digest(args, settings)
    assert executed == [True]


def _fake_rebuild(monkeypatch, out):
    import importlib.util
    from src.scholar.paths import REPO_ROOT
    spec = importlib.util.spec_from_file_location("read_pdf_rc", REPO_ROOT / "scripts" / "read_pdf.py")
    M = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(M)
    monkeypatch.setattr(M, "_load_settings", lambda cfg: SimpleNamespace(
        processing=SimpleNamespace(notes_dir=Path("/tmp/nope"))))
    monkeypatch.setattr(M, "_rebuild_month", lambda *a, **k: dict(out))
    monkeypatch.setattr(M, "_report_final", lambda r, nd: None)
    return M


def test_finalize_and_regen_exit_nonzero_when_sidecar_failed(tmp_path, monkeypatch):
    """变异 R29a/R29b：finalize/regen 的退出码不看 `sidecar_ok`。
    手动精读的 sidecar 是 `_reuse_citekeys` 的锚——写失败却 exit 0，agent 会当成功继续往下走，
    下一轮 regen 把整月 citekey 全部重算顶回（已在札记侧修正过的键就此丢失）。"""
    ok = {"month": "2026-09", "papers": 3, "broken_bundles": [], "skipped_drafts": [],
          "md": "/x.md", "index": {"papers": [], "citekey_collisions": []}, "sidecar_ok": True}
    bad = dict(ok, sidecar_ok=False)
    import src.scholar.pdf_ingest as pi
    bucket = tmp_path / "manual" / "2026-09"      # finalize 要求 month 字段与所在目录一致
    bucket.mkdir(parents=True)
    bundle = bucket / "b.paper.json"
    bundle.write_text("{}", encoding="utf-8")
    good_bundle = {"month": "2026-09", "status": "final", "close_reading_final": {"sections": [1]},
                   "cross_check_report": {"verified_count": 7}}
    for out, expect in ((ok, 0), (bad, 1)):
        M = _fake_rebuild(monkeypatch, out)
        monkeypatch.setattr(pi, "load_bundle", lambda p: dict(good_bundle))
        assert M.cmd_finalize(SimpleNamespace(config="u", bundle=str(bundle), allow_removals=False)) == expect
        assert M.cmd_regen(SimpleNamespace(config="u", month="2026-09", allow_removals=False)) == expect


def test_report_final_flags_sidecar_write_failure(capsys):
    """变异 R30：回执不打那行 ⛔ 的话，sidecar 写失败在人眼前完全不可见（只有退出码变了）。"""
    import importlib.util
    from src.scholar.paths import REPO_ROOT
    spec = importlib.util.spec_from_file_location("read_pdf_rf", REPO_ROOT / "scripts" / "read_pdf.py")
    M = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(M)
    r = {"month": "2026-09", "papers": 3, "skipped_drafts": [], "broken_bundles": [],
         "md": "/x/科研札记_2026-09_手动精读.md",
         "index": {"papers": [], "citekey_collisions": []}, "sidecar_ok": False}
    M._report_final(r, None)
    out = capsys.readouterr().out
    assert "sidecar" in out and "写失败" in out and "regen" in out
    # 首行不能是 ✅：退出码已是 1，回执首行还说成功会让人和 agent 直接往下走（第 2 轮审计）
    head = [ln for ln in out.splitlines() if "手动精读归档" in ln][0]
    assert head.startswith("⚠️") and "sidecar 写失败" in head, head
    r["sidecar_ok"] = True
    M._report_final(r, None)
    out2 = capsys.readouterr().out
    assert "写失败" not in out2
    assert [ln for ln in out2.splitlines() if "手动精读归档" in ln][0].startswith("✅")


def test_backfill_reports_sidecar_failure_loudly(monkeypatch):
    """变异 R31：sidecar 写失败不 notify —— 过夜回填里只剩一行 error 日志，无人看见（正是 43 个月量尺
    丢失的原样复现）。变异 R31b：run_month 回执不透传 sidecar_ok，上面那条也就永远不触发。"""
    import importlib.util
    from src.scholar.paths import REPO_ROOT
    spec = importlib.util.spec_from_file_location("bn_sidecar", REPO_ROOT / "scripts" / "backfill_notes.py")
    bn = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bn)
    calls = []
    monkeypatch.setattr(bn, "notify", lambda t, m: calls.append((t, m)))
    assert bn.report_sidecar_failure("2026-06", {"status": "ok", "sidecar_ok": False}) is True
    assert calls and "2026-06" in calls[0][1] and "sidecar" in calls[0][1]
    assert bn.report_sidecar_failure("2026-06", {"status": "ok", "sidecar_ok": True}) is False
    assert bn.report_sidecar_failure("2026-06", {"status": "ok"}) is False          # 老回执无该键
    assert bn.report_sidecar_failure("2026-06", {"status": "error", "sidecar_ok": False}) is False
    assert len(calls) == 1


def test_fetch_missing_pdfs_index_lookup_is_keeper_view(tmp_path):
    """变异 R36：补抓脚本用天真字典 → 取到 duplicate，拿它的 doi/arxiv 去问 API 就是问错论文。"""
    import importlib.util
    from src.scholar.paths import REPO_ROOT
    from src.scholar.embed_store import INDEX_NAME
    spec = importlib.util.spec_from_file_location("fmp_t", REPO_ROOT / "scripts" / "fetch_missing_pdfs.py")
    fm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fm)
    (tmp_path / INDEX_NAME).write_text(json.dumps({"papers": [
        {"citekey": "a2025X", "title": "T", "duplicate_of": None, "doi": "10.1/keeper", "month": "2025-01"},
        {"citekey": "a2025X", "title": "T", "duplicate_of": "doi:1@2025-01", "doi": "10.1/dup", "month": "2025-06"},
    ]}), encoding="utf-8")
    by = fm.load_keeper_index(tmp_path)
    assert by["a2025X"]["doi"] == "10.1/keeper"


def test_fetch_missing_pdfs_delegates_routes_to_fulltext(monkeypatch):
    """第 2 轮审计（MAJOR，两名审计员独立报告）：补抓脚本自持了一份 arXiv 标题检索副本，
    第 1 轮在 `fulltext` 修的「单向词面重合 → 短标题拉回别篇论文」没跟着改。
    修法是让它**委托**，这里钉住委托本身——凡两处同义的判据，只留一份。"""
    import importlib.util
    from src.scholar.paths import REPO_ROOT
    from src.scholar import fulltext as F
    spec = importlib.util.spec_from_file_location("fmp_deleg", REPO_ROOT / "scripts" / "fetch_missing_pdfs.py")
    fm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fm)

    seen = []
    monkeypatch.setattr(fm, "route_arxiv_title", lambda meta, c: seen.append(("arxiv", meta.title)) or [])
    monkeypatch.setattr(fm, "route_epmc_render", lambda meta, c: seen.append(("epmc", meta.doi)) or [])
    monkeypatch.setattr(fm, "_route_s2", lambda meta, c: seen.append(("s2", meta.doi)) or [])
    monkeypatch.setattr(fm, "_route_openalex", lambda meta, c, email="": seen.append(("oa", email)) or [])
    entry = {"citekey": "k2025X", "title": "Deep Learning for Irregular Clinical Series",
             "doi": "10.1/x", "arxiv_id": ""}
    fm.route_arxiv(entry, None)
    fm.route_epmc(entry, None)
    fm.route_s2(entry, None)
    fm.route_openalex(entry, None)
    assert [s[0] for s in seen] == ["arxiv", "epmc", "s2", "oa"]
    # 有 arxiv_id 时走直连、不做标题检索
    seen.clear()
    assert fm.route_arxiv(dict(entry, arxiv_id="2401.00001"), None) == \
        [("arxiv-id", "https://arxiv.org/pdf/2401.00001.pdf")]
    assert seen == []
    # 三闸与换宿主也是同一份实现
    assert fm.validate_pdf(b"<html>403</html>")[0] is False
    assert fm.validate_pdf(b"x" * 10)[1] == F.validate_pdf_bytes(b"x" * 10)[1]
    assert fm.rewrite_url("https://pmc.ncbi.nlm.nih.gov/articles/PMC1/pdf") == \
        F.rewrite_pmc_url("https://pmc.ncbi.nlm.nih.gov/articles/PMC1/pdf")
    # 本脚本不得再自持体积/页数常量（副本一旦存在就会漂）
    assert not hasattr(fm, "MIN_BYTES") and not hasattr(fm, "MIN_PAGES")


def test_fix_inline_refuses_when_two_keepers_want_the_same_base_key(tmp_path):
    """第 2 轮压测 CONFIRMED：`live_owner` 只反映**改之前**的索引。两个不同簇的 keeper 各自被
    计划改回同一个基键（两个 dup 恰好都持有它——dup 不是 live，不受唯一性约束），逐条看都合法，
    一起 apply 就当场造出 live 撞键，`build_all_references` 把该键整键剔除，两篇一起从书目消失。"""
    a = dict(title="Paper Alpha About Sepsis", doi="10.1/alpha", authors=("Shi Wang",))
    b = dict(title="Paper Beta About Sepsis", doi="10.1/beta", authors=("Shi Wang",))
    # 两个 auto 月各持同一个键 `shared2025Key`（分属两篇不同论文，都是 duplicate 侧）
    N.write_notes([_seg("a1", **a)], {"a1": "shared2025Key"}, out_dir=tmp_path,
                  filename="科研札记_2025-10_全文精读", emit_index_sidecar=True)
    N.write_notes([_seg("b1", **b)], {"b1": "shared2025Key"}, out_dir=tmp_path,
                  filename="科研札记_2025-11_全文精读", emit_index_sidecar=True)
    # 两个 manual 月分别是它们的 keeper，各拿一个后缀键
    N.write_notes([_seg("a2", cr=_cr(5), **a)], {"a2": "shared2025Keyb"}, out_dir=tmp_path,
                  filename="科研札记_2026-09_手动精读", emit_index_sidecar=True,
                  index_series="manual", explicit_citekey_source="fallback")
    N.write_notes([_seg("b2", cr=_cr(5), **b)], {"b2": "shared2025Keyc"}, out_dir=tmp_path,
                  filename="科研札记_2026-10_手动精读", emit_index_sidecar=True,
                  index_series="manual", explicit_citekey_source="fallback")
    idx = ni.update_index(tmp_path)
    shapes = {i["shape"] for i in ni.find_stale_inline_citekeys(idx)}
    assert shapes == {"suffix-keeper"}, shapes

    res = ni.fix_inline_citekeys(tmp_path, apply=True)
    assert res["planned"] == [] and res["applied"] == 0
    assert any("都想改回同一个基键" in s and "shared2025Key" in s for s in res["skipped"]), res["skipped"]
    # 两份 manual md 一字未动，索引里没有新造出来的撞键
    for stem in ("科研札记_2026-09_手动精读", "科研札记_2026-10_手动精读"):
        assert "shared2025Key]" not in (tmp_path / (stem + ".md")).read_text(encoding="utf-8")
    assert ni.update_index(tmp_path)["citekey_collisions"] == []   # 没被工具新造出撞键


def test_owners_keeper_wins_over_duplicate_so_the_key_stops_thrashing(tmp_path):
    """第 3 轮审计 CONFIRMED：继承成功之后，同一个 citekey 会同时挂在 keeper 与它的 duplicate 上，
    而两者的 dedup_key 可以是同一篇论文的不同形态（arXiv 剥不剥 vN）。按「同键不同 dedup_key
    即撞键」判，下一轮就不敢继承了，keeper 又退回 `<基键>b`——键在两轮之间来回抖。
    正确口径：keeper 优先，只有**两个 keeper** 共键才算撞键。"""
    idx = tmp_path / "literature_index.json"
    idx.write_text(json.dumps({"papers": [
        # 继承之后的常态：dup（auto，剥了版本）与 keeper（manual，带 vN）共用一个键
        {"citekey": "xiang2026Dual", "dedup_key": "arxiv:2602.15159", "title": "Dual Masked",
         "duplicate_of": "arxiv:2602.15159v1@2026-09", "note_file": "a.md"},
        {"citekey": "xiang2026Dual", "dedup_key": "arxiv:2602.15159v1", "title": "Dual Masked",
         "duplicate_of": None, "note_file": "m.md"},
        # 真撞键：两个 keeper 共键
        {"citekey": "clash2025", "dedup_key": "doi:1", "title": "A", "duplicate_of": None, "note_file": "a.md"},
        {"citekey": "clash2025", "dedup_key": "doi:2", "title": "B", "duplicate_of": None, "note_file": "b.md"},
    ]}), encoding="utf-8")
    owners = ni.existing_citekey_owners(idx)
    assert owners["xiang2026Dual"][0] == "arxiv:2602.15159v1", "keeper 的 dedup_key 应当胜出"
    assert owners["clash2025"] == ("", "")


def test_read_pdf_receipt_surfaces_stale_inline_citekeys(capsys):
    """持久状态不 notify，改由**有 agent 在看回执**的 finalize 报出来。"""
    import importlib.util
    from src.scholar.paths import REPO_ROOT
    spec = importlib.util.spec_from_file_location("read_pdf_stale", REPO_ROOT / "scripts" / "read_pdf.py")
    M = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(M)
    r = {"month": "2026-09", "papers": 3, "skipped_drafts": [], "broken_bundles": [],
         "md": "/x.md", "index": {"papers": [], "citekey_collisions": [],
                                  "stale_inline_citekeys": ["2025-10 的 shi2025Federated（应为 keeper 的 shi2025Federatedb，形态 suffix-keeper，见 a.md:9）"]}}
    M._report_final(r, None)
    out = capsys.readouterr().out
    assert "行内 citekey 与其 keeper 不一致" in out and "shi2025Federated" in out
    assert "--fix-inline-citekeys" in out
    r["index"]["stale_inline_citekeys"] = []
    M._report_final(r, None)
    assert "行内 citekey" not in capsys.readouterr().out


@pytest.mark.parametrize("sidecar_ok,topics_ok,expect_rc,expect_notify", [
    (True, True, 0, False),
    (True, False, 3, False),      # 只有派生物没跟上
    (False, True, 1, True),       # 量尺永久丢失：比派生物没跟上严重
    (False, False, 1, True),      # 两者并存报更严重的
    (None, True, 0, False),       # 老形态回执无该键
])
def test_ingest_notes_finish_report_exit_codes_and_alert(sidecar_ok, topics_ok, expect_rc,
                                                         expect_notify, monkeypatch, capsys):
    """第 3 轮审计 CONFIRMED：周一 09:30 那个 launchd job 是**唯一全自动写库**的，
    没有人看终端。sidecar 写失败（量尺永久不可恢复）此前既不 notify 也不改退出码。"""
    import importlib.util
    from src.scholar.paths import REPO_ROOT
    spec = importlib.util.spec_from_file_location("ing_notes_t", REPO_ROOT / "scripts" / "ingest_notes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    calls = []
    monkeypatch.setattr(mod, "notify", lambda t, m: calls.append((t, m)))
    rep = {"count": 5, "md": "/x.md", "full_text": 3, "citekey": 4}
    if sidecar_ok is not None:
        rep["sidecar_ok"] = sidecar_ok
    assert mod.finish_report(rep, topics_ok) == expect_rc
    out = capsys.readouterr().out
    assert bool(calls) is expect_notify
    assert ("永久丢失" in out) is (sidecar_ok is False)


def test_digest_path_reports_sidecar_write_failure(tmp_path, monkeypatch):
    """第 3 轮审计：月度 digest 这条产出链路此前完全不消费 `sidecar_ok`。"""
    calls = _stub_zotero_and_notes(monkeypatch)
    import src.scholar.notes as _N
    monkeypatch.setattr(_N, "write_notes", lambda *a, **k: {
        "note_path": str(tmp_path / "n.md"), "docx_path": None,
        "sidecar_ok": False, "sidecar_error": "ValueError: boom"})
    wf = _workflow(tmp_path, closeread=False)
    lines, hid = _sink()
    try:
        wf._step_sync_zotero()
    finally:
        _lg.remove(hid)
    assert any(t == "Scholar digest" and "sidecar" in m for t, m in calls["notify"]), calls["notify"]
    assert any("| ERROR" in ln and "永久丢失" in ln for ln in lines)


def test_inline_plan_flags_a_suffix_that_is_not_a_disambiguation_letter(capsys):
    """变异 R70 存活暴露的缺口：后缀提示白名单没有任何守卫。

    `_suffix_seq()` 依次发 b, c, d…，所以真正的消歧后缀是这几个字母；英文单复数之差
    （zhang2024Model / zhang2024Models）词面上也像后缀，但那是两个键本来就差一个词尾，
    改回去会把两篇论文并成一个键。这类必须提示人过目。"""
    import importlib
    cli = importlib.import_module("scripts.notes_index")

    def _plan(old, new):
        return {"planned": [{"shape": "suffix-keeper", "old": old, "new": new,
                             "month": "2026-09", "note_file": "n.md"}], "skipped": []}

    cli._print_inline_plan(_plan("zhang2024Models", "zhang2024Model"), applied=False)
    assert "不像消歧序列" in capsys.readouterr().out

    cli._print_inline_plan(_plan("zhang2024Modelb", "zhang2024Model"), applied=False)
    assert "不像消歧序列" not in capsys.readouterr().out


def test_stale_dup_shape_can_never_want_the_dup_key_it_already_has():
    """记录变异 R13 被判为**等价变异**的依据（`fix_inline_citekeys` 里那句 continue 不可达）。

    那句 continue 只在「shape 是 stale-dup，且 keeper 改名后的新键恰等于该 duplicate 现有的键」
    时才会触发。但两者互斥：若 duplicate 的键就是 keeper 后缀键的基键，
    `_stale_inline_shape` 必然把它判成 suffix-keeper、走另一分支，根本进不了那个 else。
    这条测试把这个互斥性钉住——将来谁改 `_is_suffix_key` 的判据，
    那句 continue 会从「不可达的防御」变成「真的在挡事」，届时这里会先红。
    """
    for base, suffix in [("wang2025Fed", "b"), ("li2023Patient", "c"), ("zhou2024Graph", "dd")]:
        keeper_key = base + suffix
        assert ni._stale_inline_shape(base, keeper_key) == "suffix-keeper", (base, keeper_key)
    # 反向：真正的 stale-dup 形态，其键与 keeper 的基键不可能相同
    assert ni._stale_inline_shape("wang2025Old", "wang2025Fedb") == "stale-dup"


def test_verify_deepread_batch_uses_the_keeper_view():
    """第 5 轮终审 R5-2.8：验收入口也必须经 keepers_by_citekey。

    自持一份 `{e["citekey"]: e for e in papers if not e.get("duplicate_of")}` 会漏掉
    MISSING-KEY 占位键——台账那条定的规则就是「写统计/验收代码一律经 keepers_by_citekey」。
    """
    import inspect
    import scripts.verify_deepread_batch as vb
    src = inspect.getsource(vb)
    assert "keepers_by_citekey(idx)" in src
    assert 'for e in idx["papers"]' not in src, "别再自持一份过滤"
