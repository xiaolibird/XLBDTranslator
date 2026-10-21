# -*- coding: utf-8 -*-
"""标题身份条目升级工具（scripts/promote_title_identity.py）：三闸 / 范围闸 / DOI 占用 /
6 类派生物同步 / 备份 / dry-run 零写盘。全部用 tmp_path 小夹具，不联网、不碰真实 output/。"""
import hashlib
import importlib
import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from src.scholar.schema import PaperSegment, PaperMetadata, FilterDecision      # noqa: E402
from src.scholar.notes import write_notes                                        # noqa: E402
from src.scholar import notes_index as ni                                        # noqa: E402

pt = importlib.import_module("promote_title_identity")

MONTH = "2024-05"


def _seg(i, pid, title, authors, year, **kw):
    return PaperSegment(
        segment_id=i, paper_id=pid, priority_score=0.5,
        metadata=PaperMetadata(paper_id=pid, title=title, authors=authors,
                               url="https://pubmed.ncbi.nlm.nih.gov/{}/".format(1000 + i),
                               publication_date=date(year, 5, 1), **kw),
        filter_decision=FilterDecision(paper_id=pid, title=title, verdict="included", stage="llm_judge",
                                       decision="INCLUDE", one_line="x", bucket=["A"],
                                       role="CITE_SUPPORT", confidence=0.7, flags=[]))


TITLES = {
    "pa": ("Recurrent Networks for Sparse Clinical Time Series", ["Amin Nayebi", "Bo Li"], 2024),
    "pb": ("Graph Transformers for Missingness in Records", ["Ann Lee", "Cy Wu"], 2024),
    "pc": ("Imputation Benchmarks under Informative Observation", ["Dan Park"], 2024),
}
KEYS = {"pa": "nayebi2024Recurrent", "pb": "lee2024Graph", "pc": "park2024Imputation"}


