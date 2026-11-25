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

NOW = datetime(2026, 11, 23, 12, 0, 0)                # 周一，自然周 2026-11-23
WEEK = "2026-11-23"


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


def _weekly_text(tmp_path, week=WEEK):
    return (tmp_path / W.WEEKLY_DIRNAME / "{}.md".format(week)).read_text(encoding="utf-8")


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
    state = W.begin_week(tmp_path, [_spec()], now=NOW)
    llm = _LLM(error=AssertionError("无变化不得调用模型"))
    out = W.generate_weekly_digest(llm, tmp_path, [_result(path)], state, [_spec()],
                                   source_note="科研札记_2026-11-23_全文精读.md",
                                   template="unused", now=NOW)
    assert out.changed_pages == [] and llm.calls == []
    assert "没有概念页正文的实质变化" in _weekly_text(tmp_path)

    previous = _weekly_text(tmp_path)
    out2 = W.write_no_change(tmp_path, now=NOW, detail="后来的空跑")
    assert out2.preserved_existing
    assert _weekly_text(tmp_path) == previous


def test_changed_page_writes_short_overview_links_and_programmatic_qc(tmp_path):
    before = "# 测试页\n\n旧总述。\n\n## 一节\n\n- 旧论断 [@old2025Key]"
    after = "# 测试页\n\n新总述。\n\n## 一节\n\n- 新论断 [@new2026Key]"
    _page(tmp_path, "demo", before)
    state = W.begin_week(tmp_path, [_spec()], now=NOW)
    path = _page(tmp_path, "demo", after,
                 extra_fm="ungrounded_numbers: 2\ninvalid_refs: 0\ndropped_claims: 0\n")
    report = T.ValidationReport(ungrounded_numbers=2, ungrounded_claims=1)
    report.ungrounded_details.append({"numbers": ["0.12", "0.20"], "text": "新论断含两个待回查数字"})
    llm = _LLM({
        "overview": ["本周新增跨院证据，但结论仍需外部验证。"],
        "pages": [{"slug": "demo", "changes": ["新增一项跨院比较，原有结论未被推翻。"]}],
    })
    out = W.generate_weekly_digest(llm, tmp_path, [_result(path, report=report)], state,
                                   [_spec()], hit_map={"demo": ["new2026Key"]},
                                   source_note="科研札记_2026-11-23_全文精读.md",
                                   template="{{WEEK_START}}\n{{PAGE_DIFFS}}", now=NOW)
    text = _weekly_text(tmp_path)
    assert out.changed_pages == ["demo"] and not out.degraded
    assert "本周新增跨院证据" in text
    assert "[测试页](../demo.md)" in text
    assert "`new2026Key`" in text
    assert "2 个数字未通过自动接地检查" in text
    assert "0.12、0.20" in text and "不等于错误" in text
    assert "1/2 条论断标为单一来源" in text
    assert "科研札记_2026-11-23_全文精读.md" in text
    assert "旧总述" in llm.calls[0][0] and "新总述" in llm.calls[0][0]
    assert llm.calls[0][1]["json_mode"] is True
    assert (tmp_path / W.WEEKLY_DIRNAME / "INDEX.md").exists()


def test_llm_failure_falls_back_to_exact_new_claim(tmp_path):
    before = "# 测试页\n\n## 一节\n\n- 旧论断 [@old2025Key]"
    after = "# 测试页\n\n## 一节\n\n- 新版新增的完整论断 [@new2026Key]"
    _page(tmp_path, "demo", before)
    state = W.begin_week(tmp_path, [_spec()], now=NOW)
    path = _page(tmp_path, "demo", after)
    llm = _LLM(error=RuntimeError("provider down"))
    out = W.generate_weekly_digest(llm, tmp_path, [_result(path)], state, [_spec()],
                                   template="{{PAGE_DIFFS}}", now=NOW)
    text = _weekly_text(tmp_path)
    assert out.degraded
    assert "新版新增的完整论断" in text
    assert "原文差分降级" in text
    assert "degraded: true" in text


def test_model_cannot_invent_pages_and_missing_page_uses_fallback(tmp_path):
    before = "# 测试页\n\n- 旧 [@old2025Key]"
    after = "# 测试页\n\n- 新 [@new2026Key]"
    _page(tmp_path, "demo", before)
    state = W.begin_week(tmp_path, [_spec()], now=NOW)
    path = _page(tmp_path, "demo", after)
    llm = _LLM({"overview": ["有变化"],
                "pages": [{"slug": "ghost", "changes": ["编造的页面"]}]})
    out = W.generate_weekly_digest(llm, tmp_path, [_result(path)], state, [_spec()],
                                   template="{{PAGE_DIFFS}}", now=NOW)
    text = _weekly_text(tmp_path)
    assert out.degraded and "ghost" not in text
    assert "新 [@new2026Key]" in text


