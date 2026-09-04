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
    # 形状对齐 ingest_pdf 的真实回执：权威源（crossref-*）必带 DOI；draft_status 默认 ok
    base = {"title": "Some Paper Title", "duplicate": None, "meta_source": "crossref-doi",
            "authors_n": 3, "skipped": None, "doi": "10.1000/x", "arxiv_id": None,
            "meta_degraded": [], "draft_status": "ok", "draft_note": ""}
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
    (_row(meta_source="pdf-only", authors_n=0, doi=None), True),
    (_row(meta_source="pdf-llm", authors_n=0, doi=None), True),      # 有来源但没作者，一样是 anon*
    (_row(meta_source="crossref-doi", authors_n=5), False),
    (_row(meta_source="pdf-only", authors_n=0, doi=None, skipped="final"), False),  # 跳过的不重复提醒
    # 2026-09-03 漏报的那一类：LLM 把四位作者全抽对了，但 DOI/卷期页全空——书目照样是残的
    (_row(meta_source="pdf-llm", authors_n=4, doi=None, arxiv_id="2102.09204v2"), True),
    # 权威源没 DOI 但有 arXiv id（arxiv 直查命中）：身份键不退化，不报
    (_row(meta_source="arxiv", authors_n=4, doi=None, arxiv_id="2102.09204v2"), False),
    (_row(meta_source="crossref-title", authors_n=2), False),
    # 来源看着权威却两个标识都空（不该发生，防御性）：身份键会退化，报
    (_row(meta_source="crossref-title", authors_n=2, doi=None, arxiv_id=None), True),
])
def test_thin_metadata_predicate(row, expect, capsys):
    M._print_attention([row], [])
    assert ("元数据不全" in capsys.readouterr().out) is expect


def test_thin_metadata_prints_degradation_reason_and_identifiers(capsys):
    """回执要能区分「查询失败导致的空」与「本来就没有」：有原因行的是前者。
    2026-09-03 那篇 arXiv 精确查遇 SSL 中断退化成 pdf-llm，原因只在日志中段，末尾回执全绿。"""
    row = _row(title="Towards a Mathematical Theory of Trajectory Inference",
               meta_source="pdf-llm", authors_n=4, doi=None, arxiv_id="2102.09204v2",
               meta_degraded=["arXiv 精确查失败（2102.09204v2）: [SSL: UNEXPECTED_EOF_WHILE_READING]"])
    M._print_attention([row], [])
    out = capsys.readouterr().out
    assert "元数据不全" in out and "pdf-llm" in out
    assert "DOI 无" in out and "arXiv 2102.09204v2" in out
    assert "原因：arXiv 精确查失败" in out and "UNEXPECTED_EOF" in out
    assert "网络恢复后" in out


def test_attention_block_reports_non_ok_draft_status(capsys):
    """draft_status≠ok 必须进末尾块：Hoogland 那次 27/27 块成功却无草稿，只在中间打了一行
    `draft_status=degraded`，agent 差点当成「草稿只是没写好」（实为根本没生成）。"""
    rows = [_row(title="Fine Paper"),
            _row(title="Synth Broke", draft_status="synth_failed",
                 draft_note="块通读 27/27 成功，但汇总步失败、无脚本草稿：汇总返回不可解析为分节精读（响应 12 字符…）"),
            _row(title="Partial", draft_status="degraded",
                 draft_note="2/11 块通读失败（块 3, 7），草稿只基于其余块"),
            _row(title="Skipped Final", skipped="final", draft_status="synth_failed")]
    M._print_attention(rows, [])
    out = capsys.readouterr().out
    assert "需要注意（3 项）" in out          # 2 条草稿状态 + 1 条已 final 跳过
    assert "脚本草稿 synth_failed：Synth Broke" in out and "汇总返回不可解析" in out
    assert "脚本草稿 degraded：Partial" in out and "块 3, 7" in out
    assert "走回退协议" in out
    assert "Fine Paper" not in out


@pytest.mark.parametrize("base,ctx,expect", [
    ("EHR 缺失机制", "", "EHR 缺失机制"),
    ("EHR 缺失机制", "   ", "EHR 缺失机制"),
    ("EHR 缺失机制", "景观-流形-缺失：本批为 A 层地基文献",
     "EHR 缺失机制\n\n【本批阅读目的】景观-流形-缺失：本批为 A 层地基文献"),
    ("", "本批目的", "\n\n【本批阅读目的】本批目的"),
])
def test_research_interests_with_context(base, ctx, expect):
    assert M._research_interests_with_context(base, ctx) == expect


def test_cmd_ingest_prints_fallback_protocol_when_synth_failed(tmp_path, monkeypatch, capsys):
    """无草稿的三态（api_error / synth_failed / empty）都要打回退协议，不只 api_error。"""
    from types import SimpleNamespace
    class _Proc(_FakeProc):
        notes_dir = tmp_path
        zotero_email = ""
        external_email = ""
        research_interests = "画像"
    class _S:
        processing = _Proc()
        class llm:
            closeread_model = "m"
            model = "m"
    monkeypatch.setattr(M, "_load_settings", lambda cfg: _S())
    monkeypatch.setattr(M, "LLMClient", lambda cfg: None)
    import src.scholar.pdf_ingest as pi
    seen = {}

    def _fake_ingest(pdf_path, notes_dir, month, llm, **kw):
        seen["ri"] = kw.get("research_interests")
        return {"title": "T", "bundle": "b", "pdf_path": "p", "meta_source": "crossref-doi",
                "doi": "10.1/x", "arxiv_id": None, "meta_degraded": [], "n_pages": 17,
                "chunk_ok": 27, "chunks": 27, "has_close_reading": False,
                "draft_status": "synth_failed",
                "draft_note": "块通读 27/27 成功，但汇总步失败、无脚本草稿：汇总 LLM 调用失败：TimeoutError: x",
                "authors_n": 3, "duplicate": None, "skipped": None}
    monkeypatch.setattr(pi, "ingest_pdf", _fake_ingest)
    (tmp_path / "a.pdf").write_bytes(b"%PDF")
    args = SimpleNamespace(config="u", month="2026-09", pdf=[str(tmp_path / "a.pdf")],
                           recursive=False, title=None, force=False,
                           context="插补扭曲第二批：方法学地基")
    assert M.cmd_ingest(args) == 0
    out = capsys.readouterr().out
    assert "脚本草稿这一轨缺失（synth_failed）" in out and "TimeoutError" in out
    assert "回退协议" in out and "两个 subagent 对抗生成" in out
    assert "脚本草稿 synth_failed" in out          # 末尾「需要注意」块也要有
    assert seen["ri"].endswith("【本批阅读目的】插补扭曲第二批：方法学地基")
    assert seen["ri"].startswith("画像")


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


# ---------------- R3：净删除止损闸 + 补齐半条链路 ----------------

