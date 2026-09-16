# -*- coding: utf-8 -*-
"""scripts/backfill_scholar_metadata.py：用札记里的 Scholar 片段回填残缺作者/年份并改兜底键。

片段样例取自真实札记（2024-01 anon2024Generalizable、2023-06 han2023Timeline 等）。
"""
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import backfill_scholar_metadata as B                                       # noqa: E402
from src.scholar.notes import write_notes                                    # noqa: E402
from src.scholar.notes_index import update_index                             # noqa: E402
from src.scholar.schema import FilterDecision, PaperMetadata, PaperSegment   # noqa: E402


# ---------------- 片段解析 ----------------

@pytest.mark.parametrize("abstract, title, authors, venue, year", [
    ("[PDF] Generalizable Risk Predictive Deep Learning Models F Amrollahi - 2023 The broad adoption of EHRs",
     "Generalizable Risk Predictive Deep Learning Models", ["F Amrollahi"], None, 2023),
    ("[HTML] Timeline Registration for Electronic Health Records S Jiang, R Han, K Chakrabarty, D Page, "
     "WW Stead… - AMIA Summits on …, 2023 Abstract Electronic Health Record (EHR) data are captured",
     "Timeline Registration for Electronic Health Records",
     ["S Jiang", "R Han", "K Chakrabarty", "D Page", "WW Stead…"], None, 2023),
    ("Learning Representations of Inactive Users: A Cross Domain Approach with Graph Neural Networks "
     "Z Liu, Y Shen, X Cheng - Proceedings of the 30th …, 2021 Understanding inactive users is key",
     "Learning Representations of Inactive Users: A Cross Domain Approach with Graph Neural Networks",
     ["Z Liu", "Y Shen", "X Cheng"], None, 2021),
    # 无 " - " 的作者单行：末位粘着摘要首句 → 丢掉被截的末位
    ("Private Graph Generation X Zheng, N McCarthy, J Hayes We consider the problem of generating",
     "Private Graph Generation", ["X Zheng", "N McCarthy…"], None, None),
    # 「…」之前是被截断的末位作者，保留（R1 审计：旧逻辑把 M Zhang 丢了）
    ("DyHealth: Healthcare Analytics K Zheng, S Cai, HR Chua, M Herschel, M Zhang… ABSTRACT In National University",
     "DyHealth: Healthcare Analytics", ["K Zheng", "S Cai", "HR Chua", "M Herschel", "M Zhang…"], None, None),
    ("Demographic features of sepsis in ED B CANDER, O KARAOĞLAN, F SAVRAN, M ERGİN…",
     "Demographic features of sepsis in ED", ["B CANDER", "O KARAOĞLAN", "F SAVRAN", "M ERGİN…"], None, None),
])
def test_meta_from_scholar_snippet(abstract, title, authors, venue, year):
    got = B.meta_from_scholar_snippet(abstract, title)
    got.pop("form")
    assert got == {"authors": authors, "venue": venue, "year": year}


@pytest.mark.parametrize("abstract, title", [
    # PubMed/arXiv 摘要不以标题开头 → 不是 Scholar 片段
    ("Background: Sepsis is a leading cause of death. Methods: ...", "Sepsis prediction in ED"),
    # 以标题开头但后面是正文，不是缩写式作者
    ("Sepsis prediction in ED Background Multicentre training could reduce biases", "Sepsis prediction in ED"),
    # R1 审计实测误判：单作者单行形态分不清姓和摘要首词 / 网页控件
    ("Curating COVID-19–January 2023 R Login Persistent symptoms after SARS-CoV-2", "Curating COVID-19–January 2023"),
    ("Comparison of HF Therapies With Reduced … G Fonarow, AUC Center Background: Heart failure (HF) is",
     "Comparison of HF Therapies With Reduced …"),
    ("[PDF] Exploring and Understanding Neural Models for Clinical Tasks M Sushil The use of patient data",
     "Exploring and Understanding Neural Models for Clinical Tasks"),
])
def test_non_snippet_rejected(abstract, title):
    assert B.meta_from_scholar_snippet(abstract, title) is None


# ---------------- 提案守卫 ----------------

def _e(**kw):
    base = dict(citekey="anon2024Generalizable", authors=[], year=2024, month="2024-01", doi=None,
                arxiv_id=None, title="Generalizable Risk Predictive Deep Learning Models",
                dedup_key="title:generalizableriskpredictivedeeplearningmodels")
    base.update(kw)
    return base