def _build(tmp_path):
    d = tmp_path / "notes"
    d.mkdir()
    segs = [_seg(i + 1, pid, *TITLES[pid]) for i, pid in enumerate(["pa", "pb", "pc"])]
    write_notes(segs, KEYS, out_dir=d, digest_title="科研札记 · 2024-05（全文精读）",
                filename="科研札记_{}_全文精读".format(MONTH), fallback_citekeys=True,
                emit_index_sidecar=True)
    idx = ni.update_index(d, full=True)
    ni.write_outputs(idx, d)
    # 摘要缓存：键 = 旧 title 键
    keys = {p["citekey"]: p["dedup_key"] for p in idx["papers"]}
    (d / "abstracts.json").write_text(json.dumps(
        {"generated_at": "x", "abstracts": {keys["nayebi2024Recurrent"]: "abs a", keys["lee2024Graph"]: "abs b"},
         "failures": {}}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return d, keys


def _rec(ck, title=None, doi=None, year=2024, authors=("Nayebi", "Li"), **extra):
    best = {"source": "crossref", "title": title or TITLES["pa"][0], "doi": doi, "year": year,
            "authors": list(authors), "sim": 1.0}
    best.update(extra)
    return {"citekey": ck, "category": "fillable", "best": best}


def _snapshot(d):
    return {str(p.relative_to(d)): hashlib.md5(p.read_bytes()).hexdigest()
            for p in sorted(d.rglob("*")) if p.is_file()}


def _run(d, recs, tmp_path, apply=False):
    return pt.run(d, recs, apply=apply, out_dir=tmp_path / "out", stamp="T1")


def _status(rep, ck):
    return next(r for r in rep["results"] if r["citekey"] == ck)


# ---------------- 三道闸各自拦截 ----------------

def test_pass_baseline(tmp_path):
    d, _ = _build(tmp_path)
    rep = _run(d, [_rec("nayebi2024Recurrent", doi="10.1/aaa")], tmp_path)
    r = _status(rep, "nayebi2024Recurrent")
    assert r["status"] == "pass" and r["new_key"] == "doi:10.1/aaa"
    assert r["old_key"].startswith("title:")


def test_gate_title_sim_blocks(tmp_path):
    d, _ = _build(tmp_path)
    rep = _run(d, [_rec("nayebi2024Recurrent", title="A Totally Different Paper on Other Things", doi="10.1/aaa")], tmp_path)
    r = _status(rep, "nayebi2024Recurrent")
    assert r["status"] == "blocked_title_sim" and r["gates"]["title_sim"]["value"] < 0.95
    assert r["gates"]["first_author"]["pass"] and r["gates"]["year_diff"]["pass"]


def test_gate_first_author_blocks(tmp_path):
    d, _ = _build(tmp_path)
    rep = _run(d, [_rec("nayebi2024Recurrent", doi="10.1/aaa", authors=("Zabel", "Li"))], tmp_path)
    assert _status(rep, "nayebi2024Recurrent")["status"] == "blocked_first_author"


def test_gate_first_author_accepts_full_name_and_initials_and_accent(tmp_path):
    assert pt.family_of("Amin Nayebi") == pt.family_of("Nayebi") == pt.family_of("Nayebi A") == "nayebi"
    assert pt.family_of("Müller, Hans") == pt.family_of("Muller") == "muller"


def test_gate_year_blocks_and_boundary(tmp_path):
    d, _ = _build(tmp_path)
    rep = _run(d, [_rec("nayebi2024Recurrent", doi="10.1/aaa", year=2026)], tmp_path)
    assert _status(rep, "nayebi2024Recurrent")["status"] == "blocked_year_diff"
    rep = _run(d, [_rec("nayebi2024Recurrent", doi="10.1/aaa", year=2025)], tmp_path)   # 差 1 放行
    assert _status(rep, "nayebi2024Recurrent")["status"] == "pass"


def test_empty_note_authors_cannot_verify(tmp_path):
    e = {"citekey": "x", "dedup_key": "title:abc", "title": "Abc", "authors": [], "year": 2024}
    r = pt.evaluate(_rec("x", title="Abc", doi="10.1/z"), e, [e], {})
    assert r["status"] == "blocked_first_author" and "无法核对" in r["gates"]["first_author"]["note"]


# ---------------- 范围闸 ----------------

@pytest.mark.parametrize("key", ["pmlr:v1/abc", "openreview:xyz", "doi:10.1/old", "arxiv:2401.00001"])
def test_scope_skips_non_title_keys(key):
    e = {"citekey": "x", "dedup_key": key, "title": "Abc", "authors": ["A B"], "year": 2024}
    r = pt.evaluate(_rec("x", title="Abc", doi="10.1/z", authors=("B",)), e, [e], {})
    assert r["status"] == "skip_scope" and r["new_key"] is None
    if key.startswith(("pmlr", "openreview")):
        assert "正式身份" in r["reason"]


def test_dropped_skipped():
    e = {"citekey": "x", "dedup_key": "title:abc", "title": "Abc", "authors": ["A B"], "year": 2024}
    rec = _rec("x", title="Abc", doi="10.1/z", authors=("B",))
    rec["drop_reason"] = "裁决为 MAYBE 且相似度低"
    assert pt.evaluate(rec, e, [e], {})["status"] == "skip_dropped"


def test_missing_citekey_skipped(tmp_path):
    d, _ = _build(tmp_path)
    assert _status(_run(d, [_rec("nope")], tmp_path), "nope")["status"] == "skip_scope"


# ---------------- 键位 / DOI 占用 ----------------

def test_pmid_only_has_no_key_slot(tmp_path):
    d, _ = _build(tmp_path)
    rep = _run(d, [_rec("nayebi2024Recurrent", pmid="35309007")], tmp_path)
    assert _status(rep, "nayebi2024Recurrent")["status"] == "skip_no_key_slot"


def test_doi_beats_arxiv(tmp_path):
    d, _ = _build(tmp_path)
    rep = _run(d, [_rec("nayebi2024Recurrent", doi="10.1/aaa", arxiv="2401.00001")], tmp_path)
    assert _status(rep, "nayebi2024Recurrent")["new_key"] == "doi:10.1/aaa"


def test_doi_taken_by_other_entry_skipped(tmp_path):
    d, _ = _build(tmp_path)
    # 把 lee2024Graph 先升级成 10.1/dup，再让 nayebi 申请同一 DOI
    _run(d, [_rec("lee2024Graph", title=TITLES["pb"][0], doi="10.1/dup", authors=("Lee",))], tmp_path, apply=True)
    rep = _run(d, [_rec("nayebi2024Recurrent", doi="10.1/DUP")], tmp_path)
    r = _status(rep, "nayebi2024Recurrent")
    assert r["status"] == "skip_doi_taken" and "lee2024Graph" in r["doi_taken_by"][0]


def test_doi_claimed_twice_in_batch(tmp_path):
    d, _ = _build(tmp_path)
    rep = _run(d, [_rec("nayebi2024Recurrent", doi="10.1/same"),
                   _rec("lee2024Graph", title=TITLES["pb"][0], doi="10.1/same", authors=("Lee",))], tmp_path)
    st = sorted(r["status"] for r in rep["results"])
    assert st == ["pass", "skip_doi_taken"]


# ---------------- dry-run 零写盘 / apply 与派生物 ----------------

def test_dry_run_writes_nothing_to_notes_dir(tmp_path):
    d, _ = _build(tmp_path)
    before = _snapshot(d)
    rep = _run(d, [_rec("nayebi2024Recurrent", doi="10.1/aaa")], tmp_path)
    assert _snapshot(d) == before
    assert not (d / "_archive").exists()
    assert all(Path(f).exists() and tmp_path / "out" in Path(f).parents for f in rep["files"])
    assert "dry-run" in Path(rep["files"][1]).read_text(encoding="utf-8")


def test_apply_syncs_derived_layers_and_backs_up(tmp_path):
    d, keys = _build(tmp_path)
    old = keys["nayebi2024Recurrent"]
    stem = "科研札记_{}_全文精读".format(MONTH)
    before = _snapshot(d)
    rep = _run(d, [_rec("nayebi2024Recurrent", doi="10.1/aaa")], tmp_path, apply=True)
    r = _status(rep, "nayebi2024Recurrent")
    assert r["status"] == "applied" and r["index_key_after"] == "doi:10.1/aaa"
    # 1 sidecar
    side = json.loads((d / (stem + ".index.json")).read_text(encoding="utf-8"))
    assert [p["doi"] for p in side["papers"] if p["citekey"] == "nayebi2024Recurrent"] == ["10.1/aaa"]
    # 2 references
    refs = json.loads((d / (stem + ".references.json")).read_text(encoding="utf-8"))
    assert [x["DOI"] for x in refs if x["id"] == "nayebi2024Recurrent"] == ["10.1/aaa"]
    # 3 md：链接行换成 DOI 行
    md = (d / (stem + ".md")).read_text(encoding="utf-8")
    assert "**DOI**: [10.1/aaa](https://doi.org/10.1/aaa)" in md
    assert "pubmed.ncbi.nlm.nih.gov/1001/" not in md and "pubmed.ncbi.nlm.nih.gov/1002/" in md
    # 5 abstracts 改键（另一篇不动）
    ab = json.loads((d / "abstracts.json").read_text(encoding="utf-8"))["abstracts"]
    assert old not in ab and ab["doi:10.1/aaa"] == "abs a" and keys["lee2024Graph"] in ab
    # 6 索引强扫：键已重算、其它条目键不变
    idx = json.loads((d / ni.INDEX_JSON).read_text(encoding="utf-8"))
    now = {p["citekey"]: p["dedup_key"] for p in idx["papers"]}
    assert now["nayebi2024Recurrent"] == "doi:10.1/aaa"
    assert now["lee2024Graph"] == keys["lee2024Graph"] and now["park2024Imputation"] == keys["park2024Imputation"]
    # 备份：原始字节
    bak = d / "_archive" / "promote_title_T1"
    assert rep["backup_dir"] == str(bak)
    for rel in (stem + ".index.json", stem + ".references.json", stem + ".md", "abstracts.json"):
        assert hashlib.md5((bak / rel).read_bytes()).hexdigest() == before[rel]


def test_apply_two_entries_same_month_keep_both_and_original_backup(tmp_path):
    d, keys = _build(tmp_path)
    stem = "科研札记_{}_全文精读".format(MONTH)
    before = _snapshot(d)
    rep = _run(d, [_rec("nayebi2024Recurrent", doi="10.1/aaa"),
                   _rec("lee2024Graph", title=TITLES["pb"][0], doi="10.1/bbb", authors=("Lee",))],
               tmp_path, apply=True)
    assert [r["status"] for r in rep["results"]] == ["applied", "applied"]
    side = json.loads((d / (stem + ".index.json")).read_text(encoding="utf-8"))
    assert {p["citekey"]: p["doi"] for p in side["papers"]} == {
        "nayebi2024Recurrent": "10.1/aaa", "lee2024Graph": "10.1/bbb", "park2024Imputation": None}
    # 备份是第一次写之前的原件，而不是第一条改完的中间态
    bak = d / "_archive" / "promote_title_T1"
    assert hashlib.md5((bak / (stem + ".index.json")).read_bytes()).hexdigest() == before[stem + ".index.json"]


def test_apply_manual_bundle_synced(tmp_path):
    d = tmp_path / "notes"
    d.mkdir()
    title, ck = TITLES["pa"][0], KEYS["pa"]
    seg = _seg(1, "pa", title, TITLES["pa"][1], 2024)
    write_notes([seg], {"pa": ck}, out_dir=d, digest_title="手动", filename="科研札记_2024-05_手动精读",
                fallback_citekeys=True, emit_index_sidecar=True, index_series="manual")
    bdir = d / "manual" / MONTH
    bdir.mkdir(parents=True)
    (bdir / "x.paper.json").write_text(json.dumps({"segment": {"metadata": {"title": title}}}), encoding="utf-8")
    ni.write_outputs(ni.update_index(d, full=True), d)
    rep = _run(d, [_rec(ck, doi="10.1/man")], tmp_path, apply=True)
    assert _status(rep, ck)["status"] == "applied"
    assert json.loads((bdir / "x.paper.json").read_text(encoding="utf-8"))["segment"]["metadata"]["doi"] == "10.1/man"
    assert (d / "_archive" / "promote_title_T1" / "manual" / MONTH / "x.paper.json").exists()


def test_apply_arxiv_identity(tmp_path):
    d, _ = _build(tmp_path)
    rep = _run(d, [_rec("nayebi2024Recurrent", arxiv="2401.00001")], tmp_path, apply=True)
    r = _status(rep, "nayebi2024Recurrent")
    assert r["status"] == "applied" and r["index_key_after"] == "arxiv:2401.00001"


def test_blocked_entries_not_written_on_apply(tmp_path):
    d, _ = _build(tmp_path)
    before = _snapshot(d)
    _run(d, [_rec("nayebi2024Recurrent", doi="10.1/aaa", year=2030)], tmp_path, apply=True)
    assert _snapshot(d) == before


def test_cli_single_requires_gate_inputs(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["x", "--notes-dir", str(tmp_path), "--citekey", "k", "--doi", "10.1/a"])
    assert pt.main() == 2