def test_failed_only_run_is_not_reported_as_no_change(tmp_path):
    failed = T.TopicResult(slug="broken", status="failed", error="provider down")
    state = W.begin_week(tmp_path, [], now=NOW)
    out = W.generate_weekly_digest(None, tmp_path, [failed], state, [],
                                   template="unused", now=NOW)
    text = _weekly_text(tmp_path)
    assert "没有成功更新" in text
    assert "未完成更新" in text and "provider down" in text
    assert "本轮没有概念页正文的实质变化" not in text


# ---------------------------------------------------------------- 同周累计

def test_same_week_second_run_keeps_first_run_changes(tmp_path):
    """第二次入库不能把第一次入库的变化挤出周报，基线仍须停留在周初。"""
    _page(tmp_path, "a", "# 测试页\n\n- 旧 A [@oldA]")
    _page(tmp_path, "b", "# 测试页\n\n- 旧 B [@oldB]")
    state1 = W.begin_week(tmp_path, [_spec("a"), _spec("b")], now=NOW)
    baseline_a = state1.baseline["a"]

    _page(tmp_path, "a", "# 测试页\n\n- 新 A [@aRun1]")
    llm1 = _LLM({"overview": ["A 变化"], "pages": [{"slug": "a", "changes": ["A 新增"]}]})
    W.generate_weekly_digest(llm1, tmp_path, [_result(tmp_path / "a.md", "a")], state1,
                             [_spec("a")], hit_map={"a": ["aRun1"]},
                             template="{{PAGE_DIFFS}}", now=NOW)
    assert "旧 A" in llm1.calls[0][0] and "新 A" in llm1.calls[0][0]

    # 同周第二次运行：只触发了 B；A 的基线不得被“运行前正文”顶掉。
    state2 = W.begin_week(tmp_path, [_spec("b")], now=NOW)
    assert state2.baseline["a"] == baseline_a
    _page(tmp_path, "b", "# 测试页\n\n- 新 B [@bRun2]")
    llm2 = _LLM({"overview": ["A 与 B 变化"], "pages": [
        {"slug": "a", "changes": ["A 新增"]},
        {"slug": "b", "changes": ["B 新增"]}]})
    W.generate_weekly_digest(llm2, tmp_path, [_result(tmp_path / "b.md", "b")], state2,
                             [_spec("b")], hit_map={"b": ["bRun2"]},
                             template="{{PAGE_DIFFS}}", now=NOW)

    prompt2 = llm2.calls[0][0]
    assert "旧 A" in prompt2 and "新 A" in prompt2       # 第一次的变化仍在
    assert "旧 B" in prompt2 and "新 B" in prompt2
    text = _weekly_text(tmp_path)
    assert "A 新增" in text and "B 新增" in text
    assert "`aRun1`" in text and "`bRun2`" in text       # 触发键按周累计


def test_same_content_rerun_skips_llm_and_does_not_rewrite(tmp_path):
    _page(tmp_path, "demo", "# 测试页\n\n- 旧 [@old]")
    state = W.begin_week(tmp_path, [_spec()], now=NOW)
    path = _page(tmp_path, "demo", "# 测试页\n\n- 新 [@new]")
    payload = {"overview": ["变化"], "pages": [{"slug": "demo", "changes": ["新增"]}]}
    out1 = W.generate_weekly_digest(_LLM(payload), tmp_path, [_result(path)], state,
                                    [_spec()], template="{{PAGE_DIFFS}}", now=NOW)
    before = out1.path.read_text(encoding="utf-8")

    state2 = W.begin_week(tmp_path, [_spec()], now=NOW)
    llm2 = _LLM(error=AssertionError("内容未变不得再调模型"))
    out2 = W.generate_weekly_digest(llm2, tmp_path, [_result(path)], state2, [_spec()],
                                    template="{{PAGE_DIFFS}}", now=NOW)

    assert out2.preserved_existing and llm2.calls == []
    assert out2.path.read_text(encoding="utf-8") == before


