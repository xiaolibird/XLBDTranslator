# -*- coding: utf-8 -*-
"""前缀抢救的完整性门槛。

对抗审核实测出两个致命场景：漏逗号若落在**同一篇的字段之间**（而非元素之间），
抢救会产出「id 在、内容没了」的残骸，而两个 call site 原本只用「id 在不在」当门槛，
于是残骸一路走到 COMPLETED / 正常 llm_judge 裁决，任何计数器都抓不到。
"""
from types import SimpleNamespace

from src.scholar.workflow import ScholarWorkflow
from src.scholar.schema import DigestStatus


def _wf():
    wf = ScholarWorkflow.__new__(ScholarWorkflow)
    wf._salvage_batches = 0
    wf._salvage_dropped = 0
    return wf


# ---------------- 裁决侧 ----------------

def test_verdict_completeness_gate():
    """契约里 one_line/confidence 排最后，抢救最先削掉它们——只查 id+decision 会放行残骸。"""
    complete = {"id": 1, "decision": "INCLUDE", "one_line": "有用", "confidence": 0.8}
    only_conf = {"id": 2, "decision": "INCLUDE", "one_line": "", "confidence": 0.8}
    truncated = {"id": 3, "decision": "INCLUDE", "bucket": ["E"],
                 "flags": ["THREAT"], "role": "MUST_ENGAGE"}      # one_line/confidence 被削
    assert ScholarWorkflow._verdict_is_complete(complete) is True
    assert ScholarWorkflow._verdict_is_complete(only_conf) is True
    assert ScholarWorkflow._verdict_is_complete(truncated) is False


def test_salvaged_truncated_verdict_is_dropped_not_passed_through():
    """抢救模式下残缺裁决必须被丢弃（→ 调用方逐篇回退），而非伪装成正常 llm_judge。

    放行它的代价：空 one_line 会让 embed_store 的 paper 向量退化成纯标题
    （`title + "\\n" + one_line if one_line else title`），此后近邻召回长期失真。
    """
    wf = _wf()
    bad = '{"verdicts":[{"id":1,"decision":"INCLUDE","one_line":"好","confidence":0.9},\n' \
          '{"id":2,"decision":"INCLUDE","bucket":["E"],"flags":["THREAT"],"role":"MUST_ENGAGE"}\n' \
          '{"id":3,"decision":"EXCLUDE"}]}'
    out = wf._parse_filter_response(bad, valid_ids={1, 2, 3})
    assert set(out) == {1}                      # 只有完整的那条留下
    assert wf._salvage_batches == 1
    assert wf._salvage_dropped >= 1


def test_normal_verdicts_not_gated():
    """合法 JSON 不进抢救模式，完整性门槛不得生效——否则会误杀历史上本就没 one_line 的裁决。"""
    wf = _wf()
    good = '{"verdicts":[{"id":1,"decision":"EXCLUDE"},{"id":2,"decision":"INCLUDE"}]}'
    out = wf._parse_filter_response(good, valid_ids={1, 2})
    assert set(out) == {1, 2}
    assert wf._salvage_batches == 0
    assert wf._salvage_dropped == 0
