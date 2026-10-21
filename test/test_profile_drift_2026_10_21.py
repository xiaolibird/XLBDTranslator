# -*- coding: utf-8 -*-
"""WO-8c 月度画像漂移：成簇判据（篇数 + 占比）、补勾/取消两类、语义聚类（桩 embedding）、
不改画像、无成簇不通知、报告内容。不调 LLM、不联网、不弹通知、不读真实 output/。
"""
import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar import confirm as C                                       # noqa: E402
from src.scholar import profile_drift as PD                                # noqa: E402
from src.scholar import thresholds as TH                                   # noqa: E402

NOW = datetime(2026, 11, 1, 10, 30, 0)
_n = [0]


def L(decision, filt, *, reason=None, bucket=None, weeks_ago=1, title=None, cat=None):
    _n[0] += 1
    t = (NOW - timedelta(weeks=weeks_ago)).isoformat(timespec="seconds")
    return {"dedup_key": "k{}".format(_n[0]), "confirmed_at": t, "user_decision": decision, "filter_decision": filt,
            "exclude_reason": reason, "filter_bucket": bucket or [], "title": title or "Paper {}".format(_n[0]),
            "abstract": "abs", "one_line": "用处", "filter_confidence": 0.6,
            "category": cat or ("lowconf_exclude" if filt == "EXCLUDE" else "include")}


def added(n, reason="X1", **kw):
    return [L("keep", "EXCLUDE", reason=reason, **kw) for _ in range(n)]


def removed(n, bucket=("D",), **kw):
    return [L("drop", "INCLUDE", bucket=list(bucket), **kw) for _ in range(n)]


def test_reason_cluster_needs_both_count_and_share():
    labels = added(5, "X1") + added(3, "X2")                        # X1: 5/8=62% 且 ≥5 → 成簇
    a = PD.analyze(labels, now=NOW)
    reason = [c for c in a.clusters if c.kind == "reason"]
    assert [(c.key, c.n) for c in reason] == [("X1", 5)] and reason[0].share == pytest.approx(5 / 8)
    # 篇数够但占比 < 50%：不成簇
    a = PD.analyze(added(5, "X1") + added(6, "X2") + added(6, "X3"), now=NOW)
    assert [c for c in a.clusters if c.kind == "reason" and c.key == "X1"] == []
    # 占比够但篇数 < 5：不成簇
    assert PD.analyze(added(4, "X1"), now=NOW).clusters == []
    # 边界：恰好 5 篇、恰好 50%
    a = PD.analyze(added(5, "X1") + added(5, "X2"), now=NOW)
    assert sorted(c.key for c in a.clusters if c.kind == "reason") == ["X1", "X2"]


def test_thresholds_are_the_wired_defaults():
    import inspect
    sig = inspect.signature(PD.analyze)
    assert sig.parameters["min_cluster"].default is TH.DRIFT_MIN_CLUSTER == 5
    assert sig.parameters["min_share"].default is TH.DRIFT_MIN_SHARE == 0.5
    assert sig.parameters["lookback_weeks"].default is TH.DRIFT_LOOKBACK_WEEKS == 4


def test_removed_cluster_by_bucket_and_window():
    labels = removed(6, ("D",)) + removed(2, ("A",))
    a = PD.analyze(labels, now=NOW)
    cs = [c for c in a.clusters if c.side == "removed"]
    assert [(c.kind, c.key, c.n) for c in cs] == [("bucket", "D", 6)]
    # 窗口外（5 周前）的不计
    old = removed(6, ("D",), weeks_ago=5)
    assert PD.analyze(old, now=NOW).clusters == [] and PD.analyze(old, now=NOW).n_labels == 0


def test_agreements_are_not_disagreements():
    labels = ([L("drop", "EXCLUDE", reason="X1") for _ in range(10)]       # 同意筛选器：不是分歧
              + [L("keep", "INCLUDE", bucket=["A"]) for _ in range(10)])
    a = PD.analyze(labels, now=NOW)
    assert a.n_added == 0 and a.n_removed == 0 and a.clusters == []


