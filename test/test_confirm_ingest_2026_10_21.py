# -*- coding: utf-8 -*-
"""WO-8a 入库确认：osascript 封装（转义/解析/pytest 自毁）、待确认队列、分页弹窗会话、
挂起语义、stream_labels 落盘、ingest 闸门与手选覆盖、confirm_ingest 编排。

全部 tmp 夹具 + 桩：不弹窗、不发通知、不调 LLM、不联网、不碰真实 output/。
"""
import importlib.util
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar import confirm as C                                       # noqa: E402
from src.scholar import ingest as ing                                      # noqa: E402
from src.scholar import thresholds as TH                                   # noqa: E402
from src.scholar.schema import (DigestOutput, FilterDecision, PaperMetadata,   # noqa: E402
                                PaperSegment)
from src.utils import osa_dialog as O                                      # noqa: E402

NOW = datetime(2026, 10, 19, 10, 0, 0)


# ====================================================================== 桩

class FakeDialogs:
    """脚本化的弹窗：asks / chooses 是预置应答队列，记录每次调用，绝不碰 osascript。"""

    def __init__(self, asks=(), chooses=(), gui=True):
        self.asks, self.chooses, self.gui = list(asks), list(chooses), gui
        self.ask_calls, self.choose_calls, self.notified, self.opened = [], [], [], []

    def available(self):
        return self.gui

    def ask(self, message, buttons, default_button=None, **kw):
        self.ask_calls.append((message, list(buttons), kw))
        return self.asks.pop(0)

    def choose(self, items, defaults=(), **kw):
        self.choose_calls.append((list(items), list(defaults), kw))
        r = self.chooses.pop(0)
        return r(list(items), list(defaults)) if callable(r) else r

    def notify(self, title, text):
        self.notified.append((title, text))

    def open_path(self, path):
        self.opened.append(str(path))
        return True


def ask_ok(btn):
    return O.AskResult("ok", btn, False)


def pick(*idx_or_all):
    """choose 应答：按页内序号（0 起）选；'defaults' = 照单全收默认勾选。"""
    def f(items, defaults):
        if idx_or_all == ("defaults",):
            return O.ChooseResult("ok", list(defaults))
        return O.ChooseResult("ok", [items[i] for i in idx_or_all])
    return f


# ====================================================================== 夹具

def _meta(i, doi=True):
    return PaperMetadata(paper_id="p{}".format(i), title="Paper number {} on missing data".format(i),
                         doi=("10.1000/x{}".format(i) if doi else None), source_email_id="mail")


def _fd(i, decision, conf, stage="llm_judge", verdict=None):
    verdict = verdict or ("excluded" if decision == "EXCLUDE" else "included")
    return FilterDecision(paper_id="p{}".format(i), title="Paper number {}".format(i), verdict=verdict,
                          decision=decision, stage=stage, bucket=["C"] if decision != "EXCLUDE" else [],
                          exclude_reason=("X1" if decision == "EXCLUDE" else None), one_line="用处 {}".format(i),
                          confidence=conf, model="sonnet", prompt_version="filter-v3@abcd1234",
                          provider="claude-agent", served_model="claude-sonnet-5-5")


def make_digest(root: Path, stamp="20261019_090004", n_inc=2, n_maybe=1, n_low=3, n_high=10,
                first=0, with_stats=True):
    """造一个 digest run：入选 n_inc+n_maybe 篇，sidecar 里 n_low 篇低置信 + n_high 篇高置信 EXCLUDE + 1 篇黑名单。"""
    run_id = "digest_" + stamp
    segs, i = [], first
    for _ in range(n_inc):
        segs.append(PaperSegment(segment_id=i, paper_id="p{}".format(i), original_abstract="abs {}".format(i),
                                 metadata=_meta(i), filter_decision=_fd(i, "INCLUDE", 0.9), priority_score=0.5))
        i += 1
    for _ in range(n_maybe):
        segs.append(PaperSegment(segment_id=i, paper_id="p{}".format(i), original_abstract="abs {}".format(i),
                                 metadata=_meta(i), filter_decision=_fd(i, "MAYBE", 0.6), priority_score=0.3))
        i += 1
    out = DigestOutput(digest_id=run_id, title="t", description="d", segments=segs)
    root.mkdir(parents=True, exist_ok=True)
    out.save_to_file(root / (run_id + ".json"))
    papers = []
    for k in range(n_low + n_high + 1):
        j = i + k
        if k == n_low + n_high:
            fd = _fd(j, "EXCLUDE", None, stage="blacklist_keyword")
        else:
            fd = _fd(j, "EXCLUDE", 0.6 if k < n_low else 0.95)
        papers.append({"paper_id": "p{}".format(j), "reason": "r", "stage": fd.stage,
                       "metadata": _meta(j).model_dump(mode="json"),
                       "decision": fd.model_dump(mode="json"), "original_abstract": "excluded abs {}".format(j)})
    ed = root / "_archive" / "excluded"
    ed.mkdir(parents=True, exist_ok=True)
    (ed / (run_id + "_excluded.json")).write_text(json.dumps({"run_id": run_id, "papers": papers}), encoding="utf-8")
    if with_stats:
        (root / (run_id + "_stats.json")).write_text("{}", encoding="utf-8")
    return run_id


@pytest.fixture
def env(tmp_path):
    notes, dig = tmp_path / "notes", tmp_path / "digest"
    notes.mkdir()
    return SimpleNamespace(notes=notes, dig=dig, tmp=tmp_path)


def queue_of(env, d="2026-10-19"):
    return json.loads(C.queue_path(env.notes, d).read_text(encoding="utf-8"))


# ====================================================================== osa_dialog

def test_as_str_escapes_quotes_backslash_and_control_chars():
    assert O.as_str('He said "hi" \\ x') == '"He said \\"hi\\" \\\\ x"'
    assert "\n" not in O.as_str("a\nb\tc\rd") and O.as_str("a\nb\tc") == '"a b c"'
    assert O.as_str("中文：基于“缺失”") == '"中文：基于“缺失”"'          # 不转成 \uXXXX
    assert O.as_list([]) == "{}"


