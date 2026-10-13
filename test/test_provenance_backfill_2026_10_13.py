# -*- coding: utf-8 -*-
"""存量来源回填与分组报表测试（可信度加固 WO-3）。

覆盖：overlay 合并（只填缺失不覆盖 / native 跳过 / 改 citekey 后按键梯认领 / 认不到计数）、
update_index 接线（overlay 缺失或损坏时照常构建）、回填规则（多份 digest 取最晚、decision 不一致不回填、
复审升级识别、manual bundle、unknown-legacy、dry-run 不写盘）、分组报表（各组之和=keeper 数、
实测/请求值拆分）。全部 tmp 目录小夹具，不联网、不调 LLM、不读真实 output/。
"""
import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest
from loguru import logger as loguru_logger

from src.scholar import notes_index as ni
from src.scholar import pdf_ingest as pi
from src.scholar.notes import write_notes
from src.scholar.schema import (
    CloseReadSection, CloseReadSentence, CloseReading, FilterDecision, PaperMetadata, PaperSegment,
)

ROOT = Path(__file__).resolve().parents[1]


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BF = _load_script("backfill_provenance")
RP = _load_script("provenance_report")


def _entry(citekey, doi=None, title="T", series="auto", **kw):
    e = {"citekey": citekey, "doi": doi, "arxiv_id": None, "title": title, "series": series,
         "dedup_key": ("doi:" + doi.lower()) if doi else "title:" + ni._norm_title(title),
         "decision": "INCLUDE", "duplicate_of": None, "month": "2026-01",
         "tag_counts": {"citable": 2}}
    e.update(kw)
    return e


def _overlay(*records):
    return {"schema": 1, "generated_at": "x", "records": list(records)}


# ---------------- overlay 合并 ----------------

def test_overlay_fills_only_missing_and_never_overwrites():
    e = _entry("a2026X", doi="10.1/a", decision_model="sonnet", decision_model_verified=False,
               decision_prompt_version=None)
    ov = _overlay({"dedup_key": "doi:10.1/a", "fields": {
        "decision_model": "deepseek-v4-flash",        # 已有 → 不覆盖
        "decision_model_verified": True,              # 已有 False（有意义的值）→ 不覆盖
        "decision_prompt_version": "filter-v3@x",     # None → 填
        "decision_source": "digest",                  # 缺键 → 填
        "evil_key": 1}})                              # 不在白名单 → 丢弃
    st = ni.apply_provenance_overlay([e], ov)
    assert e["decision_model"] == "sonnet" and e["decision_model_verified"] is False
    assert e["decision_prompt_version"] == "filter-v3@x" and e["decision_source"] == "digest"
    assert "evil_key" not in e
    assert st["entries_filled"] == 1 and st["keys_filled"] == 2 and st["unmatched"] == 0


def test_overlay_skips_native_entries_and_counts_unmatched():
    nat = _entry("n2026X", doi="10.1/n", provenance_origin="native", decision_model=None)
    ov = _overlay({"dedup_key": "doi:10.1/n", "fields": {"decision_model": "m"}},
                  {"dedup_key": "doi:10.1/zzz", "title": "No Such Paper", "fields": {"decision_model": "m"}})
    st = ni.apply_provenance_overlay([nat], ov)
    assert nat["decision_model"] is None
    assert st["skipped_native"] == 1 and st["unmatched"] == 1 and st["entries_filled"] == 0


def test_overlay_matches_after_citekey_change_and_key_drift():
    """citekey 改了不影响（按身份键认领）；dedup_key 随键梯漂移后退到 DOI / 规范化标题。"""
    renamed = _entry("brandNew2026", doi="10.1/a")            # 与生成 overlay 时的 citekey 不同
    drifted = _entry("d2026X", title="Some Title", dedup_key="id:legacy")   # 冻结的旧键
    ov = _overlay({"dedup_key": "doi:10.1/a", "fields": {"decision_source": "digest"}},
                  {"dedup_key": "stale-key", "title": "some  title!", "fields": {"decision_source": "digest"}})
    st = ni.apply_provenance_overlay([renamed, drifted], ov)
    assert renamed["decision_source"] == "digest" and drifted["decision_source"] == "digest"
    assert st["unmatched"] == 0