def test_rebuild_month_refuses_net_removal_when_a_bundle_is_rejected(tmp_path):
    """R2 把回执改红、退出码改成 1,只解决了「你会知道出事了」——那一篇仍然已经从
    md/references/sidecar/索引/书目/向量库里消失。整月重建是**整篇重写**,所以必须在
    动库之前拦:这一轮会不会净删掉上一轮已归档的条目?"""
    from src.scholar import pdf_ingest as pi
    month = "2026-08"
    a = _manual_seg("pa1", "Archived Paper A", "Zhang", "10.1/a1")
    b = _manual_seg("pb1", "Archived Paper B", "Wang", "10.1/b1")
    _write_final_bundle(tmp_path, month, a, "/a1.pdf")
    _write_final_bundle(tmp_path, month, b, "/b1.pdf")
    r1 = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert r1["papers"] == 2
    md_before = (tmp_path / "科研札记_{}_手动精读.md".format(month)).read_text(encoding="utf-8")

    # B 的 bundle 被写坏（agent 手改 JSON 的常态失误）
    pi.bundle_path(tmp_path, month, "pb1").write_text('{"status": "final"', encoding="utf-8")
    r2 = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert r2.get("refused") is True
    assert r2["removed_keys"], "必须点名哪些条目会被净删除"
    md_after = (tmp_path / "科研札记_{}_手动精读.md".format(month)).read_text(encoding="utf-8")
    assert md_after == md_before, "整月必须一字未动"
    assert "Archived Paper B" in md_after


def test_rebuild_month_allows_removal_with_flag(tmp_path):
    """确要删时 --allow-removals 放行（否则修不掉「我就是不想要那篇了」的场景）。"""
    from src.scholar import pdf_ingest as pi
    month = "2026-08"
    a = _manual_seg("pa2", "Keep Me", "Zhang", "10.1/a2")
    b = _manual_seg("pb2", "Drop Me", "Wang", "10.1/b2")
    _write_final_bundle(tmp_path, month, a, "/a2.pdf")
    _write_final_bundle(tmp_path, month, b, "/b2.pdf")
    M._rebuild_month(tmp_path, month, _FakeSettings())
    pi.bundle_path(tmp_path, month, "pb2").write_text('{"status": "final"', encoding="utf-8")
    r = M._rebuild_month(tmp_path, month, _FakeSettings(), allow_removals=True)
    assert not r.get("refused") and r["papers"] == 1
    md = (tmp_path / "科研札记_{}_手动精读.md".format(month)).read_text(encoding="utf-8")
    assert "Drop Me" not in md and "Keep Me" in md


def test_rebuild_month_does_not_refuse_on_clean_removal(tmp_path):
    """没有 bundle 被拒收时,条目变少是人主动删了 bundle,不该拦。"""
    from src.scholar import pdf_ingest as pi
    month = "2026-08"
    a = _manual_seg("pa3", "Stay", "Zhang", "10.1/a3")
    b = _manual_seg("pb3", "Gone", "Wang", "10.1/b3")
    _write_final_bundle(tmp_path, month, a, "/a3.pdf")
    _write_final_bundle(tmp_path, month, b, "/b3.pdf")
    M._rebuild_month(tmp_path, month, _FakeSettings())
    pi.bundle_path(tmp_path, month, "pb3").unlink()        # 干净删除,无拒收
    r = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert not r.get("refused") and r["papers"] == 1


def test_rebuild_month_refuses_on_verified_count_zero_removal(tmp_path):
    """verified_count=0 对一篇**已归档**论文同样是「从 md 里删掉」,判据必须把 skipped
    也算进去——R1/R2 都只把 broken 当危害。"""
    month = "2026-08"
    a = _manual_seg("pa4", "Archived X", "Zhang", "10.1/a4")
    b = _manual_seg("pb4", "Archived Y", "Wang", "10.1/b4")
    _write_final_bundle(tmp_path, month, a, "/a4.pdf")
    _write_final_bundle(tmp_path, month, b, "/b4.pdf")
    M._rebuild_month(tmp_path, month, _FakeSettings())
    _write_final_bundle(tmp_path, month, b, "/b4.pdf",
                        cross_check_report={"verified_count": 0, "corrected": [], "added": []})
    r = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert r.get("refused") is True


def test_rebuild_month_rejects_non_dict_cross_check(tmp_path):
    """R3 变异发现的缺口:非 dict 报告的拒收只在 cmd_finalize 侧有测,_rebuild_month
    侧没有——而 regen 与同月兄弟的 finalize 都走这条路。"""
    month = "2026-08"
    seg = _manual_seg("pnd", "Non Dict Report", "Li", "10.1/nd")
    _write_final_bundle(tmp_path, month, seg, "/nd.pdf",
                        cross_check_report="已亲读核验，纠错3处")
    r = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert r["papers"] == 0 and any("pnd" in n for n in r["broken_bundles"])


def test_regen_exits_nonzero_when_a_bundle_was_not_ingested(tmp_path, monkeypatch):
    """R2 的非 0 退出码只在 finalize 侧有测;regen 是 SKILL 教的批量重建入口。"""
    from types import SimpleNamespace
    from src.scholar import pdf_ingest as pi
    month = "2026-08"
    good = _manual_seg("pg", "Good", "Zhou", "10.1/g")
    _write_final_bundle(tmp_path, month, good, "/g.pdf")
    pi.bundle_path(tmp_path, month, "pbroken").write_text('{"status":', encoding="utf-8")

    class _Proc(_FakeProc):
        notes_dir = tmp_path
    class _Settings:
        processing = _Proc()
    monkeypatch.setattr(M, "_load_settings", lambda cfg: _Settings())
    rc = M.cmd_regen(SimpleNamespace(config="unused", month=month, allow_removals=False))
    assert rc == 1


def test_cmd_ingest_forwards_title_ignored(tmp_path, monkeypatch):
    """R3 变异发现:_print_attention 本体测了,但 cmd_ingest 是否真的把 --title 传进去没测。"""
    from types import SimpleNamespace
    seen = {}
    monkeypatch.setattr(M, "_print_attention",
                        lambda outs, failed, title_ignored="": seen.update(t=title_ignored))
    class _Proc(_FakeProc):
        notes_dir = tmp_path
        zotero_email = ""
        external_email = ""
        research_interests = ""
    class _S:
        processing = _Proc()
        class llm:
            closeread_model = "m"
            model = "m"
    monkeypatch.setattr(M, "_load_settings", lambda cfg: _S())
    monkeypatch.setattr(M, "LLMClient", lambda cfg: None)
    import src.scholar.pdf_ingest as pi
    monkeypatch.setattr(pi, "ingest_pdf", lambda *a, **k: {
        "title": "T", "bundle": "b", "pdf_path": "p", "meta_source": "x", "doi": None,
        "arxiv_id": None, "n_pages": 1, "chunk_ok": 1, "chunks": 1,
        "has_close_reading": True, "draft_status": "ok", "authors_n": 1, "duplicate": None})
    for f in ("a.pdf", "b.pdf"):
        (tmp_path / f).write_bytes(b"%PDF")
    args = SimpleNamespace(config="u", month="2026-08", pdf=[str(tmp_path)], recursive=False,
                           title="Exact Title", force=False)
    M.cmd_ingest(args)
    assert seen.get("t") == "Exact Title"
    args.pdf = [str(tmp_path / "a.pdf")]
    M.cmd_ingest(args)
    assert seen.get("t") == "", "单篇时 --title 生效，不该报忽略"


# ---------------- R4：整月重建的并发缺口（见 docs/bugs/2026-09-03-finalize-concurrency.md） ----------------

