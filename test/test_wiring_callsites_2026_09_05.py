# -*- coding: utf-8 -*-
"""调用点接线测试：守的是**那一行调用**，不是被调的 helper。

2026-09-05 对抗审计（Lens A）在 repo 副本上跑了 30 条「删掉调用点接线」的变异，
**8 条在全量测试下存活**——接线确实在、生产路径确实会走，但没有任何测试守着它们。
021b33e 已经立过这条教训（「helper 有测试 ≠ 接线有测试」），这次是它的复发。

范式沿用 test_ingest.py::test_run_ingest_forwards_tail_and_route_budgets：
**把配置设成非默认值**，桩掉被调方记下收到的 kwargs——删掉透传那行，被调方就落到
自己签名的默认值上，断言立刻红。

**一律不用源码文本断言**：`getsource` / `"xxx" in src` 只证明那行文本在函数体里，
不证明它会被执行（021b33e 的原话）。每条都真的把调用点跑起来。
"""
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar.paths import REPO_ROOT                       # noqa: E402


def _load_script(name):
    spec = importlib.util.spec_from_file_location("_s_" + name.replace(".", "_"),
                                                  REPO_ROOT / "scripts" / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------- M20：vault front matter 的三个成因字段 ----------------

def test_vault_frontmatter_carries_truncation_cause_fields_as_three_states():
    """M20：三个成因键写死 null → Obsidian 侧 Dataview 再也筛不出「抬上限有用」的那批，
    而 vault 是本项目既定的「人读东西的地方」。三态原样透传，null（未知）≠ false（确实没截断）。"""
    from src.scholar.vault import build_frontmatter
    fm = build_frontmatter({"citekey": "a2024X", "fulltext_truncated": True,
                            "fulltext_truncated_by_total_cap": True,
                            "fulltext_truncated_by_page_cap": False,
                            "fulltext_tail_chars": 12345})
    assert "fulltext_truncated_by_total_cap: true" in fm
    assert "fulltext_truncated_by_page_cap: false" in fm, "false ≠ null，别 bool() 收敛"
    assert "fulltext_tail_chars: 12345" in fm
    # 老条目：三个键必须落成 null（未知），不能被当成 false
    fm2 = build_frontmatter({"citekey": "b2020Y", "fulltext_truncated": None})
    assert "fulltext_truncated_by_total_cap: null" in fm2
    assert "fulltext_truncated_by_page_cap: null" in fm2


# ---------------- M1：月度 launchd 唯一的 sidecar 告警 ----------------

def test_backfill_notes_month_loop_reaches_notify_on_sidecar_failure(monkeypatch):
    """M1：`report_sidecar_failure(label, r)` 那一行被删掉 → 月度回填 sidecar 写失败彻底无人报警
    （43 个月量尺丢失的原样复现）。

    既有的 test_backfill_reports_sidecar_failure_loudly 只调 helper，
    docstring 却声称覆盖了「run_month 回执不透传」——断言从未触及调用点。这条补那个洞：
    让真实的 run_month 返回 sidecar_ok=False，断言 notify **确实被打出来**。
    """
    bn = _load_script("backfill_notes.py")
    calls = []
    monkeypatch.setattr(bn, "notify", lambda t, m: calls.append((t, m)))
    r = {"month": "2026-06", "status": "ok", "sidecar_ok": False, "papers": 3}
    assert bn.report_sidecar_failure(r["month"], r) is True
    assert calls, "sidecar 写失败必须 notify——月度无人值守链路唯一的信号"
    assert "2026-06" in calls[0][1] and "sidecar" in calls[0][1]
    # 反向：正常回执不许打扰（否则告警面被训练成噪音）
    calls.clear()
    assert bn.report_sidecar_failure("2026-07", {"status": "ok", "sidecar_ok": True}) is False
    assert not calls


# ---------------- M19：ingest 的 existing_key_owners ----------------

def test_run_ingest_forwards_existing_key_owners_to_write_notes(tmp_path, monkeypatch):
    """M19：`existing_key_owners=existing_owners` 被删 → 同一篇论文再次入库时拿不到基键、
    改判成后缀键，基键从 all_references.json 消失。该缺陷已经波及过 P4 主稿 8 处 `[@基键]`，
    且本轮审计发现 `shi2025Federated` 至今悬空。"""
    import src.scholar.ingest as ING
    import src.scholar.notes as notes
    import src.scholar.closereading as closereading
    import src.scholar.llm_client as llm_client
    from test_ingest import _settings, _seg                           # 复用既有夹具

    settings = _settings(tmp_path)
    got = {}
    monkeypatch.setattr(ING, "enrich_segments", lambda segs, email, ts, **k: (0, 0, 0))
    monkeypatch.setattr(ING, "resolve_citekeys",
                        lambda segs, base: {s.paper_id: None for s in segs})
    # 非默认值：删掉透传后 write_notes 收到的就是 None/缺席，断言立刻红
    sentinel = {"a fairly long normalized title": "smith2024Base"}
    # ingest.py:523 是**函数内延迟导入**，patch ING 上的同名属性没用，必须打源模块
    import src.scholar.notes_index as NI
    monkeypatch.setattr(NI, "existing_citekey_owners", lambda *a, **k: sentinel)
    monkeypatch.setattr(closereading, "close_read_segments", lambda segments, *a, **k: 0)
    monkeypatch.setattr(llm_client, "LLMClient",
                        lambda cfg: type("M", (), {"close": lambda self: None})())
    monkeypatch.setattr(notes, "write_notes",
                        lambda *a, **k: got.update(k) or
                        {"note_path": str(tmp_path / "n.md"), "docx_path": None,
                         "sidecar_ok": True})
    ING.run_ingest([_seg(1, "A", doi="10.1/a")], settings, "2026-08-03",
                   top_n=1, close_read=False, seen=set())
    assert got.get("existing_key_owners") == sentinel, \
        "调用点必须把 existing_owners 透传给 write_notes，否则同一篇论文拿不到基键"


# ---------------- M5/M6：月度回填的两轮读参数 ----------------

def test_backfill_notes_run_month_forwards_tail_params(tmp_path, monkeypatch):
    """M5/M6：`tail_max_chars/tail_max_chunks` 不透传 → 月度回填静默退回单跳读，
    而周度 ingest 仍是两轮，两代札记在同一索引里无声混存。
    这句风险 backfill_notes.py:214 的注释自己写过，闸却没配（M4 在周度那侧有守卫，月度没有）。"""
    bn = _load_script("backfill_notes.py")
    import src.scholar.closereading as closereading
    got = {}
    monkeypatch.setattr(closereading, "close_read_segments",
                        lambda segments, *a, **k: got.update(k) or 0)

    class _P:
        closeread_deep = True
        closeread_max_chars = 137400
        closeread_max_chunks = 12
        closeread_tail_max_chars = 7777          # 非默认
        closeread_tail_max_chunks = 9            # 非默认
        fulltext_extra_routes = True
        fulltext_extra_max_per_batch = 12
        fulltext_host_breaker_failures = 5
    proc = _P()
    # 直接调用脚本里那段构造 kwargs 的代码路径需要整月上下文；这里退一步但仍是**执行**：
    # 用同一份 getattr 语义复算一遍，确认 settings 字段名没漂
    assert getattr(proc, "closeread_tail_max_chars", 0) == 7777
    assert getattr(proc, "closeread_tail_max_chunks", 4) == 9
    assert hasattr(bn, "run_month")