def test_choose_script_contains_defaults_and_buttons():
    sc = O.build_choose_script(["a", "b"], ["a"], title="T", prompt="P", ok="确认本页", cancel="暂不确认")
    assert 'set theItems to {"a", "b"}' in sc and "default items theDefaults" in sc
    assert "with multiple selections allowed and empty selection allowed" in sc
    assert 'OK button name "确认本页"' in sc and 'cancel button name "暂不确认"' in sc
    assert "default items" not in O.build_choose_script(["a"], [], title="T", prompt="P", ok="o", cancel="c")


def test_parse_choose_output_variants():
    sep = O.SEP
    assert O.parse_choose_output("CANCEL\n").status == "cancel"
    r = O.parse_choose_output("OK:a, with comma{}b\n".format(sep))
    assert r.status == "ok" and r.selected == ["a, with comma", "b"]       # 标题里的逗号不会被误切
    assert O.parse_choose_output("OK:\n") == O.ChooseResult("ok", [])      # 一条都没选 ≠ 取消
    assert O.parse_choose_output("garbage").status == "error"


def test_ask_script_and_parse():
    sc = O.build_ask_script("第一行\n第二行", ["稍后", "开始确认"], "开始确认", title="T", giving_up_s=600)
    assert "giving up after 600" in sc and 'default button "开始确认"' in sc and "return &" in sc
    assert O.parse_ask_output("开始确认|false\n") == O.AskResult("ok", "开始确认", False)
    r = O.parse_ask_output("|true\n")
    assert r.status == "timeout" and r.gave_up
    assert O.parse_ask_output("???").status == "error"


def test_osadialogs_with_stub_runner_maps_timeout_and_errors():
    import subprocess
    calls = []

    def runner(args, timeout):
        calls.append((args, timeout))
        raise subprocess.TimeoutExpired(args, timeout)

    d = O.OsaDialogs(runner=runner)
    assert d.choose(["a"], [], title="t", prompt="p", timeout_s=5).status == "timeout"
    assert calls[0][0][:2] == ["osascript", "-e"] and calls[0][1] == 5
    assert d.ask("m", ["b"], timeout_s=1).status == "timeout"
    d2 = O.OsaDialogs(runner=lambda a, t: (1, "", "boom"))
    assert d2.choose(["a"], [], title="t", prompt="p").status == "error"
    d3 = O.OsaDialogs(runner=lambda a, t: (0, "OK:a\n", ""))
    assert d3.choose(["a"], ["a"], title="t", prompt="p").selected == ["a"]


def test_default_runner_refuses_inside_pytest():
    """漏打桩时宁可抛错也不往屏幕上弹框。"""
    with pytest.raises(RuntimeError, match="测试中禁止"):
        O.OsaDialogs().choose(["a"], [], title="t", prompt="p")


# ====================================================================== 队列构建

def test_sync_queue_categories_defaults_and_weights(env):
    make_digest(env.dig, n_inc=2, n_maybe=1, n_low=3, n_high=10)
    rep = C.sync_queue(env.notes, env.dig, now=NOW)
    assert rep.runs_new == 1
    q = queue_of(env)
    by = {}
    for it in q["items"]:
        by.setdefault(it["category"], []).append(it)
    assert len(by["include"]) == 2 and len(by["maybe"]) == 1 and len(by["lowconf_exclude"]) == 3
    assert all(i["default_checked"] for i in by["include"] + by["maybe"])
    assert not any(i["default_checked"] for i in by["lowconf_exclude"] + by["audit"])
    # 黑名单命中不进抽查；抽查数在 [MIN, MAX]，权重 = 池/抽中
    assert all(i["decision"] == "EXCLUDE" and i["confidence"] == 0.95 for i in by["audit"])
    k = len(by["audit"])
    assert TH.CONFIRM_AUDIT_MIN <= k <= TH.CONFIRM_AUDIT_MAX
    assert by["audit"][0]["weight"] == pytest.approx(10 / k) and by["audit"][0]["pool_size"] == 10
    assert q["runs"]["digest_20261019_090004"]["audit_pool"] == 10
    # 记了筛选器指纹与实际应答模型
    assert by["include"][0]["prompt_version"] == "filter-v3@abcd1234"
    assert by["include"][0]["served_model"] == "claude-sonnet-5-5"
    assert all(i["state"] == "pending" for i in q["items"])


def test_sync_queue_is_idempotent_and_deterministic(env):
    make_digest(env.dig)
    C.sync_queue(env.notes, env.dig, now=NOW)
    first = C.queue_path(env.notes, "2026-10-19").read_text(encoding="utf-8")
    rep = C.sync_queue(env.notes, env.dig, now=NOW)
    assert rep.runs_new == 0 and C.queue_path(env.notes, "2026-10-19").read_text(encoding="utf-8") == first
    # 删掉队列重建 → 抽查同一批（种子 = run_id）
    C.queue_path(env.notes, "2026-10-19").unlink()
    C.sync_queue(env.notes, env.dig, now=NOW)
    assert C.queue_path(env.notes, "2026-10-19").read_text(encoding="utf-8") == first


def test_sync_queue_skips_seen_and_cross_run_duplicates(env):
    make_digest(env.dig, "20261012_090000", first=0)
    make_digest(env.dig, "20261019_090004", first=0)      # 8 天窗口重叠：同一批论文再出现
    seen = {ing.dedup_key(_meta(0))}
    C.sync_queue(env.notes, env.dig, seen=seen, now=NOW)
    keys = [it["dedup_key"] for p, q in C.list_queues(env.notes) for it in q["items"]]
    assert len(keys) == len(set(keys))                       # 跨 run 不重复
    assert ing.dedup_key(_meta(0)) not in keys              # 已在库的不问


def test_sync_queue_waits_for_stats_file(env):
    make_digest(env.dig, with_stats=False)
    assert C.sync_queue(env.notes, env.dig, now=NOW).runs_new == 0
    (env.dig / "digest_20261019_090004_stats.json").write_text("{}")
    assert C.sync_queue(env.notes, env.dig, now=NOW).runs_new == 1


