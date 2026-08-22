# -*- coding: utf-8 -*-
"""execute() 收尾步骤 best-effort 化回归（F7）。

背景（2026-08-17 launchd 实测）：digest 产出四件套在 Step 4 已完整落盘，退出码却
是 1——Step 4 之后的收尾（Zotero 联动 / 标记已读）任何异常都会穿到 cli 兜底
sys.exit(1)，launchd 的 status 1 让人误判 digest 整体失败。现在收尾步骤各自
try/except → warning + notify，继续 return output。

这份测试锁三条边界：
1. 标记已读抛异常 → execute 正常返回 output 且 notify 恰好一次；
2. Zotero 步骤抛异常 → 同上；
3. **语义护栏**：Step 4（生成输出）抛异常仍必须上抛——那时产出确实不完整，
   exit 1 是对的，best-effort 不允许扩张到产出层。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.scholar.schema import DigestOutput, ScholarSettings  # noqa: E402
from src.scholar import workflow as W                          # noqa: E402

MINIMAL_ENV = """
GMAIL__CREDENTIALS_PATH=fake/creds.json
GMAIL__TOKEN_PATH=fake/token.json
LLM__PROVIDER=gemini
LLM__GEMINI_API_KEY=FAKE_KEY_FOR_TEST
LLM__MODEL=fake-model
"""


def _make_settings(tmp_path: Path) -> ScholarSettings:
    env_file = tmp_path / "scholar_test.env"
    env_file.write_text(MINIMAL_ENV, encoding="utf-8")
    settings = ScholarSettings.from_env_file(env_file)
    settings.processing.output_dir = tmp_path / "out"
    settings.processing.translate_abstracts = False
    settings.processing.generate_summary = False
    return settings


def _mock_main_steps(wf, monkeypatch):
    """把 Step 1~4 全部 mock 成无副作用，execute() 直达收尾段。"""
    output = DigestOutput(digest_id="t", segments=[])
    monkeypatch.setattr(wf, "_step_fetch_emails", lambda: None)
    monkeypatch.setattr(wf, "_step_parse_emails", lambda: None)
    monkeypatch.setattr(wf, "_step_fetch_external_sources", lambda: None)
    monkeypatch.setattr(wf, "_step_filter_papers", lambda: None)
    monkeypatch.setattr(wf, "_calculate_rule_based_priority", lambda: None)
    monkeypatch.setattr(wf, "_step_generate_output", lambda: output)
    return output


def _capture_notify(monkeypatch):
    calls = []
    monkeypatch.setattr(W, "notify", lambda title, text: calls.append((title, text)))
    return calls


def test_mark_read_failure_does_not_break_execute(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)
    settings.processing.auto_mark_read = True
    settings.processing.zotero_enabled = False
    wf = W.ScholarWorkflow(settings)
    output = _mock_main_steps(wf, monkeypatch)
    calls = _capture_notify(monkeypatch)

    def _boom():
        raise KeyError("metadata")   # 2026-08-17 事故的嫌疑异常形状

    monkeypatch.setattr(wf, "_step_mark_emails_read", _boom)
    assert wf.execute() is output
    assert len(calls) == 1 and "标记" in calls[0][1]


def test_zotero_failure_does_not_break_execute(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)
    settings.processing.auto_mark_read = False
    settings.processing.zotero_enabled = True
    wf = W.ScholarWorkflow(settings)
    output = _mock_main_steps(wf, monkeypatch)
    calls = _capture_notify(monkeypatch)

    def _boom():
        raise RuntimeError("Zotero 不在线")

    monkeypatch.setattr(wf, "_step_sync_zotero", _boom)
    assert wf.execute() is output
    assert len(calls) == 1 and "Zotero" in calls[0][1]


def test_generate_output_failure_still_raises(tmp_path, monkeypatch):
    """语义护栏：Step 4 失败时产出不完整，必须继续上抛（→ cli exit 1）。"""
    settings = _make_settings(tmp_path)
    settings.processing.auto_mark_read = False
    settings.processing.zotero_enabled = False
    wf = W.ScholarWorkflow(settings)
    _mock_main_steps(wf, monkeypatch)
    calls = _capture_notify(monkeypatch)

    def _boom():
        raise RuntimeError("写盘失败")

    monkeypatch.setattr(wf, "_step_generate_output", _boom)
    with pytest.raises(RuntimeError):
        wf.execute()
    assert calls == []   # 收尾 notify 不该被误触发


def test_mark_read_skips_entries_without_metadata(tmp_path, monkeypatch):
    """根因修复：self.emails 里缺 metadata 的条目不再让 Step 5 整体 KeyError。"""
    settings = _make_settings(tmp_path)
    settings.processing.days_to_fetch = 7
    wf = W.ScholarWorkflow(settings)

    class _Meta:
        email_id = "id-1"

    wf.emails = [{"metadata": _Meta()}, {"raw": "缺 metadata 的坏条目"}]
    marked = []

    class _Gmail:
        def mark_as_read(self, ids):
            marked.append(list(ids))
            return True

    wf.gmail_client = _Gmail()
    wf._step_mark_emails_read()
    assert marked == [["id-1"]]