def test_rebuild_month_must_not_drop_bundle_still_on_disk(tmp_path):
    """并发场景：A 会话 glob 完 bundle 列表后，B 会话新写了一份 final bundle。
    A 用陈旧列表重建 → 那篇会被静默抹出 md/索引，而它的 bundle **仍在盘上**。

    与 test_rebuild_month_does_not_refuse_on_clean_removal 的区别正在于此：
    人主动删 bundle 时文件已不在盘上（该放行）；并发时文件在盘上（该拦）。
    两者在 segments 列表上完全同形，故守卫无法靠「条目变少」分辨。

    这里用「永久说谎的 glob」模拟最坏情况的陈旧——**列表本身不可信**。提交前重查
    因此必须用一条**独立的**目录读取路径（os.scandir），复用同一次 glob 的结果就
    对这类陈旧完全隐形。修复后本例走「3 轮都不稳定 → 整月一字未动」，md 里的 B 保住。
    """
    from src.scholar import pdf_ingest as pi
    import pathlib
    month = "2026-08"
    a = _manual_seg("pa9", "Paper A Archived", "Zhang", "10.1/a9")
    b = _manual_seg("pb9", "Paper B NewlyAdded", "Wang", "10.1/b9")
    _write_final_bundle(tmp_path, month, a, "/a9.pdf")
    _write_final_bundle(tmp_path, month, b, "/b9.pdf")
    M._rebuild_month(tmp_path, month, _FakeSettings())
    b_path = pi.bundle_path(tmp_path, month, "pb9")

    orig_glob = pathlib.Path.glob

    def _stale_glob(self, pat):                      # A 没看见 B 新写的那份
        out = list(orig_glob(self, pat))
        if self.name == month:
            out = [p for p in out if p != b_path]
        return iter(out)

    pathlib.Path.glob = _stale_glob
    try:
        M._rebuild_month(tmp_path, month, _FakeSettings())
    finally:
        pathlib.Path.glob = orig_glob

    md = (tmp_path / "科研札记_{}_手动精读.md".format(month)).read_text(encoding="utf-8")
    assert b_path.exists(), "前提：B 的 bundle 仍在盘上"
    assert "Paper B NewlyAdded" in md, "盘上仍有 bundle 的论文不得被整月重建抹掉"


def test_rebuild_month_rewrites_global_index_across_all_months(tmp_path):
    """爆炸半径：_rebuild_month 收尾的 update_index 扫的是**全部月份**，
    故两个会话即使精读不同月份，全局索引仍会对撞——按月加锁不够，得锁到索引层。
    """
    a = _manual_seg("pm1", "July Paper", "Zhang", "10.1/m1")
    b = _manual_seg("pm2", "August Paper", "Wang", "10.1/m2")
    _write_final_bundle(tmp_path, "2026-07", a, "/m1.pdf")
    _write_final_bundle(tmp_path, "2026-08", b, "/m2.pdf")
    M._rebuild_month(tmp_path, "2026-07", _FakeSettings())
    # 只重建 8 月，7 月的条目也会被重新写进全局索引
    M._rebuild_month(tmp_path, "2026-08", _FakeSettings())
    idx = json.loads((tmp_path / "literature_index.json").read_text(encoding="utf-8"))
    titles = {p.get("title") for p in idx["papers"]}
    assert {"July Paper", "August Paper"} <= titles, \
        "重建 8 月却重写了含 7 月条目的全局索引 → 跨月并发同样对撞"


def test_adding_a_paper_renumbers_existing_headings(tmp_path):
    """为什么不能「按论文 id 单篇 append/覆盖」：小节标题里嵌了**全月排名与优先级档位**
    （`## 🔴 高 3. Title [@key]`，档位由 _priority_tier(rank, total) 算），
    新增一篇会改变 total 与其后所有篇的序号，故单篇 upsert 无法就地完成。
    """
    month = "2026-08"
    segs = [_manual_seg("pr%d" % i, "Paper %d" % i, "Author%d" % i, "10.1/r%d" % i)
            for i in range(1, 4)]
    for i, s in enumerate(segs):
        s.priority_score = 1.0 - i * 0.1          # 拉开排名，避免并列
        _write_final_bundle(tmp_path, month, s, "/r%d.pdf" % i)
    M._rebuild_month(tmp_path, month, _FakeSettings())
    md_before = (tmp_path / "科研札记_{}_手动精读.md".format(month)).read_text(encoding="utf-8")
    heads_before = [l for l in md_before.splitlines() if l.startswith("## ") and "[@" in l]

    extra = _manual_seg("pr9", "Paper Inserted", "AuthorZ", "10.1/r9")
    extra.priority_score = 0.95                   # 插进已有序列中间
    _write_final_bundle(tmp_path, month, extra, "/r9.pdf")
    M._rebuild_month(tmp_path, month, _FakeSettings())
    md_after = (tmp_path / "科研札记_{}_手动精读.md".format(month)).read_text(encoding="utf-8")
    heads_after = [l for l in md_after.splitlines() if l.startswith("## ") and "[@" in l]

    assert len(heads_after) == len(heads_before) + 1
    moved = [h for h in heads_before if h not in heads_after]
    assert moved, "新增一篇后，既有论文的小节标题（序号/档位）必然改变 → 单篇 append 不成立"


# ---------------- R5：并发修复（锁 + 提交前重查）----------------

def test_rebuild_month_reruns_and_includes_bundle_written_during_collection(tmp_path,
                                                                            monkeypatch):
    """忠实复现生产竞态（时间差、非"永久说谎的 glob"）：A 已 glob 完列表、正在逐份读
    bundle 时，B 会话新写了一份 final bundle。

    期望**自愈而非拒绝**：提交前重查发现磁盘多了一份 → 用新列表重来一轮 → 两篇都进 md。
    这比"拒绝改动"更好——拒绝的话 A 自己那篇也归不了档。
    """
    from src.scholar import pdf_ingest as pi
    month = "2026-08"
    a = _manual_seg("pa10", "Paper A First", "Zhang", "10.1/a10")
    b = _manual_seg("pb10", "Paper B Concurrent", "Wang", "10.1/b10")
    _write_final_bundle(tmp_path, month, a, "/a10.pdf")

    orig_load = pi.load_bundle
    fired = {"n": 0}

    def _load_and_race(bf):
        # 第一次读 bundle 的瞬间，另一个会话写入了 B（A 的 glob 早已取完，看不到它）
        if fired["n"] == 0:
            fired["n"] = 1
            _write_final_bundle(tmp_path, month, b, "/b10.pdf")
        return orig_load(bf)

    monkeypatch.setattr(pi, "load_bundle", _load_and_race)
    r = M._rebuild_month(tmp_path, month, _FakeSettings())

    assert not r.get("refused"), "并发新增应当自愈重来，而不是拒绝整月"
    assert r["papers"] == 2, "重来那一轮必须看到 B"
    md = (tmp_path / "科研札记_{}_手动精读.md".format(month)).read_text(encoding="utf-8")
    assert "Paper A First" in md and "Paper B Concurrent" in md


def test_bundle_inventory_detects_draft_flipped_to_final_without_path_change(tmp_path):
    """指纹必须带 mtime/size：本工作流的常态是 ingest 先落 draft、agent 后把**同一个
    文件**改写成 final——路径集合根本没变，只比路径集会漏掉（缺陷文档实测过）。
    """
    from src.scholar.pdf_ingest import BUNDLE_SUFFIX
    month = "2026-08"
    seg = _manual_seg("pf1", "Flip Me", "Zhao", "10.1/f1")
    _write_final_bundle(tmp_path, month, seg, "/f1.pdf")
    mdir = tmp_path / "manual" / month
    before = M._bundle_inventory(mdir, BUNDLE_SUFFIX)

    import time as _t
    _t.sleep(0.01)
    _write_final_bundle(tmp_path, month, seg, "/f1.pdf",          # 同一路径改写
                        cross_check_report={"verified_count": 9, "corrected": [], "added": []})
    after = M._bundle_inventory(mdir, BUNDLE_SUFFIX)

    assert set(before) == set(after), "前提：路径集合没变"
    assert before != after, "只比路径集会漏掉 draft→final 翻转，指纹必须带 mtime/size"


