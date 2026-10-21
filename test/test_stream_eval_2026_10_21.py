# -*- coding: utf-8 -*-
"""WO-8b 持续评测：stream_labels → S-stream 层、窗口滚动、抽样权重还原漏判、报告、指纹覆盖告警、
build-gold CLI 端到端。全部 tmp 夹具，不调 LLM、不联网、不读真实 output/。
"""
import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar import confirm as C                                       # noqa: E402
from src.scholar import filter_eval as FE                                  # noqa: E402


def lab(i, decision, filt, cat, conf=0.9, weight=1.0, when="2026-10-19T10:00:00", checked=None, override=None):
    return {"schema": 1, "confirmed_at": when, "dedup_key": "doi:10.9/s{}".format(i), "title": "Stream paper {}".format(i),
            "abstract": "abs {}".format(i), "category": cat, "user_decision": decision,
            "default_checked": (cat in ("include", "maybe")) if checked is None else checked,
            "override": ((decision == "keep") != (cat in ("include", "maybe"))) if override is None else override,
            "filter_decision": filt, "filter_confidence": conf, "sample_weight": weight,
            "prompt_version": "filter-v3@abcd1234"}


def test_stream_gold_items_mapping_and_sample():
    labels = [lab(1, "keep", "INCLUDE", "include"), lab(2, "drop", "MAYBE", "maybe", conf=0.6),
              lab(3, "keep", "EXCLUDE", "lowconf_exclude", conf=0.6), lab(4, "drop", "EXCLUDE", "audit", conf=0.95, weight=34.0)]
    items, st = FE.stream_gold_items(labels)
    by = {i["dedup_key"]: i for i in items}
    assert {i["stratum"] for i in items} == {"S-stream"} and st["n_in_window"] == 4
    assert by["doi:10.9/s1"]["label"] == "positive" and by["doi:10.9/s2"]["label"] == "negative"
    assert by["doi:10.9/s3"]["label"] == "positive" and by["doi:10.9/s3"]["override"] is True
    s4 = by["doi:10.9/s4"]
    assert s4["label"] == "negative" and s4["sample"]["weight"] == 34.0 and s4["sample"]["prod_verdict"] == "EXCLUDE"
    assert s4["sample"]["conf_bin"] == FE.conf_bin(0.95) and by["doi:10.9/s1"]["sample"]["conf_bin"] == "-"
    assert by["doi:10.9/s1"]["anchored"] is True and by["doi:10.9/s3"]["anchored"] is False   # 默认勾选才算锚定
    assert all(i["label_source"] == "user_confirm" and i["split"] == FE.split_of(i["item_id"]) for i in items)


def test_window_rolls_by_confirmed_at_and_defaults_to_latest_label():
    now = datetime(2026, 10, 19, 10, 0, 0)
    labels = [lab(1, "keep", "INCLUDE", "include", when=now.isoformat()),
              lab(2, "keep", "INCLUDE", "include", when=(now - timedelta(weeks=11)).isoformat()),
              lab(3, "keep", "INCLUDE", "include", when=(now - timedelta(weeks=13)).isoformat())]
    items, st = FE.stream_gold_items(labels, window_weeks=12)
    assert sorted(i["dedup_key"] for i in items) == ["doi:10.9/s1", "doi:10.9/s2"]
    assert st["n_out_of_window"] == 1 and st["as_of"] == now.isoformat(timespec="seconds")
    items2, _ = FE.stream_gold_items(labels, window_weeks=4)
    assert [i["dedup_key"] for i in items2] == ["doi:10.9/s1"]
    # 显式 as_of：把窗口推回去
    items3, _ = FE.stream_gold_items(labels, as_of=now - timedelta(weeks=12), window_weeks=2)
    assert [i["dedup_key"] for i in items3] == ["doi:10.9/s3"]
    # 同一份标签重建 → 同样的 gold 字节
    assert FE.gold_bytes(FE.stream_gold_items(labels)[0]) == FE.gold_bytes(FE.stream_gold_items(list(reversed(labels)))[0])


def test_bad_rows_ignored():
    items, st = FE.stream_gold_items([{"dedup_key": "x", "user_decision": "maybe", "confirmed_at": "2026-10-19T00:00:00"},
                                      {"user_decision": "keep", "confirmed_at": "2026-10-19T00:00:00"},
                                      lab(1, "keep", "INCLUDE", "include", when="not-a-date")])
    assert items == [] and st["n_in_window"] == 0


