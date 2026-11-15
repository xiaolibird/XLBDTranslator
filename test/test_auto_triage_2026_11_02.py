# -*- coding: utf-8 -*-
"""自动筛选（替代入库确认弹窗）：解析鲁棒性、分流规则、失败保持待确认、机器标签不进评测/漂移、端到端。

全部 tmp 夹具 + 桩：不调 LLM、不弹窗、不联网、不碰真实 output/。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from src.scholar import auto_triage as AT                    # noqa: E402
from src.scholar import confirm as C                          # noqa: E402
from src.scholar import filter_eval as FE                     # noqa: E402
from src.scholar import profile_drift as PD                   # noqa: E402

import auto_triage as CLI                                     # noqa: E402

NOW = datetime(2026, 11, 2, 10, 0, 0)


def _item(pid, title, cat="include", abstract="abstract text"):
    return {"paper_id": pid, "title": title, "abstract": abstract, "category": cat,
            "dedup_key": "title:" + AT._norm_title(title), "decision": "INCLUDE", "confidence": 0.8,
            "one_line": "x", "bucket": ["A"], "state": "pending", "default_checked": cat in ("include", "maybe"),
            "run_id": "digest_20261102_090000"}


def _queue(tmp_path, items, run_date="2026-11-02"):
    nd = tmp_path / "notes"
    (nd / C.PENDING_DIRNAME).mkdir(parents=True)
    qp = nd / C.PENDING_DIRNAME / "confirm_{}.json".format(run_date)
    qp.write_text(json.dumps({"schema": 1, "created_at": NOW.isoformat(), "run_date": run_date,
                              "runs": {"digest_20261102_090000": {}}, "items": items}, ensure_ascii=False),
                  encoding="utf-8")
    return nd, qp


def _verdict_json(spec):
    return json.dumps({"verdicts": [{"id": i, "verdict": v, "priority": "normal", "reason": "r"}
                                    for i, v in spec.items()]})


# ------------------------------------------------------------------ 解析

def test_parse_tolerates_fences_and_prose():
    raw = "好的\n```json\n" + _verdict_json({"a": "keep"}) + "\n```"
    assert AT.parse_verdicts(raw, {"a"})["a"]["verdict"] == "keep"


def test_parse_drops_unknown_duplicate_and_invalid():
    raw = json.dumps({"verdicts": [
        {"id": "a", "verdict": "keep", "priority": "weird", "reason": "r"},
        {"id": "a", "verdict": "drop", "reason": "dup"},
        {"id": "zzz", "verdict": "keep"},
        {"id": "b", "verdict": "maybe"}]})
    out = AT.parse_verdicts(raw, {"a", "b"})
    assert set(out) == {"a"} and out["a"]["verdict"] == "keep" and out["a"]["priority"] == "normal"


# ------------------------------------------------------------------ 分流

def test_routing_categories_dup_and_llm(tmp_path):
    rows = [(Path("q"), _item("p1", "Longitudinal causal inference under informative missingness", "include")),
            (Path("q"), _item("p2", "Some radiology foundation model for chest images", "maybe")),
            (Path("q"), _item("p3", "Diffusion language models are fast decoders now", "lowconf_exclude")),
            (Path("q"), _item("p4", "Already in the library via a doi key paper", "include"))]
    lib = {AT._norm_title("Already in the library via a doi key paper"): "x2026Already"}
    calls = []

    def call(prompt):
        calls.append(prompt)
        return _verdict_json({"p1": "keep", "p2": "drop"})
    tr = AT.triage_items(rows, None, lib_titles=lib, call=call)
    assert [i["paper_id"] for _, i, _ in tr.keep] == ["p1"]
    assert [i["paper_id"] for _, i, _ in tr.drop_llm] == ["p2"]
    assert [i["paper_id"] for _, i, _ in tr.drop_dup] == ["p4"]
    assert [i["paper_id"] for _, i in tr.drop_cat] == ["p3"]
    assert len(calls) == 1 and "p3" not in calls[0] and "p4" not in calls[0]   # 排除档/库内已有不送审


def test_llm_failure_leaves_undecided_not_dropped(tmp_path):
    rows = [(Path("q"), _item("p1", "A genuinely relevant ICU trajectory study")),
            (Path("q"), _item("p2", "Another candidate for the missingness line"))]

    def boom(prompt):
        raise RuntimeError("所有 LLM provider 均不可用")
    tr = AT.triage_items(rows, None, call=boom)
    assert len(tr.undecided) == 2 and not tr.keep and not tr.drop_llm


def test_missing_verdict_retried_then_undecided():
    rows = [(Path("q"), _item("p1", "Candidate one title is long enough")),
            (Path("q"), _item("p2", "Candidate two title is long enough"))]
    n = {"c": 0}

    def call(prompt):
        n["c"] += 1
        return _verdict_json({"p1": "keep"})            # 永远漏 p2
    tr = AT.triage_items(rows, None, call=call)
    assert n["c"] == 2                                  # 漏判重试一次
    assert [i["paper_id"] for _, i, _ in tr.keep] == ["p1"]
    assert [i["paper_id"] for _, i, _ in tr.undecided] == ["p2"]


# ------------------------------------------------------------------ 落盘与标签

def test_apply_marks_agent_source_and_keeps_undecided_pending(tmp_path):
    items = [_item("p1", "Keep this one because it is relevant"),
             _item("p2", "Drop this one because it is irrelevant"),
             _item("p3", "Flaky one the model never answered")]
    nd, qp = _queue(tmp_path, items)
    n_calls = {"c": 0}

    def call(prompt):
        n_calls["c"] += 1
        if "p3" in prompt:                              # 每批 1 篇：只有 p3 那一批失败
            raise RuntimeError("rate limited")
        return _verdict_json({"p1": "keep", "p2": "drop"})
    tr = AT.triage_items(C.pending_items(nd), None, call=call, batch_size=1)
    assert [i["paper_id"] for _, i, _ in tr.undecided] == ["p3"]
    n = AT.apply(nd, tr, now=NOW)
    q = C.load_queue(qp)
    st = {i["paper_id"]: i["state"] for i in q["items"]}
    assert st == {"p1": "keep", "p2": "drop", "p3": "pending"}      # 失败的不丢弃，保持待确认
    assert all(i["decision_source"] == "auto_agent" for i in q["items"] if i["state"] != "pending")
    labels = [json.loads(l) for l in C.labels_path(nd).read_text(encoding="utf-8").splitlines()]
    assert n == len(labels) == 2 and {l["user_decision_source"] for l in labels} == {"auto_agent"}


def test_machine_labels_excluded_from_eval_and_drift(tmp_path):
    items = [_item("p1", "Keep this one because it is relevant")]
    nd, qp = _queue(tmp_path, items)
    tr = AT.triage_items(C.pending_items(nd), None, call=lambda p: _verdict_json({"p1": "keep"}))
    AT.apply(nd, tr, now=NOW)
    labels = C.load_stream_labels(nd)
    assert len(labels) == 1
    gold, _ = FE.stream_gold_items(labels, as_of=NOW)
    assert gold == []                                   # 不进评测集
    # 漂移分析的输入过滤：auto_agent 行不得作为用户偏好信号进入窗口
    seen = []
    PD.analyze(labels, now=NOW, embed_fn=lambda xs: seen.extend(xs) or [[1.0, 0.0] for _ in xs])
    assert seen == []                                   # 过滤后无标签 → 从未去嵌入任何文本


# ------------------------------------------------------------------ 端到端

def test_cli_end_to_end(tmp_path, capsys):
    items = [_item("p1", "Self censoring longitudinal causal inference missingness"),
             _item("p2", "Histopathology foundation model for slides", "maybe"),
             _item("p3", "Diffusion language model decoding", "lowconf_exclude")]
    nd, qp = _queue(tmp_path, items)
    dd = tmp_path / "digest"
    dd.mkdir()
    notified, ran = [], []
    rc = CLI.main(["--notes-dir", str(nd), "--digest-dir", str(dd), "--date", "2026-11-02"],
                  llm=object(), notify=lambda t, m: notified.append((t, m)),
                  runner=lambda argv: ran.append(argv) or 0, now=NOW, today=NOW.date(),
                  call=lambda p: _verdict_json({"p1": "keep", "p2": "drop"}))
    assert rc == 0
    q = C.load_queue(qp)
    assert {i["paper_id"]: i["state"] for i in q["items"]} == {"p1": "keep", "p2": "drop", "p3": "drop"}
    assert all(i.get("ingested_at") for i in q["items"] if i["state"] == "keep")   # 入库成功后标记
    assert ran and "--week" in ran[0] and "2026-11-02" in ran[0]
    rep = (nd / C.PENDING_DIRNAME / "auto_triage_2026-11-02.md").read_text(encoding="utf-8")
    assert "保留 1" in rep and "Self censoring" in rep and "Histopathology" in rep
    assert notified and "保留 1" in notified[0][1]


def test_cli_dry_run_writes_nothing(tmp_path):
    nd, qp = _queue(tmp_path, [_item("p1", "Self censoring longitudinal causal inference missingness")])
    dd = tmp_path / "digest"
    dd.mkdir()
    notified, ran = [], []
    CLI.main(["--notes-dir", str(nd), "--digest-dir", str(dd), "--date", "2026-11-02", "--dry-run"],
             llm=object(), notify=lambda t, m: notified.append(1), runner=lambda a: ran.append(a) or 0,
             now=NOW, today=NOW.date(), call=lambda p: _verdict_json({"p1": "keep"}))
    assert C.load_queue(qp)["items"][0]["state"] == "pending"
    assert not C.labels_path(nd).exists() and not ran and not notified


def test_cli_all_llm_failure_exits_1_and_keeps_pending(tmp_path):
    nd, qp = _queue(tmp_path, [_item("p1", "Self censoring longitudinal causal inference missingness")])
    dd = tmp_path / "digest"
    dd.mkdir()

    def boom(p):
        raise RuntimeError("down")
    rc = CLI.main(["--notes-dir", str(nd), "--digest-dir", str(dd), "--date", "2026-11-02"],
                  llm=object(), notify=lambda t, m: None, runner=lambda a: 0,
                  now=NOW, today=NOW.date(), call=boom)
    assert rc == 1 and C.load_queue(qp)["items"][0]["state"] == "pending"