def test_bundle_inventory_reads_disk_independently_of_glob(tmp_path):
    """提交前重查必须走**独立**的目录读取路径：若它复用采集那次 glob，
    "我手上这份列表已经过时"这件事对守卫就完全隐形（这正是守卫要发现的东西）。
    """
    import pathlib
    from src.scholar.pdf_ingest import BUNDLE_SUFFIX
    month = "2026-08"
    seg = _manual_seg("pi1", "Hidden From Glob", "Sun", "10.1/i1")
    _write_final_bundle(tmp_path, month, seg, "/i1.pdf")
    mdir = tmp_path / "manual" / month

    orig_glob = pathlib.Path.glob
    pathlib.Path.glob = lambda self, pat: iter([])      # glob 全盘说谎
    try:
        assert M._bundle_inventory(mdir, BUNDLE_SUFFIX), \
            "_bundle_inventory 不得依赖 glob（否则陈旧列表对守卫隐形）"
    finally:
        pathlib.Path.glob = orig_glob


def test_rebuild_month_flags_concurrent_change_during_write(tmp_path, monkeypatch):
    """写盘期间（write_notes 那一段）磁盘又变了：内容已经落盘且本身是对的，只是可能
    缺最新那篇。不回滚，但必须**标红 + 非 0 退出**，否则 agent 收到绿回执就不会重跑。
    """
    from types import SimpleNamespace
    from src.scholar import notes as notes_mod
    month = "2026-08"
    a = _manual_seg("pw1", "Written Paper", "Zhang", "10.1/w1")
    b = _manual_seg("pw2", "Arrived Late", "Wang", "10.1/w2")
    _write_final_bundle(tmp_path, month, a, "/w1.pdf")

    orig_write = notes_mod.write_notes
    fired = {"n": 0}

    def _write_and_race(*args, **kw):
        out = orig_write(*args, **kw)
        if fired["n"] == 0:          # 只在第一轮制造并发，之后让它收敛
            fired["n"] = 1
            _write_final_bundle(tmp_path, month, b, "/w2.pdf")
        return out

    monkeypatch.setattr(notes_mod, "write_notes", _write_and_race)
    r = M._rebuild_month(tmp_path, month, _FakeSettings())
    # 第二轮会看到 B 并收敛；这里只断言机制被触发过（重来了一轮）
    assert fired["n"] == 1
    assert r["papers"] == 2, "写盘期间到达的那篇应在下一轮被收进来"

    class _Proc(_FakeProc):
        notes_dir = tmp_path
    class _Settings:
        processing = _Proc()
    monkeypatch.setattr(M, "_load_settings", lambda cfg: _Settings())
    # concurrent 标记必须让 regen 退非 0（与 refused/broken 同档）
    monkeypatch.setattr(M, "_rebuild_month",
                        lambda *a, **k: {"month": month, "papers": 1, "concurrent": True,
                                         "skipped_drafts": [], "broken_bundles": [],
                                         "index": {"papers": [], "citekey_collisions": []}})
    rc = M.cmd_regen(SimpleNamespace(config="unused", month=month, allow_removals=False))
    assert rc == 1, "并发未收敛必须非 0 退出，否则 agent 不会重跑"


def test_report_final_concurrent_wording_is_not_a_deletion_warning(tmp_path, capsys):
    """并发导致的拒绝**没有**净删除清单：报成「会净删除 0 篇已归档论文」会把人引向
    「数据出问题了」的错误方向，而真相是「什么都没动，稍后重跑即可」。
    """
    M._report_final({"month": "2026-08", "papers": 0, "refused": True, "reason": "concurrent",
                     "skipped_drafts": [], "broken_bundles": [],
                     "index": {"papers": [], "citekey_collisions": []}}, tmp_path)
    out = capsys.readouterr().out
    assert "整月未改动" in out and "数据没丢" in out
    assert "净删除" not in out, "并发拒绝不该说成净删除（会误导排查方向）"


def test_rebuild_lock_serialises_two_rebuilds(tmp_path, monkeypatch):
    """锁：另一轮重建持锁时，本轮等待到上限后拒绝（整月一字未动），而不是并行写盘。

    flock 按 open file description 生效——同进程内另开一个 fd 同样会被挡住，
    故可在进程内构造竞争。
    """
    month = "2026-08"
    seg = _manual_seg("pl1", "Locked Out", "Qian", "10.1/l1")
    _write_final_bundle(tmp_path, month, seg, "/l1.pdf")

    holder = M._RebuildLock(tmp_path)
    assert holder.acquire(), "前提：先手拿到锁"
    try:
        monkeypatch.setattr(M, "_REBUILD_LOCK_TIMEOUT", 0.3)
        r = M._rebuild_month(tmp_path, month, _FakeSettings())
    finally:
        holder.release()

    assert r.get("refused") is True and r.get("reason") == "locked"
    assert not (tmp_path / "科研札记_{}_手动精读.md".format(month)).exists(), \
        "等锁失败时不得写盘"
    # 锁释放后照常重建
    r2 = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert r2["papers"] == 1 and not r2.get("refused")


def test_rebuild_lock_is_released_and_reentrant_across_calls(tmp_path):
    """锁必须在 finally 里释放：漏释放会让同一进程内的下一次重建（regen 批量按月循环、
    或同一会话连着 finalize 两篇）直接等锁超时。
    """
    month = "2026-08"
    seg = _manual_seg("pl2", "Twice", "Zhou", "10.1/l2")
    _write_final_bundle(tmp_path, month, seg, "/l2.pdf")
    for _ in range(3):
        r = M._rebuild_month(tmp_path, month, _FakeSettings())
        assert r["papers"] == 1 and not r.get("refused")


# ---------------- R6：第 1 轮对抗审计/压测的回归（变异存活 + 实测缺陷）----------------

def test_inplace_draft_to_final_rewrite_is_caught_end_to_end(tmp_path, monkeypatch):
    """变异 M1 存活补测：把重查削弱成「只比路径集」时，本例必红。

    另一会话把**同一个文件**从 draft 改写成 final（本工作流常态：ingest 落 draft、
    agent 后写 final），路径集合完全没变——只有 mtime/size 变。端到端必须发现并重来，
    否则那篇被静默丢掉。
    """
    from src.scholar import pdf_ingest as pi
    month = "2026-08"
    a = _manual_seg("pm1", "Anchor Paper", "Zhang", "10.1/m1")
    b = _manual_seg("pm2", "Flipped To Final", "Wang", "10.1/m2")
    _write_final_bundle(tmp_path, month, a, "/m1.pdf")
    # B 先以 draft 落盘（不会被纳入重建）
    b_path = pi.bundle_path(tmp_path, month, "pm2")
    pi.write_bundle(b_path, status="draft", month=month, pdf_path="/m2.pdf",
                    metadata_source="crossref-doi", segment=b,
                    close_reading_script=b.close_reading)

    orig_load = pi.load_bundle
    fired = {"n": 0}

    def _flip_during_collection(bf):
        if fired["n"] == 0:                     # 读 A 的瞬间，对方把 B 原地翻成 final
            fired["n"] = 1
            _write_final_bundle(tmp_path, month, b, "/m2.pdf")
        return orig_load(bf)

    monkeypatch.setattr(pi, "load_bundle", _flip_during_collection)
    r = M._rebuild_month(tmp_path, month, _FakeSettings())

    assert set(_bundle_names(tmp_path, month)) == {"pm1.paper.json", "pm2.paper.json"}, \
        "前提：路径集合自始至终没变，只有内容/mtime 变"
    assert r["papers"] == 2, "同路径 draft→final 翻转必须被重查发现并在下一轮收进来"
    md = (tmp_path / "科研札记_{}_手动精读.md".format(month)).read_text(encoding="utf-8")
    assert "Flipped To Final" in md