SNIP = {"authors": ["F Amrollahi"], "venue": None, "year": 2023}


def test_propose_empty_authors_and_email_year():
    ch, notes = B.propose(_e(), SNIP)
    assert ch["authors"] == ["F Amrollahi"]
    assert ch["issued"]["date"] == date(2023, 1, 1)


def test_propose_keeps_full_names():
    ch, _ = B.propose(_e(authors=["Fatemeh Amrollahi"]), SNIP)
    assert "authors" not in ch


def test_propose_full_names_with_consortium_not_garbage():
    e = _e(authors=["Peter A. Wijeratne", "Daniel C. Alexander",
                    "for the Alzheimer’s Disease Neuroimaging Initiative"])
    ch, _ = B.propose(e, {"authors": ["PA Wijeratne", "DC Alexander"], "venue": None, "year": None})
    assert "authors" not in ch


def test_propose_garbage_authors_replaced():
    ch, _ = B.propose(_e(authors=["properties such as portability", "where definitions are stored"]),
                      {"authors": ["M Chapman", "S Mumtaz"], "venue": None, "year": None})
    assert ch["authors"] == ["M Chapman", "S Mumtaz"]


def test_propose_first_author_restored():
    e = _e(citekey="han2023Timeline", authors=["R Han", "K Chakrabarty", "D Page", "WW Stead…"], year=2023,
           month="2023-06")
    ch, notes = B.propose(e, {"authors": ["S Jiang", "R Han", "K Chakrabarty", "D Page", "WW Stead"],
                              "venue": None, "year": 2023})
    assert ch["authors"][0] == "S Jiang" and "首作者" in notes[0]


def test_propose_rejects_mismatched_abbrev():
    e = _e(authors=["M Lewis", "J Figueroa"])
    ch, notes = B.propose(e, {"authors": ["GE Maor", "M Beladev"], "venue": None, "year": None})
    assert "authors" not in ch and notes[0].startswith("拒绝")


def test_propose_year_untouched_with_doi_or_non_email_year():
    ch, _ = B.propose(_e(authors=["F Amrollahi"], doi="10.1/x"), SNIP)
    assert "issued" not in ch
    ch, notes = B.propose(_e(authors=["F Amrollahi"], year=2015), SNIP)     # 现年份不是入库月份年
    assert "issued" not in ch and "不改" in notes[0]


def test_generated_key_detection_and_new_key():
    e = _e()
    assert B.is_generated_key(e)
    assert B.is_generated_key(dict(e, citekey="anon2024Generalizableb"))
    assert not B.is_generated_key(dict(e, citekey="dayanFederatedLearningPredicting2021"))
    ch, _ = B.propose(e, SNIP)
    assert B.plan_new_key(e, ch, {}) == ("amrollahi2023Generalizable", "")


def test_new_key_suffix_for_other_paper_and_block_for_suspected_duplicate():
    e = _e()
    ch, _ = B.propose(e, SNIP)
    owners = {"amrollahi2023Generalizable": {"doi:10.9/other"}}
    titles = {"doi:10.9/other": ["A totally unrelated survey of cardiology"]}
    assert B.plan_new_key(e, ch, owners, titles) == ("amrollahi2023Generalizableb", "")
    titles = {"doi:10.9/other": ["Generalizable Risk Predictive Deep Learning Models for Sepsis"]}
    new, why = B.plan_new_key(e, ch, owners, titles)
    assert new is None and "疑似同文" in why


# ---------------- 端到端：tmp 札记库 --apply ----------------

