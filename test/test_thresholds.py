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