def _bundle_names(notes_dir, month):
    import os as _os
    return sorted(n for n in _os.listdir(notes_dir / "manual" / month)
                  if n.endswith(".paper.json"))


def test_concurrent_flag_and_finalize_exit_code_on_real_path(tmp_path, monkeypatch):
    """变异 M6/M7 存活补测：`out["concurrent"]=True` 与 cmd_finalize 的退出码此前
    只被「把 _rebuild_month 换成硬编码字典」的测试覆盖，真实路径上零覆盖。
    这里让并发**持续**发生（每轮都插），逼真实代码走到标记那一行。
    """
    from types import SimpleNamespace
    from src.scholar import notes as notes_mod
    month = "2026-08"
    a = _manual_seg("pc1", "Base Paper", "Zhang", "10.1/c1")
    bf_a = _write_final_bundle(tmp_path, month, a, "/c1.pdf")

    orig_write = notes_mod.write_notes
    n = {"i": 0}

    def _always_race(*args, **kw):
        out = orig_write(*args, **kw)
        n["i"] += 1                              # 每轮写完都再塞一篇 → 永远收敛不了
        extra = _manual_seg("px%d" % n["i"], "Racer %d" % n["i"], "Wang", "10.1/x%d" % n["i"])
        _write_final_bundle(tmp_path, month, extra, "/x%d.pdf" % n["i"])
        return out

    monkeypatch.setattr(notes_mod, "write_notes", _always_race)
    r = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert r.get("concurrent") is True, "持续并发下必须标 concurrent（真实路径）"
    assert not r.get("refused"), "已经写过盘就不能报成整月未改动"

    # 真实标记 → cmd_finalize 必须退非 0（agent 收到绿回执就不会重跑）
    class _Proc(_FakeProc):
        notes_dir = tmp_path
    class _Settings:
        processing = _Proc()
    monkeypatch.setattr(M, "_load_settings", lambda cfg: _Settings())
    monkeypatch.setattr(notes_mod, "write_notes", orig_write)   # 停止捣乱
    monkeypatch.setattr(M, "_rebuild_month", lambda *a, **k: dict(r))
    rc = M.cmd_finalize(SimpleNamespace(config="unused", bundle=str(bf_a),
                                        allow_removals=False))
    assert rc == 1


def test_lock_released_when_write_notes_raises(tmp_path, monkeypatch):
    """变异 M12 存活补测：原测试靠「连跑三次都成功」钉锁释放，但 CPython 引用计数在
    函数返回时关掉 fd、关 fd 即释放 flock，所以删掉 finally 也照样绿（恒真断言）。
    真正需要 finally 的是**异常路径**——这里显式持有锁对象、不让 GC 帮忙。
    """
    from src.scholar import notes as notes_mod
    month = "2026-08"
    seg = _manual_seg("pe1", "Boom", "Zhao", "10.1/e1")
    _write_final_bundle(tmp_path, month, seg, "/e1.pdf")

    def _boom(*a, **kw):
        raise RuntimeError("写盘炸了")

    # 必须**持有** _rebuild_month 内部造的那个锁对象：否则函数一返回，CPython 引用计数
    # 就把 fd 关了，而关 fd 即释放 flock——删掉 finally 也照样绿，断言恒真（变异实证）。
    real_lock_cls = M._RebuildLock
    made = []

    class _KeepAlive(real_lock_cls):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            made.append(self)

    monkeypatch.setattr(M, "_RebuildLock", _KeepAlive)
    monkeypatch.setattr(notes_mod, "write_notes", _boom)
    with pytest.raises(RuntimeError):
        M._rebuild_month(tmp_path, month, _FakeSettings())
    assert made, "前提：本轮确实构造过锁"

    monkeypatch.setattr(M, "_REBUILD_LOCK_TIMEOUT", 0.3)
    probe = real_lock_cls(tmp_path)
    try:
        assert probe.acquire(), "异常路径必须在 finally 里释放锁，否则整库重建被永久挡住"
    finally:
        probe.release()


def test_stat_before_load_detects_file_changed_while_being_read(tmp_path, monkeypatch):
    """变异 M4b 存活补测：stat 必须取在 load **之前**。取在之后的话，「我读它的时候
    它正被改写」会被记成「没变」——正是要发现的那件事被自己抹平。
    """
    from src.scholar import pdf_ingest as pi
    month = "2026-08"
    seg = _manual_seg("ps1", "Being Rewritten", "Sun", "10.1/s1")
    _write_final_bundle(tmp_path, month, seg, "/s1.pdf")

    orig_load = pi.load_bundle
    fired = {"n": 0}

    def _rewrite_while_reading(bf):
        data = orig_load(bf)
        if fired["n"] == 0:                      # 读完这一份的瞬间，对方把它改写了
            fired["n"] = 1
            _write_final_bundle(tmp_path, month, seg, "/s1.pdf",
                                cross_check_report={"verified_count": 7,
                                                    "corrected": [], "added": []})
        return data

    monkeypatch.setattr(pi, "load_bundle", _rewrite_while_reading)
    r = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert fired["n"] == 1
    assert r["papers"] == 1 and not r.get("refused"), "重来一轮后应正常收敛"


def test_concurrent_partial_write_is_retried_not_reported_as_broken_json(tmp_path,
                                                                        monkeypatch):
    """审计 F1：净删除闸必须排在提交前重查**之后**。

    另一会话正原地改写某份**已归档**论文的 bundle 时，本轮会读到半截 JSON → 记成
    broken → 闸门开火 → 直接 return，一轮都不重试，回执还教用户「去修一份根本没坏的
    JSON」。正确行为是先按并发重来；重来之后还坏才是真的坏。
    """
    from src.scholar import pdf_ingest as pi
    month = "2026-08"
    a = _manual_seg("pp1", "Stable Paper", "Zhang", "10.1/p1")
    b = _manual_seg("pp2", "Being Rewritten Paper", "Wang", "10.1/p2")
    _write_final_bundle(tmp_path, month, a, "/p1.pdf")
    _write_final_bundle(tmp_path, month, b, "/p2.pdf")
    M._rebuild_month(tmp_path, month, _FakeSettings())        # 两篇都已归档
    b_path = pi.bundle_path(tmp_path, month, "pp2")
    b_path.write_text('{"status": "final"', encoding="utf-8")  # 对方写到一半

    orig_load = pi.load_bundle
    done = {"v": False}

    def _finish_write_after_read(bf):
        try:
            return orig_load(bf)
        finally:
            if bf.name == b_path.name and not done["v"]:
                done["v"] = True                  # 对方把这一份写完了 → mtime 再变
                _write_final_bundle(tmp_path, month, b, "/p2.pdf")

    monkeypatch.setattr(pi, "load_bundle", _finish_write_after_read)
    r = M._rebuild_month(tmp_path, month, _FakeSettings())

    assert not r.get("refused"), "并发半截 JSON 不该被当成「你的 JSON 坏了」直接拒绝"
    assert r["papers"] == 2 and not r.get("broken_bundles")