def _write_fixture_month(nd: Path):
    fd = FilterDecision(paper_id="p1", title="t", verdict="included", stage="llm_judge", decision="MAYBE",
                        one_line="x", bucket=[], role="BACKGROUND", confidence=0.3, flags=[])
    segs = [
        PaperSegment(segment_id=1, paper_id="p1",
                     original_abstract="[PDF] Generalizable Risk Predictive Deep Learning Models F Amrollahi "
                                       "- 2023 The broad adoption of Electronic Health Records",
                     metadata=PaperMetadata(paper_id="p1", title="Generalizable Risk Predictive Deep Learning Models",
                                            authors=[], email_received_at=datetime(2024, 1, 28)),
                     filter_decision=fd),
        PaperSegment(segment_id=2, paper_id="p2",
                     original_abstract="[HTML] Timeline Registration for Electronic Health Records S Jiang, R Han, "
                                       "K Chakrabarty… - AMIA Summits on …, 2023 Abstract Electronic Health Record",
                     metadata=PaperMetadata(paper_id="p2", title="Timeline Registration for Electronic Health Records",
                                            authors=["R Han", "K Chakrabarty…"], publication_date=date(2023, 1, 1),
                                            email_received_at=datetime(2024, 1, 28)),
                     filter_decision=fd.model_copy(update={"paper_id": "p2"})),
        PaperSegment(segment_id=3, paper_id="p3",
                     original_abstract="Some Crossref Enriched Paper About Sepsis JA Doe - Lancet, 2023 Body text",
                     metadata=PaperMetadata(paper_id="p3", title="Some Crossref Enriched Paper About Sepsis",
                                            authors=["Jane A. Doe"], doi="10.1/abc",
                                            publication_date=date(2023, 5, 1)),
                     filter_decision=fd.model_copy(update={"paper_id": "p3"})),
    ]
    write_notes(segs, {"p1": None, "p2": None, "p3": None}, out_dir=nd,
                digest_title="科研札记 · 2024-01（全文精读）", filename="科研札记_2024-01_全文精读",
                fallback_citekeys=True, emit_index_sidecar=False)


def test_end_to_end_apply(tmp_path, monkeypatch):
    nd = tmp_path / "notes"
    nd.mkdir()
    _write_fixture_month(nd)
    before = {e["title"][:10]: e for e in update_index(nd)["papers"]}
    assert before["Generaliza"]["citekey"] == "anon2024Generalizable"
    assert before["Timeline R"]["citekey"].startswith("han2023")

    monkeypatch.setattr(sys, "argv", ["x", "--notes-dir", str(nd), "--apply", "--no-external-scan"])
    assert B.main() == 0

    after = {e["title"][:10]: e for e in update_index(nd, full=True)["papers"]}
    g, t, c = after["Generaliza"], after["Timeline R"], after["Some Cross"]
    assert g["citekey"] == "amrollahi2023Generalizable" and g["authors"] == ["F Amrollahi"] and g["year"] == 2023
    assert t["citekey"] == "jiang2023Timeline" and t["authors"][:2] == ["S Jiang", "R Han"]
    assert c["authors"] == ["Jane A. Doe"] and c["citekey"] == before["Some Cross"]["citekey"]   # 全名不动
    # 三处一致：md 里无旧键，CSL id 已换
    md = (nd / "科研札记_2024-01_全文精读.md").read_text(encoding="utf-8")
    refs = json.loads((nd / "科研札记_2024-01_全文精读.references.json").read_text(encoding="utf-8"))
    assert "anon2024Generalizable" not in md and "[@amrollahi2023Generalizable]" in md
    assert {r["id"] for r in refs} >= {"amrollahi2023Generalizable", "jiang2023Timeline"}
    # 对照表与备份
    tables = list((nd / "_rekey").glob("scholar_metadata_*.json"))
    pairs = {(p["old"], p["new"]) for p in json.loads(tables[0].read_text(encoding="utf-8"))}
    assert ("anon2024Generalizable", "amrollahi2023Generalizable") in pairs
    assert list((nd / "_archive").glob("scholar_meta_*/科研札记_2024-01_全文精读.md"))

    # 幂等：再跑一遍无可修
    monkeypatch.setattr(sys, "argv", ["x", "--notes-dir", str(nd)])
    recs = B.collect(nd, update_index(nd))
    assert not [r for r in recs if r["ch"]]


def test_propose_email_boilerplate_authors_are_garbage():
    """真实条目 famia2023Realtime：作者被解析成订阅者头衔。"""
    e = _e(citekey="famia2023Realtime", authors=["FAMIA", "FIAHSI", "FACMI"], year=2023, month="2023-01",
           title="Real-time prediction of acute kidney injury in critical care")
    ch, _ = B.propose(e, {"authors": ["J Richter"], "venue": None, "year": 2022})
    assert ch["authors"] == ["J Richter"] and ch["issued"]["date"].year == 2022