def test_degraded_report_is_retried_on_next_run(tmp_path):
    """一次模型故障不能固化成整周终稿：降级报告的指纹不落盘，下次仍重试。"""
    _page(tmp_path, "demo", "# 测试页\n\n- 旧 [@old]")
    state = W.begin_week(tmp_path, [_spec()], now=NOW)
    path = _page(tmp_path, "demo", "# 测试页\n\n- 新 [@new]")
    out1 = W.generate_weekly_digest(_LLM(error=RuntimeError("provider down")), tmp_path,
                                    [_result(path)], state, [_spec()],
                                    template="{{PAGE_DIFFS}}", now=NOW)
    assert out1.degraded

    state2 = W.begin_week(tmp_path, [_spec()], now=NOW)
    llm2 = _LLM({"overview": ["综合恢复"], "pages": [{"slug": "demo", "changes": ["新增"]}]})
    out2 = W.generate_weekly_digest(llm2, tmp_path, [_result(path)], state2, [_spec()],
                                    template="{{PAGE_DIFFS}}", now=NOW)
    assert out2.degraded is False and llm2.calls
    assert "综合恢复" in out2.path.read_text(encoding="utf-8")


def test_state_write_merges_a_concurrent_writer(tmp_path):
    _page(tmp_path, "demo", "# 测试页\n\n- 旧")
    state = W.begin_week(tmp_path, [_spec()], now=NOW)
    path = tmp_path / W.WEEKLY_DIRNAME / "{}{}".format(WEEK, W._STATE_SUFFIX)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["baseline"]["other"] = "另一页周初"
    data["citekeys"]["other"] = ["kOther"]
    data["failures"]["other"] = {"status": "failed", "error": "boom"}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    W._save_state(tmp_path, state, strict=False, merge=True)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["baseline"]["other"] == "另一页周初"
    assert saved["citekeys"]["other"] == ["kOther"]
    assert saved["failures"]["other"]["error"] == "boom"
    assert saved["baseline"]["demo"]                       # 本次状态的基线也没丢


def test_failed_page_stays_in_weekly_report_until_it_succeeds(tmp_path):
    _page(tmp_path, "x", "# 测试页\n\n- 旧 X")
    _page(tmp_path, "y", "# 测试页\n\n- 旧 Y")
    spec_y = _spec("y")
    state = W.begin_week(tmp_path, [spec_y], now=NOW)
    _page(tmp_path, "y", "# 测试页\n\n- 新 Y")
    failed = T.TopicResult(slug="x", status="failed", error="provider down")
    W.generate_weekly_digest(
        _LLM({"overview": ["Y 变化"], "pages": [{"slug": "y", "changes": ["Y 新增"]}]}),
        tmp_path, [failed, _result(tmp_path / "y.md", "y")], state, [spec_y],
        template="{{PAGE_DIFFS}}", now=NOW)
    assert "provider down" in _weekly_text(tmp_path)

    # 下一轮完全没碰 x，失败记录仍须结转（否则周报会静默漏掉没更新的页）。
    _page(tmp_path, "y", "# 测试页\n\n- 再新 Y")
    state2 = W.begin_week(tmp_path, [spec_y], now=NOW)
    W.generate_weekly_digest(
        _LLM({"overview": ["Y 再变"], "pages": [{"slug": "y", "changes": ["Y 再变"]}]}),
        tmp_path, [_result(tmp_path / "y.md", "y")], state2, [spec_y],
        template="{{PAGE_DIFFS}}", now=NOW)
    assert "provider down" in _weekly_text(tmp_path)

    # x 成功重合成后，失败记录清除——且必须**落盘**（merge 不能把清除吞掉）。
    state3 = W.begin_week(tmp_path, [_spec("x")], now=NOW)
    _page(tmp_path, "x", "# 测试页\n\n- 新 X")
    W.generate_weekly_digest(
        _LLM({"overview": ["X 恢复"], "pages": [{"slug": "x", "changes": ["X 新增"]}]}),
        tmp_path, [_result(tmp_path / "x.md", "x")], state3, [_spec("x")],
        template="{{PAGE_DIFFS}}", now=NOW)
    text = _weekly_text(tmp_path)
    assert "provider down" not in text and "X 新增" in text
    saved = json.loads((tmp_path / W.WEEKLY_DIRNAME / "{}{}".format(WEEK, W._STATE_SUFFIX))
                       .read_text(encoding="utf-8"))
    assert "x" not in (saved["failures"] or {})