def test_gate_after_a_successful_write_does_not_claim_month_untouched(tmp_path,
                                                                     monkeypatch):
    """审计/压测发现的**回归**：闸门被搬进重试循环后，第 2 轮才触发的闸会否定第 1 轮
    已经落的盘——回执谎称「整月一字未动」，而 refused 分支不刷 write_outputs，
    于是月度 md/sidecar 有新论文、全局索引没有，**持久不一致**。

    正确行为：已写盘那一轮是数据完整的，收下它走正常收尾，broken 照报、退出码照样非 0。
    """
    from src.scholar import pdf_ingest as pi
    from src.scholar import notes as notes_mod
    month = "2026-08"
    a = _manual_seg("pg1", "Archived A", "Zhang", "10.1/g1")
    b = _manual_seg("pg2", "Archived B", "Wang", "10.1/g2")
    c = _manual_seg("pg3", "Newly Added C", "Li", "10.1/g3")
    _write_final_bundle(tmp_path, month, a, "/g1.pdf")
    _write_final_bundle(tmp_path, month, b, "/g2.pdf")
    M._rebuild_month(tmp_path, month, _FakeSettings())         # A、B 已归档
    _write_final_bundle(tmp_path, month, c, "/g3.pdf")         # agent 新写 C

    orig_write = notes_mod.write_notes
    fired = {"n": 0}

    def _break_b_during_write(*args, **kw):
        out = orig_write(*args, **kw)                          # 第 1 轮写盘：A+B+C 都进去了
        if fired["n"] == 0:
            fired["n"] = 1
            pi.bundle_path(tmp_path, month, "pg2").write_text(
                '{"status": "final"', encoding="utf-8")        # 写盘期间 B 的 JSON 被改坏
        return out

    monkeypatch.setattr(notes_mod, "write_notes", _break_b_during_write)
    r = M._rebuild_month(tmp_path, month, _FakeSettings())

    assert not r.get("refused"), "第 1 轮已写盘，不能报成「整月一字未动」"
    assert r["broken_bundles"], "B 的 JSON 确实坏了，必须报出来让人去修"
    md = (tmp_path / "科研札记_{}_手动精读.md".format(month)).read_text(encoding="utf-8")
    idx = json.loads((tmp_path / "literature_index.json").read_text(encoding="utf-8"))
    titles = {p.get("title") for p in idx["papers"]}
    assert "Newly Added C" in md, "第 1 轮写进 md 的内容是完整的，不该回滚"
    assert "Newly Added C" in titles, "全局索引必须与月度 md 一致（refused 分支曾漏刷）"


def test_directory_named_like_a_bundle_does_not_wedge_the_month(tmp_path):
    """压测发现：`<x>.paper.json` 若是**目录**，采集侧 glob 收得到、重查侧 scandir
    的 is_file() 收不到 → 指纹恒不相等 → 三轮全判并发 → 该月**永久**归档不进，
    回执还教人「稍后重跑」（永远不会奏效）。两侧口径必须一致。
    """
    month = "2026-08"
    seg = _manual_seg("pd1", "Real Paper", "Qian", "10.1/d1")
    _write_final_bundle(tmp_path, month, seg, "/d1.pdf")
    (tmp_path / "manual" / month / "trap.paper.json").mkdir()   # 同名目录陷阱

    r = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert not r.get("refused"), "同名目录不该把整月卡成永久 refused"
    assert r["papers"] == 1
    md = (tmp_path / "科研札记_{}_手动精读.md".format(month)).read_text(encoding="utf-8")
    assert "Real Paper" in md


def test_report_final_keeps_broken_headline_when_also_concurrent(tmp_path, capsys):
    """审计 F7：两者并存时首行必须是 ⛔（有 bundle 没入库，要人去修），
    不能被 ⚠️ 并发降级；但并发提示本身也不能丢。
    """
    M._report_final({"month": "2026-08", "papers": 2, "concurrent": True,
                     "skipped_drafts": [], "broken_bundles": ["x.paper.json"],
                     "index": {"papers": [], "citekey_collisions": []}}, tmp_path)
    out = capsys.readouterr().out
    headline = next(ln for ln in out.splitlines() if "手动精读归档" in ln)
    assert headline.startswith("⛔"), "两者并存时首行必须是 ⛔（要人去修 JSON），不能被 ⚠️ 降级"
    assert "有 bundle 未入库" in out and "并发归档" in out


# ---------------- R7：第 2 轮对抗审计/压测的回归 ----------------

def test_non_regular_bundle_file_cannot_silently_drop_an_archived_paper(tmp_path):
    """第 2 轮审计抓到的**回归**：给采集侧加 `is_file()` 过滤后，非普通文件
    （同名目录 / 悬空软链）会从 consumed、disk、broken 三处同时消失 → 净删除闸失明
    → 那篇已归档论文在**绿回执 + exit 0** 下被整篇重写抹掉。必须记进 broken。
    """
    from src.scholar import pdf_ingest as pi
    month = "2026-08"
    a = _manual_seg("pn1", "Keeps Living", "Zhang", "10.1/n1")
    b = _manual_seg("pn2", "Must Not Vanish", "Wang", "10.1/n2")
    _write_final_bundle(tmp_path, month, a, "/n1.pdf")
    b_path = _write_final_bundle(tmp_path, month, b, "/n2.pdf")
    M._rebuild_month(tmp_path, month, _FakeSettings())      # A、B 都已归档

    b_path.unlink()
    b_path.symlink_to(tmp_path / "does_not_exist.json")     # 悬空软链：既非普通文件也读不出

    r = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert r.get("refused") is True, "已归档论文的 bundle 变成非普通文件 → 闸必须开火"
    assert any("pn2" in n for n in r["broken_bundles"]), "非普通文件必须报进 broken"
    md = (tmp_path / "科研札记_{}_手动精读.md".format(month)).read_text(encoding="utf-8")
    assert "Must Not Vanish" in md, "整月必须一字未动"


def test_unverified_broken_is_not_named_as_something_to_fix(tmp_path, monkeypatch, capsys):
    """第 2 轮审计：在重查（一）处夭折的那一轮，其 broken 多半是「对方写到一半」的半截
    JSON。把它当「你的 JSON 坏了」点名，就是教人去修一份此刻已经好了的文件。
    """
    from src.scholar import pdf_ingest as pi
    month = "2026-08"
    a = _manual_seg("pu1", "Anchor", "Zhang", "10.1/u1")
    b = _manual_seg("pu2", "Half Written", "Wang", "10.1/u2")
    _write_final_bundle(tmp_path, month, a, "/u1.pdf")
    b_path = pi.bundle_path(tmp_path, month, "pu2")

    orig_load = pi.load_bundle

    def _always_half_then_finish(bf):
        # 每一轮：读到 B 时它是半截的，读完立刻被写完 → 指纹变 → 重查夭折 → 下一轮同理
        if bf.name == b_path.name:
            try:
                return orig_load(bf)
            finally:
                _write_final_bundle(tmp_path, month, b, "/u2.pdf")
        out = orig_load(bf)
        b_path.write_text('{"status": "final"', encoding="utf-8")
        return out

    monkeypatch.setattr(pi, "load_bundle", _always_half_then_finish)
    r = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert not r.get("broken_bundles"), "未经重查证实的 broken 不许进正式清单"
    if r.get("unverified_broken"):
        M._report_final(r, tmp_path)
        out = capsys.readouterr().out
        assert "先别改" in out, "要明说这份清单不可信，别教人去修好文件"