def _entries(items, verdict_of):
    reps = [[{"item_id": i["item_id"], "verdict": verdict_of(i), "confidence": 0.9, "stage": "llm_judge",
              "provider": "claude-agent", "served_model": "m", "fell_back": False} for i in items]]
    return FE.build_entries(items, reps)


def test_s_stream_metrics_restore_miss_rate_with_weights():
    labels = [
        lab(1, "keep", "EXCLUDE", "lowconf_exclude", conf=0.6, weight=1.0),     # 漏判 1/2（低置信池全取）
        lab(2, "drop", "EXCLUDE", "lowconf_exclude", conf=0.6, weight=1.0),
        lab(3, "keep", "EXCLUDE", "audit", conf=0.95, weight=10.0),             # 抽查：1 个漏判代表 10 个
        lab(4, "drop", "EXCLUDE", "audit", conf=0.95, weight=10.0),
        lab(5, "drop", "EXCLUDE", "audit", conf=0.95, weight=10.0),
        lab(6, "keep", "INCLUDE", "include"), lab(7, "drop", "INCLUDE", "include"),
        lab(8, "drop", "MAYBE", "maybe", conf=0.6),
    ]
    items, _ = FE.stream_gold_items(labels)
    # 本次复跑：把 s1、s3、s6 保留（MAYBE/INCLUDE），其余 EXCLUDE；s7 被误判 INCLUDE
    now_v = {"doi:10.9/s1": "MAYBE", "doi:10.9/s3": "INCLUDE", "doi:10.9/s6": "INCLUDE", "doi:10.9/s7": "INCLUDE"}
    m = FE.compute_metrics(_entries(items, lambda i: now_v.get(i["dedup_key"], "EXCLUDE")))["s_stream"]
    pm = m["prod_miss"]
    assert (pm["k"], pm["n"]) == (2, 5)
    assert pm["weighted"] == pytest.approx((1 + 10) / (1 + 1 + 10 + 10 + 10))        # Σw·pos / Σw
    assert m["prod_miss_by_category"] == {"audit": {"k": 1, "n": 3}, "lowconf_exclude": {"k": 1, "n": 2}}
    assert (m["prod_false_keep"]["k"], m["prod_false_keep"]["n"]) == (2, 3)          # 取消 s7、s8
    assert (m["now_keep_pos"]["k"], m["now_keep_pos"]["n"]) == (3, 3)                # keep 的 s1/s3/s6 全保留
    assert (m["now_false_include"]["k"], m["now_false_include"]["n"]) == (1, 5)      # drop 里只有 s7 被判 INCLUDE
    assert m["n_anchored"] == 3 and m["n"] == 8


def test_no_stream_items_means_no_s_section():
    m = FE.compute_metrics([])
    assert m["s_stream"] == {"n": 0}


def _run_cfg():
    return {"gold_version": "gold-v2@deadbeef", "split": "all", "strata": ["S-stream"], "smoke": False, "repeats": 1,
            "seed": 1, "abstract_mode": "prod", "rag_on": False, "batch_size": 20,
            "prompt_fingerprint": "filter-v3@x", "requested_model": "sonnet", "main_provider": "claude-agent"}


def test_report_prints_s_stream_block_and_paired_compare():
    labels = [lab(i, "keep" if i % 2 else "drop", "EXCLUDE", "audit", conf=0.95, weight=5.0) for i in range(6)]
    items, _ = FE.stream_gold_items(labels)
    base = FE.assemble_run("run_A", _run_cfg(), {"n": 6}, items, [[
        {"item_id": i["item_id"], "verdict": "INCLUDE", "confidence": .9, "stage": "llm_judge", "provider": "p",
         "served_model": "m", "fell_back": False} for i in items]], status="complete", started_at="t")
    new = FE.assemble_run("run_B", _run_cfg(), {"n": 6}, items, [[
        {"item_id": i["item_id"], "verdict": "EXCLUDE" if n == 0 else "INCLUDE", "confidence": .9, "stage": "llm_judge",
         "provider": "p", "served_model": "m", "fell_back": False} for n, i in enumerate(items)]],
        status="complete", started_at="t")
    txt = FE.format_report(new, baseline=base)
    assert "S 入库确认标签 6 条" in txt and "加权还原=50.0%" in txt and "锚定 0 条" in txt
    assert "配对比较" in txt                      # 只报告：报告函数不抛、不返回放行/拒绝


def test_s_stream_is_in_strata_and_default_run_selection():
    assert "S-stream" in FE.STRATA and "S-stream" in FE.DEFAULT_RUN_STRATA
    labels = [lab(1, "keep", "INCLUDE", "include")]
    items, _ = FE.stream_gold_items(labels)
    sel, _ = FE.select_items(items, split="test")        # --split 不作用于 S（规模小，一律全收）
    assert len(sel) == 1
    assert FE.gold_counts(items)["S-stream"]["n"] == 1


