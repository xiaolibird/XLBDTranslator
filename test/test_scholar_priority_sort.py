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


def _make_paper(segment_id: int, title: str, priority_score: float = 0.5, filter_decision=None) -> PaperSegment:
    return PaperSegment(
        segment_id=segment_id,
        paper_id="paper_{}".format(segment_id),
        priority_score=priority_score,
        metadata=PaperMetadata(
            paper_id="paper_{}".format(segment_id),
            title=title,
            source_email_id="email_1",
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