def test_propose_does_not_downgrade_fuller_abbrev_list():
    """真实条目 fadhilla2026Alternative：现表带中间名、无省略号，不换成片段的更短缩写。"""
    e = _e(authors=["E S Fadhilla", "M Shoufie Ukhtary", "A N Atmaja", "B E Gunara"])
    ch, _ = B.propose(e, {"authors": ["ES Fadhilla", "MS Ukhtary", "AN Atmaja", "BE Gunara"],
                          "venue": None, "year": None})
    assert "authors" not in ch


# ---------------- R1 对抗审计的回归 ----------------

def _seg(pid, title, abstract, authors, **meta):
    fd = FilterDecision(paper_id=pid, title="t", verdict="included", stage="llm_judge", decision="MAYBE",
                        one_line="x", bucket=[], role="BACKGROUND", confidence=0.3, flags=[])
    return PaperSegment(segment_id=abs(hash(pid)) % 1000, paper_id=pid, original_abstract=abstract,
                        metadata=PaperMetadata(paper_id=pid, title=title, authors=authors, **meta),
                        filter_decision=fd)


def test_apply_does_not_shift_note_lines(tmp_path, monkeypatch):
    """不往 md 插行：其余条目的 note_line 写回前后一致（topics 的 文件.md:行号 锚点不失效）。"""
    nd = tmp_path / "notes"
    nd.mkdir()
    _write_fixture_month(nd)
    before = {e["dedup_key"]: e["note_line"] for e in update_index(nd)["papers"]}
    monkeypatch.setattr(sys, "argv", ["x", "--notes-dir", str(nd), "--apply", "--no-external-scan"])
    assert B.main() == 0
    after = {e["dedup_key"]: e["note_line"] for e in update_index(nd, full=True)["papers"]}
    assert before == after


def test_new_merge_is_previewed_and_not_rekeyed(tmp_path, monkeypatch, capsys):
    """真实案例 portability2021Desiderata：改掉残片作者后被标题相似度层并入 DOI 条目。
    预演必须报出这条新合并，且不对它改键、不去改它的行内键。"""
    nd = tmp_path / "notes"
    nd.mkdir()
    segs = [
        _seg("d1", "DESIDERATA FOR THE DEVELOPMENT OF NEXT-GENERATION ELECTRONIC HEALTH RECORD PHENOTYPE LIBRARIES",
             "Abstract text", ["Martin Chapman", "Shahzad Mumtaz"], doi="10.1093/gigascience/giab059",
             publication_date=date(2021, 9, 1)),
        _seg("d2", "DESIDERATA FOR THE DEVELOPMENT OF NEXT-GENERATION PHENOTYPE LIBRARIES",
             "DESIDERATA FOR THE DEVELOPMENT OF NEXT-GENERATION PHENOTYPE LIBRARIES M Chapman, S Mumtaz, "
             "LV Rasmussen - bioRxiv, 2021 Background Well-defined phenotypes",
             ["properties such as portability", "where definitions are stored"],
             email_received_at=datetime(2021, 6, 3)),
    ]
    write_notes(segs, {"d1": "chapman2021Desiderata", "d2": "portability2021Desiderata"}, out_dir=nd,
                digest_title="t", filename="科研札记_2021-06_全文精读", fallback_citekeys=True,
                emit_index_sidecar=False)
    idx = update_index(nd)
    assert not any(e.get("duplicate_of") for e in idx["papers"])            # 改前守卫拦着
    recs = B.collect(nd, idx)
    after = B.simulate(nd, recs)
    merges = B.new_merges(idx, after)
    assert [m["entry"]["citekey"] for m in merges] == ["portability2021Desiderata"]
    plan, skipped = B.plan_rekeys(recs, after)
    assert plan == [] and "新合并" in skipped[0]

    monkeypatch.setattr(sys, "argv", ["x", "--notes-dir", str(nd), "--apply", "--no-external-scan"])
    assert B.main() == 1                                                     # 默认遇到新合并就中止、不写盘
    assert not any(e.get("duplicate_of") for e in update_index(nd, full=True)["papers"])
    monkeypatch.setattr(sys, "argv", ["x", "--notes-dir", str(nd), "--apply", "--accept-merges",
                                      "--no-external-scan"])
    assert B.main() == 0
    md = (nd / "科研札记_2021-06_全文精读.md").read_text(encoding="utf-8")
    assert "[@portability2021Desiderata]" in md                              # 行内键没被顺手改