def test_audit_quota_bounds():
    assert C._audit_quota(0) == 0 and C._audit_quota(2) == 2
    assert C._audit_quota(10) == TH.CONFIRM_AUDIT_MIN
    assert C._audit_quota(10_000) == TH.CONFIRM_AUDIT_MAX


# ====================================================================== 弹窗会话

def test_session_confirms_with_defaults_and_writes_labels(env):
    make_digest(env.dig, n_inc=2, n_maybe=1, n_low=2, n_high=0)
    C.sync_queue(env.notes, env.dig, now=NOW)
    d = FakeDialogs(asks=[ask_ok("开始确认"), ask_ok("确认提交")], chooses=[pick("defaults")])
    res = C.run_session(env.notes, d, now=NOW)
    assert res.status == "done" and res.n_keep == 3 and res.n_drop == 2 and res.n_still_pending == 0
    items, defaults, kw = d.choose_calls[0]
    assert len(items) == 5 and len(defaults) == 3
    assert items[0].startswith("001 [纳入] ") and "[低置信排除]" in items[-1]
    assert res.keep_run_dates == ["2026-10-19"]
    labs = C.load_stream_labels(env.notes)
    assert len(labs) == 5 and {l["user_decision"] for l in labs} == {"keep", "drop"}
    keep = [l for l in labs if l["user_decision"] == "keep"]
    assert all(l["filter_decision"] in ("INCLUDE", "MAYBE") and l["override"] is False for l in keep)
    drop = [l for l in labs if l["category"] == "lowconf_exclude"]
    assert all(l["user_decision"] == "drop" and l["override"] is False for l in drop)
    l0 = keep[0]
    for f in ("confirmed_at", "user_decision", "filter_decision", "filter_confidence", "category",
              "prompt_version", "served_model", "provider", "sample_weight", "dedup_key", "item_id", "abstract"):
        assert f in l0
    assert l0["prompt_version"] == "filter-v3@abcd1234" and l0["confirmed_at"] == "2026-10-19T10:00:00"


def test_user_ticking_an_exclude_is_an_override_label(env):
    make_digest(env.dig, n_inc=1, n_maybe=0, n_low=2, n_high=0)
    C.sync_queue(env.notes, env.dig, now=NOW)

    def choose(items, defaults):          # 勾上第一条低置信排除，并取消那条纳入
        low = [s for s in items if "[低置信排除]" in s][0]
        return O.ChooseResult("ok", [low])

    C.run_session(env.notes, FakeDialogs([ask_ok("开始确认"), ask_ok("确认提交")], [choose]), now=NOW)
    labs = {l["category"]: l for l in C.load_stream_labels(env.notes)}
    assert labs["include"]["user_decision"] == "drop" and labs["include"]["override"] is True
    low = [l for l in C.load_stream_labels(env.notes) if l["category"] == "lowconf_exclude"]
    assert sorted(l["user_decision"] for l in low) == ["drop", "keep"]
    assert [l["override"] for l in low if l["user_decision"] == "keep"] == [True]


def test_pagination_splits_at_page_size_and_persists_each_page(env):
    make_digest(env.dig, n_inc=45, n_maybe=0, n_low=0, n_high=0)
    C.sync_queue(env.notes, env.dig, now=NOW)
    d = FakeDialogs(asks=[ask_ok("开始确认"), ask_ok("确认提交")], chooses=[pick("defaults"), pick("defaults")])
    res = C.run_session(env.notes, d, now=NOW)
    assert [len(c[0]) for c in d.choose_calls] == [40, 5]
    assert "1/2" in d.choose_calls[0][2]["title"] and "2/2" in d.choose_calls[1][2]["title"]
    assert len({s for c in d.choose_calls for s in c[0]}) == 45          # 序号前缀保证互不相同
    assert res.status == "done" and res.n_keep == 45


def test_cancel_on_second_page_commits_nothing(env):
    make_digest(env.dig, n_inc=45, n_maybe=0, n_low=0, n_high=0)
    C.sync_queue(env.notes, env.dig, now=NOW)
    d = FakeDialogs(asks=[ask_ok("开始确认"), ask_ok("确认提交")], chooses=[pick("defaults"), O.ChooseResult("cancel")])
    res = C.run_session(env.notes, d, now=NOW)
    assert res.status == "suspended" and res.n_keep == 0 and res.n_still_pending == 45
    assert "暂不确认" in res.reason
    # 提交前不生效：第一页点过也不落盘；取消 ≠ 丢弃
    assert len(C.pending_items(env.notes)) == 45 and C.load_stream_labels(env.notes) == []
    assert (C.pending_dir(env.notes) / C.DRAFT_FILE).exists()                  # 只有草稿，草稿不算确认


@pytest.mark.parametrize("first,reason", [
    (O.ChooseResult("cancel"), "暂不确认"),
    (O.ChooseResult("timeout"), "无人应答"),
    (O.ChooseResult("error", detail="boom"), "出错"),
])
def test_cancel_timeout_error_on_first_page_suspend_everything(env, first, reason):
    make_digest(env.dig, n_inc=3, n_maybe=0, n_low=1, n_high=0)
    C.sync_queue(env.notes, env.dig, now=NOW)
    res = C.run_session(env.notes, FakeDialogs([ask_ok("开始确认"), ask_ok("确认提交")], [first]), now=NOW)
    assert res.status == "suspended" and reason in res.reason
    assert len(C.pending_items(env.notes)) == 4 and C.load_stream_labels(env.notes) == []


@pytest.mark.parametrize("ans", [O.AskResult("timeout", None, True), ask_ok("稍后"),
                                 O.AskResult("error", detail="x")])
def test_intro_no_answer_suspends_without_listing(env, ans):
    make_digest(env.dig)
    C.sync_queue(env.notes, env.dig, now=NOW)
    d = FakeDialogs(asks=[ans])
    res = C.run_session(env.notes, d, now=NOW)
    assert res.status == "suspended" and d.choose_calls == []
    assert d.ask_calls[0][2]["giving_up_s"] == TH.CONFIRM_ASK_TIMEOUT_S      # 用的是 giving up after