def test_overlay_series_scoped_and_duplicates_filled():
    auto_keeper = _entry("k2026X", doi="10.1/s")
    auto_dup = _entry("k2026X", doi="10.1/s", duplicate_of="k2026X", month="2026-02")
    manual = _entry("k2026X", doi="10.1/s", series="manual")
    ov = _overlay({"dedup_key": "doi:10.1/s", "series": "auto", "fields": {"decision_source": "digest"}})
    ni.apply_provenance_overlay([auto_keeper, auto_dup, manual], ov)
    assert auto_keeper["decision_source"] == "digest" and auto_dup["decision_source"] == "digest"
    assert "decision_source" not in manual                      # 另一系列不被串味


# ---------------- update_index 接线 ----------------

def _segments():
    fd = FilterDecision(paper_id="pa", title="t", verdict="included", stage="llm_judge",
                        decision="INCLUDE", one_line="x", bucket=["A"], role="CITE_SUPPORT",
                        confidence=0.7, flags=[])
    a = PaperSegment(segment_id=1, paper_id="pa", priority_score=0.9,
                     metadata=PaperMetadata(paper_id="pa", title="Deep EHR Paper", doi="10.1/aaa",
                                            publication_date=date(2025, 3, 2)),
                     filter_decision=fd)
    return [a]


def _write_month(tmp_path, sidecar=False):
    return write_notes(_segments(), {"pa": "public2025Deep"}, out_dir=tmp_path,
                       digest_title="科研札记 · 2025-03（全文精读）",
                       filename="科研札记_2025-03_全文精读",
                       fallback_citekeys=True, emit_index_sidecar=sidecar)


def _put_overlay(tmp_path, text):
    d = tmp_path / "_provenance"
    d.mkdir(exist_ok=True)
    (d / "overlay.json").write_text(text, encoding="utf-8")


def test_update_index_merges_overlay_for_month_without_sidecar(tmp_path):
    _write_month(tmp_path, sidecar=False)                     # 无 sidecar → md 降级解析，没有来源键
    _put_overlay(tmp_path, json.dumps(_overlay(
        {"dedup_key": "doi:10.1/aaa", "series": "auto",
         "fields": {"decision_source": "digest", "decision_model": "sonnet",
                    "decision_model_verified": False, "provenance_origin": "backfill-digest-json"}})))
    e = ni.update_index(tmp_path, full=True)["papers"][0]
    assert e["decision_model"] == "sonnet" and e["decision_model_verified"] is False
    assert e["provenance_origin"] == "backfill-digest-json"


def test_update_index_overlay_does_not_override_sidecar_values(tmp_path):
    _write_month(tmp_path, sidecar=True)
    sc = tmp_path / "科研札记_2025-03_全文精读.index.json"
    data = json.loads(sc.read_text(encoding="utf-8"))
    data["papers"][0].update({"decision_model": "gemini-x", "decision_model_verified": True})
    sc.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    _put_overlay(tmp_path, json.dumps(_overlay(
        {"dedup_key": "doi:10.1/aaa", "fields": {"decision_model": "sonnet", "decision_source": "digest"}})))
    e = ni.update_index(tmp_path, full=True)["papers"][0]
    assert e["decision_model"] == "gemini-x" and e["decision_model_verified"] is True
    assert e["decision_source"] == "digest"                   # 缺失的键照填


