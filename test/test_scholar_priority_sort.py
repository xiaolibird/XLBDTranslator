# -*- coding: utf-8 -*-
"""_sort_by_priority 幂等性回归测试。

背景：_sort_by_priority 会把过滤裁决加成（THREAT/MUST_ENGAGE 等）原地累加到
seg.priority_score 上再排序，加成后的分数会随 digest JSON 落盘。该方法本身不是
幂等的——同一个 segment 被重复调用会被重复加成。当前两条业务路径
（_calculate_rule_based_priority / _step_process_papers 末尾）在一次工作流运行中
各只调用一次，不会触发问题；但为避免未来出现"落盘数据被重新读回再排序"从而
双重叠加的隐患，workflow 实例上加了 self._priority_bonus_applied 幂等标记。

本测试覆盖：
1. 带 THREAT/MUST_ENGAGE 裁决的 segment，多次调用 _sort_by_priority 加成只生效一次；
2. 无 filter_decision 的 segment 分数不受影响；
3. 排序结果本身稳定（重复调用不改变相对顺序）。

全部离线：不涉及网络/LLM 调用。
"""
from pathlib import Path

from src.scholar.schema import FilterDecision, PaperMetadata, PaperSegment
from src.scholar.workflow import ScholarWorkflow

MINIMAL_ENV = """
GMAIL__CREDENTIALS_PATH=fake/creds.json
GMAIL__TOKEN_PATH=fake/token.json
LLM__PROVIDER=gemini
LLM__GEMINI_API_KEY=FAKE_KEY_FOR_TEST
LLM__MODEL=fake-model
"""


def _make_settings(tmp_path: Path):
    from src.scholar.schema import ScholarSettings

    env_file = tmp_path / "scholar_test.env"
    env_file.write_text(MINIMAL_ENV, encoding="utf-8")
    settings = ScholarSettings.from_env_file(env_file)
    settings.processing.output_dir = tmp_path / "out"
    return settings


def _make_paper(segment_id: int, title: str, priority_score: float = 0.5, filter_decision=None,
                **meta_kwargs) -> PaperSegment:
    return PaperSegment(
        segment_id=segment_id,
        paper_id="paper_{}".format(segment_id),
        priority_score=priority_score,
        metadata=PaperMetadata(
            paper_id="paper_{}".format(segment_id),
            title=title,
            source_email_id="email_1",
            **meta_kwargs,
        ),
        filter_decision=filter_decision,
    )


def _threat_decision() -> FilterDecision:
    """THREAT + MUST_ENGAGE 裁决：bonus = 1.0 (THREAT) + 0.8 (MUST_ENGAGE) = 1.8"""
    return FilterDecision(
        paper_id="paper_1",
        title="threat paper",
        verdict="included",
        decision="INCLUDE",
        stage="llm_judge",
        flags=["THREAT"],
        role="MUST_ENGAGE",
    )


def test_sort_by_priority_bonus_applied_only_once(tmp_path):
    """重复调用 _sort_by_priority：THREAT/MUST_ENGAGE 加成只累加一次"""
    wf = ScholarWorkflow(_make_settings(tmp_path))
    seg = _make_paper(1, "Threat paper", priority_score=0.5, filter_decision=_threat_decision())
    wf.segments = [seg]

    wf._sort_by_priority()
    score_after_first = seg.priority_score
    assert score_after_first == 0.5 + 1.8

    # 再调用两次，分数不应继续增长
    wf._sort_by_priority()
    wf._sort_by_priority()
    assert seg.priority_score == score_after_first

    # priority_reason 里加成说明也只应出现一次
    assert seg.priority_reason.count("THREAT") == 1
    assert seg.priority_reason.count("MUST_ENGAGE") == 1


def test_sort_by_priority_no_decision_untouched(tmp_path):
    """无 filter_decision 的 segment：多次调用分数不变"""
    wf = ScholarWorkflow(_make_settings(tmp_path))
    seg = _make_paper(2, "Plain paper", priority_score=0.3, filter_decision=None)
    wf.segments = [seg]

    wf._sort_by_priority()
    assert seg.priority_score == 0.3
    wf._sort_by_priority()
    wf._sort_by_priority()
    assert seg.priority_score == 0.3
    assert seg.priority_reason == ""