def test_semantic_clustering_with_stub_embedding():
    topic_a = added(5, "X1", title="graph topic")
    topic_b = added(5, "X2", title="radiology topic")
    labels = topic_a + topic_b + added(1, "X3", title="odd one")

    def emb(texts):
        out = []
        for t in texts:
            v = np.zeros(3)
            v[0 if t.startswith("graph") else 1 if t.startswith("radiology") else 2] = 1.0
            out.append(v)
        return np.array(out)

    a = PD.analyze(labels, now=NOW, embed_fn=emb, min_share=0.4)
    sem = [c for c in a.clusters if c.kind == "semantic"]
    assert a.semantic_status == "ok" and sorted(c.n for c in sem) == [5, 5]
    assert {r["title"] for c in sem for r in c.rows} >= {"graph topic", "radiology topic"}
    # 默认 50% 门槛下 5/11 都不够
    assert [c for c in PD.analyze(labels, now=NOW, embed_fn=emb).clusters if c.kind == "semantic"] == []


def test_semantic_unavailable_degrades_not_crashes():
    labels = added(6, "X1")
    assert PD.analyze(labels, now=NOW, embed_fn=None).semantic_status == "unavailable"

    def boom(texts):
        raise RuntimeError("ollama down")
    a = PD.analyze(labels, now=NOW, embed_fn=boom)
    assert a.semantic_status == "unavailable" and [c.kind for c in a.clusters] == ["reason"]
    assert PD.analyze(added(2), now=NOW, embed_fn=boom).semantic_status == "skipped_few"


def test_report_lists_papers_and_suggestion_with_profile_line():
    profile = {"exclusion_dims": "# 排除\nX1. 纯影像诊断，无缺失处理。\nX2. 其它", "inclusion_dims": "A. 缺失机制\nD. 跨域迁移"}
    labels = added(5, "X1", title="Chest CT lesion study") + removed(5, ("D",), title="Domain shift survey")
    a = PD.analyze(labels, now=NOW)
    txt = PD.render_report(a, profile=profile)
    assert "# 研究画像漂移提醒（2026-11）" in txt and "不会自动改画像" in txt
    assert "Chest CT lesion study" in txt and "Domain shift survey" in txt
    assert "X1. 纯影像诊断" in txt and "D. 跨域迁移" in txt              # 建议里带现行维度原文
    assert "补勾簇 · 按排除原因" in txt and "取消簇 · 按纳入维度" in txt and "锚定" in txt
    assert "X1" in PD.summary_text(a)
    assert PD.dim_line("X9", profile) == "" and PD.dim_line("A", profile).startswith("A.")


def _script():
    spec = importlib.util.spec_from_file_location("profile_drift_cli", Path(__file__).resolve().parents[1] / "scripts" / "profile_drift.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _write_labels(notes, rows):
    lp = C.labels_path(notes)
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


def test_cli_writes_report_and_notifies_only_when_clustered(tmp_path):
    cli, notes = _script(), tmp_path / "notes"
    sent = []
    notify = lambda t, x: sent.append((t, x))                                       # noqa: E731
    assert cli.main(["--notes-dir", str(notes), "--no-semantic"], notify=notify, now=NOW) == 0   # 无标签文件
    assert sent == []
    _write_labels(notes, added(2) + removed(2))
    assert cli.main(["--notes-dir", str(notes), "--no-semantic"], notify=notify, now=NOW) == 0
    assert sent == [] and not list(C.labels_path(notes).parent.glob("drift_*.md"))         # 无成簇：静默、不写报告
    _write_labels(notes, added(6, "X1"))
    assert cli.main(["--notes-dir", str(notes), "--no-semantic", "--dry-run"], notify=notify, now=NOW) == 0
    assert sent == [] and not list(C.labels_path(notes).parent.glob("drift_*.md"))         # dry-run 不写不通知
    assert cli.main(["--notes-dir", str(notes), "--no-semantic"], notify=notify, now=NOW) == 0
    rep = C.labels_path(notes).parent / "drift_2026-11.md"
    assert rep.exists() and "X1" in rep.read_text(encoding="utf-8")
    assert len(sent) == 1 and "补勾" in sent[0][1] and "drift_2026-11.md" in sent[0][1]


def test_cli_never_touches_profile(tmp_path):
    cli, notes = _script(), tmp_path / "notes"
    prof = Path(__file__).resolve().parents[1] / "config" / "research_profile.yaml"
    before = prof.read_bytes() if prof.exists() else b""
    _write_labels(notes, added(6, "X1"))
    cli.main(["--notes-dir", str(notes), "--no-semantic"], notify=lambda *a: None, now=NOW)
    assert (prof.read_bytes() if prof.exists() else b"") == before


def test_cli_bad_as_of_is_exit_2(tmp_path):
    assert _script().main(["--notes-dir", str(tmp_path), "--as-of", "not-a-date"]) == 2