def test_zombie_failure_not_resurrected_by_state_merge(tmp_path):
    """R1-F2/R2-F1：pop 只改内存；merge 若表达不了删除，失败记录会从盘上复活。"""
    _page(tmp_path, "x", "# 测试页\n\n- 旧 X")
    _page(tmp_path, "y", "# 测试页\n\n- 旧 Y")
    state = W.begin_week(tmp_path, [_spec("x"), _spec("y")], now=NOW)
    failed = T.TopicResult(slug="x", status="failed", error="provider down")
    W.generate_weekly_digest(
        _LLM({"overview": ["-"], "pages": []}), tmp_path, [failed], state, [_spec("x")],
        template="{{PAGE_DIFFS}}", now=NOW)

    # run2：x 成功且零正文变化 → failures 清除必须穿透 merge 落盘。
    state2 = W.begin_week(tmp_path, [_spec("x")], now=NOW)
    merged = T.TopicResult(slug="x", status="merged", path=tmp_path / "x.md", changed=False)
    W.generate_weekly_digest(_LLM(), tmp_path, [merged], state2, [_spec("x")],
                             template="{{PAGE_DIFFS}}", now=NOW)
    saved = json.loads((tmp_path / W.WEEKLY_DIRNAME / "{}{}".format(WEEK, W._STATE_SUFFIX))
                       .read_text(encoding="utf-8"))
    assert saved["failures"] == {}

    # run3：只动无关页，报告不得把 x 宣称成失败。
    _page(tmp_path, "y", "# 测试页\n\n- 新 Y")
    state3 = W.begin_week(tmp_path, [_spec("y")], now=NOW)
    W.generate_weekly_digest(
        _LLM({"overview": ["Y 变化"], "pages": [{"slug": "y", "changes": ["Y 新增"]}]}),
        tmp_path, [_result(tmp_path / "y.md", "y")], state3, [_spec("y")],
        template="{{PAGE_DIFFS}}", now=NOW)
    text = _weekly_text(tmp_path)
    assert "provider down" not in text


def test_recovered_page_without_new_changes_rewrites_stale_failure(tmp_path):
    """R1-F3：失败页恢复且无新变化时，报告里的过时「未完成更新」必须被重写掉。"""
    _page(tmp_path, "x", "# 测试页\n\n- 旧 X")
    state = W.begin_week(tmp_path, [_spec("x")], now=NOW)
    failed = T.TopicResult(slug="x", status="failed", error="provider down")
    W.generate_weekly_digest(_LLM(), tmp_path, [failed], state, [_spec("x")],
                             template="{{PAGE_DIFFS}}", now=NOW)
    assert "provider down" in _weekly_text(tmp_path)

    state2 = W.begin_week(tmp_path, [_spec("x")], now=NOW)
    merged = T.TopicResult(slug="x", status="merged", path=tmp_path / "x.md", changed=False)
    W.generate_weekly_digest(_LLM(error=AssertionError("无变化不得调模型")),
                             tmp_path, [merged], state2, [_spec("x")],
                             template="{{PAGE_DIFFS}}", now=NOW)
    text = _weekly_text(tmp_path)
    assert "provider down" not in text
    assert "\n## 未完成更新" not in text       # 过时清单已重写掉（速览里的说明行不算）


def _tamper_page(path, handwritten, *, drop_sentinel=False):
    """在生成块内手写一行（哨兵还在，哈希失配）；或直接删掉哨兵行。"""
    raw = path.read_text(encoding="utf-8")
    if drop_sentinel:
        raw = raw.replace("<!-- END GENERATED · 以下内容属于你，生成器永不触碰 -->", "")
        raw += "\n- 用户的私人批注：导师说这段要删 [@k]\n"
    else:
        marker = "<!-- END GENERATED"
        idx = raw.find(marker)
        assert idx > 0
        raw = raw[:idx] + "- 用户手写在生成块里的内容：这条结论要删 [@k9]\n\n" + raw[idx:]
    path.write_text(raw, encoding="utf-8")


