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


def _write_final_bundle(notes_dir, month, seg, pdf_path):
    from src.scholar import pdf_ingest as pi
    bf = pi.bundle_path(notes_dir, month, seg.paper_id)
    pi.write_bundle(bf, status="final", month=month, pdf_path=pdf_path,
                    metadata_source="crossref-doi", segment=seg,
                    close_reading_script=seg.close_reading,
                    close_reading_final=seg.close_reading.model_dump(mode="json"))
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