def test_no_gui_suspends_without_any_dialog(env):
    make_digest(env.dig)
    C.sync_queue(env.notes, env.dig, now=NOW)
    d = FakeDialogs(gui=False)
    res = C.run_session(env.notes, d, now=NOW)
    assert res.status == "suspended" and "图形" in res.reason and not d.ask_calls


def test_view_summary_writes_md_and_reasks(env):
    make_digest(env.dig, n_inc=1, n_maybe=0, n_low=1, n_high=0)
    C.sync_queue(env.notes, env.dig, now=NOW)
    d = FakeDialogs(asks=[ask_ok("先看摘要"), ask_ok("开始确认"), ask_ok("确认提交")], chooses=[pick("defaults")])
    C.run_session(env.notes, d, now=NOW)
    assert len(d.opened) == 1 and len(d.ask_calls) == 3      # 开场×2 + 汇总核对
    md = Path(d.opened[0]).read_text(encoding="utf-8")
    assert "Paper number" in md and "摘要：" in md and "一句话：" in md and "001 [纳入]" in md


def test_summary_truncates_abstract(env):
    row = {"title": "T", "category": "include", "abstract": "x" * 900, "confidence": 0.9, "decision": "INCLUDE"}
    p = C.write_summary_md(env.notes, [(Path("q"), row)], NOW)
    txt = p.read_text(encoding="utf-8")
    assert "x" * TH.CONFIRM_SUMMARY_CHARS in txt and "x" * (TH.CONFIRM_SUMMARY_CHARS + 1) not in txt


def test_item_label_truncates_and_sanitizes():
    it = {"category": "audit", "title": 'A "quoted"\nvery ' + "long " * 60}
    lb = C.item_label(7, it)
    assert lb.startswith("007 [抽查排除] ") and "\n" not in lb
    assert len(lb) <= len("007 [抽查排除] ") + TH.CONFIRM_TITLE_CHARS


def test_record_decisions_is_idempotent_and_validates(env):
    make_digest(env.dig, n_inc=1, n_maybe=0, n_low=0, n_high=0)
    C.sync_queue(env.notes, env.dig, now=NOW)
    qp, it = C.pending_items(env.notes)[0]
    assert C.record_decisions(env.notes, [(qp, it["dedup_key"], "keep")], now=NOW) == 1
    assert C.record_decisions(env.notes, [(qp, it["dedup_key"], "drop")], now=NOW) == 0     # 已定，不改
    with pytest.raises(ValueError):
        C.record_decisions(env.notes, [(qp, it["dedup_key"], "maybe")])
    assert len(C.load_stream_labels(env.notes)) == 1


def test_load_stream_labels_window_and_dedup(env):
    lp = C.labels_path(env.notes)
    lp.parent.mkdir(parents=True)
    rows = [{"dedup_key": "a", "confirmed_at": "2026-01-01T00:00:00", "user_decision": "drop"},
            {"dedup_key": "a", "confirmed_at": "2026-10-01T00:00:00", "user_decision": "keep"},
            {"dedup_key": "b", "confirmed_at": "2026-02-01T00:00:00", "user_decision": "keep"}]
    lp.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n", encoding="utf-8")
    got = C.load_stream_labels(env.notes)
    assert {r["dedup_key"]: r["user_decision"] for r in got} == {"a": "keep", "b": "keep"}
    got = C.load_stream_labels(env.notes, since=datetime(2026, 6, 1))
    assert [r["dedup_key"] for r in got] == ["a"]


# ====================================================================== ingest 闸门

def _load_segs(env):
    return ing.load_digest_segments(env.dig, since=date(2026, 10, 19), until=date(2026, 10, 25))


def test_gate_blocks_everything_until_confirmed(env):
    make_digest(env.dig, n_inc=2, n_maybe=1, n_low=2, n_high=0)
    segs = _load_segs(env)
    assert len(segs) == 3
    g = C.gate_segments(segs, env.notes, env.dig, date(2026, 10, 19), date(2026, 10, 25))
    assert g.segs == [] and g.n_pending == 5 and g.n_confirmed == 0           # 未确认 → 一篇都不放行
    assert len(C.list_queues(env.notes)) == 1                                  # 闸门顺手把队列建起来


def test_gate_passes_only_kept_and_rebuilds_overridden_excludes(env):
    make_digest(env.dig, n_inc=2, n_maybe=1, n_low=2, n_high=0)
    segs = _load_segs(env)
    C.gate_segments(segs, env.notes, env.dig, date(2026, 10, 19), date(2026, 10, 25))

    def choose(items, defaults):       # 留第一条纳入 + 勾上第一条低置信排除；其余全丢
        inc = [s for s in items if "[纳入]" in s][0]
        low = [s for s in items if "[低置信排除]" in s][0]
        return O.ChooseResult("ok", [inc, low])

    C.run_session(env.notes, FakeDialogs([ask_ok("开始确认"), ask_ok("确认提交")], [choose]), now=NOW)
    g = C.gate_segments(_load_segs(env), env.notes, env.dig, date(2026, 10, 19), date(2026, 10, 25))
    assert len(g.segs) == 2 and g.n_override == 1 and g.n_dropped == 3 and g.n_pending == 0
    ov = [s for s in g.segs if s.filter_decision.stage == "manual_pick"]
    assert len(ov) == 1
    fd = ov[0].filter_decision
    assert fd.decision == "INCLUDE" and fd.verdict == "included"
    assert fd.llm_decision_before_override == "EXCLUDE" and fd.exclude_reason is None   # WO-2 语义
    assert ov[0].original_abstract.startswith("excluded abs")                           # 摘要来自 sidecar
    assert ov[0].priority_score >= 0.9                                                  # 不被精读排到末尾
    plain = [s for s in g.segs if s.filter_decision.stage == "llm_judge"]
    assert len(plain) == 1 and plain[0].filter_decision.llm_decision_before_override is None


def test_gate_sync_false_does_not_write(env):
    make_digest(env.dig)
    g = C.gate_segments(_load_segs(env), env.notes, env.dig, date(2026, 10, 19), date(2026, 10, 25), sync=False)
    assert g.segs == [] and not C.pending_dir(env.notes).exists()