def test_fingerprint_coverage(tmp_path):
    edir = tmp_path / "e"
    assert FE.fingerprint_coverage(edir, "filter-v3@x")["runs_dir_exists"] is False        # CI：无数据 → 不误报
    rd = edir / "runs"
    rd.mkdir(parents=True)

    def put(name, fp, status="complete", smoke=False):
        (rd / (name + ".json")).write_text(json.dumps(
            {"run_id": name, "status": status, "config": {"prompt_fingerprint": fp, "smoke": smoke}}), encoding="utf-8")
    (rd / "run_bad.json").write_text("{", encoding="utf-8")
    put("run_other", "filter-v3@other")
    put("run_smoke", "filter-v3@x", smoke=True)
    put("run_partial", "filter-v3@x", status="partial")
    cov = FE.fingerprint_coverage(edir, "filter-v3@x")
    assert cov["runs_dir_exists"] and cov["has_record"] is False and cov["n_runs_total"] == 3
    put("run_ok", "filter-v3@x")
    cov = FE.fingerprint_coverage(edir, "filter-v3@x")
    assert cov["has_record"] and cov["run_ids"] == ["run_ok"]


def _cli():
    spec = importlib.util.spec_from_file_location("filter_eval_cli2", Path(__file__).resolve().parents[1] / "scripts" / "filter_eval.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_cli_check_never_fails_and_skips_without_data(tmp_path, capsys):
    cli = _cli()
    notes = tmp_path / "notes"
    assert cli.main(["--notes-dir", str(notes), "--digest-dir", str(tmp_path / "d"), "check"]) == 0
    assert "跳过" in capsys.readouterr().out


def test_cli_build_gold_with_stream_labels_new_version(tmp_path):
    cli = _cli()
    notes, digest = tmp_path / "notes", tmp_path / "digest"
    (notes / "manual" / "B1").mkdir(parents=True)
    (digest / "_archive" / "excluded").mkdir(parents=True)
    (notes / "literature_index.json").write_text(json.dumps({"papers": []}), encoding="utf-8")
    (notes / "abstracts.json").write_text(json.dumps({"abstracts": {}}), encoding="utf-8")
    bundle = {"paper_id": "p1", "segment": {"original_abstract": "abs", "metadata": {"title": "Man 1", "doi": "10.2000/x"}}}
    (notes / "manual" / "B1" / "a.paper.json").write_text(json.dumps(bundle), encoding="utf-8")
    bib = tmp_path / "a.bib"
    bib.write_text("", encoding="utf-8")
    base = ["--notes-dir", str(notes), "--digest-dir", str(digest), "--bib-arxiv", str(bib),
            "--bib-ieee", str(tmp_path / "none.bib")]
    assert cli.main(base + ["registry-export", "--kind", "batches"]) == 0
    FE._write_csv(FE.registry_paths(FE.eval_dir(notes), 1)["batches"], ("batch", "n_papers", "class", "note"),
                  [("B1", 1, "core", "")])
    assert cli.main(base + ["registry-import", "--kind", "batches"]) == 0
    assert cli.main(base + ["build-gold"]) == 0
    v1 = (FE.eval_dir(notes) / "gold_v1.jsonl").read_bytes()
    # 写两条确认标签（走真实 record 路径的格式）
    lp = C.labels_path(notes)
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text("\n".join(json.dumps(lab(i, "keep", "INCLUDE", "include")) for i in (1, 2)) + "\n", encoding="utf-8")
    # 新版本 + 沿用 v1 登记
    assert cli.main(base + ["--version", "2", "build-gold"]) == 2                      # 没登记 v2 → 拒绝
    assert cli.main(base + ["--version", "2", "build-gold", "--registry-version", "1", "--stream-labels"]) == 0
    items = [json.loads(l) for l in (FE.eval_dir(notes) / "gold_v2.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum(i["stratum"] == "S-stream" for i in items) == 2
    meta = json.loads((FE.eval_dir(notes) / "gold_v2.meta.json").read_text(encoding="utf-8"))
    assert meta["stream"]["n_in_window"] == 2 and meta["stream"]["labels_sha256"]
    assert (FE.eval_dir(notes) / "gold_v1.jsonl").read_bytes() == v1                    # v1 不受影响
    assert cli.main(base + ["--version", "2", "stats"]) == 0
    # 幂等
    assert cli.main(base + ["--version", "2", "build-gold", "--registry-version", "1", "--stream-labels"]) == 0
