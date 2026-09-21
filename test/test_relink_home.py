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
    other = downloads / "fine.pdf"
    other.write_bytes(b"fine")
    notes, bdir = _mk(tmp_path, "2026-09-05-t", [("e1", "10.1/d", "Paper D", "k2020Dup", src),
                                                 ("e2", "10.1/e", "Paper E", "k2020Fine", other)])
    plan = RL.build_plan("2026-09-05-t", None, notes, 0, home=True)
    RL.apply_plan(plan)
    # 绝不覆盖：撞名那篇两份文件都原样、bundle 不改；但它不再拖累同月其余篇目（此前整月 abort，
    # 且每次 finalize 都重复失败——对抗审核 F9-B）
    assert src.read_bytes() == b"new" and (reading / "2026-09-05-t" / "k2020Dup.pdf").read_bytes() == b"old"
    assert json.loads((bdir / "e1.paper.json").read_text("utf-8"))["pdf_path"] == str(src)
    assert (reading / "2026-09-05-t" / "k2020Fine.pdf").read_bytes() == b"fine" and not other.exists()


def test_home_identical_copy_already_in_reading_only_backfills(tmp_path, roots):
    reading, downloads = roots
    src = downloads / "dl.pdf"
    src.write_bytes(b"same bytes")
    (reading / "2026-09-05-t").mkdir()
    (reading / "2026-09-05-t" / "k2020Same.pdf").write_bytes(b"same bytes")
    notes, bdir = _mk(tmp_path, "2026-09-05-t", [("e1", "10.1/s", "Paper S", "k2020Same", src)])
    RL.apply_plan(RL.build_plan("2026-09-05-t", None, notes, 0, home=True))
    assert json.loads((bdir / "e1.paper.json").read_text("utf-8"))["pdf_path"].endswith("k2020Same.pdf")
    assert src.exists()                                   # 源文件要不要清由用户定，脚本不替人删


def test_home_copies_instead_of_moving_out_of_protected_trees_and_logs_every_transfer(tmp_path, roots, monkeypatch):
    """对抗审核 F2：locate_pdf 返回的 Zotero storage 路径拿去 ingest→finalize，附件会被搬出 Zotero。"""
    reading, downloads = roots
    zotero = tmp_path / "Zotero" / "storage" / "ABCD1234"
    zotero.mkdir(parents=True)
    monkeypatch.setenv("SCHOLAR_KEEP_ROOTS", str(tmp_path / "Zotero"))
    att = zotero / "Smith - 2020 - Paper.pdf"
    att.write_bytes(b"zotero attachment")
    loose = downloads / "loose.pdf"
    loose.write_bytes(b"loose")
    notes, bdir = _mk(tmp_path, "2026-09-05-t", [("e1", "10.1/z", "Paper Z", "smith2020Paper", att),
                                                 ("e2", "10.1/l", "Paper L", "k2020Loose", loose)])
    RL.apply_plan(RL.build_plan("2026-09-05-t", None, notes, 0, home=True))
    assert att.read_bytes() == b"zotero attachment"       # Zotero 里的原件还在
    assert (reading / "2026-09-05-t" / "smith2020Paper.pdf").read_bytes() == b"zotero attachment"
    assert not loose.exists()                             # 普通散件照旧是搬
    log = [json.loads(l) for l in (notes / "_archive" / "pdf_moves.jsonl").read_text("utf-8").splitlines()]
    assert sorted((r["op"], r["citekey"]) for r in log) == [("copy", "smith2020Paper"), ("move", "k2020Loose")]


def test_home_two_bundles_sharing_one_source_pdf_both_end_up_with_a_file(tmp_path, roots):
    reading, downloads = roots
    src = downloads / "shared.pdf"
    src.write_bytes(b"shared")
    notes, bdir = _mk(tmp_path, "2026-09-05-t", [("e1", "10.1/a", "Paper A", "k2020A", src),
                                                 ("e2", "10.1/b", "Paper B", "k2020B", src)])
    RL.apply_plan(RL.build_plan("2026-09-05-t", None, notes, 0, home=True))
    for name, ck in (("e1", "k2020A"), ("e2", "k2020B")):
        p = Path(json.loads((bdir / (name + ".paper.json")).read_text("utf-8"))["pdf_path"])
        assert p.name == ck + ".pdf" and p.read_bytes() == b"shared"


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


def test_dead_link_does_not_silently_repoint_to_a_different_pdf_with_the_same_name(tmp_path, roots):
    """F9-C：死链 + 目标位恰好有个同 citekey 的**别的** PDF（同一篇的另一版本），此前被当成
    「上次搬完没回填」而把 bundle 静默改指过去。"""
    import fitz
    reading, downloads = roots
    d = reading / "2026-09-05-t"
    d.mkdir()
    doc = fitz.open()
    for _ in range(9):                                      # 目标位那份 9 页
        doc.new_page()
    doc.save(str(d / "k2020Dup.pdf")); doc.close()
    notes, bdir = _mk(tmp_path, "2026-09-05-t", [("e1", "10.1/d", "Paper D", "k2020Dup", downloads / "gone.pdf")])
    b = json.loads((bdir / "e1.paper.json").read_text("utf-8"))
    b["n_pages"] = 3                                        # bundle 记的是 3 页 → 对不上
    (bdir / "e1.paper.json").write_text(json.dumps(b, ensure_ascii=False, indent=2), "utf-8")
    plan = RL.build_plan("2026-09-05-t", None, notes, 0, home=True)
    assert plan[0].get("dead") and not plan[0]["backfill_only"]
    RL.apply_plan(plan)
    assert json.loads((bdir / "e1.paper.json").read_text("utf-8"))["pdf_path"].endswith("gone.pdf")


def test_half_finished_move_self_heals_even_when_the_bucket_dir_would_be_recomputed(tmp_path, roots):
    """F9-D：纯月份桶的目标目录按 bundle mtime 推；bundle 被别的流程重写过之后，重跑推出来的是
    当天的目录，而 PDF 躺在当初那个日期目录里——此前自愈失效、判成死链。"""
    reading, downloads = roots
    old_dir = reading / "2026-09-18"
    old_dir.mkdir()
    (old_dir / "k2020Moved.pdf").write_bytes(b"pdf")
    notes, bdir = _mk(tmp_path, "2026-09", [("e1", "10.1/m", "Paper M", "k2020Moved", downloads / "gone.pdf")])
    plan = RL.build_plan("2026-09", None, notes, 0, home=True)
    assert plan[0]["backfill_only"] and plan[0]["dst"] == old_dir / "k2020Moved.pdf"
    RL.apply_plan(plan)
    assert json.loads((bdir / "e1.paper.json").read_text("utf-8"))["pdf_path"].endswith("2026-09-18/k2020Moved.pdf")