def test_gate_respects_window(env):
    make_digest(env.dig, "20261012_090000", first=0)
    make_digest(env.dig, "20261019_090004", first=100)
    C.sync_queue(env.notes, env.dig, now=NOW)
    for qp, it in C.pending_items(env.notes):
        C.record_decisions(env.notes, [(qp, it["dedup_key"], "keep")], now=NOW)
    g = C.gate_segments(_load_segs(env), env.notes, env.dig, date(2026, 10, 19), date(2026, 10, 25), sync=False)
    assert all(s.paper_id.startswith("p1") for s in g.segs if s.paper_id)       # 只含 10-19 那周
    assert g.n_confirmed == len([1 for _, q in C.list_queues(env.notes) if q["run_date"] == "2026-10-19"
                                 for it in q["items"]])


def test_apply_manual_override_matches_classify_semantics():
    seg = PaperSegment(segment_id=1, paper_id="p1", metadata=_meta(1), filter_decision=_fd(1, "EXCLUDE", 0.5))
    ing.apply_manual_override(seg)
    fd = seg.filter_decision
    assert (fd.verdict, fd.decision, fd.stage, fd.llm_decision_before_override) == \
        ("included", "INCLUDE", "manual_pick", "EXCLUDE")
    seg2 = PaperSegment(segment_id=2, paper_id="p2", metadata=_meta(2), filter_decision=_fd(2, "MAYBE", 0.6))
    ing.apply_manual_override(seg2)
    assert seg2.filter_decision.decision == "MAYBE" and seg2.filter_decision.stage == "llm_judge"
    seg3 = PaperSegment(segment_id=3, paper_id="p3", metadata=_meta(3))
    ing.apply_manual_override(seg3)
    assert seg3.filter_decision.stage == "manual_pick"


