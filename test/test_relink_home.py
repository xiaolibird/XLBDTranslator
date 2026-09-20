# -*- coding: utf-8 -*-
"""relink_manual_pdfs 的归位模式（--home）。

背景：read_pdf ingest 只把 PDF 的绝对路径写进 bundle、不管文件在哪。2026-09-20 盘点
333 篇手动精读，31 篇的 PDF 躺在 Downloads / 会话临时目录，4 篇已随清理丢失成死链。
归位模式由 finalize 收尾自动调用，这里锁住它的四条约定：
  · Reading 之外的 PDF 搬进 Reading/<目录>/<citekey>.pdf 并回填 pdf_path，其余键一字不动；
  · 已在 Reading 下的不挪、不改名（那是用户按主题手工摆好的）；
  · 目录规则：--reading-dir > 带日期的桶名 > bundle 修改日期；
  · 死链只标记、不拦同月其余篇目。
"""
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _load():
    spec = importlib.util.spec_from_file_location("relink_cli", REPO / "scripts" / "relink_manual_pdfs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RL = _load()


def _mk(tmp_path, month, papers):
    """papers: [(bundle 名, doi, 标题, citekey, pdf 路径 or None)]"""
    notes = tmp_path / "notes"
    bdir = notes / "manual" / month
    bdir.mkdir(parents=True)
    entries = []
    for name, doi, title, ck, pdf in papers:
        b = {"status": "final", "month": month, "paper_id": name, "pdf_path": str(pdf or ""),
             "n_pages": 3, "one_line": "保持原样",
             "segment": {"metadata": {"doi": doi, "arxiv_id": None, "title": title}}}
        (bdir / (name + ".paper.json")).write_text(json.dumps(b, ensure_ascii=False, indent=2), "utf-8")
        entries.append({"series": "manual", "month": month, "doi": doi, "arxiv_id": None,
                        "title": title, "citekey": ck})
    (notes / RL.INDEX_JSON).write_text(json.dumps({"papers": entries}, ensure_ascii=False), "utf-8")
    return notes, bdir


@pytest.fixture
def roots(tmp_path, monkeypatch):
    reading = tmp_path / "Reading"
    reading.mkdir()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    monkeypatch.setenv("SCHOLAR_READING_ROOT", str(reading))
    return reading, downloads


def test_home_moves_outside_pdf_and_only_touches_pdf_path(tmp_path, roots):
    reading, downloads = roots
    src = downloads / "1-s2.0-S0950705125016405-main.pdf"
    src.write_bytes(b"%PDF-fake")
    notes, bdir = _mk(tmp_path, "2026-09", [("aaa", "10.1/x", "Paper X", "shi2025Federated", src)])
    before = json.loads((bdir / "aaa.paper.json").read_text("utf-8"))

    plan = RL.build_plan("2026-09", None, notes, 0, home=True)
    assert [p["citekey"] for p in plan] == ["shi2025Federated"]
    assert RL.apply_plan(plan) == 1

    day = date.fromtimestamp((bdir / "aaa.paper.json").stat().st_mtime).isoformat()
    dst = reading / day / "shi2025Federated.pdf"
    assert dst.read_bytes() == b"%PDF-fake" and not src.exists()
    after = json.loads((bdir / "aaa.paper.json").read_text("utf-8"))
    assert after["pdf_path"] == str(dst)
    assert {k for k in after if after[k] != before.get(k)} == {"pdf_path"}


def test_home_leaves_pdfs_already_under_reading_alone(tmp_path, roots):
    reading, _ = roots
    themed = reading / "2026-09-03-景观地基"
    themed.mkdir()
    kept = themed / "lavenant2024_trajectory_inference_theory.pdf"   # 非 citekey 名，也不许改
    kept.write_bytes(b"%PDF")
    notes, _ = _mk(tmp_path, "2026-09", [("bbb", "10.1/y", "Paper Y", "lavenant2024Toward", kept)])
    assert RL.build_plan("2026-09", None, notes, 0, home=True) == []
    assert kept.exists()


def test_home_dir_rule_priority(tmp_path, roots):
    reading, downloads = roots
    a = downloads / "a.pdf"
    a.write_bytes(b"1")
    notes, _ = _mk(tmp_path, "2026-09-11-异配图GNN", [("ccc", "10.1/z", "Paper Z", "xu2024Multi", a)])
    p = RL.build_plan("2026-09-11-异配图GNN", None, notes, 0, home=True)[0]
    assert p["dst"] == reading / "2026-09-11-异配图GNN" / "xu2024Multi.pdf"      # 带日期的桶名
    p = RL.build_plan("2026-09-11-异配图GNN", None, notes, 0, home=True, reading_dir="2026-09-20-SM源头")[0]
    assert p["dst"].parent == reading / "2026-09-20-SM源头"                       # 显式指定优先


def test_home_dead_link_is_flagged_not_fatal(tmp_path, roots):
    reading, downloads = roots
    ok = downloads / "ok.pdf"
    ok.write_bytes(b"1")
    gone = Path("/private/tmp/nonexistent-session/scratchpad/agniel2018_bmj.pdf")
    notes, bdir = _mk(tmp_path, "2026-08-16-x", [
        ("d1", "10.1/ok", "Paper OK", "ok2020Paper", ok),
        ("d2", "10.1/gone", "Paper Gone", "agniel2018Biases", gone)])
    plan = RL.build_plan("2026-08-16-x", None, notes, 0, home=True)
    assert {p["citekey"]: bool(p.get("dead")) for p in plan} == {"ok2020Paper": False, "agniel2018Biases": True}
    assert RL.apply_plan(plan) == 1
    assert (reading / "2026-08-16-x" / "ok2020Paper.pdf").exists()
    assert json.loads((bdir / "d2.paper.json").read_text("utf-8"))["pdf_path"] == str(gone)   # 死链原样保留


def test_home_refuses_to_overwrite_existing_target(tmp_path, roots):
    reading, downloads = roots
    src = downloads / "dup.pdf"
    src.write_bytes(b"new")
    (reading / "2026-09-05-t").mkdir()
    (reading / "2026-09-05-t" / "k2020Dup.pdf").write_bytes(b"old")
    notes, _ = _mk(tmp_path, "2026-09-05-t", [("e1", "10.1/d", "Paper D", "k2020Dup", src)])
    with pytest.raises(RL.PlanError):
        RL.build_plan("2026-09-05-t", None, notes, 0, home=True)
    assert src.read_bytes() == b"new"


def test_non_home_mode_still_aborts_on_missing_source(tmp_path, roots):
    _, downloads = roots
    notes, _ = _mk(tmp_path, "2026-07-28-TFM", [("f1", "10.1/m", "Paper M", "m2020Paper", downloads / "nope.pdf")])
    with pytest.raises(RL.PlanError):
        RL.build_plan("2026-07-28-TFM", downloads, notes, 0)


# ---------------- finalize 收尾的接线 ----------------

def _load_read_pdf():
    spec = importlib.util.spec_from_file_location("read_pdf_cli_home", REPO / "scripts" / "read_pdf.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_finalize_hook_is_inert_on_non_real_notes_dir(tmp_path, monkeypatch, capsys):
    """临时 notes 目录 + 未设 SCHOLAR_READING_ROOT：一个字节都不许动（防测试数据进真实 Reading）。"""
    monkeypatch.delenv("SCHOLAR_READING_ROOT", raising=False)
    src = tmp_path / "Downloads" / "p.pdf"
    src.parent.mkdir()
    src.write_bytes(b"1")
    notes, bdir = _mk(tmp_path, "2026-09", [("g1", "10.1/g", "Paper G", "g2020Paper", src)])
    _load_read_pdf()._home_pdfs_best_effort(notes, "2026-09")
    assert src.exists() and capsys.readouterr().out == ""
    assert json.loads((bdir / "g1.paper.json").read_text("utf-8"))["pdf_path"] == str(src)


def test_finalize_hook_homes_and_never_raises(tmp_path, roots, capsys):
    reading, downloads = roots
    src = downloads / "p.pdf"
    src.write_bytes(b"1")
    notes, bdir = _mk(tmp_path, "2026-09-20-SM源头", [("h1", "10.1/h", "Paper H", "mitra2023Learning", src)])
    M = _load_read_pdf()
    M._home_pdfs_best_effort(notes, "2026-09-20-SM源头")
    assert (reading / "2026-09-20-SM源头" / "mitra2023Learning.pdf").exists()
    assert "PDF 已归位" in capsys.readouterr().out
    (notes / RL.INDEX_JSON).unlink()                 # 索引没了：只告警，不抛
    M._home_pdfs_best_effort(notes, "2026-09-20-SM源头")
    assert "不影响归档" in capsys.readouterr().out