def test_tampered_page_never_enters_report(tmp_path):
    """R2-F2：哨兵被手改/被删的页不能作为 diff 源，手写内容不得泄漏进周报。"""
    _page(tmp_path, "demo", "# 测试页\n\n- 周初论断 [@old]")
    state = W.begin_week(tmp_path, [_spec()], now=NOW)
    path = _page(tmp_path, "demo", "# 测试页\n\n- 周初论断 [@old]")

    for variant, drop in (("手改生成块", False), ("删除哨兵", True)):
        _tamper_page(path, "私人内容", drop_sentinel=drop)
        llm = _LLM(error=RuntimeError("provider down"))    # 强制走降级路径
        out = W.generate_weekly_digest(llm, tmp_path, [_result(path)], state, [_spec()],
                                       template="{{PAGE_DIFFS}}", now=NOW)
        text = _weekly_text(tmp_path)
        assert "这条结论要删" not in text, variant          # 手写行绝不进正文
        assert "私人批注" not in text, variant
        assert "demo" in text and ("哨兵" in text or "无法安全比较" in text), variant
        # 修复页面后自动恢复跟踪。
        _page(tmp_path, "demo", "# 测试页\n\n- 新论断 [@new]")
        state2 = W.begin_week(tmp_path, [_spec()], now=NOW)
        W.generate_weekly_digest(
            _LLM({"overview": ["恢复"], "pages": [{"slug": "demo", "changes": ["新增论断"]}]}),
            tmp_path, [_result(path)], state2, [_spec()],
            template="{{PAGE_DIFFS}}", now=NOW)
        assert "新增论断" in _weekly_text(tmp_path)
        _page(tmp_path, "demo", "# 测试页\n\n- 周初论断 [@old]")      # 回到周初供下一变体重跑
        state = W.begin_week(tmp_path, [_spec()], now=NOW)
        path = tmp_path / "demo.md"


def test_missing_page_is_listed_not_ignored(tmp_path):
    """R1-F5/R2-F4：页面被删要上报告，不能只留一条日志。"""
    _page(tmp_path, "gone", "# 测试页\n\n- 周初正文")
    state = W.begin_week(tmp_path, [_spec("gone")], now=NOW)
    (tmp_path / "gone.md").unlink()

    out = W.generate_weekly_digest(_LLM(error=AssertionError("缺页不得调模型")),
                                   tmp_path, [], state, [_spec("gone")],
                                   template="{{PAGE_DIFFS}}", now=NOW)
    text = _weekly_text(tmp_path)
    assert "`gone`" in text and "消失" in text
    assert "实质变化" not in text.split("未完成更新")[0].split("本周速览")[-1]   # 不是「正常无变化」

    # 页面恢复后自动清除 missing 记录。
    _page(tmp_path, "gone", "# 测试页\n\n- 周初正文")
    state2 = W.begin_week(tmp_path, [_spec("gone")], now=NOW)
    W.generate_weekly_digest(_LLM(), tmp_path, [], state2, [_spec("gone")],
                             template="{{PAGE_DIFFS}}", now=NOW)
    assert "消失" not in _weekly_text(tmp_path)


def test_reverted_page_migrated_to_earlier_updates(tmp_path):
    """R2-F3：页面回退到周初后，旧报告的失真描述迁进「本周更早的更新」。"""
    _page(tmp_path, "v", "# 测试页\n\n- 周初 V0")
    state = W.begin_week(tmp_path, [_spec("v")], now=NOW)
    _page(tmp_path, "v", "# 测试页\n\n- 新增跨院对比 V1 [@k]")
    out1 = W.generate_weekly_digest(
        _LLM({"overview": ["新增跨院对比"], "pages": [{"slug": "v", "changes": ["新增跨院对比，结论更强"]}]}),
        tmp_path, [_result(tmp_path / "v.md", "v")], state, [_spec("v")],
        template="{{PAGE_DIFFS}}", now=NOW)
    assert "新增跨院对比" in out1.path.read_text(encoding="utf-8")

    _page(tmp_path, "v", "# 测试页\n\n- 周初 V0")           # 回退到周初
    state2 = W.begin_week(tmp_path, [_spec("v")], now=NOW)
    out2 = W.generate_weekly_digest(_LLM(error=AssertionError("无变化不得调模型")),
                                    tmp_path, [], state2, [_spec("v")],
                                    template="{{PAGE_DIFFS}}", now=NOW)
    text = _weekly_text(tmp_path)
    assert not out2.preserved_existing
    assert "回退" in text and "本周更早的更新" in text
    assert "新增跨院对比" in text                          # 历史描述保留在 legacy 节
    fm, _body = T.split_frontmatter(text)
    assert fm["changed_pages"] == []                       # frontmatter 反映现状


