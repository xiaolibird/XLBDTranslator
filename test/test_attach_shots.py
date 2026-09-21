# -*- coding: utf-8 -*-
"""给已归档 md 补原文截图（notes.attach_shots_to_md）。

自动月度/周札记的 digest JSON 不存精读分节，证据句只在 md 里——所以不能像手动精读那样整月
write_notes 重建，只能在已有 md 上「找到每一句、把图片行插回它下面」。风险在于它成了第三条渲染
通路：这里最要紧的一条断言是**等价性**——对一份无截图的 write_notes 产物补截图，结果必须与
write_notes 直接带截图写出的逐字相同。两条通路一旦漂移，同一篇论文在不同札记里配出不同的图。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
fitz = pytest.importorskip("fitz")

_spec = importlib.util.spec_from_file_location("tps_helpers", REPO / "test" / "test_page_shots.py")
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)

from src.scholar.notes import write_notes, attach_shots_to_md  # noqa: E402
from src.scholar.notes_index import parse_note_md  # noqa: E402


def _strip_volatile(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.startswith("generated"))


def _two_papers(tmp_path):
    """一篇带图（有 PDF）、一篇无 PDF——后者必须一字不动。"""
    import copy
    seg, pdf = H._segment_with_figure(tmp_path)
    other = copy.deepcopy(seg)
    other.paper_id = other.metadata.paper_id = "pid2"
    other.metadata.title = "A second paper without any local PDF"
    other.metadata.doi = "10.1/other"
    return [seg, other], pdf


def test_attach_equals_write_notes_rendering(tmp_path):
    segs, pdf = _two_papers(tmp_path)
    keys = {"pid1": "mitra2023Learning", "pid2": "other2020Paper"}
    ref, plain = tmp_path / "ref", tmp_path / "plain"
    ref.mkdir(); plain.mkdir()
    r1 = write_notes(segs, dict(keys), out_dir=ref, filename="n", index_series="auto",
                     pdf_paths={"pid1": str(pdf)}, embed_fn=H._fake_embed)
    r2 = write_notes(segs, dict(keys), out_dir=plain, filename="n", index_series="auto")
    md = Path(r2["note_path"])
    assert "原文截图" not in md.read_text("utf-8")
    pdf_for = {"mitra2023Learning": str(pdf)}.get
    rep = attach_shots_to_md(md, pdf_for, plain, embed_fn=H._fake_embed)
    assert rep["papers"] == 2 and rep["shot_papers"] == 1 and rep["changed"]
    assert _strip_volatile(md.read_text("utf-8")) == _strip_volatile(Path(r1["note_path"]).read_text("utf-8"))


def test_attach_is_idempotent_and_keeps_evidence(tmp_path):
    segs, pdf = _two_papers(tmp_path)
    out = tmp_path / "n"
    out.mkdir()
    r = write_notes(segs, {"pid1": "mitra2023Learning", "pid2": "other2020Paper"}, out_dir=out,
                    filename="n", index_series="auto")
    md = Path(r["note_path"])
    before = parse_note_md(md)
    pdf_for = {"mitra2023Learning": str(pdf)}.get
    attach_shots_to_md(md, pdf_for, out, embed_fn=H._fake_embed)
    once = md.read_text("utf-8")
    rep = attach_shots_to_md(md, pdf_for, out, embed_fn=H._fake_embed)
    assert not rep["changed"] and md.read_text("utf-8") == once          # 重跑一字不变
    after = parse_note_md(md)
    assert [e["highlights"] for e in after] == [e["highlights"] for e in before]   # 证据句一条没动
    assert once.count("**原文页面**") == 1


def test_attach_keeps_old_shots_when_pdf_disappears_and_dry_run_writes_nothing(tmp_path):
    segs, pdf = _two_papers(tmp_path)
    out = tmp_path / "n"
    out.mkdir()
    r = write_notes(segs, {"pid1": "mitra2023Learning", "pid2": "other2020Paper"}, out_dir=out,
                    filename="n", index_series="auto")
    md = Path(r["note_path"])
    attach_shots_to_md(md, {"mitra2023Learning": str(pdf)}.get, out, embed_fn=H._fake_embed)
    with_shots = md.read_text("utf-8")
    rep = attach_shots_to_md(md, lambda ck: None, out, embed_fn=H._fake_embed)
    assert not rep["changed"] and md.read_text("utf-8") == with_shots   # PDF 这轮找不到：不清掉已插好的图
    plain = tmp_path / "p"
    plain.mkdir()
    r2 = write_notes(segs, {"pid1": "mitra2023Learning", "pid2": "other2020Paper"}, out_dir=plain,
                     filename="n", index_series="auto")
    md2 = Path(r2["note_path"])
    raw = md2.read_text("utf-8")
    rep = attach_shots_to_md(md2, {"mitra2023Learning": str(pdf)}.get, plain, embed_fn=H._fake_embed, apply=False)
    assert rep["changed"] and md2.read_text("utf-8") == raw              # 干跑不写盘


def test_attach_survives_stale_shot_lines_left_by_text_surgery(tmp_path):
    """backfill_deepread 的文本手术只替换到最后一句证据，句后残留的旧截图行与「原文页面」块会留在原处。
    补截图必须把它们清掉重插，不能叠出两份。"""
    segs, pdf = _two_papers(tmp_path)
    out = tmp_path / "n"
    out.mkdir()
    r = write_notes(segs, {"pid1": "mitra2023Learning", "pid2": "other2020Paper"}, out_dir=out,
                    filename="n", index_series="auto", pdf_paths={"pid1": str(pdf)}, embed_fn=H._fake_embed)
    md = Path(r["note_path"])
    clean = md.read_text("utf-8")
    # 模拟手术：把中间一句下面的截图行挪到节尾之外（残留）、再复制一份原文页面块
    doubled = clean.replace("**原文页面**", "  ![原文出处 p.16](assets/shots/mitra2023Learning/q_stale.jpg)\n**原文页面**", 1)
    md.write_text(doubled, encoding="utf-8")
    attach_shots_to_md(md, {"mitra2023Learning": str(pdf)}.get, out, embed_fn=H._fake_embed)
    assert md.read_text("utf-8") == clean


def test_strip_if_no_shots_clears_stale_images_after_text_surgery(tmp_path):
    """backfill_deepread 整节换掉精读后，旧句子的截图行对不上新句子：找不到 PDF 也要清掉。
    同一份 md 里没被改写的论文，找不到 PDF 时照旧原样保留。"""
    segs, pdf = _two_papers(tmp_path)
    out = tmp_path / "n"
    out.mkdir()
    r = write_notes(segs, {"pid1": "mitra2023Learning", "pid2": "other2020Paper"}, out_dir=out, filename="n",
                    index_series="auto", pdf_paths={"pid1": str(pdf)}, embed_fn=H._fake_embed)
    md = Path(r["note_path"])
    with_shots = md.read_text("utf-8")
    assert "![" in with_shots
    attach_shots_to_md(md, lambda ck: None, out, embed_fn=H._fake_embed)              # 未改写：保留
    assert md.read_text("utf-8") == with_shots
    attach_shots_to_md(md, lambda ck: None, out, embed_fn=H._fake_embed,
                       strip_if_no_shots={"mitra2023Learning"})                         # 改写过：清掉
    txt = md.read_text("utf-8")
    assert "assets/shots/" not in txt and "**原文页面**" not in txt
    assert [e["highlights"] for e in parse_note_md(md)] == [e["highlights"] for e in parse_note_md(Path(r["note_path"]))]


def test_auto_shot_kwargs_only_for_close_read_papers_with_a_local_pdf(tmp_path, monkeypatch):
    """自动链路：精读时落在 <scratch>/<paper_id[:16]>.pdf 的那份才给；没精读、没 PDF 的不给；
    一篇都没有时连向量服务都不探活。"""
    import copy
    from src.scholar import notes as NT
    import src.scholar.embeddings as EM
    calls = []
    monkeypatch.setattr(EM, "passage_embed_fn", lambda s: calls.append(1) or H._fake_embed)
    seg, _pdf = H._segment_with_figure(tmp_path)
    seg.paper_id = "a" * 32
    no_pdf = copy.deepcopy(seg); no_pdf.paper_id = "b" * 32
    no_cr = copy.deepcopy(seg); no_cr.paper_id = "c" * 32; no_cr.close_reading = None
    scratch = tmp_path / "scholar_pdfs"
    scratch.mkdir()
    (scratch / ("a" * 16 + ".pdf")).write_bytes(_pdf.read_bytes())
    (scratch / ("c" * 16 + ".pdf")).write_bytes(_pdf.read_bytes())
    kw = NT.auto_shot_kwargs([seg, no_pdf, no_cr], object(), scratch_dir=str(scratch))
    assert list(kw["pdf_paths"]) == ["a" * 32] and kw["embed_fn"] is H._fake_embed
    assert NT.auto_shot_kwargs([no_pdf, no_cr], object(), scratch_dir=str(scratch)) == {}
    assert len(calls) == 1
