# -*- coding: utf-8 -*-
"""概念页每周人读增量摘要：纯本地单测，不连模型/向量库。"""
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scholar import topics as T                    # noqa: E402
from src.scholar import topic_weekly as W              # noqa: E402

NOW = datetime(2026, 11, 23, 12, 0, 0)


def _page(topics_dir, slug, body, *, extra_fm=""):
    topics_dir.mkdir(parents=True, exist_ok=True)
    fm = ('---\ntopic: "{}"\ntitle: "测试页"\nn_claims: 2\n'
          'single_source_claims: 1\n{}---'.format(slug, extra_fm))
    path = topics_dir / "{}.md".format(slug)
    path.write_text(T.assemble(fm, body, T.DEFAULT_USER_ZONE), encoding="utf-8")
    return path


def _result(path, slug="demo", *, report=None, status="merged"):
    return T.TopicResult(slug=slug, status=status, path=path, changed=True, report=report)


def _spec(slug="demo"):
    return T.TopicSpec(slug=slug, title="测试页", question="问题", queries=["q"])


class _LLM:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def call(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        if self.error:
            raise self.error
        return json.dumps(self.payload, ensure_ascii=False)


def test_week_start_uses_natural_monday():
    assert W.week_start(datetime(2026, 11, 23, 1)) == "2026-11-23"
    assert W.week_start(datetime(2026, 11, 29, 23)) == "2026-11-23"
    assert W.week_start(datetime(2026, 11, 30, 1)) == "2026-11-30"


def test_no_change_skips_llm_and_never_overwrites_existing_report(tmp_path):
    body = "# 测试页\n\n总述不变。\n\n## 一节\n\n- 同一论断 [@a2026Key]"
    path = _page(tmp_path, "demo", body)
    llm = _LLM(error=AssertionError("无变化不得调用模型"))
    out = W.generate_weekly_digest(llm, tmp_path, [_result(path)], {"demo": body}, [_spec()],
                                   source_note="科研札记_2026-11-23_全文精读.md",
                                   template="unused", now=NOW)
    assert out.changed_pages == [] and llm.calls == []
    assert "没有概念页正文的实质变化" in out.path.read_text(encoding="utf-8")

    previous = out.path.read_text(encoding="utf-8")
    out2 = W.write_no_change(tmp_path, now=NOW, detail="后来的空跑")
    assert out2.preserved_existing
    assert out.path.read_text(encoding="utf-8") == previous


def test_changed_page_writes_short_overview_links_and_programmatic_qc(tmp_path):
    before = "# 测试页\n\n旧总述。\n\n## 一节\n\n- 旧论断 [@old2025Key]"
    after = "# 测试页\n\n新总述。\n\n## 一节\n\n- 新论断 [@new2026Key]"
    path = _page(tmp_path, "demo", after,
                 extra_fm="ungrounded_numbers: 2\ninvalid_refs: 0\ndropped_claims: 0\n")
    report = T.ValidationReport(ungrounded_numbers=2, ungrounded_claims=1)
    report.ungrounded_details.append({"numbers": ["0.12", "0.20"], "text": "新论断含两个待回查数字"})
    llm = _LLM({
        "overview": ["本周新增跨院证据，但结论仍需外部验证。"],
        "pages": [{"slug": "demo", "changes": ["新增一项跨院比较，原有结论未被推翻。"]}],
    })
    out = W.generate_weekly_digest(llm, tmp_path, [_result(path, report=report)],
                                   {"demo": before}, [_spec()],
                                   hit_map={"demo": ["new2026Key"]},
                                   source_note="科研札记_2026-11-23_全文精读.md",
                                   template="{{WEEK_START}}\n{{PAGE_DIFFS}}", now=NOW)
    text = out.path.read_text(encoding="utf-8")
    assert out.changed_pages == ["demo"] and not out.degraded
    assert "本周新增跨院证据" in text
    assert "[测试页](../demo.md)" in text
    assert "`new2026Key`" in text
    assert "2 个数字未通过自动接地检查" in text
    assert "0.12、0.20" in text and "不等于错误" in text
    assert "1/2 条论断标为单一来源" in text
    assert "科研札记_2026-11-23_全文精读.md" in text
    assert llm.calls[0][1]["json_mode"] is True
    assert (tmp_path / W.WEEKLY_DIRNAME / "INDEX.md").exists()


def test_llm_failure_falls_back_to_exact_new_claim(tmp_path):
    before = "# 测试页\n\n## 一节\n\n- 旧论断 [@old2025Key]"
    after = "# 测试页\n\n## 一节\n\n- 新版新增的完整论断 [@new2026Key]"
    path = _page(tmp_path, "demo", after)
    llm = _LLM(error=RuntimeError("provider down"))
    out = W.generate_weekly_digest(llm, tmp_path, [_result(path)], {"demo": before}, [_spec()],
                                   template="{{PAGE_DIFFS}}", now=NOW)
    text = out.path.read_text(encoding="utf-8")
    assert out.degraded
    assert "新版新增的完整论断" in text
    assert "原文差分降级" in text
    assert "degraded: true" in text


def test_model_cannot_invent_pages_and_missing_page_uses_fallback(tmp_path):
    before = "# 测试页\n\n- 旧 [@old2025Key]"
    after = "# 测试页\n\n- 新 [@new2026Key]"
    path = _page(tmp_path, "demo", after)
    llm = _LLM({"overview": ["有变化"],
                "pages": [{"slug": "ghost", "changes": ["编造的页面"]}]})
    out = W.generate_weekly_digest(llm, tmp_path, [_result(path)], {"demo": before}, [_spec()],
                                   template="{{PAGE_DIFFS}}", now=NOW)
    text = out.path.read_text(encoding="utf-8")
    assert out.degraded and "ghost" not in text
    assert "新 [@new2026Key]" in text


def test_failed_only_run_is_not_reported_as_no_change(tmp_path):
    failed = T.TopicResult(slug="broken", status="failed", error="provider down")
    out = W.generate_weekly_digest(None, tmp_path, [failed], {}, [], template="unused", now=NOW)
    text = out.path.read_text(encoding="utf-8")
    assert "没有成功更新" in text
    assert "未完成更新" in text and "provider down" in text
    assert "本轮没有概念页正文的实质变化" not in text


def test_vault_sync_roundtrip_preserves_user_annotation(tmp_path):
    notes_dir, vault_dir = tmp_path / "notes", tmp_path / "vault"
    topics_dir = notes_dir / T.TOPICS_DIRNAME
    path = _page(topics_dir, "demo", "# 测试页\n\n- 新论断 [@a2026Key]")
    W.generate_weekly_digest(
        _LLM({"overview": ["变化"], "pages": [{"slug": "demo", "changes": ["新增论断"]}]}),
        topics_dir, [_result(path)], {"demo": "# 测试页\n\n- 旧论断"}, [_spec()],
        template="{{PAGE_DIFFS}}", now=NOW)

    rep = W.sync_weekly_to_vault(notes_dir, vault_dir, {"a2026Key": "a2026Key"})
    dst = vault_dir / T.VAULT_TOPICS_DIR / W.VAULT_WEEKLY_DIR / "2026-11-23.md"
    assert rep["new"] == 1 and dst.exists()
    dst.write_text(dst.read_text(encoding="utf-8").replace("## 我的批注", "## 我的批注\n\n人工保留"),
                   encoding="utf-8")
    rep2 = W.sync_weekly_to_vault(notes_dir, vault_dir, {"a2026Key": "a2026Key"})
    assert "人工保留" in dst.read_text(encoding="utf-8")
    assert rep2["conflicts"] == []
    assert (dst.parent / "INDEX.md").exists()


def test_topics_index_links_weekly_digest_only_when_available(tmp_path):
    spec = _spec()
    _page(tmp_path, "demo", "正文")
    assert "weekly/INDEX.md" not in T.render_topics_index(tmp_path, [spec])
    (tmp_path / W.WEEKLY_DIRNAME).mkdir()
    (tmp_path / W.WEEKLY_DIRNAME / "INDEX.md").write_text("# 周报\n", encoding="utf-8")
    assert "[每周人读增量](weekly/INDEX.md)" in T.render_topics_index(tmp_path, [spec])


def test_trigger_adds_weekly_flag_only_for_weekly_entry(monkeypatch, tmp_path):
    monkeypatch.setattr(T, "notes_dir_is_production", lambda _d: True)
    monkeypatch.setattr("src.utils.notify.notify", lambda *_a: None)
    seen = []

    def _run(cmd, **_kwargs):
        seen.append(cmd)
        return T.ChildRun(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(T, "_run_refresh_child", _run)
    T.trigger_topic_refresh(tmp_path, note_md="week.md", notify_title="t", subject="s")
    T.trigger_topic_refresh(tmp_path, note_md="week.md", notify_title="t", subject="s",
                            weekly_digest=True)
    assert "--weekly-digest" not in seen[0]
    assert "--weekly-digest" in seen[1]


def test_parse_digest_rejects_non_json():
    with pytest.raises(W.WeeklyDigestError):
        W.parse_digest_synthesis("没有 JSON", ["demo"])