def test_failure_only_report_survives_state_loss(tmp_path):
    """R1-F4：changed_pages=[] 的失败-only 报告，状态被删后迁移不能漏掉它。"""
    _page(tmp_path, "x", "# 测试页\n\n- 周初 X")
    state = W.begin_week(tmp_path, [_spec("x")], now=NOW)
    failed = T.TopicResult(slug="x", status="failed", error="provider down")
    W.generate_weekly_digest(_LLM(), tmp_path, [failed], state, [_spec("x")],
                             template="{{PAGE_DIFFS}}", now=NOW)
    (tmp_path / W.WEEKLY_DIRNAME / "{}{}".format(WEEK, W._STATE_SUFFIX)).unlink()

    # 状态重建（基线=当时正文），之后 x 再有新变化——旧报告里的失败记录必须
    # 作为历史保留，新变化照常进分页变化。
    state2 = W.begin_week(tmp_path, [_spec("x")], now=NOW)
    assert "provider down" in state2.legacy_body
    _page(tmp_path, "x", "# 测试页\n\n- 新 X")
    W.generate_weekly_digest(
        _LLM({"overview": ["X 变化"], "pages": [{"slug": "x", "changes": ["X 新增"]}]}),
        tmp_path, [_result(tmp_path / "x.md", "x")], state2, [_spec("x")],
        template="{{PAGE_DIFFS}}", now=NOW)
    text = _weekly_text(tmp_path)
    assert "provider down" in text and "X 新增" in text
    assert text.count("本周更早的更新") == 1


def test_concurrent_writer_report_not_clobbered(tmp_path):
    """R1-F1：并发进程刚写进报告的页变化，后写者不得用陈旧快照覆盖掉。"""
    _page(tmp_path, "a", "# 测试页\n\n- 旧 A")
    _page(tmp_path, "b", "# 测试页\n\n- 旧 B")
    p1_state = W.begin_week(tmp_path, [_spec("a")], now=NOW)      # P1 只见 a
    p2_state = W.begin_week(tmp_path, [_spec("b")], now=NOW)      # P2 从盘上读到 a+b

    _page(tmp_path, "b", "# 测试页\n\n- 新 B [@kB]")
    W.generate_weekly_digest(
        _LLM({"overview": ["B 变化"], "pages": [{"slug": "b", "changes": ["B 新增"]}]}),
        tmp_path, [_result(tmp_path / "b.md", "b")], p2_state, [_spec("b")],
        hit_map={"b": ["kB"]}, template="{{PAGE_DIFFS}}", now=NOW)
    assert "B 新增" in _weekly_text(tmp_path)

    _page(tmp_path, "a", "# 测试页\n\n- 新 A [@kA]")
    W.generate_weekly_digest(
        _LLM({"overview": ["A 与 B 变化"], "pages": [
            {"slug": "a", "changes": ["A 新增"]},
            {"slug": "b", "changes": ["B 累计新增"]}]}),
        tmp_path, [_result(tmp_path / "a.md", "a")], p1_state, [_spec("a")],
        hit_map={"a": ["kA"]}, template="{{PAGE_DIFFS}}", now=NOW)

    text = _weekly_text(tmp_path)
    assert "A 新增" in text
    # P1 渲染前重读盘上状态（b 的基线可见、正文已是新版），b 的变化必须在报告里；
    # 即使靠 reload 没赶上（正文尚未落盘），至少要迁进「本周更早的更新」。
    assert "B 累计新增" in text or "B 新增" in text


def test_review_note_persists_across_runs(tmp_path):
    """R2-F6：首个未接地位置的细节行在累计重渲染时不能消失。"""
    extra = "ungrounded_numbers: 1\ninvalid_refs: 0\ndropped_claims: 0\n"
    _page(tmp_path, "a", "# 测试页\n\n- 旧 [@old]")
    state = W.begin_week(tmp_path, [_spec("a")], now=NOW)
    path = _page(tmp_path, "a", "# 测试页\n\n- 新论断数字 0.3 [@new]", extra_fm=extra)
    report = T.ValidationReport(ungrounded_numbers=1, ungrounded_claims=1)
    report.ungrounded_details.append({"numbers": ["0.3"], "text": "新论断数字 0.3"})
    W.generate_weekly_digest(
        _LLM({"overview": ["A 变化"], "pages": [{"slug": "a", "changes": ["新增"]}]}),
        tmp_path, [_result(path, "a", report=report)], state, [_spec("a")],
        template="{{PAGE_DIFFS}}", now=NOW)
    assert "首个未接地位置：0.3" in _weekly_text(tmp_path)

    # run2 只触发 b：a 的细节行来自 state 缓存，不得消失。
    _page(tmp_path, "b", "# 测试页\n\n- 旧 B")
    state2 = W.begin_week(tmp_path, [_spec("a"), _spec("b")], now=NOW)
    _page(tmp_path, "b", "# 测试页\n\n- 新 B")
    W.generate_weekly_digest(
        _LLM({"overview": ["B 变化"], "pages": [
            {"slug": "a", "changes": ["新增"]},
            {"slug": "b", "changes": ["B 新增"]}]}),
        tmp_path, [_result(tmp_path / "b.md", "b")], state2, [_spec("b")],
        template="{{PAGE_DIFFS}}", now=NOW)
    text = _weekly_text(tmp_path)
    assert "首个未接地位置：0.3" in text
    assert "1 个数字未通过自动接地检查" in text


