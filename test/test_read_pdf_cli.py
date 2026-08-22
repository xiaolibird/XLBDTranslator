# -*- coding: utf-8 -*-
"""read_pdf CLI 的两处「防人祸」输出，以及相对路径的仓库根锚定。

这两件事都不是逻辑正确性问题，是**信息可见性**问题——今天各栽了一次：
  · 只读到 31 页 PDF 的第 12 页就断言草稿引用的附录表格是编造的（其实每个数都对）；
  · 21 篇一批 ingest，「索引里已有同文」的提示夹在第 3 篇的输出中间，被后面 18 篇刷没了，
    结果重读了一篇 2026-06 已精读过的论文。
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _load():
    spec = importlib.util.spec_from_file_location("read_pdf_cli", REPO / "scripts" / "read_pdf.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load()


# ---------------- 亲读窗口提示 ----------------

def test_read_plan_states_total_and_last_window():
    s = M._read_plan(31)
    assert "31 页" in s and "1-20" in s and "21-31" in s


def test_read_plan_unknown_pages_says_so_loudly():
    """页数读不出时不能装作没事——必须明说未知，否则等于默认「读了开头就够」。"""
    s = M._read_plan(None)
    assert "未知" in s and "1-20" not in s


# ---------------- 末尾汇总块 ----------------

def _row(**kw):
    base = {"title": "Some Paper Title", "duplicate": None, "meta_source": "crossref-doi",
            "authors_n": 3, "skipped": None}
    base.update(kw)
    return base


def test_attention_block_surfaces_duplicate_at_the_end(capsys):
    outs = [_row(title="P{}".format(i)) for i in range(20)]
    outs.insert(3, _row(title="Already Read Paper", duplicate={
        "note_file": "科研札记_2026-06_手动精读.md", "month": "2026-06", "citekey": "old2026Key"}))
    M._print_attention(outs, [])
    out = capsys.readouterr().out
    assert "需要注意（1 项）" in out
    assert "Already Read Paper" in out and "old2026Key" in out
    # 汇总块必须是最后一段，不能又被后续输出盖掉
    assert out.strip().splitlines()[-1].lstrip().startswith("→")


def test_attention_block_flags_thin_metadata():
    """pdf-only / 零作者 → citekey 退化成 anon*，bibliography 缺卷期页。库里已积了 95 个 anon*。"""
    outs = [_row(title="No Authors", meta_source="pdf-only", authors_n=0)]
    M._print_attention(outs, [])


@pytest.mark.parametrize("row,expect", [
    (_row(meta_source="pdf-only", authors_n=0), True),
    (_row(meta_source="pdf-llm", authors_n=0), True),      # 有来源但没作者，一样是 anon*
    (_row(meta_source="crossref-doi", authors_n=5), False),
    (_row(meta_source="pdf-only", authors_n=0, skipped="final"), False),  # 跳过的不重复提醒
])
def test_thin_metadata_predicate(row, expect, capsys):
    M._print_attention([row], [])
    assert ("元数据不全" in capsys.readouterr().out) is expect


def test_attention_block_silent_when_nothing_to_report(capsys):
    """没事就别印——每批都刷一个空的「需要注意」框，几次之后就没人看了。"""
    M._print_attention([_row(), _row()], [])
    assert capsys.readouterr().out == ""


def test_attention_block_lists_failures(capsys):
    M._print_attention([], [("broken.pdf", "PDF 抽不出文本（可能是扫描件/加密）")])
    out = capsys.readouterr().out
    assert "broken.pdf" in out and "需要注意（1 项）" in out


# ---------------- 仓库根锚定 ----------------

def test_anchor_pins_relative_path_to_repo_root(tmp_path, monkeypatch):
    """回归：cwd 漂了也不能改变 `output/scholar_notes` 指向何处。

    实际故障是 `output/scholar_notes` 解析成了 `output/scholar_notes/output/scholar_notes`——
    札记写进空目录、索引读回空，不报错、只丢数据。
    """
    from src.scholar.paths import repo_path, REPO_ROOT
    monkeypatch.chdir(tmp_path)
    assert repo_path("output/scholar_notes") == REPO_ROOT / "output" / "scholar_notes"


def test_anchor_leaves_absolute_paths_alone(tmp_path):
    from src.scholar.paths import repo_path
    assert repo_path(tmp_path) == tmp_path
    assert repo_path(str(tmp_path / "x")) == tmp_path / "x"


def test_anchor_expands_home():
    from src.scholar.paths import repo_path
    got = repo_path("~/Documents/ScholarVault")
    assert got.is_absolute() and "~" not in str(got)


def test_repo_root_is_the_repo():
    from src.scholar.paths import REPO_ROOT
    assert (REPO_ROOT / "src" / "scholar" / "pdf_ingest.py").exists()
    assert (REPO_ROOT / "scripts" / "read_pdf.py").exists()


# ---------------- _rebuild_month citekey 抖动回归 ----------------

def _manual_seg(paper_id, title, author, doi, year=2026):
    from datetime import date as _date
    from src.scholar.schema import (
        PaperSegment, PaperMetadata, FilterDecision, CloseReading,
        CloseReadSection, CloseReadSentence,
    )
    fd = FilterDecision(paper_id=paper_id, title=title, verdict="included",
                        decision="INCLUDE", stage="llm_judge", one_line="手动深读文献")
    cr = CloseReading(from_full_text=True, source="manual-pdf", sections=[
        CloseReadSection(heading="研究问题", sentences=[
            CloseReadSentence(text="研究缺失机制。", tag=None)])])
    return PaperSegment(
        segment_id=1, paper_id=paper_id, priority_score=1.0,
        metadata=PaperMetadata(paper_id=paper_id, title=title, authors=[author], doi=doi,
                               publication_date=_date(year, 8, 1)),
        filter_decision=fd, close_reading=cr)


def _write_final_bundle(notes_dir, month, seg, pdf_path,
                        cross_check_report="default"):
    """写一份 status=final 的 bundle。默认带合规核验报告（重建纳入的硬前提）；
    传 cross_check_report=None 模拟「agent 自报 final 但没核验」的搭车 bundle。"""
    from src.scholar import pdf_ingest as pi
    if cross_check_report == "default":
        cross_check_report = {"verified_count": 3, "corrected": [], "added": []}
    bf = pi.bundle_path(notes_dir, month, seg.paper_id)
    pi.write_bundle(bf, status="final", month=month, pdf_path=pdf_path,
                    metadata_source="crossref-doi", segment=seg,
                    close_reading_script=seg.close_reading,
                    close_reading_final=seg.close_reading.model_dump(mode="json"),
                    cross_check_report=cross_check_report)
    return bf


class _FakeProc:
    notes_instruction = ""
    notes_emit_docx = False
    notes_docx_cjk_font = ""


class _FakeSettings:
    processing = _FakeProc()


def _citekey_of(notes_dir, paper_title):
    idx = json.loads((notes_dir / "literature_index.json").read_text(encoding="utf-8"))
    for p in idx["papers"]:
        if p.get("title") == paper_title:
            return p.get("citekey")
    return None


def test_finalize_requires_cross_check_report(tmp_path, monkeypatch):
    """双轨核验门禁：status=final + close_reading_final 都是 agent 自报，不读 PDF 抄草稿
    也能满足。finalize 必须另验 cross_check_report.verified_count>=1，缺失即拒绝归档；
    写回有效报告后放行。"""
    from types import SimpleNamespace
    from src.scholar import pdf_ingest as pi
    seg = _manual_seg("pfinal01", "Gate Paper", "Ann Lee", "10.9/gate")
    bf = tmp_path / "manual" / "2026-08" / "pfinal01.bundle.json"
    bf.parent.mkdir(parents=True)
    pi.write_bundle(bf, status="final", month="2026-08", pdf_path="x.pdf",
                    metadata_source="crossref-doi", segment=seg,
                    close_reading_script=seg.close_reading,
                    close_reading_final=seg.close_reading.model_dump(mode="json"))

    class _Proc:
        notes_dir = tmp_path
    class _Settings:
        processing = _Proc()
    monkeypatch.setattr(M, "_load_settings", lambda cfg: _Settings())
    rebuilt = []
    monkeypatch.setattr(M, "_rebuild_month", lambda *a, **k: rebuilt.append(1) or {"month": "2026-08"})
    monkeypatch.setattr(M, "_report_final", lambda *a, **k: None)
    args = SimpleNamespace(config="unused", bundle=str(bf))

    assert M.cmd_finalize(args) == 1        # 无核验报告 → 拒绝
    assert not rebuilt                       # 且不能已经动了库
    data = json.loads(bf.read_text(encoding="utf-8"))
    data["cross_check_report"] = {"verified_count": 0, "corrected": [], "added": []}
    bf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert M.cmd_finalize(args) == 1        # verified_count=0 同样拒绝（空转核验）
    data["cross_check_report"]["verified_count"] = 3
    bf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert M.cmd_finalize(args) == 0 and rebuilt  # 有效核验 → 放行


# ---------------- --month 入口校验 ----------------

def test_month_arg_accepts_index_visible_shapes():
    """纯月/专题批次都是存量在用的合法形状（manual/2026-07-28-TFM 等），校验不能收紧误伤。"""
    for v in ["2026-07", "2026-07-17", "2026-07-28-TFM"]:
        assert M._month_arg(v) == v


def test_month_arg_rejects_index_invisible_month():
    """回归：--month 2026-7 此前 md/sidecar 照常落盘、退出 0，但 NOTE_MD_RE 认不出文件名，
    该篇对索引/seen/向量库全部不可见、下月被当新论文重读 → 必须在 argparse 层直接拒收。"""
    import argparse
    for bad in ["2026-7", "2026-13", "202607", "test"]:
        with pytest.raises(argparse.ArgumentTypeError):
            M._month_arg(bad)


def test_cli_malformed_month_exits_2_before_ingest(monkeypatch, capsys):
    """全链路：畸形 --month 在参数解析期就退 2，根本走不到 ingest（更不会落盘）。"""
    monkeypatch.setattr(sys, "argv",
                        ["read_pdf.py", "ingest", "x.pdf", "--month", "2026-7"])
    with pytest.raises(SystemExit) as ei:
        M.main()
    assert ei.value.code == 2


def test_finalize_rejects_legacy_bundle_with_bad_month(tmp_path, monkeypatch):
    """修复前用畸形 --month ingest 出的存量 bundle：finalize 是落盘前最后一道闸，
    必须拒绝并给出改法，而不是写出一份索引认不出的札记。"""
    from types import SimpleNamespace
    from src.scholar import pdf_ingest as pi
    seg = _manual_seg("pbadm01", "Bad Month Paper", "Ann Lee", "10.9/badm")
    bf = tmp_path / "manual" / "2026-7" / "pbadm01.bundle.json"
    bf.parent.mkdir(parents=True)
    pi.write_bundle(bf, status="final", month="2026-7", pdf_path="x.pdf",
                    metadata_source="crossref-doi", segment=seg,
                    close_reading_script=seg.close_reading,
                    close_reading_final=seg.close_reading.model_dump(mode="json"),
                    cross_check_report={"verified_count": 3, "corrected": [], "added": []})

    class _Proc:
        notes_dir = tmp_path
    class _Settings:
        processing = _Proc()
    monkeypatch.setattr(M, "_load_settings", lambda cfg: _Settings())
    rebuilt = []
    monkeypatch.setattr(M, "_rebuild_month",
                        lambda *a, **k: rebuilt.append(1) or {"month": "2026-7"})
    monkeypatch.setattr(M, "_report_final", lambda *a, **k: None)
    args = SimpleNamespace(config="unused", bundle=str(bf))
    assert M.cmd_finalize(args) == 1        # 非法月份 → 拒绝归档
    assert not rebuilt                       # 且不能已经动了库


def test_rebuild_month_keeps_existing_citekey_stable_across_reruns(tmp_path):
    """回归 read_pdf.py:266 — 同月第二次 finalize 不能把第一篇的 citekey 抖成 xxxb 再抖回来。

    第一次 finalize（仅 A）落 citekey 到索引；第二次 finalize（新增 B 后整月重建）
    此前会把 A 自己上一轮写入索引的 citekey 误判为「库内已占用」，被迫加消歧后缀。
    """
    month = "2026-08"
    segA = _manual_seg("pa", "Missing Data Structures Paper", "Zhang", "10.1/a")
    _write_final_bundle(tmp_path, month, segA, "/a.pdf")

    r1 = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert r1["papers"] == 1
    key_a_first = _citekey_of(tmp_path, "Missing Data Structures Paper")
    assert key_a_first and not key_a_first.endswith("b")

    # 追加第二篇不同论文，触发整月重建
    segB = _manual_seg("pb", "Second Independent Paper", "Wang", "10.1/b")
    _write_final_bundle(tmp_path, month, segB, "/b.pdf")

    r2 = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert r2["papers"] == 2
    key_a_second = _citekey_of(tmp_path, "Missing Data Structures Paper")
    key_b = _citekey_of(tmp_path, "Second Independent Paper")

    assert key_a_second == key_a_first, (
        "A 的 citekey 在第二次 finalize 后被抖动：{} -> {}".format(key_a_first, key_a_second))
    assert key_b and key_b != key_a_second

    # 第三次 finalize（再加一篇 C）：A、B 的键仍应保持不变
    segC = _manual_seg("pc", "Third Independent Paper", "Li", "10.1/c")
    _write_final_bundle(tmp_path, month, segC, "/c.pdf")
    r3 = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert r3["papers"] == 3
    assert _citekey_of(tmp_path, "Missing Data Structures Paper") == key_a_first
    assert _citekey_of(tmp_path, "Second Independent Paper") == key_b


# ---------------- _rebuild_month 核验门禁（防未核验 bundle 搭车入库） ----------------

def test_rebuild_month_excludes_final_bundle_without_cross_check(tmp_path):
    """回归：cmd_finalize 门禁只查被点名的 bundle，同月「status=final 但无
    cross_check_report」的兄弟 bundle 此前会随整月重建搭车写进 md/索引——
    脚本草稿的系统性偏差正是靠亲读核验挡的，_rebuild_month 必须在纳入时拒绝。"""
    month = "2026-08"
    segA = _manual_seg("pa", "Verified Paper", "Zhang", "10.1/a")
    _write_final_bundle(tmp_path, month, segA, "/a.pdf")
    # B：agent 自报 final、有 close_reading_final，但从未写核验报告
    segB = _manual_seg("pb", "Unverified Freeloader Paper", "Wang", "10.1/b")
    bf_b = _write_final_bundle(tmp_path, month, segB, "/b.pdf", cross_check_report=None)

    r = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert r["papers"] == 1
    assert bf_b.name in r["skipped_drafts"]
    assert _citekey_of(tmp_path, "Verified Paper")
    assert _citekey_of(tmp_path, "Unverified Freeloader Paper") is None
    md = (tmp_path / "科研札记_{}_手动精读.md".format(month)).read_text(encoding="utf-8")
    assert "Unverified Freeloader Paper" not in md


def test_rebuild_month_all_unverified_finals_rebuild_nothing(tmp_path):
    """regen 直调 _rebuild_month 零门禁的路径：整月只有未核验 final 时不得产出札记。"""
    month = "2026-08"
    seg = _manual_seg("pa", "Only Unverified Paper", "Li", "10.1/a")
    bf = _write_final_bundle(tmp_path, month, seg, "/a.pdf", cross_check_report=None)
    r = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert r["papers"] == 0 and bf.name in r["skipped_drafts"]
    assert not (tmp_path / "科研札记_{}_手动精读.md".format(month)).exists()


def test_rebuild_month_keeps_legacy_heterogeneous_report(tmp_path):
    """门禁只查「报告存在」不卡 verified_count>=1：存量已核验 bundle 的报告是
    异构 schema（verified_count 为 None / 用 verified 键，实测 7 份），
    硬卡计数会把真核验过的旧篇挤出当月重建。"""
    month = "2026-07"
    seg = _manual_seg("pa", "Legacy Verified Paper", "Chen", "10.1/legacy")
    _write_final_bundle(tmp_path, month, seg, "/a.pdf",
                        cross_check_report={"verified_count": None, "corrected": [],
                                            "added": [], "notes": "旧 schema 核验记录"})
    r = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert r["papers"] == 1 and not r["skipped_drafts"]
    assert _citekey_of(tmp_path, "Legacy Verified Paper")


# ---------------- _rebuild_month 沿用札记侧改过的 citekey ----------------

def _sidecar_rename(notes_dir, month, old, new):
    """模拟 audit_citekeys_vs_pmlr.py --apply：只改札记侧三处，不碰 bundle。"""
    stem = "科研札记_{}_手动精读".format(month)
    md = notes_dir / (stem + ".md")
    md.write_text(md.read_text(encoding="utf-8").replace(
        "[@{}]".format(old), "[@{}]".format(new)), encoding="utf-8")
    sp = notes_dir / (stem + ".index.json")
    data = json.loads(sp.read_text(encoding="utf-8"))
    for r in data["papers"]:
        if r.get("citekey") == old:
            r["citekey"] = new
    sp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    rp = notes_dir / (stem + ".references.json")
    items = json.loads(rp.read_text(encoding="utf-8"))
    for it in items:
        if it.get("id") == old:
            it["id"] = new
    rp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def test_rebuild_month_reuses_renamed_citekey_instead_of_reverting(tmp_path):
    """札记侧改过的 citekey 必须被 regen 沿用，不能被 bundle 里的旧元数据顶回去。

    bundle 里没有 citekey 字段，此前 _rebuild_month 对每篇现算 _fallback_citekey，
    于是 audit 按 PMLR 官方 slug 修正过的键（复姓/年份）会在下次 regen 全部丢失。
    """
    month = "2026-08"
    seg = _manual_seg("pa", "Uncertainty Aware Logistic Regression", "Fuertes", "10.1/a")
    _write_final_bundle(tmp_path, month, seg, "/a.pdf")
    M._rebuild_month(tmp_path, month, _FakeSettings())
    old = _citekey_of(tmp_path, "Uncertainty Aware Logistic Regression")

    fixed = "torresfuertes2026Uncertainty"          # 官方 slug 修正后的键
    assert fixed != old
    _sidecar_rename(tmp_path, month, old, fixed)

    M._rebuild_month(tmp_path, month, _FakeSettings())
    assert _citekey_of(tmp_path, "Uncertainty Aware Logistic Regression") == fixed

    # 沿用的是兜底键，不能在 sidecar 里冒充 Zotero 权威键
    sc = json.loads((tmp_path / "科研札记_{}_手动精读.index.json".format(month))
                    .read_text(encoding="utf-8"))
    row = next(r for r in sc["papers"] if r["citekey"] == fixed)
    assert row["citekey_source"] == "fallback"


def test_rebuild_month_new_paper_still_gets_fallback_key(tmp_path):
    """沿用机制不能挡住新论文：库里没有的照常现算兜底键，且不与沿用键相撞。"""
    month = "2026-08"
    seg = _manual_seg("pa", "First Paper About Masking", "Fani", "10.1/a")
    _write_final_bundle(tmp_path, month, seg, "/a.pdf")
    M._rebuild_month(tmp_path, month, _FakeSettings())
    _sidecar_rename(tmp_path, month,
                    _citekey_of(tmp_path, "First Paper About Masking"), "fani2026Masking")

    segB = _manual_seg("pb", "Second Paper About Masking", "Fani", "10.1/b")
    _write_final_bundle(tmp_path, month, segB, "/b.pdf")
    M._rebuild_month(tmp_path, month, _FakeSettings())

    assert _citekey_of(tmp_path, "First Paper About Masking") == "fani2026Masking"
    kb = _citekey_of(tmp_path, "Second Paper About Masking")
    assert kb and kb != "fani2026Masking"


def test_rebuild_month_refuses_reuse_when_batch_has_duplicate_dedup_key(tmp_path):
    """本批两份 bundle 同 dedup_key（同 DOI 换标题重 ingest）时拒绝沿用——
    否则两篇会拿到同一个显式键，而 write_notes 对显式键不做查重 = 静默撞键。"""
    month = "2026-08"
    seg = _manual_seg("pa", "Shared DOI Paper", "Zhang", "10.1/dup")
    _write_final_bundle(tmp_path, month, seg, "/a.pdf")
    M._rebuild_month(tmp_path, month, _FakeSettings())
    _sidecar_rename(tmp_path, month,
                    _citekey_of(tmp_path, "Shared DOI Paper"), "zhang2026Shared")

    # 同 DOI、不同 paper_id/标题 —— dedup_key 相同
    segB = _manual_seg("pb", "Shared DOI Paper Revised Title", "Zhang", "10.1/dup")
    _write_final_bundle(tmp_path, month, segB, "/b.pdf")
    M._rebuild_month(tmp_path, month, _FakeSettings())

    idx = json.loads((tmp_path / "literature_index.json").read_text(encoding="utf-8"))
    keys = [p["citekey"] for p in idx["papers"] if p.get("month") == month]
    assert len(keys) == len(set(keys)), "撞键：{}".format(keys)


# ---------------- R1：坏 bundle 隔离 / 回执 / 门禁 ----------------

def test_rebuild_month_isolates_malformed_bundle(tmp_path):
    """一份 agent 手写坏的 bundle 只能坏它自己，不能连坐整月。

    close_reading_final 的 tag 越出 CloseReadTag 的 Literal（写成近义词「可引用」）会让
    CloseReading.model_validate 抛 ValidationError；此前 segment_from_bundle 在循环里
    裸奔，整月 finalize 抛裸 traceback 退出，同月全部已核验论文一篇都进不了库。
    """
    from src.scholar import pdf_ingest as pi
    month = "2026-08"
    good = _manual_seg("pgood", "Good Paper", "Zhang", "10.1/good")
    _write_final_bundle(tmp_path, month, good, "/good.pdf")

    bad = _manual_seg("pbadtag", "Bad Tag Paper", "Wang", "10.1/badtag")
    final = bad.close_reading.model_dump(mode="json")
    final["sections"][0]["sentences"][0]["tag"] = "可引用"        # 合法值是「可引用证据」
    pi.write_bundle(pi.bundle_path(tmp_path, month, bad.paper_id), status="final",
                    month=month, pdf_path="/badtag.pdf", metadata_source="crossref-doi",
                    segment=bad, close_reading_script=bad.close_reading,
                    close_reading_final=final,
                    cross_check_report={"verified_count": 2, "corrected": [], "added": []})

    r = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert r["papers"] == 1, "坏 bundle 不该拖垮同月合规论文"
    assert any("pbadtag" in n for n in r["broken_bundles"])


def test_rebuild_month_rejects_string_shaped_cross_check(tmp_path):
    """corrected 写成字符串会被 `for c in corrected[:20]` 逐字符切片成十几条单字
    highlights 全进 refutable 取证轴——必须拒收该篇，而不是静默投毒。"""
    month = "2026-08"
    seg = _manual_seg("pstr", "String Report Paper", "Li", "10.1/str")
    _write_final_bundle(tmp_path, month, seg, "/str.pdf",
                        cross_check_report={"verified_count": 2,
                                            "corrected": "把表3的AUC从0.91改成0.81",
                                            "added": []})
    r = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert r["papers"] == 0
    assert any("pstr" in n for n in r["broken_bundles"])


def test_rebuild_month_reports_unreadable_bundle(tmp_path, capsys):
    """读不出的 bundle 此前既不进 papers 也不进 skipped_drafts，_report_final 照打 ✅——
    一篇已完成亲读核验的论文永久蒸发且回执是绿的。"""
    from src.scholar import pdf_ingest as pi
    month = "2026-08"
    good = _manual_seg("pok", "Fine Paper", "Zhang", "10.1/ok")
    _write_final_bundle(tmp_path, month, good, "/ok.pdf")
    broken = pi.bundle_path(tmp_path, month, "pbroken")
    broken.write_text('{"status": "final", "close_reading_final"', encoding="utf-8")

    r = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert r["papers"] == 1
    assert any("pbroken" in n for n in r["broken_bundles"])
    M._report_final(r, tmp_path)
    out = capsys.readouterr().out
    assert "未入库" in out and "pbroken" in out


def test_rebuild_month_rejects_explicit_zero_verified_count(tmp_path):
    """自报 verified_count=0 的 bundle 自己 finalize 会被拒，却能靠同月兄弟搭车进整月重建。
    只拒显式的 0——legacy 的 None/缺键仍放行（见 test_rebuild_month_keeps_legacy_...）。"""
    month = "2026-08"
    good = _manual_seg("pv1", "Verified Paper", "Zhang", "10.1/v1")
    _write_final_bundle(tmp_path, month, good, "/v1.pdf")
    zero = _manual_seg("pv0", "Zero Verified Paper", "Wang", "10.1/v0")
    _write_final_bundle(tmp_path, month, zero, "/v0.pdf",
                        cross_check_report={"verified_count": 0, "corrected": [], "added": []})
    r = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert r["papers"] == 1
    assert any("pv0" in n for n in r["skipped_drafts"])


def test_correction_lines_are_not_tagged_refutable(tmp_path):
    """纠错条不再冒充「可反驳观点」：实测 918/3630（25.3%）的 manual refutable 取证条
    是「Opus 原稿写错了」这类草稿勘误，不是论文的可质疑处。句子仍留在札记里。"""
    month = "2026-08"
    seg = _manual_seg("pcorr", "Correction Paper", "Li", "10.1/corr")
    _write_final_bundle(tmp_path, month, seg, "/corr.pdf",
                        cross_check_report={
                            "verified_count": 5,
                            "corrected": [{"page": 4, "note": "Opus 原稿称 AUC 0.91，原文 Table 2 为 0.87"}],
                            "added": []})
    r = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert r["papers"] == 1
    idx = json.loads((tmp_path / "literature_index.json").read_text(encoding="utf-8"))
    paper = next(p for p in idx["papers"] if p.get("title") == "Correction Paper")
    refutable = [h for h in (paper.get("highlights") or []) if h.get("role") == "refutable"]
    assert not any(h["text"].startswith("纠错") for h in refutable)
    md = (tmp_path / "科研札记_{}_手动精读.md".format(month)).read_text(encoding="utf-8")
    assert "Opus 原稿称 AUC 0.91" in md, "留痕不能丢，只是不再打 tag"


def test_cross_check_report_alias_keys_are_read(tmp_path):
    """早期 schema 用 corrections/additions，只认 corrected/added 会把核验内容静默吞成
    「纠错 0 处、补漏 0 处」（存量实测约 10 篇已如此）。"""
    month = "2026-08"
    seg = _manual_seg("palias", "Alias Report Paper", "Zhao", "10.1/alias")
    _write_final_bundle(tmp_path, month, seg, "/alias.pdf",
                        cross_check_report={"verified_count": 4,
                                            "corrections": ["原文 Table 3 与正文自相矛盾"],
                                            "additions": ["补漏：附录 D 的敏感性分析"]})
    r = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert r["papers"] == 1
    md = (tmp_path / "科研札记_{}_手动精读.md".format(month)).read_text(encoding="utf-8")
    assert "纠错 1 处、补漏 1 处" in md
    assert "原文 Table 3 与正文自相矛盾" in md


def test_finalize_rejects_month_bucket_mismatch(tmp_path, monkeypatch):
    """bundle 的 month 字段与所在目录不一致时，按月重建扫的是空桶——退出码 0、
    打印「✅ 归档 X：0 篇」，一篇已核验论文彻底消失。必须在动库之前拒绝。"""
    from types import SimpleNamespace
    from src.scholar import pdf_ingest as pi
    seg = _manual_seg("pmis", "Mismatch Paper", "Ann Lee", "10.9/mis")
    bf = tmp_path / "manual" / "2026-08" / "pmis.bundle.json"
    bf.parent.mkdir(parents=True)
    pi.write_bundle(bf, status="final", month="2026-07", pdf_path="x.pdf",
                    metadata_source="crossref-doi", segment=seg,
                    close_reading_script=seg.close_reading,
                    close_reading_final=seg.close_reading.model_dump(mode="json"),
                    cross_check_report={"verified_count": 3, "corrected": [], "added": []})

    class _Proc:
        notes_dir = tmp_path
    class _Settings:
        processing = _Proc()
    monkeypatch.setattr(M, "_load_settings", lambda cfg: _Settings())
    rebuilt = []
    monkeypatch.setattr(M, "_rebuild_month", lambda *a, **k: rebuilt.append(1) or {"month": "x"})
    monkeypatch.setattr(M, "_report_final", lambda *a, **k: None)
    assert M.cmd_finalize(SimpleNamespace(config="unused", bundle=str(bf))) == 1
    assert not rebuilt


def test_finalize_rejects_non_dict_cross_check_report(tmp_path, monkeypatch):
    """报告写成字符串时，cmd_finalize 的 ccr.get 会抛 AttributeError 裸 traceback。"""
    from types import SimpleNamespace
    from src.scholar import pdf_ingest as pi
    seg = _manual_seg("pncd", "Non Dict Paper", "Ann Lee", "10.9/ncd")
    bf = tmp_path / "manual" / "2026-08" / "pncd.bundle.json"
    bf.parent.mkdir(parents=True)
    pi.write_bundle(bf, status="final", month="2026-08", pdf_path="x.pdf",
                    metadata_source="crossref-doi", segment=seg,
                    close_reading_script=seg.close_reading,
                    close_reading_final=seg.close_reading.model_dump(mode="json"),
                    cross_check_report="已亲读核验，纠错3处")

    class _Proc:
        notes_dir = tmp_path
    class _Settings:
        processing = _Proc()
    monkeypatch.setattr(M, "_load_settings", lambda cfg: _Settings())
    monkeypatch.setattr(M, "_rebuild_month", lambda *a, **k: pytest.fail("不该走到重建"))
    assert M.cmd_finalize(SimpleNamespace(config="unused", bundle=str(bf))) == 1


def test_title_ignored_in_batch_is_announced(capsys):
    """--title 批量时被丢弃。告警必须落在末尾的「需要注意」块——循环前 warning 一次
    正好落在本文件反复论证过的「21 篇一批必被淹掉」的位置。"""
    outs = [{"title": "A", "duplicate": None, "meta_source": "crossref-doi", "authors_n": 3},
            {"title": "B", "duplicate": None, "meta_source": "crossref-doi", "authors_n": 2}]
    M._print_attention(outs, [], title_ignored="An Exact Paper Title")
    out = capsys.readouterr().out
    assert "--title" in out and "批量" in out and "单独" in out


# ---------------- R2：R1 自引入缺陷的回归 ----------------

def test_cross_check_alias_count_field_is_ignored_not_rejected(tmp_path):
    """R1 自引入:早期 schema 里 `added_new` 是**计数**不是数组(磁盘上真有一份),
    链式 or 把 2 取进 added → isinstance 拒收 → 下一次同月 finalize 就把这篇已归档的
    论文从 md/索引/书目/向量库一并抹掉。别名只能在取到数组时才认。"""
    month = "2026-08"
    seg = _manual_seg("pcount", "Count Alias Paper", "Sun", "10.1/count")
    _write_final_bundle(tmp_path, month, seg, "/count.pdf",
                        cross_check_report={"verified_count": 30,
                                            "confirmed_accurate": 22,
                                            "corrected_or_rewritten": 6,
                                            "added_new": 2,
                                            "corrections": ["原文 Table 2 的 AUC 为 0.87"]})
    r = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert r["papers"] == 1, "计数型别名不该让已归档论文被拒收"
    assert not r["broken_bundles"]
    md = (tmp_path / "科研札记_{}_手动精读.md".format(month)).read_text(encoding="utf-8")
    assert "纠错 1 处、补漏 0 处" in md          # added_new=2 不当数组用
    assert "原文 Table 2 的 AUC 为 0.87" in md


def test_main_key_written_as_wrong_type_is_still_rejected(tmp_path):
    """收窄不能把投毒路径一起放行:主键 corrected 显式写成字符串仍必须拒收。"""
    month = "2026-08"
    seg = _manual_seg("pstr2", "String Main Key", "Qian", "10.1/str2")
    _write_final_bundle(tmp_path, month, seg, "/str2.pdf",
                        cross_check_report={"verified_count": 2,
                                            "corrected": "把表3的AUC从0.91改成0.81"})
    r = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert r["papers"] == 0 and any("pstr2" in n for n in r["broken_bundles"])


def test_finalize_exits_nonzero_when_a_bundle_was_not_ingested(tmp_path, monkeypatch):
    """在 _rebuild_month 里「拒收」= 从已归档 md 里删除。绿回执 + exit 0 会让 agent
    直接往下走,所以有 broken_bundles 时必须非 0 且抬头变红。"""
    from types import SimpleNamespace
    from src.scholar import pdf_ingest as pi
    month = "2026-08"
    good = _manual_seg("pfine", "Fine Paper", "Zhou", "10.1/fine")
    _write_final_bundle(tmp_path, month, good, "/fine.pdf")
    broken = pi.bundle_path(tmp_path, month, "pbad")
    broken.write_text('{"status": "final", "close_reading_final"', encoding="utf-8")
    bundle = pi.bundle_path(tmp_path, month, good.paper_id)

    class _Proc(_FakeProc):
        notes_dir = tmp_path
    class _Settings:
        processing = _Proc()
    monkeypatch.setattr(M, "_load_settings", lambda cfg: _Settings())
    assert M.cmd_finalize(SimpleNamespace(config="unused", bundle=str(bundle))) == 1


def test_report_final_header_turns_red_on_broken_bundles(capsys):
    M._report_final({"month": "2026-08", "papers": 1, "skipped_drafts": [],
                     "broken_bundles": ["x.paper.json"],
                     "index": {"papers": [], "citekey_collisions": []}}, None)
    out = capsys.readouterr().out
    assert "⛔ 手动精读归档" in out and "✅ 手动精读归档" not in out