@pytest.mark.parametrize("content", ["{not json", '{"schema": 1}', '[]'])
def test_update_index_survives_corrupt_overlay(tmp_path, content):
    _write_month(tmp_path, sidecar=False)
    _put_overlay(tmp_path, content)
    msgs = []
    hid = loguru_logger.add(lambda m: msgs.append(str(m)), level="WARNING")   # 项目日志走 loguru
    try:
        idx = ni.update_index(tmp_path, full=True)
    finally:
        loguru_logger.remove(hid)
    assert len(idx["papers"]) == 1 and idx["papers"][0]["citekey"] == "public2025Deep"
    assert not idx["papers"][0].get("decision_source")
    assert any("overlay" in m for m in msgs)


def test_update_index_without_overlay_is_silent_noop(tmp_path):
    _write_month(tmp_path, sidecar=False)
    msgs = []
    hid = loguru_logger.add(lambda m: msgs.append(str(m)), level="WARNING")
    try:
        idx = ni.update_index(tmp_path, full=True)
    finally:
        loguru_logger.remove(hid)
    assert len(idx["papers"]) == 1
    assert not [m for m in msgs if "overlay" in m]


# ---------------- 回填规则 ----------------

def _digest(path, segs, name):
    (path / name).write_text(json.dumps({"digest_id": name, "segments": segs}, ensure_ascii=False),
                             encoding="utf-8")


def _dseg(title, doi, decision, decided_at, model="deepseek-v4-flash", pv="filter-v3@0854", stage="llm_judge"):
    return {"segment_id": 1, "paper_id": "p", "metadata": {"paper_id": "p", "title": title, "doi": doi},
            "filter_decision": {"decision": decision, "stage": stage, "model": model,
                                "prompt_version": pv, "decided_at": decided_at}}