# ---------------------------------------------------------------- 状态与迁移

def _legacy_report(tmp_path, week=WEEK, changed_pages=("demo",)):
    weekly = tmp_path / W.WEEKLY_DIRNAME
    weekly.mkdir(parents=True, exist_ok=True)
    body = ("# 概念页每周增量 · {} 周\n\n"
            "> 这是给人快速导航的增量层，不替代概念页与证据原句。具体数字、强结论和写稿引用仍请点回原页核查。\n\n"
            "## 本周速览\n\n- 周一的变化\n\n## 分页变化\n\n"
            "### [测试页](../demo.md)\n\n- 旧周报里的变化\n\n"
            "---\n\n自动质检只负责发现可疑点：未接地不等于错误，单一来源不等于虚假；"
            "它们也不能证明其余转述已经人工核验。\n").format(week)
    fm = T._render_frontmatter({
        "title": "概念页每周增量 · {} 周".format(week),
        "type": "topic_weekly_digest", "weekly": week,
        "changed_pages": list(changed_pages), "degraded": False})
    path = weekly / "{}.md".format(week)
    path.write_text(T.assemble(fm, body, T.DEFAULT_USER_ZONE), encoding="utf-8")
    return path


def test_legacy_report_is_migrated_under_earlier_updates(tmp_path):
    _legacy_report(tmp_path)
    _page(tmp_path, "demo", "# 测试页\n\n- 周初正文")
    state = W.begin_week(tmp_path, [_spec()], now=NOW)
    assert "旧周报里的变化" in state.legacy_body

    _page(tmp_path, "demo", "# 测试页\n\n- 周二正文")
    out = W.generate_weekly_digest(
        _LLM({"overview": ["周二变化"], "pages": [{"slug": "demo", "changes": ["周二新增"]}]}),
        tmp_path, [_result(tmp_path / "demo.md")], state, [_spec()],
        template="{{PAGE_DIFFS}}", now=NOW)
    text = out.path.read_text(encoding="utf-8")
    assert "周二新增" in text and "旧周报里的变化" in text
    assert "本周更早的更新" in text
    # 旧速览是分页变化的摘要：规范化迁移时丢弃（内容在分页变化里），防层层叠加；
    # 新报告自己的速览保留一份。
    assert "周一的变化" not in text
    assert text.count("## 本周速览") == 1
    assert text.count("自动质检只负责发现可疑点") == 1      # 旧页脚不重复

    # 重跑仍是同一份内容，不得把迁移段落嵌套一遍。
    state2 = W.begin_week(tmp_path, [_spec()], now=NOW)
    llm2 = _LLM(error=AssertionError("内容未变不得再调模型"))
    W.generate_weekly_digest(llm2, tmp_path, [_result(tmp_path / "demo.md")], state2,
                             [_spec()], template="{{PAGE_DIFFS}}", now=NOW)
    assert llm2.calls == []
    assert out.path.read_text(encoding="utf-8") == text


def test_corrupt_state_recovers_and_keeps_existing_report(tmp_path):
    _page(tmp_path, "demo", "# 测试页\n\n- 周初正文")
    weekly = tmp_path / W.WEEKLY_DIRNAME
    weekly.mkdir(parents=True, exist_ok=True)
    (weekly / "{}{}".format(WEEK, W._STATE_SUFFIX)).write_text("{", encoding="utf-8")
    _legacy_report(tmp_path)

    state = W.begin_week(tmp_path, [_spec()], now=NOW)

    assert state.baseline["demo"].strip()
    assert "旧周报里的变化" in state.legacy_body
    # 重建后的状态必须能正常落盘。
    saved = json.loads((weekly / "{}{}".format(WEEK, W._STATE_SUFFIX)).read_text(encoding="utf-8"))
    assert saved["version"] == W._STATE_VERSION and saved["week"] == WEEK