def test_resume_after_crash_between_phases(tmp_path, monkeypatch):
    """写回完成、改键前崩溃：重跑已算不出旧键，必须能按落盘计划 --resume（R1 审计）。"""
    nd = tmp_path / "notes"
    nd.mkdir()
    _write_fixture_month(nd)
    monkeypatch.setattr(B, "run_rekeys", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash")))
    monkeypatch.setattr(sys, "argv", ["x", "--notes-dir", str(nd), "--apply", "--no-external-scan"])
    with pytest.raises(RuntimeError):
        B.main()
    monkeypatch.undo()
    pend = B.unfinished_pending(nd)
    assert len(pend) == 1
    # 未完成计划挡住新一轮 --apply
    monkeypatch.setattr(sys, "argv", ["x", "--notes-dir", str(nd), "--apply", "--no-external-scan"])
    assert B.main() == 1
    monkeypatch.setattr(sys, "argv", ["x", "--notes-dir", str(nd), "--resume", str(pend[0]), "--no-external-scan"])
    assert B.main() == 0
    keys = {e["citekey"] for e in update_index(nd, full=True)["papers"]}
    assert {"amrollahi2023Generalizable", "jiang2023Timeline"} <= keys
    assert not B.unfinished_pending(nd)


def test_ledgers_follow_rekey(tmp_path, monkeypatch):
    nd = tmp_path / "notes"
    nd.mkdir()
    _write_fixture_month(nd)
    (nd / "backfill_expand_progress.json").write_text(json.dumps(
        {"done": {}, "failed": {"anon2024Generalizable": {"citekey": "anon2024Generalizable", "reason": "no_output"}},
         "schema_version": 3}), encoding="utf-8")
    (nd / "需下载全文清单.md").write_text("- `anon2024Generalizable` 缺全文\n- `anon2024Generalizableb` 别的\n",
                                     encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["x", "--notes-dir", str(nd), "--apply", "--no-external-scan"])
    assert B.main() == 0
    led = json.loads((nd / "backfill_expand_progress.json").read_text(encoding="utf-8"))
    assert list(led["failed"]) == ["amrollahi2023Generalizable"]
    assert led["failed"]["amrollahi2023Generalizable"]["citekey"] == "amrollahi2023Generalizable"
    md = (nd / "需下载全文清单.md").read_text(encoding="utf-8")
    assert "`amrollahi2023Generalizable` 缺全文" in md and "`anon2024Generalizableb` 别的" in md   # 不误伤前缀相同的键


def test_scan_external_reports_text_and_filenames(tmp_path):
    (tmp_path / "draft.md").write_text("如 [@anon2024Generalizable] 所示", encoding="utf-8")
    (tmp_path / "anon2024Generalizable.pdf").write_bytes(b"%PDF")
    (tmp_path / "other.md").write_text("[@anon2024Generalizableb]", encoding="utf-8")
    hits = B.scan_external({"anon2024Generalizable": "amrollahi2023Generalizable"}, dirs=(str(tmp_path),))
    assert len(hits) == 2 and any("draft.md" in h for h in hits) and any(".pdf" in h for h in hits)


# ---------------- R1 数据审计的回归 ----------------

def test_glued_names_and_short_titles_rejected():
    # liu2023pretrain：两个人名粘成 "R PisaniAnthony"
    assert B.meta_from_scholar_snippet(
        "Pretrained models for depression screening BP Datta, R PisaniAnthony - Computers in Biology, 2023 Text",
        "Pretrained models for depression screening") is None
    # anon2024Mohsen：「标题」其实是首作者名
    assert B.meta_from_scholar_snippet(
        "Mohsen Bakouri NM Alqahtani, OM Alhussain, N Alrashidi - JPMS, 2024 Background: Sepsis",
        "Mohsen Bakouri") is None
    assert not B._glued(["A McDonald", "K MacKenzie", "E DeLong"])


def test_year_doubt_blocks_rekey(tmp_path):
    """nagavedu2018Peer：片段 2024、现 2018（从摘要 "In 2018" 抠出）——守卫拦了改年份，也必须拦改键。"""
    e = _e(citekey="nagavedu2018Peer", authors=["K Nagavedu"], year=2018, month="2024-05",
           title="Peer Comparison Letters for High-Volume Opioid Prescribers")
    ch, notes = B.propose(e, {"authors": ["S Ghildayal", "K Nagavedu"], "venue": None, "year": 2024})
    assert ch.pop("_year_doubt") is True and "issued" not in ch
    rec = {"entry": e, "snippet": {"form": "dash"}, "ch": ch, "notes": notes, "year_doubt": True}
    after = {"papers": [dict(e, authors=ch["authors"])]}
    plan, skipped = B.plan_rekeys([rec], after)
    assert plan == [] and "年份存疑" in skipped[0]


def test_inventory_reports_follow_rekey(tmp_path):
    nd = tmp_path / "notes"
    (nd / "_inventory").mkdir(parents=True)
    rep = nd / "_inventory" / "missing_ids_20260913T091005.json"
    rep.write_text(json.dumps({"n": 2, "records": [{"citekey": "anon2024Generalizable", "hits": []},
                                                   {"citekey": "other2024Paper", "hits": []}]}), encoding="utf-8")
    from src.scholar.metadata_writeback import Backup
    changed = B.rename_ledgers(nd, {"anon2024Generalizable": "amrollahi2023Generalizable"},
                               Backup(tmp_path / "bk", nd))
    recs = json.loads(rep.read_text(encoding="utf-8"))["records"]
    assert [r["citekey"] for r in recs] == ["amrollahi2023Generalizable", "other2024Paper"]
    assert changed == ["_inventory/" + rep.name]


# ---------------- R2 对抗审计的回归 ----------------

def _topic_page(nd, name, body, tampered=False):
    from src.scholar.vault import GEN_END, _gen_hash
    h = "00000000" if tampered else _gen_hash(body)
    (nd / "topics").mkdir(exist_ok=True)
    (nd / "topics" / name).write_text(
        "<!-- BEGIN GENERATED v1 h={} -->\n{}\n{}\n".format(h, body, GEN_END), encoding="utf-8")


def test_resume_redoes_side_effects_after_crash_in_finalize(tmp_path, monkeypatch):
    """改名做完、收尾崩溃：续跑必须对已完成的键重做 topics 改名（R2 审计 C1：原先 touched 为空直接跳过）。"""
    nd = tmp_path / "notes"
    nd.mkdir()
    _write_fixture_month(nd)
    _topic_page(nd, "t.md", "证据见 [@anon2024Generalizable]。")
    real = B.announce_rekey_side_effects
    monkeypatch.setattr(B, "announce_rekey_side_effects", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(sys, "argv", ["x", "--notes-dir", str(nd), "--apply", "--no-external-scan"])
    with pytest.raises(RuntimeError):
        B.main()
    monkeypatch.setattr(B, "announce_rekey_side_effects", real)
    pend = B.unfinished_pending(nd)
    assert len(pend) == 1 and B._load_pending(pend[0])["stage"] == "renamed"
    monkeypatch.setattr(sys, "argv", ["x", "--notes-dir", str(nd), "--resume", str(pend[0]), "--no-external-scan"])
    assert B.main() == 0
    assert "[@amrollahi2023Generalizable]" in (nd / "topics" / "t.md").read_text(encoding="utf-8")
    assert not B.unfinished_pending(nd)


def test_resume_refuses_when_writeback_not_done(tmp_path, monkeypatch):
    """写回中途崩溃（stage 仍是 planned）：续跑不许改键，否则键名与作者对不上（R2 审计 C2）。"""
    nd = tmp_path / "notes"
    nd.mkdir()
    _write_fixture_month(nd)
    monkeypatch.setattr(B, "apply_changes", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("half")))
    monkeypatch.setattr(B, "simulate", lambda nd_, recs: update_index(nd_, full=True))
    monkeypatch.setattr(sys, "argv", ["x", "--notes-dir", str(nd), "--apply", "--no-external-scan"])
    with pytest.raises(RuntimeError):
        B.main()
    monkeypatch.undo()
    pend = B.unfinished_pending(nd)
    assert B._load_pending(pend[0])["stage"] == "planned"
    monkeypatch.setattr(sys, "argv", ["x", "--notes-dir", str(nd), "--resume", str(pend[0]), "--no-external-scan"])
    assert B.main() == 1
    assert "anon2024Generalizable" in {e["citekey"] for e in update_index(nd, full=True)["papers"]}


def test_refused_rename_does_not_touch_topics(tmp_path, monkeypatch):
    """某条改名被拒：topics 里的该键不得被换成新键（R2 审计 B：原先用整份计划的映射）。"""
    nd = tmp_path / "notes"
    nd.mkdir()
    _write_fixture_month(nd)
    _topic_page(nd, "t.md", "见 [@anon2024Generalizable] 与 [@han2023Timeline]。")
    real = B._rename_citekey_in_note
    monkeypatch.setattr(B, "_rename_citekey_in_note",
                        lambda nd_, e, old, new: "refused" if old == "anon2024Generalizable" else real(nd_, e, old, new))
    monkeypatch.setattr(sys, "argv", ["x", "--notes-dir", str(nd), "--apply", "--no-external-scan"])
    assert B.main() == 1
    page = (nd / "topics" / "t.md").read_text(encoding="utf-8")
    assert "[@anon2024Generalizable]" in page and "[@jiang2023Timeline]" in page


def test_keys_on_tampered_topic_pages_not_rekeyed(tmp_path, monkeypatch):
    """生成块被手改过的概念页引用的键不改（改名工具会跳过该页 → 悬空；真实案例 eilbeck2022Standard）。"""
    nd = tmp_path / "notes"
    nd.mkdir()
    _write_fixture_month(nd)
    _topic_page(nd, "adversarial.md", "见 [@anon2024Generalizable]。", tampered=True)
    monkeypatch.setattr(sys, "argv", ["x", "--notes-dir", str(nd), "--apply", "--no-external-scan"])
    assert B.main() == 0
    keys = {e["citekey"] for e in update_index(nd, full=True)["papers"]}
    assert "anon2024Generalizable" in keys and "jiang2023Timeline" in keys


def test_backup_covers_rename_only_files(tmp_path, monkeypatch):
    """只在改名阶段被改的三处文件也要进备份（R2 审计 E）。"""
    nd = tmp_path / "notes"
    nd.mkdir()
    _write_fixture_month(nd)
    monkeypatch.setattr(sys, "argv", ["x", "--notes-dir", str(nd), "--apply", "--no-external-scan"])
    assert B.main() == 0
    bk = next((nd / "_archive").glob("scholar_meta_*"))
    names = {p.name for p in bk.iterdir()}
    assert {"科研札记_2024-01_全文精读.md", "科研札记_2024-01_全文精读.references.json"} <= names
    orig = (bk / "科研札记_2024-01_全文精读.md").read_text(encoding="utf-8")
    assert "[@anon2024Generalizable]" in orig


# ---------------- R2 数据审计的回归 ----------------

def test_surname_first_chinese_detection():
    assert B.surname_first_chinese("LUO Yan")
    assert B.surname_first_chinese("ZHANG Wei")
    assert not B.surname_first_chinese("AAS Soltan")      # Scholar 缩写
    assert not B.surname_first_chinese("JMS Smith")
    assert not B.surname_first_chinese("Yan Luo")


def test_pinyin_and_lowercase_title_block_rekey():
    e = _e(citekey="yan2021Interpretable", authors=["LUO Yan"], year=2021, month="2021-12",
           title="Interpretable prediction of sepsis")
    ch = {"issued": {"date": date(2022, 1, 1), "precision": "year", "force": True}}
    rec = {"entry": e, "snippet": {"form": "dash", "year": 2022}, "ch": ch, "notes": [], "year_doubt": False}
    plan, skipped = B.plan_rekeys([rec], {"papers": [e]})
    assert plan == [] and "中文姓在前" in skipped[0]
    e2 = _e(citekey="anon2024Convergence", title="with the Convergence of Blockchain, AI, and IoMT", month="2024-11")
    rec2 = {"entry": e2, "snippet": {"form": "dash"}, "ch": {"authors": ["A Goel"]}, "notes": [], "year_doubt": False}
    plan, skipped = B.plan_rekeys([rec2], {"papers": [e2]})
    assert plan == [] and "小写开头" in skipped[0]


def test_rekey_table_marks_email_year(tmp_path, monkeypatch):
    nd = tmp_path / "notes"
    nd.mkdir()
    _write_fixture_month(nd)
    monkeypatch.setattr(sys, "argv", ["x", "--notes-dir", str(nd), "--apply", "--no-external-scan"])
    assert B.main() == 0
    rows = json.loads(next((nd / "_rekey").glob("scholar_metadata_*.json")).read_text(encoding="utf-8"))
    src = {r["new"]: r["year_source"] for r in rows}
    assert src["amrollahi2023Generalizable"] == "片段元数据行"
    assert not (nd / "topics" / ".rekey.lock").exists()