def test_stale_index_does_not_trigger_sync_that_would_delete_peer_vectors(tmp_path,
                                                                          monkeypatch):
    """第 2 轮压测抓到的新窗口：把 best-effort 同步移到锁外后，A 释放锁→同步之间，
    B 可能已经重建并刷新了全局索引。A 手上的 idx 是**陈旧**的，拿它 sync_store 会把
    B 刚嵌入的 chunk 当成"库里多出来的"删掉（向量库的 0.5 骤缩闸拦不住，只少一篇）。
    指纹变了就必须跳过本轮同步。
    """
    month = "2026-08"
    seg = _manual_seg("pv1", "Vector Paper", "Zhao", "10.1/v1")
    _write_final_bundle(tmp_path, month, seg, "/v1.pdf")

    synced = []
    monkeypatch.setattr(M, "_sync_embedding_best_effort",
                        lambda nd, idx, st: synced.append(idx))

    orig_write_outputs = M.__dict__.get("write_outputs")   # 模块内是函数内 import，改不到
    real_update = None

    # 在锁内写完索引之后、锁外同步之前，模拟"另一轮重建刷新了全局索引"
    orig_stat_fp = M._stat_fp
    calls = {"n": 0}

    def _fp_changes_after_lock(path):
        calls["n"] += 1
        # 第 1 次（锁内记指纹）给真值，第 2 次（锁外比对）给一个不同的值 = 别人改过了
        return orig_stat_fp(path) if calls["n"] == 1 else (12345, 999)

    monkeypatch.setattr(M, "_stat_fp", _fp_changes_after_lock)
    r = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert r.get("sync_skipped") is True, "索引已被别人刷新时必须跳过同步"
    assert not synced, "拿陈旧 idx 同步会删掉并发方刚嵌入的向量"


def test_best_effort_syncs_run_outside_the_rebuild_lock(tmp_path, monkeypatch):
    """变异 M4 补测：两个 best-effort 同步必须在**锁外**跑。topics 合成是 timeout 2400s
    的子进程，而等锁上限只有 180s——放锁内的话一次慢合成能让全库 finalize 撞
    refused/locked 半小时（压测实证）。
    """
    month = "2026-08"
    seg = _manual_seg("pk1", "Outside Lock", "Sun", "10.1/k1")
    _write_final_bundle(tmp_path, month, seg, "/k1.pdf")

    seen = {}

    def _probe_lock(nd, idx, st):
        probe = M._RebuildLock(tmp_path)
        seen["free"] = probe.acquire()      # 同步进行时，锁必须已经放开
        if seen["free"]:
            probe.release()

    monkeypatch.setattr(M, "_REBUILD_LOCK_TIMEOUT", 0.3)
    monkeypatch.setattr(M, "_sync_embedding_best_effort", _probe_lock)
    M._rebuild_month(tmp_path, month, _FakeSettings())
    assert seen.get("free") is True, "同步跑在锁内 → 慢同步会把全库重建挡在门外"


def test_successful_heal_reports_green_not_concurrent(tmp_path, monkeypatch):
    """变异 M6 补测：并发被自愈收敛之后必须是**绿回执**（unstable 要复位），
    否则"自愈而非拒绝"的收益在回执上看不出来，用户照样以为要重跑。
    """
    from src.scholar import pdf_ingest as pi
    month = "2026-08"
    a = _manual_seg("ph1", "First", "Zhang", "10.1/h1")
    b = _manual_seg("ph2", "Arrived During Collection", "Wang", "10.1/h2")
    _write_final_bundle(tmp_path, month, a, "/h1.pdf")

    orig_load = pi.load_bundle
    fired = {"n": 0}

    def _race_once(bf):
        if fired["n"] == 0:
            fired["n"] = 1
            _write_final_bundle(tmp_path, month, b, "/h2.pdf")
        return orig_load(bf)

    monkeypatch.setattr(pi, "load_bundle", _race_once)
    r = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert r["papers"] == 2
    assert not r.get("concurrent"), "收敛之后 unstable 必须复位，回执要是绿的"
    assert not r.get("refused")


def test_papers_count_matches_what_is_actually_in_the_md(tmp_path, monkeypatch):
    """变异 M5 补测：papers 必须是**写盘那一轮**的篇数（描述 md 里实际有几篇），
    不是循环结束时的 segments（那可能是后来某轮读到的、并没写进 md 的数字）。
    """
    from src.scholar import pdf_ingest as pi
    from src.scholar import notes as notes_mod
    month = "2026-08"
    a = _manual_seg("pq1", "A", "Zhang", "10.1/q1")
    b = _manual_seg("pq2", "B", "Wang", "10.1/q2")
    c = _manual_seg("pq3", "C", "Li", "10.1/q3")
    for s, pth in ((a, "/q1.pdf"), (b, "/q2.pdf"), (c, "/q3.pdf")):
        _write_final_bundle(tmp_path, month, s, pth)
    M._rebuild_month(tmp_path, month, _FakeSettings())

    orig_write = notes_mod.write_notes
    fired = {"n": 0}

    def _break_one_during_write(*args, **kw):
        out = orig_write(*args, **kw)          # 这一轮把 3 篇都写进了 md
        if fired["n"] == 0:
            fired["n"] = 1
            pi.bundle_path(tmp_path, month, "pq2").write_text('{"x"', encoding="utf-8")
        return out

    monkeypatch.setattr(notes_mod, "write_notes", _break_one_during_write)
    r = M._rebuild_month(tmp_path, month, _FakeSettings())
    md = (tmp_path / "科研札记_{}_手动精读.md".format(month)).read_text(encoding="utf-8")
    n_in_md = sum(1 for ln in md.splitlines() if ln.startswith("## ") and "[@" in ln)
    assert r["papers"] == n_in_md == 3, "回执篇数必须等于 md 里实际的篇数"


def test_receipt_after_a_write_never_names_an_unverified_broken_file(tmp_path, monkeypatch):
    """正常收尾路径上的同一件事（变异实证此前无保护）：写过盘之后，末轮在重查（一）
    处夭折，那轮的 broken 是「对方写到一半」的半截 JSON——回执必须用**上一次经重查
    证实**的清单，而不是末轮那份。
    """
    from src.scholar import pdf_ingest as pi
    from src.scholar import notes as notes_mod
    month = "2026-08"
    a = _manual_seg("pz1", "Written OK", "Zhang", "10.1/z1")
    b = _manual_seg("pz2", "Half Written Peer", "Wang", "10.1/z2")
    _write_final_bundle(tmp_path, month, a, "/z1.pdf")
    b_path = pi.bundle_path(tmp_path, month, "pz2")

    orig_write = notes_mod.write_notes

    def _spawn_half_b(*args, **kw):
        out = orig_write(*args, **kw)          # 第 1 轮 A 正常写盘 → verified = 空清单
        b_path.write_text('{"status": "final"', encoding="utf-8")   # 写盘期间冒出半截 B
        return out

    orig_load = pi.load_bundle

    def _b_keeps_flapping(bf):
        if bf.name == b_path.name:
            try:
                return orig_load(bf)           # 半截 → 进 broken
            finally:
                _write_final_bundle(tmp_path, month, b, "/z2.pdf")   # 读完即被写完 → 指纹变
        out = orig_load(bf)
        if b_path.exists():
            b_path.write_text('{"status": "final"', encoding="utf-8")  # 下一轮又是半截
        return out

    monkeypatch.setattr(notes_mod, "write_notes", _spawn_half_b)
    monkeypatch.setattr(pi, "load_bundle", _b_keeps_flapping)
    r = M._rebuild_month(tmp_path, month, _FakeSettings())

    assert r.get("concurrent") is True, "写过盘但始终没收敛 → 该标 concurrent"
    assert not r.get("broken_bundles"), \
        "末轮未通过重查，它的 broken 不可信，不许进正式清单（会教人去修好文件）"