def test_state_from_previous_week_is_not_reused(tmp_path):
    _page(tmp_path, "demo", "# 测试页\n\n- 现在的正文")
    weekly = tmp_path / W.WEEKLY_DIRNAME
    weekly.mkdir(parents=True, exist_ok=True)
    stale = {"version": W._STATE_VERSION, "week": "2026-11-16",
             "baseline": {"demo": "上一周的正文"}, "citekeys": {},
             "failures": {}, "runs": [], "legacy_body": "", "rendered_signature": ""}
    (weekly / "2026-11-16{}".format(W._STATE_SUFFIX)).write_text(
        json.dumps(stale, ensure_ascii=False), encoding="utf-8")

    state = W.begin_week(tmp_path, [_spec()], now=NOW)

    assert state.week == WEEK
    assert state.baseline["demo"].endswith("现在的正文") and state.legacy_body == ""


def test_state_corrupted_twice_does_not_nest_legacy(tmp_path):
    """R2-F5：状态反复损坏迁移时，legacy 头与速览不得层层叠加。"""
    _page(tmp_path, "demo", "# 测试页\n\n- 旧 [@old]")
    state = W.begin_week(tmp_path, [_spec()], now=NOW)
    path = _page(tmp_path, "demo", "# 测试页\n\n- 新 [@new]")
    W.generate_weekly_digest(
        _LLM({"overview": ["变化一"], "pages": [{"slug": "demo", "changes": ["新增一"]}]}),
        tmp_path, [_result(path)], state, [_spec()],
        template="{{PAGE_DIFFS}}", now=NOW)

    for round_no, marker in ((2, "新增二"), (3, "新增三")):
        # 状态再次损坏 → begin_week 以「当时的正文」重建基线 → 页面再加新变化。
        state_file = tmp_path / W.WEEKLY_DIRNAME / "{}{}".format(WEEK, W._STATE_SUFFIX)
        state_file.write_text("{", encoding="utf-8")
        state_r = W.begin_week(tmp_path, [_spec()], now=NOW)
        _page(tmp_path, "demo", "# 测试页\n\n- 旧 [@old]\n- {}".format(marker))
        W.generate_weekly_digest(
            _LLM({"overview": ["变化{}".format(round_no)],
                  "pages": [{"slug": "demo", "changes": [marker]}]}),
            tmp_path, [_result(tmp_path / "demo.md")], state_r, [_spec()],
            template="{{PAGE_DIFFS}}", now=NOW)

    text = _weekly_text(tmp_path)
    assert text.count("## 本周更早的更新") == 1      # 节标题只一次（速览文案的引用不算）
    assert text.count("## 本周速览") == 1
    assert "新增一" in text and "新增二" in text and "新增三" in text
    assert text.count("- 新增一\n") == 1             # 同一段落不得迁两份


def test_vault_sync_roundtrip_preserves_user_annotation(tmp_path):
    notes_dir, vault_dir = tmp_path / "notes", tmp_path / "vault"
    topics_dir = notes_dir / T.TOPICS_DIRNAME
    _page(topics_dir, "demo", "# 测试页\n\n- 旧论断 [@a2026Key]")
    state = W.begin_week(topics_dir, [_spec()], now=NOW)
    path = _page(topics_dir, "demo", "# 测试页\n\n- 新论断 [@a2026Key]")
    W.generate_weekly_digest(
        _LLM({"overview": ["变化"], "pages": [{"slug": "demo", "changes": ["新增论断"]}]}),
        topics_dir, [_result(path)], state, [_spec()],
        template="{{PAGE_DIFFS}}", now=NOW)

    rep = W.sync_weekly_to_vault(notes_dir, vault_dir, {"a2026Key": "a2026Key"})
    dst = vault_dir / T.VAULT_TOPICS_DIR / W.VAULT_WEEKLY_DIR / "{}.md".format(WEEK)
    assert rep["new"] == 1 and dst.exists()
    dst.write_text(dst.read_text(encoding="utf-8").replace("## 我的批注", "## 我的批注\n\n人工保留"),
                   encoding="utf-8")
    rep2 = W.sync_weekly_to_vault(notes_dir, vault_dir, {"a2026Key": "a2026Key"})
    assert "人工保留" in dst.read_text(encoding="utf-8")
    assert rep2["conflicts"] == []
    assert (dst.parent / "INDEX.md").exists()
    # 状态文件不能被当作周报同步进 vault。
    assert not (dst.parent / "{}{}".format(WEEK, W._STATE_SUFFIX)).exists()


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