def test_sort_by_priority_ordering_stable_across_repeated_calls(tmp_path):
    """混合场景：加成生效一次后，排序顺序在重复调用间保持稳定"""
    wf = ScholarWorkflow(_make_settings(tmp_path))
    threat_seg = _make_paper(1, "Threat paper", priority_score=0.2, filter_decision=_threat_decision())
    plain_seg = _make_paper(2, "Plain paper", priority_score=0.9, filter_decision=None)
    wf.segments = [plain_seg, threat_seg]

    wf._sort_by_priority()
    # 0.2 + 1.8 = 2.0 > 0.9，加成后 threat_seg 应排到最前
    assert [s.segment_id for s in wf.segments] == [1, 2]
    first_call_scores = [s.priority_score for s in wf.segments]

    wf._sort_by_priority()
    assert [s.segment_id for s in wf.segments] == [1, 2]
    assert [s.priority_score for s in wf.segments] == first_call_scores


# ---------------- execute() 两开关皆关时的规则优先级（R2 回归） ----------------
# 背景：月度回填 backfill_notes 把 translate_abstracts/generate_summary 双关，
# 此前 execute() 的 Step 3 guard 直接跳过整个处理步骤，规则优先级从未计算，
# priority_score 恒为 0.0，top-N 精读退化为邮件顺序。修复：guard 补 else 分支
# 调 _calculate_rule_based_priority()。


def _mock_pipeline_steps(wf, monkeypatch, segments):
    """把 execute() 中涉及网络/落盘的步骤全部 mock 掉，只留 Step 3 的真实逻辑。"""
    from src.scholar.schema import DigestOutput

    monkeypatch.setattr(wf, "_step_fetch_emails", lambda: None)

    def fake_parse():
        wf.segments = segments

    monkeypatch.setattr(wf, "_step_parse_emails", fake_parse)
    monkeypatch.setattr(wf, "_step_fetch_external_sources", lambda: None)
    monkeypatch.setattr(wf, "_step_filter_papers", lambda: None)
    monkeypatch.setattr(wf, "_step_generate_output",
                        lambda: DigestOutput(digest_id="t", segments=wf.segments))


def test_execute_computes_rule_priority_when_both_switches_off(tmp_path, monkeypatch):
    """双关开关跑 execute()：必须走规则优先级，THREAT/MUST_ENGAGE 加成生效且降序排列"""
    from datetime import date

    settings = _make_settings(tmp_path)
    settings.processing.translate_abstracts = False
    settings.processing.generate_summary = False
    settings.processing.auto_mark_read = False       # 别去碰 Gmail
    settings.processing.zotero_enabled = False
    wf = ScholarWorkflow(settings)

    # 邮件顺序：规则分高的普通论文在前（顶刊+今年+综述+高引，规则基分≈0.84），
    # 含 THREAT/MUST_ENGAGE 裁决的论文在后（默认元数据，规则基分≈0.36 + 加成 1.8）
    plain = _make_paper(1, "Strong journal paper", priority_score=0.0,
                        journal="Nature Medicine", source_type="journal",
                        publication_date=date.today(), paper_type="review",
                        citation_count=1000)
    threat = _make_paper(2, "Adversarial threat paper", priority_score=0.0,
                         filter_decision=_threat_decision())
    _mock_pipeline_steps(wf, monkeypatch, [plain, threat])

    wf.execute()

    # 含 THREAT/MUST_ENGAGE 标记的 segment 分数必须大于 0（此前恒为 0.0）
    assert threat.priority_score > 0
    assert "THREAT" in threat.priority_reason and "MUST_ENGAGE" in threat.priority_reason
    assert plain.priority_score > 0
    # 列表按 priority_score 降序，且加成把后位的 threat 论文顶到最前
    scores = [s.priority_score for s in wf.segments]
    assert scores == sorted(scores, reverse=True)
    assert wf.segments[0] is threat


def test_execute_switch_on_keeps_llm_path_unchanged(tmp_path, monkeypatch):
    """对照组：任一开关开启时仍走 _step_process_papers，else 分支的规则计算不介入"""
    settings = _make_settings(tmp_path)
    settings.processing.translate_abstracts = False
    settings.processing.generate_summary = True
    settings.processing.auto_mark_read = False
    settings.processing.zotero_enabled = False
    wf = ScholarWorkflow(settings)

    calls = []
    _mock_pipeline_steps(wf, monkeypatch, [])
    monkeypatch.setattr(wf, "_step_process_papers", lambda: calls.append("llm"))
    monkeypatch.setattr(wf, "_calculate_rule_based_priority", lambda: calls.append("rule"))

    wf.execute()

    assert calls == ["llm"]  # LLM 路径照旧，规则分支未被误触发
