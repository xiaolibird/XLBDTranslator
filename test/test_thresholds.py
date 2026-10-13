# -*- coding: utf-8 -*-
"""检索阈值集中化（F4）的防漂移锚。

thresholds.py 是三条检索链路阈值的唯一集中地——这些断言锁的不是数值本身
（数值变更须走重标定，见 thresholds.py 的铁律注释），而是**接线**：消费方必须
引用常量而不是又在自己那里长出一个字面量。换 embedding 模型重标定时，改
thresholds.py 一处即可全链生效，任何一处断线这里先红。
"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.scholar import thresholds as TH   # noqa: E402
from src.scholar import topics as T        # noqa: E402


def test_topics_constants_are_wired():
    assert T.DEFAULT_MIN_SIM is TH.TOPICS_MIN_SIM
    assert T.DEFAULT_RELATIVE_ALPHA is TH.TOPICS_RELATIVE_ALPHA


def test_workflow_neighbor_default_is_wired():
    from src.scholar.workflow import ScholarWorkflow
    sig = inspect.signature(ScholarWorkflow._library_neighbors)
    assert sig.parameters["min_sim"].default is TH.DIGEST_NEIGHBOR_MIN_SIM


def test_qa_gap_threshold_follows_topics():
    """qa 的概念页通道阈值挂在 topics.DEFAULT_MIN_SIM 上，链条不允许中断。"""
    from src.scholar import qa as Q
    assert Q.DEFAULT_GAP_TOPIC_MIN_SIM is TH.TOPICS_MIN_SIM


def test_qa_gap_evidence_threshold_is_wired():
    """句级通道阈值 2026-08-27 从 qa.py 的字面量搬进 thresholds。

    这条通道与概念页通道**有意不同档**（前者剥完脚手架后真/假 gap 只差 0.008，
    取值偏向精确率），所以它是独立常量而非挂在 TOPICS_MIN_SIM 上——但必须引用
    thresholds，否则换 embedding 重标定时又会被漏掉。
    """
    from src.scholar import qa as Q
    assert Q.DEFAULT_GAP_EVIDENCE_MIN_SIM is TH.QA_GAP_EVIDENCE_MIN_SIM
    assert TH.QA_GAP_EVIDENCE_MIN_SIM > TH.TOPICS_MIN_SIM


def test_evidence_sufficiency_constants_are_wired():
    """证据充分性（WO-5）：coverage 的判据默认值与页面级调用点必须引用 thresholds 常量，
    不允许在 coverage/topics/qa 里长出字面量。换 embedding 重标定只改 thresholds.py 一处。"""
    from src.scholar import coverage as C
    from src.scholar import qa as Q
    sig = inspect.signature(C.fixed_sufficiency)
    assert sig.parameters["min_sim"].default is TH.EVIDENCE_SUFFICIENCY_MIN_SIM
    assert sig.parameters["min_sources"].default is TH.TOPIC_MIN_INDEPENDENT_SOURCES
    dyn = inspect.signature(C.qa_dynamic_verdict)
    assert dyn.parameters["floor"].default is TH.EVIDENCE_DYNAMIC_FLOOR
    assert dyn.parameters["expected_chance"].default is TH.EVIDENCE_DYNAMIC_EXPECTED_CHANCE
    assert dyn.parameters["min_sources"].default is TH.QA_MIN_INDEPENDENT_SOURCES
    # answer 级阈值与概念页下限：渲染/闸门代码读的就是这两个常量对象
    assert C.ANSWER_MIN_SOURCES is TH.QA_ANSWER_MIN_SOURCES
    # 概念页不接动态判据：其固定下限不得低于召回门槛，否则拒答口径比召回还松
    assert TH.EVIDENCE_SUFFICIENCY_MIN_SIM >= TH.TOPICS_MIN_SIM
    # 标定选定值（改值须先重标，见 thresholds.py 注释）
    assert (TH.EVIDENCE_DYNAMIC_FLOOR, TH.EVIDENCE_DYNAMIC_EXPECTED_CHANCE,
            TH.QA_MIN_INDEPENDENT_SOURCES) == (0.58, 1.0, 2)
    assert (TH.EVIDENCE_SUFFICIENCY_MIN_SIM, TH.TOPIC_MIN_INDEPENDENT_SOURCES) == (0.55, 3)