# ---------------- R8：第 3 轮对抗审计/压测的回归 ----------------

def test_symlink_loop_bundle_does_not_wedge_the_month(tmp_path):
    """第 3 轮压测抓到的实质缺陷：`DirEntry.is_file()` 撞符号链接环会**抛** ELOOP，
    该判断若在 per-entry 的 try 之外，整份 scandir 会从那一条起静默截断 →
    与采集侧口径分叉 → 指纹恒不等 → 该月**永久**卡死，回执还谎称"另有会话在归档"。
    """
    month = "2026-08"
    seg = _manual_seg("psl", "Survives The Loop", "Zhang", "10.1/sl")
    _write_final_bundle(tmp_path, month, seg, "/sl.pdf")
    mdir = tmp_path / "manual" / month
    a, b = mdir / "loop_a.paper.json", mdir / "loop_b.paper.json"
    a.symlink_to(b)
    b.symlink_to(a)                                   # 互指的符号链接环

    inv = M._bundle_inventory(mdir, ".paper.json")
    assert "psl.paper.json" in inv, "链接环不得让整份目录列表静默截断"

    r = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert not r.get("refused"), "链接环不该把整月卡成永久 refused"
    assert r["papers"] == 1
    assert any("loop_" in n for n in r["broken_bundles"]), "环条目应按「读不出」报出来"


def test_no_final_bundle_month_retries_instead_of_naming_a_half_written_file(tmp_path,
                                                                             monkeypatch):
    """变异 F1 补测（最值钱的一条）：早退若排在重查**之前**，「本月一份 final 都没有」
    这个结论本身就可能来自陈旧列表——变异后实测是 `papers=0` + ⛔ 点名一份完好文件，
    以及「前一轮已写盘后早退 → 绿回执 + exit 0 而 md 里有货」。
    """
    from src.scholar import pdf_ingest as pi
    month = "2026-08"
    seg = _manual_seg("pnf", "Becomes Final", "Wang", "10.1/nf")
    b_path = pi.bundle_path(tmp_path, month, "pnf")
    pi.write_bundle(b_path, status="draft", month=month, pdf_path="/nf.pdf",
                    metadata_source="crossref-doi", segment=seg,
                    close_reading_script=seg.close_reading)

    orig_load = pi.load_bundle
    fired = {"n": 0}

    def _flip_to_final_during_collection(bf):
        out = orig_load(bf)
        if fired["n"] == 0:                 # 读它的瞬间，对方把 draft 写成了 final
            fired["n"] = 1
            _write_final_bundle(tmp_path, month, seg, "/nf.pdf")
        return out

    monkeypatch.setattr(pi, "load_bundle", _flip_to_final_during_collection)
    r = M._rebuild_month(tmp_path, month, _FakeSettings())
    assert fired["n"] == 1
    assert r["papers"] == 1, "重查必须先于早退：否则这篇会被判成「本月没有 final」"
    assert not r.get("broken_bundles"), "不该点名一份完好文件"


def test_early_exit_after_a_write_keeps_papers_and_md(tmp_path, monkeypatch):
    """变异 F2 补测：早退分支必须沿用**写盘那一轮**的 papers 与 md。
    报 0 篇而 md 里有货 = 回执与磁盘直接矛盾，且 out["md"] 缺失会让锁外 topics 同步被跳过。
    """
    from src.scholar import pdf_ingest as pi
    from src.scholar import notes as notes_mod
    month = "2026-08"
    seg = _manual_seg("pea", "Solo Paper", "Li", "10.1/ea")
    bf = _write_final_bundle(tmp_path, month, seg, "/ea.pdf")

    orig_write = notes_mod.write_notes
    fired = {"n": 0}

    def _demote_to_draft_after_write(*args, **kw):
        out = orig_write(*args, **kw)          # 第 1 轮：Solo 已写进 md
        if fired["n"] == 0:
            fired["n"] = 1                     # 写盘期间对方把它翻回 draft → 下一轮无 final
            pi.write_bundle(bf, status="draft", month=month, pdf_path="/ea.pdf",
                            metadata_source="crossref-doi", segment=seg,
                            close_reading_script=seg.close_reading)
        return out

    monkeypatch.setattr(notes_mod, "write_notes", _demote_to_draft_after_write)
    r = M._rebuild_month(tmp_path, month, _FakeSettings())
    md = (tmp_path / "科研札记_{}_手动精读.md".format(month)).read_text(encoding="utf-8")
    assert "Solo Paper" in md, "前提：第 1 轮确实写进了 md"
    assert r["papers"] == 1, "早退不能报 0 篇——md 里有货"
    assert r.get("md"), "早退必须带 md 键，否则锁外的 topics 同步被跳过"


def test_fifo_lock_file_degrades_instead_of_hanging(tmp_path):
    """变异 F3/F4 补测：锁文件是 FIFO 时 `open(..., "w")` 会**无限阻塞**，
    而 timeout 只包 flock 等待循环、包不住 open 本身（压测抓到过挂死栈）。
    必须 fail-open 并把降级如实标进结果。
    """
    import os as _os
    import threading
    month = "2026-08"
    seg = _manual_seg("pfi", "Fifo Guarded", "Sun", "10.1/fi")
    _write_final_bundle(tmp_path, month, seg, "/fi.pdf")
    _os.mkfifo(str(tmp_path / ".rebuild.lock"))       # 无读端的 FIFO

    box = {}

    def _run():
        box["r"] = M._rebuild_month(tmp_path, month, _FakeSettings())

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(timeout=20)
    assert not th.is_alive(), "FIFO 锁文件不得让 finalize 永久挂死"
    assert box["r"]["papers"] == 1
    assert box["r"].get("unlocked") is True, "fail-open 必须如实标出来（回执要说本轮没加锁）"


def test_receipt_truncates_huge_broken_list(tmp_path, capsys):
    """1000 份坏 bundle 会把回执打成单行两万字符——agent 会话里是纯上下文污染。"""
    M._report_final({"month": "2026-08", "papers": 0,
                     "skipped_drafts": [], "index": {"papers": [], "citekey_collisions": []},
                     "broken_bundles": ["b%03d.paper.json" % i for i in range(1000)]},
                    tmp_path)
    out = capsys.readouterr().out
    assert max(len(ln) for ln in out.splitlines()) < 500, "回执单行不得爆炸"
    assert "等共 1000 份" in out


# ---------------- 台账批（2026-09-04）：元数据退化回执 / 草稿五态 ----------------
# 上面 test_thin_metadata_predicate 等已覆盖 _print_attention；本节补 ingest_pdf 侧的状态判定，
# 放在本文件是因为 _stub_ingest_env 在 test_pdf_ingest 里，这里用独立的最小打桩。