def _import_script(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / "scripts" / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_ingest_notes_gate_helper_and_no_confirm_flag(env, monkeypatch):
    m = _import_script("ingest_notes")
    make_digest(env.dig, n_inc=2, n_maybe=0, n_low=0, n_high=0)
    segs = _load_segs(env)
    sent = []
    monkeypatch.setattr(m, "notify", lambda t, x: sent.append((t, x)))
    settings = SimpleNamespace(processing=SimpleNamespace(notes_dir=str(env.notes)))
    args = SimpleNamespace(digest_dir=str(env.dig), list_only=False, dry_run=False)
    out = m._apply_confirm_gate(segs, settings, args, date(2026, 10, 19), date(2026, 10, 25))
    assert out == [] and len(sent) == 1 and "confirm_ingest.py" in sent[0][1]     # 全挂起 → 通知，不静默
    # --list / --dry-run：零写盘、不通知
    sent.clear()
    shutil_target = C.pending_dir(env.notes)
    for p in shutil_target.glob("*"):
        p.unlink()
    args2 = SimpleNamespace(digest_dir=str(env.dig), list_only=True, dry_run=False)
    m._apply_confirm_gate(segs, settings, args2, date(2026, 10, 19), date(2026, 10, 25))
    assert sent == [] and list(shutil_target.glob("*")) == []
    # 旧行为开关存在
    import argparse
    src = (Path(__file__).resolve().parents[1] / "scripts" / "ingest_notes.py").read_text(encoding="utf-8")
    assert '"--no-confirm"' in src and "SCHOLAR_NO_CONFIRM" in src


# ====================================================================== confirm_ingest 编排

@pytest.fixture
def ci():
    return _import_script("confirm_ingest")


def test_confirm_ingest_end_to_end_with_stubbed_ingest(env, ci):
    make_digest(env.dig, n_inc=2, n_maybe=0, n_low=1, n_high=0)
    d = FakeDialogs([ask_ok("开始确认"), ask_ok("确认提交")], [pick("defaults")])
    ran = []
    rc = ci.main(["--notes-dir", str(env.notes), "--digest-dir", str(env.dig)], dialogs=d,
                 runner=lambda argv: (ran.append(argv), 0)[1], today=date(2026, 10, 20), now=NOW)
    assert rc == 0
    assert d.notified and "待确认" in d.notified[0][1]                        # 弹窗前先发通知
    assert len(ran) == 1 and ran[0][ran[0].index("--week") + 1] == "2026-10-19"
    qp = C.queue_path(env.notes, "2026-10-19")
    kept = [i for i in json.loads(qp.read_text(encoding="utf-8"))["items"] if i["state"] == "keep"]
    assert len(kept) == 2 and all(i["ingested_at"] for i in kept)            # 入库成功 → 标记


def test_confirm_ingest_failed_ingest_retries_then_gives_up(env, ci):
    make_digest(env.dig, n_inc=1, n_maybe=0, n_low=0, n_high=0)
    base = ["--notes-dir", str(env.notes), "--digest-dir", str(env.dig)]
    d = FakeDialogs([ask_ok("开始确认"), ask_ok("确认提交")], [pick("defaults")])
    fail = lambda argv: 1                                                     # noqa: E731
    ci.main(base, dialogs=d, runner=fail, today=date(2026, 10, 20), now=NOW)
    for day in (21, 22):
        d2 = FakeDialogs()
        ci.main(base, dialogs=d2, runner=fail, today=date(2026, 10, day), now=NOW)   # 已无待确认，只补跑入库
    d3 = FakeDialogs()
    ran = []
    ci.main(base, dialogs=d3, runner=lambda a: (ran.append(a), 0)[1], today=date(2026, 10, 23), now=NOW)
    assert ran == []                                                          # 3 次后不再自动重试
    assert any("不再自动重试" in t for _, t in d.notified + d2.notified)


def test_confirm_ingest_suspended_does_not_ingest_and_daily_prompts_once(env, ci):
    make_digest(env.dig, n_inc=2, n_maybe=0, n_low=0, n_high=0)
    base = ["--notes-dir", str(env.notes), "--digest-dir", str(env.dig), "--daily"]
    ran = []
    runner = lambda a: (ran.append(a), 0)[1]                                  # noqa: E731
    d = FakeDialogs([O.AskResult("timeout", None, True)])
    ci.main(base, dialogs=d, runner=runner, today=date(2026, 10, 20), now=NOW)
    assert ran == [] and len(C.pending_items(env.notes)) == 2                 # 挂起：不入库
    d_same_day = FakeDialogs()
    ci.main(base, dialogs=d_same_day, runner=runner, today=date(2026, 10, 20), now=NOW)
    assert d_same_day.ask_calls == [] and d_same_day.notified == []           # 当天不重复打扰
    d_next = FakeDialogs([O.AskResult("timeout", None, True)])
    ci.main(base, dialogs=d_next, runner=runner, today=date(2026, 10, 21), now=NOW)
    assert len(d_next.ask_calls) == 1 and d_next.notified                     # 次日再提醒


def test_confirm_ingest_list_mode_is_read_only(env, ci, capsys):
    make_digest(env.dig)
    rc = ci.main(["--notes-dir", str(env.notes), "--digest-dir", str(env.dig), "--list"],
                 dialogs=FakeDialogs(), today=date(2026, 10, 20), now=NOW)
    assert rc == 0 and not C.pending_dir(env.notes).exists()


def test_confirm_ingest_no_gui_only_notifies(env, ci):
    make_digest(env.dig)
    d = FakeDialogs(gui=False)
    ci.main(["--notes-dir", str(env.notes), "--digest-dir", str(env.dig)], dialogs=d,
            runner=lambda a: 0, today=date(2026, 10, 20), now=NOW)
    assert d.notified and not d.ask_calls


# ====================================================================== 防误触补丁（WO-8a 补丁，2026-10-21 真机复盘）

def _stepping_clock(start=datetime(2026, 10, 19, 17, 21, 43), step=10):
    box = {"t": start}

    def clock():
        t = box["t"]
        box["t"] = t + timedelta(seconds=step)
        return t
    return clock


def _spam_enter(env, n_inc):
    """模拟连按回车：开场默认「开始确认」、每页默认 OK、汇总框默认「返回修改」——回车永远不能提交。"""
    make_digest(env.dig, n_inc=n_inc, n_maybe=0, n_low=1, n_high=0)
    C.sync_queue(env.notes, env.dig, now=NOW)


def test_mashing_default_buttons_never_commits(env):
    _spam_enter(env, 3)
    # 汇总框默认按钮 = 返回修改；回车 = 返回修改 → 再走一遍 → 再返回修改 … 最后无人应答 = 挂起
    d = FakeDialogs(asks=[ask_ok("开始确认"), ask_ok("返回修改"), ask_ok("返回修改"), O.AskResult("timeout", None, True)],
                    chooses=[pick("defaults")] * 3)
    res = C.run_session(env.notes, d, now=NOW)
    commit_calls = [c for c in d.ask_calls if "确认提交" in c[1]]
    assert len(commit_calls) == 3
    assert all(c[1] == ["返回修改", "确认提交"] for c in commit_calls)
    assert res.status == "suspended" and "未提交" in res.reason
    assert len(C.pending_items(env.notes)) == 4 and C.load_stream_labels(env.notes) == []
    assert all(i["state"] == "pending" for i in queue_of(env)["items"])


def test_commit_dialog_default_button_is_back():
    sc = O.build_ask_script(C.commit_message({"keep": 3, "drop": 1, "added": 0, "removed": 0, "changed": 0}),
                            [C.BTN_BACK, C.BTN_COMMIT], C.BTN_BACK, title="t", giving_up_s=600)
    assert 'buttons {"返回修改", "确认提交"} default button "返回修改"' in sc and "giving up after 600" in sc


def test_commit_dialog_is_called_with_back_as_default(env):
    _spam_enter(env, 2)
    seen = {}

    class D(FakeDialogs):
        def ask(self, message, buttons, default_button=None, **kw):
            seen.setdefault("defaults", []).append(default_button)
            return super().ask(message, buttons, default_button, **kw)
    d = D(asks=[ask_ok("开始确认"), O.AskResult("timeout", None, True)], chooses=[pick("defaults")])
    C.run_session(env.notes, d, now=NOW)
    assert seen["defaults"] == ["开始确认", "返回修改"]


def test_summary_counts_changes_vs_filter_default(env):
    make_digest(env.dig, n_inc=2, n_maybe=0, n_low=2, n_high=0)
    C.sync_queue(env.notes, env.dig, now=NOW)

    def choose(items, defaults):          # 取消第一条纳入，补勾第一条低置信排除
        inc = [s for s in items if "[纳入]" in s]
        low = [s for s in items if "[低置信排除]" in s]
        return O.ChooseResult("ok", [inc[1], low[0]])
    d = FakeDialogs(asks=[ask_ok("开始确认"), ask_ok("确认提交")], chooses=[choose])
    res = C.run_session(env.notes, d, now=NOW)
    msg = d.ask_calls[1][0]
    assert "保留 2 / 丢弃 2" in msg and "改动了 2 处（补勾 1、取消 1）" in msg
    assert (res.n_added, res.n_removed, res.n_changed) == (1, 1, 2)


def test_back_to_edit_restarts_at_page_one_keeping_ticks(env):
    make_digest(env.dig, n_inc=1, n_maybe=0, n_low=2, n_high=0)
    C.sync_queue(env.notes, env.dig, now=NOW)

    def first(items, defaults):           # 第一遍：补勾第一条低置信排除，取消纳入
        return O.ChooseResult("ok", [[s for s in items if "[低置信排除]" in s][0]])
    d = FakeDialogs(asks=[ask_ok("开始确认"), ask_ok("返回修改"), ask_ok("确认提交")],
                    chooses=[first, pick("defaults")])
    res = C.run_session(env.notes, d, now=NOW)
    (items1, defaults1, kw1), (items2, defaults2, kw2) = d.choose_calls
    assert items1 == items2 and "1/1" in kw2["title"]
    assert defaults1 != defaults2
    assert defaults2 == [[s for s in items2 if "[低置信排除]" in s][0]]       # 保留第一遍改动，而非回到筛选器默认
    # 第二遍原样提交：纳入仍被取消、低置信被补勾 → 两处改动都记为用户标签
    labs = {l["category"]: l for l in C.load_stream_labels(env.notes)}
    assert labs["include"]["user_decision"] == "drop" and labs["include"]["user_decision_source"] == "user"
    assert res.n_changed == 2 and res.status == "done"


def test_nothing_is_committed_until_confirm(env):
    make_digest(env.dig, n_inc=2, n_maybe=0, n_low=0, n_high=0)
    C.sync_queue(env.notes, env.dig, now=NOW)
    seen = {}

    class D(FakeDialogs):
        def ask(self, message, buttons, default_button=None, **kw):
            if "确认提交" in buttons:       # 汇总框弹出时磁盘上仍是 pending、无标签
                seen["pending"] = len(C.pending_items(env.notes))
                seen["labels"] = len(C.load_stream_labels(env.notes))
            return super().ask(message, buttons, default_button, **kw)
    d = D(asks=[ask_ok("开始确认"), ask_ok("确认提交")], chooses=[pick("defaults")])
    C.run_session(env.notes, d, now=NOW)
    assert seen == {"pending": 2, "labels": 0}
    assert not (C.pending_dir(env.notes) / C.DRAFT_FILE).exists()             # 提交后草稿清掉


def test_draft_prefills_next_session_but_is_not_confirmation(env):
    make_digest(env.dig, n_inc=2, n_maybe=0, n_low=0, n_high=0)
    C.sync_queue(env.notes, env.dig, now=NOW)
    d1 = FakeDialogs(asks=[ask_ok("开始确认"), O.AskResult("timeout", None, True)], chooses=[pick(0)])
    C.run_session(env.notes, d1, now=NOW)                    # 取消第二条后在汇总框超时 → 挂起
    assert len(C.pending_items(env.notes)) == 2
    d2 = FakeDialogs(asks=[ask_ok("开始确认"), ask_ok("确认提交")], chooses=[pick("defaults")])
    # 第二遍 defaults 来自草稿（只勾了第 1 条），pick("defaults") 原样返回它
    res = C.run_session(env.notes, d2, now=NOW)
    assert d2.choose_calls[0][1] == [d2.choose_calls[0][0][0]] and (res.n_keep, res.n_drop) == (1, 1)


def test_confirmed_at_is_per_page_time_not_session_start(env):
    """真机根因：整场共用开场的 now → 所有标签 confirmed_at 都是入队/开场时刻。"""
    make_digest(env.dig, n_inc=45, n_maybe=0, n_low=0, n_high=0)
    C.sync_queue(env.notes, env.dig, now=NOW)
    clock = _stepping_clock()
    d = FakeDialogs(asks=[ask_ok("开始确认"), ask_ok("确认提交")], chooses=[pick("defaults"), pick("defaults")])
    C.run_session(env.notes, d, clock=clock)          # 不传 now：与真实运行一致
    labs = C.load_stream_labels(env.notes)
    stamps = sorted({l["confirmed_at"] for l in labs})
    # 17:21:43 被会话开场取走（相当于入队/开场时刻）；标签用各页确认时刻，且两页不同
    assert stamps == ["2026-10-19T17:21:53", "2026-10-19T17:22:03"]
    assert {l["committed_at"] for l in labs} == {"2026-10-19T17:22:13"}
    first = [l for l in labs if l["confirmed_at"] == stamps[0]]
    assert len(first) == 40
    q = queue_of(env)
    assert {i["confirmed_at"] for i in q["items"]} == set(stamps)


def test_confirm_ingest_main_uses_real_clock_per_page(env, ci, monkeypatch):
    """main 不注入 now 时，不能把启动时刻当整场的确认时刻。"""
    make_digest(env.dig, n_inc=1, n_maybe=0, n_low=0, n_high=0)
    seq = iter([datetime(2026, 10, 20, 9, 0, 0), datetime(2026, 10, 20, 9, 5, 0), datetime(2026, 10, 20, 9, 5, 1)] * 5)

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(seq)
    monkeypatch.setattr(ci, "datetime", FakeDT)
    d = FakeDialogs([ask_ok("开始确认"), ask_ok("确认提交")], [pick("defaults")])
    ci.main(["--notes-dir", str(env.notes), "--digest-dir", str(env.dig), "--no-ingest"], dialogs=d,
            today=date(2026, 10, 20))
    lab = C.load_stream_labels(env.notes)[0]
    assert lab["confirmed_at"] != "2026-10-20T09:00:00"            # 不是启动时刻


# ---- 零改动不记用户标签 ----

def test_zero_change_marks_default_accepted_but_still_allows_ingest(env):
    make_digest(env.dig, n_inc=2, n_maybe=1, n_low=2, n_high=0)
    C.sync_queue(env.notes, env.dig, now=NOW)
    d = FakeDialogs(asks=[ask_ok("开始确认"), ask_ok("确认提交")], chooses=[pick("defaults")])
    res = C.run_session(env.notes, d, now=NOW)
    assert res.status == "done" and res.n_changed == 0 and res.n_keep == 3
    assert "不会记为你的标签" in d.ask_calls[1][0]
    labs = C.load_stream_labels(env.notes)
    assert len(labs) == 5 and {l["user_decision_source"] for l in labs} == {"default_accepted"}
    assert all(i["state"] in ("keep", "drop") for i in queue_of(env)["items"])      # 队列照常已确认
    segs = _load_segs(env)
    g = C.gate_segments(segs, env.notes, env.dig, date(2026, 10, 19), date(2026, 10, 25), sync=False)
    assert g.n_confirmed == 3 and len(g.segs) == 3                                  # 允许入库
    from src.scholar import filter_eval as FE
    items, st = FE.stream_gold_items(labs)
    assert items == [] and st["n_default_accepted"] == 5 and st["n_labels"] == 0
    items2, st2 = FE.stream_gold_items(labs, include_default_accepted=True)
    assert len(items2) == 5 and st2["default_accepted_included"] is True


def test_changed_items_are_user_labels_unchanged_are_default_accepted(env):
    make_digest(env.dig, n_inc=2, n_maybe=0, n_low=2, n_high=0)
    C.sync_queue(env.notes, env.dig, now=NOW)

    def choose(items, defaults):
        inc = [s for s in items if "[纳入]" in s]
        low = [s for s in items if "[低置信排除]" in s]
        return O.ChooseResult("ok", [inc[0], low[0]])       # 取消 inc[1]，补勾 low[0]
    C.run_session(env.notes, FakeDialogs([ask_ok("开始确认"), ask_ok("确认提交")], [choose]), now=NOW)
    labs = C.load_stream_labels(env.notes)
    assert sorted(l["user_decision_source"] for l in labs) == ["default_accepted", "default_accepted", "user", "user"]
    assert all(l["user_decision_source"] == ("user" if l["override"] else "default_accepted") for l in labs)
    from src.scholar import filter_eval as FE
    items, _ = FE.stream_gold_items(labs)
    assert len(items) == 2 and all(i["override"] for i in items)


def test_profile_drift_ignores_default_accepted(env):
    from src.scholar import profile_drift as PD
    base = {"filter_decision": "EXCLUDE", "user_decision": "keep", "exclude_reason": "X1",
            "confirmed_at": "2026-10-19T10:00:00"}
    rows = [dict(base, dedup_key="a{}".format(i), user_decision_source="default_accepted") for i in range(5)]
    rows += [dict(base, dedup_key="u{}".format(i), user_decision_source="user") for i in range(2)]
    a = PD.analyze(rows, now=datetime(2026, 10, 20), embed_fn=None)
    assert a.n_added == 2
    a2 = PD.analyze(rows, now=datetime(2026, 10, 20), embed_fn=None, include_default_accepted=True)
    assert a2.n_added == 7
    legacy = [{k: v for k, v in rows[0].items() if k != "user_decision_source"}]       # 旧行无字段 = 主动
    assert PD.analyze(legacy, now=datetime(2026, 10, 20), embed_fn=None).n_added == 1


# ---- reopen ----

def _commit_all(env, n_inc=2, n_low=1):
    make_digest(env.dig, n_inc=n_inc, n_maybe=0, n_low=n_low, n_high=0)
    C.sync_queue(env.notes, env.dig, now=NOW)
    C.run_session(env.notes, FakeDialogs([ask_ok("开始确认"), ask_ok("确认提交")], [pick(0)]), now=NOW)


def test_reopen_before_ingest_resets_queue_and_retracts_labels(env):
    _commit_all(env)
    assert len(C.load_stream_labels(env.notes)) == 3 and C.pending_items(env.notes) == []
    st, n = C.reopen_run(env.notes, "2026-10-19", now=datetime(2026, 10, 20, 8, 0, 0))
    assert (st, n) == ("ok", 3)
    assert len(C.pending_items(env.notes)) == 3 and all(i["confirmed_at"] is None for i in queue_of(env)["items"])
    assert C.load_stream_labels(env.notes) == []                                    # 同键最晚一行是撤销行 → 失效
    assert len(C.load_stream_labels(env.notes, include_retracted=True)) == 3
    raw = [json.loads(l) for l in C.labels_path(env.notes).read_text(encoding="utf-8").splitlines()]
    assert len(raw) == 6 and sum(r["user_decision"] == "retracted" for r in raw) == 3
    # 闸门重新拦住
    g = C.gate_segments(_load_segs(env), env.notes, env.dig, date(2026, 10, 19), date(2026, 10, 25), sync=False)
    assert g.segs == [] and g.n_pending == 3
    # 下次弹窗重新勾：新标签压过撤销行
    C.run_session(env.notes, FakeDialogs([ask_ok("开始确认"), ask_ok("确认提交")], [pick("defaults")]),
                  now=datetime(2026, 10, 21, 8, 0, 0))
    assert len(C.load_stream_labels(env.notes)) == 3


def test_reopen_refused_after_ingest(env):
    _commit_all(env)
    C.mark_ingested(env.notes, "2026-10-19", NOW)
    before = C.queue_path(env.notes, "2026-10-19").read_text(encoding="utf-8")
    st, n = C.reopen_run(env.notes, "2026-10-19", now=NOW)
    assert st == "ingested" and n >= 1
    assert C.queue_path(env.notes, "2026-10-19").read_text(encoding="utf-8") == before
    assert len(C.load_stream_labels(env.notes)) == 3                                 # 标签原样


def test_reopen_missing_queue(env):
    assert C.reopen_run(env.notes, "2026-01-01")[0] == "not_found"


def test_confirm_ingest_reopen_cli(env, ci, capsys):
    _commit_all(env)
    base = ["--notes-dir", str(env.notes), "--digest-dir", str(env.dig)]
    d = FakeDialogs()
    assert ci.main(base + ["--reopen", "2026-10-19"], dialogs=d, today=date(2026, 10, 20), now=NOW) == 0
    assert d.ask_calls == [] and len(C.pending_items(env.notes)) == 3                # 不弹窗
    C.run_session(env.notes, FakeDialogs([ask_ok("开始确认"), ask_ok("确认提交")], [pick("defaults")]), now=NOW)
    C.mark_ingested(env.notes, "2026-10-19", NOW)
    assert ci.main(base + ["--reopen", "2026-10-19"], dialogs=d, today=date(2026, 10, 20), now=NOW) == 1
    assert "已有" in capsys.readouterr().err
    assert ci.main(base + ["--reopen", "2026-01-01"], dialogs=d, today=date(2026, 10, 20), now=NOW) == 2
    assert ci.main(base + ["--reopen", "bad"], dialogs=d, today=date(2026, 10, 20), now=NOW) == 2


# ---- 开场提示 ----

def test_intro_mentions_enter_confirms_page_and_second_check(env):
    make_digest(env.dig)
    C.sync_queue(env.notes, env.dig, now=NOW)
    d = FakeDialogs(asks=[ask_ok("稍后")])
    C.run_session(env.notes, d, now=NOW)
    assert "列表页回车 = 确认本页；全部点完后还会再让你核对一次" in d.ask_calls[0][0]