def _bundle(notes_dir, month, title, doi, script_model="sonnet", final_model="opus-gen"):
    meta = PaperMetadata(paper_id="pm_" + doi, title=title, doi=doi)
    script = CloseReading(model=script_model, sections=[
        CloseReadSection(heading="a", sentences=[CloseReadSentence(text="x")])])
    final = CloseReading(model=final_model, sections=[
        CloseReadSection(heading="终稿", sentences=[CloseReadSentence(text="y")])])
    seg = pi.build_segment(meta, "abs", "摘要", script, "一句话")
    d = notes_dir / "manual" / month
    d.mkdir(parents=True, exist_ok=True)
    (d / "pm.paper.json").write_text(json.dumps({
        "month": month, "status": "final", "paper_id": "pm_" + doi,
        "segment": seg.model_dump(mode="json"),
        "close_reading_script": script.model_dump(mode="json"),
        "close_reading_final": final.model_dump(mode="json")}, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def world(tmp_path):
    notes, digest = tmp_path / "notes", tmp_path / "digest"
    (digest / "_archive" / "misc").mkdir(parents=True)
    notes.mkdir()
    papers = [
        _entry("multi2026A", doi="10.1/multi", title="Multi Digest", decision="INCLUDE"),
        _entry("mism2026B", doi="10.1/mism", title="Mismatch", decision="INCLUDE"),
        _entry("upg2026C", doi="10.1/upg", title="Upgraded Paper", decision="INCLUDE"),
        _entry("none2026D", doi="10.1/none", title="Never In Digest", decision="MAYBE"),
        _entry("man2026E", doi="10.1/man", title="Manual Paper", series="manual", month="2026-08"),
        _entry("bk2026F", title="A Book", series="book"),
        _entry("multi2026A", doi="10.1/multi", title="Multi Digest", duplicate_of="multi2026A",
               month="2026-02"),
    ]
    (notes / "literature_index.json").write_text(
        json.dumps({"papers": papers}, ensure_ascii=False), encoding="utf-8")
    # multi：两份 digest，晚的那份才算（且 decision 与索引一致）；早的那份是另一个模型
    _digest(digest, [_dseg("Multi Digest", "10.1/multi", "INCLUDE", "2026-03-01T10:00:00", model="old-model")],
            "digest_20260301_100000.json")
    _digest(digest, [_dseg("Multi Digest", "10.1/multi", "INCLUDE", "2026-05-02T09:00:00", model="sonnet")],
            "digest_20260502_090000.json")
    # mism：digest 说 MAYBE、索引是 INCLUDE → 不回填
    _digest(digest, [_dseg("Mismatch", "10.1/mism", "MAYBE", "2026-04-01T10:00:00")],
            "digest_20260401_100000.json")
    # upg：digest 里是 MAYBE，08-08 扫描把它升成 INCLUDE
    _digest(digest, [_dseg("Upgraded Paper", "10.1/upg", "MAYBE", "2026-02-01T10:00:00")],
            "digest_20260201_100000.json")
    scan = [{"citekey": "upg2026C", "title": "Upgraded Paper", "doi": "10.1/upg", "arxiv_id": None,
             "old_decision": "MAYBE", "new_decision": "INCLUDE"},
            {"citekey": "x", "title": "x", "doi": "10.1/other", "arxiv_id": None,
             "old_decision": "MAYBE", "new_decision": "MAYBE"}]
    scan_path = digest / "_archive" / "misc" / "promotion_scan_20260808.json"
    scan_path.write_text(json.dumps(scan), encoding="utf-8")
    _bundle(notes, "2026-08", "Manual Paper", "10.1/man")
    return notes, digest, scan_path


def _by_key(overlay):
    return {(r["series"], r["dedup_key"]): r["fields"] for r in overlay["records"]}


def test_backfill_rules(world):
    notes, digest, scan = world
    overlay, report = BF.build_overlay(notes, digest, scan)
    r = _by_key(overlay)
    # 规则 1：多份 digest 取最晚；digest 只有请求值 → verified=False
    f = r[("auto", "doi:10.1/multi")]
    assert f["decision_model"] == "sonnet" and f["decision_model_verified"] is False
    assert f["decision_source"] == "digest" and f["decision_at"] == "2026-05-02"
    assert f["decision_prompt_version"] == "filter-v3@0854"
    assert f["provenance_origin"] == "backfill-digest-json"
    # decision 不一致 → 不回填、列入报告，落到 unknown-legacy
    f = r[("auto", "doi:10.1/mism")]
    assert f == {"decision_source": "unknown-legacy", "provenance_origin": "unknown-legacy"}
    assert [m["citekey"] for m in report["rule1_digest"]["mismatches"]] == ["mism2026B"]
    # 规则 2：复审升级优先于 digest（digest 里它是 MAYBE，不该被当成不一致）
    f = r[("auto", "doi:10.1/upg")]
    assert f["decision_source"] == "promotion-scan-2026-08-08"
    assert f["decision_model"] == "deepseek-v4-flash" and f["decision_model_verified"] is False
    assert f["decision_prompt_version"] == "filter-v3@08540624" and f["decision_at"] == "2026-08-08"
    assert f["decision_model_note"] and f["provenance_origin"] == "backfill-promotion-scan"
    # 规则 3：manual 取脚本草稿模型 + 终稿署名
    f = r[("manual", "doi:10.1/man")]
    assert f["decision_source"] == "manual" and f["closeread_model"] == "sonnet"
    assert f["closeread_model_verified"] is False and f["closeread_final_by"] == "opus-gen"
    assert f["provenance_origin"] == "backfill-bundle"
    # 规则 4：digest 没见过的 auto / 书籍 → unknown-legacy，且不推测任何模型
    for key in (("auto", "doi:10.1/none"), ("book", "title:" + ni._norm_title("A Book"))):
        assert r[key] == {"decision_source": "unknown-legacy", "provenance_origin": "unknown-legacy"}
    # 每个 (series, 身份键) 一条；duplicate 副本与 keeper 合并成一条
    assert len(overlay["records"]) == 6
    assert report["manual_closeread_model_backfilled"] == 1
    assert report["unknown_legacy_keepers"] == 3


def test_backfill_upgrade_requires_md_marker_when_md_readable(world):
    notes, digest, scan = world
    # 给 upgraded 条目挂上能读到的 md，但裁决行没有「复审升级」标记 → 不算升级条目
    idx = json.loads((notes / "literature_index.json").read_text(encoding="utf-8"))
    for e in idx["papers"]:
        if e["citekey"] == "upg2026C":
            e.update(note_file="n.md", note_line=1)
    (notes / "literature_index.json").write_text(json.dumps(idx), encoding="utf-8")
    (notes / "n.md").write_text("## x [@upg2026C]\n**裁决**: `INCLUDE` · conf 0.5\n", encoding="utf-8")
    _, report = BF.build_overlay(notes, digest, scan)
    assert report["rule2_promotion"]["no_md_marker"] == 1
    (notes / "n.md").write_text("## x [@upg2026C]\n**裁决**: `INCLUDE` · 复审升级 2026-08-08\n",
                                encoding="utf-8")
    _, report = BF.build_overlay(notes, digest, scan)
    assert report["rule2_promotion"]["matched"] == 1


def test_backfill_cli_dry_run_writes_nothing_then_apply(world, monkeypatch, capsys):
    notes, digest, scan = world
    base = ["backfill_provenance.py", "--notes-dir", str(notes), "--digest-dir", str(digest),
            "--promotion-scan", str(scan)]
    monkeypatch.setattr("sys.argv", base)
    BF.main()
    assert not (notes / "_provenance").exists()               # 缺省 dry-run：什么都不写
    monkeypatch.setattr("sys.argv", base + ["--apply", "--dry-run"])
    BF.main()
    assert not (notes / "_provenance").exists()               # 显式 --dry-run 优先
    monkeypatch.setattr("sys.argv", base + ["--apply"])
    BF.main()
    ov = json.loads((notes / "_provenance" / "overlay.json").read_text(encoding="utf-8"))
    assert ov["schema"] == 1 and len(ov["records"]) == 6
    assert (notes / "_provenance" / "backfill_report.json").exists()
    # 写出的 overlay 能被索引合并逻辑直接消费
    papers = json.loads((notes / "literature_index.json").read_text(encoding="utf-8"))["papers"]
    st = ni.apply_provenance_overlay(papers, ni.load_provenance_overlay(notes))
    assert st["unmatched"] == 0 and st["entries_filled"] == len(papers)


# ---------------- 分组报表 ----------------

def test_report_groups_sum_to_keepers_and_split_verified():
    papers = [
        _entry("a2026A", doi="10.1/1", decision_model="sonnet", decision_model_verified=True),
        _entry("b2026B", doi="10.1/2", decision_model="sonnet", decision_model_verified=False,
               decision="MAYBE", tag_counts={"citable": 4}),
        _entry("c2026C", doi="10.1/3", decision_model="deepseek-v4-flash", decision_model_verified=False),
        _entry("d2026D", doi="10.1/4"),                                           # 未知
        _entry("a2026A", doi="10.1/1", duplicate_of="a2026A", decision_model="zzz"),   # 副本不计
    ]
    rep = RP.group_report(papers, "decision_model")
    assert rep["keepers"] == 4 and rep["total_in_groups"] == 4
    rows = {r["group"]: r for r in rep["rows"]}
    assert rows["sonnet"]["n"] == 2 and rows["sonnet"]["verified_ratio"] == 0.5
    assert rows["sonnet"]["include_ratio"] == 0.5 and rows["sonnet"]["avg_citable"] == 3.0
    assert rows[RP.UNKNOWN]["n"] == 1 and rows[RP.UNKNOWN]["verified_ratio"] is None
    rep2 = RP.group_report(papers, "decision_model", split_verified=True)
    assert {r["group"] for r in rep2["rows"]} == {
        "sonnet [实测]", "sonnet [请求值]", "deepseek-v4-flash [请求值]", RP.UNKNOWN}
    assert rep2["total_in_groups"] == 4
    assert RP.group_report(papers, "decision_model", series="manual")["keepers"] == 0
