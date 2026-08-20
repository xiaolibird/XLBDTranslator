# -*- coding: utf-8 -*-
"""json_tools.loads_lenient：LLM 返回畸形 JSON 时的前缀抢救。

覆盖的畸形类型来自真实生产事故：2026-08-17 那轮 digest 有 15 篇同批同位置报
`Expecting ',' delimiter: line 124 column 1`，即数组元素间漏逗号。
"""
import json

import pytest

from src.utils.json_tools import loads_lenient, strip_code_fences


def test_normal_json_unchanged():
    """合法 JSON 必须逐字等价于 json.loads，抢救逻辑不得改变正常路径。"""
    for s in ['{"a": 1}', '[1, 2, 3]', '{"n": null, "s": "x"}']:
        assert loads_lenient(s) == json.loads(s)


def test_strips_code_fences():
    assert loads_lenient('```json\n{"a": 1}\n```') == {"a": 1}


def test_salvages_missing_comma_between_elements():
    """生产事故的原型：元素间漏逗号 → 报 Expecting ',' delimiter，且位置在行首。"""
    bad = '{"papers":[{"id":1},\n{"id":2}\n{"id":3}]}'
    with pytest.raises(json.JSONDecodeError) as ei:
        json.loads(bad)
    assert "Expecting ',' delimiter" in str(ei.value)      # 错误签名与生产一致
    got = loads_lenient(bad)
    assert [p["id"] for p in got["papers"]] == [1, 2]      # 畸形点之后被丢弃


def test_salvages_truncation_and_prose_and_trailing_comma():
    assert loads_lenient('{"m": ["one", "two", "thr') == {"m": ["one", "two"]}
    assert loads_lenient('{"p":[{"id":1},\n说明：以下为第二部分\n{"id":2}]}') == {"p": [{"id": 1}]}
    assert loads_lenient('{"p":[{"id":1},{"id":2},]}') == {"p": [{"id": 1}, {"id": 2}]}


def test_does_not_break_strings_containing_brackets():
    """字符串内的 ] } 不参与括号栈，否则会在错误位置截断。"""
    assert loads_lenient('{"s":"has ] and } inside","x":[1,2') == {"s": "has ] and } inside", "x": [1]}


def test_returns_none_when_unsalvageable():
    """救不出来返回 None（不抛），由调用方决定抛还是走原有回退。"""
    assert loads_lenient("not json") is None
    assert loads_lenient("") is None


def test_pdf_ingest_alias_points_to_shared_impl():
    """pdf_ingest 的 _loads_lenient 必须是共享实现的别名，不能再各持一份。"""
    from src.scholar import pdf_ingest
    assert pdf_ingest._loads_lenient is loads_lenient


# ---------------- 抢救的规模闸 ----------------

def test_salvage_bails_out_on_oversized_input():
    """救不回来的路径是 O(n²)：实测 90 KB 逗号密集串曾耗时 26 s。超限必须直接认输。"""
    import time
    from src.utils.json_tools import _SALVAGE_MAX_CHARS
    huge = "{" + ("," * (_SALVAGE_MAX_CHARS + 10))
    t0 = time.time()
    assert loads_lenient(huge) is None
    assert time.time() - t0 < 1.0          # 命中长度闸，不进扫描循环


def test_salvage_scans_backward_from_error_position():
    """从 JSONDecodeError.pos 起反扫，而非从串尾——否则每次 json.loads 都是 O(n)，
    5000 次尝试仍要十几秒。实测 190 KB 逗号串由 16.6 s 降到毫秒级。"""
    import time
    dense = "{" + ("," * 190_000)
    t0 = time.time()
    assert loads_lenient(dense) is None
    assert time.time() - t0 < 1.0


# ==================== looks_like_complete_json ====================
# 2026-08-20：LLM 客户端层判「响应有没有被截断」的判据。宽松度必须夹在
# 「不误判加了开场白的完好 JSON」与「不放过半截 JSON」之间。

from src.utils.json_tools import looks_like_complete_json


@pytest.mark.parametrize("text", [
    '{"a": 1}',
    '[1, 2, 3]',
    '```json\n{"a": 1}\n```',
    '好的，结果如下：\n[{"id": 1}]\n以上。',           # topics.py 那种带开场白的
    'Here is the result:\n{"verdicts": []}',
])
def test_完整json判为完整(text):
    assert looks_like_complete_json(text) is True


@pytest.mark.parametrize("text", [
    '',
    '   ',
    'not json at all',
    '{"a": 1, "b": "半截',
    '{"verdicts": [{"id": 1, "decision": "INCLUDE"}, {"id": 2, "one_lin',
])
def test_半截或非json判为不完整(text):
    assert looks_like_complete_json(text) is False


def test_半截数组不得被开场白兜底救成完整():
    """最关键的一条：'[{"id":1},{"id":2,"t":"未闭' 里抠得出一个完整的 {"id":1}。
    若无条件走「从散文里抠 JSON」的兜底，这条半截数组会被判成完整——而它
    正是生产上 08-17 那批截断的真实形状。故正文以 { 或 [ 开头时不许再抠。"""
    assert looks_like_complete_json('[{"id": 1}, {"id": 2, "t": "未闭') is False


def test_判据倒挂回归_同一份json加不加开场白必须同判():
    """曾经的判据是「先 json.loads，失败就从散文里抠首个 {...}」，方向是倒挂的：
    去掉开场白判成截断、加上开场白判成完整——加句废话就能让成本减半。"""
    core = '[{"id": 1}, {"id": 2}]'
    assert looks_like_complete_json(core + '\n\n以上是 2 条裁决。') is True
    assert looks_like_complete_json('好的：\n' + core + '\n以上是 2 条裁决。') is True


@pytest.mark.parametrize("text", [
    '{"a": 1,}',                     # 尾随逗号
    '{"a": 1}\n{"b": 2}',            # 多对象拼接
    '[{"a": 1} {"b": 2}]',           # 数组元素间漏逗号
])
def test_格式跑偏不算截断(text):
    """这些都让 json.loads 失败，但结构是写完的，重试换不来更好的结果，
    而 loads_lenient 本来就救得回。判成截断只会白烧一次调用 + 打假警报。"""
    assert looks_like_complete_json(text) is True
    assert loads_lenient(text) is not None


def test_前言加完整json必须能被抢救层救回():
    """闸门判它「结构完整」放行，而 workflow 的两个解析点只剥 ``` 围栏、不抠前言。
    少了 loads_lenient 的剥前言，这种响应会让 filter 整批 20 篇降级成关键词裁决。"""
    got = loads_lenient('好的，以下是本批的裁决结果：\n{"verdicts": [{"id": 1, "decision": "INCLUDE"}]}')
    assert got == {"verdicts": [{"id": 1, "decision": "INCLUDE"}]}


def test_前言里回显的示例不得被当成载荷救回():
    """模型爱在开场白里回显 prompt 的输出示例。若抢救层从**示例**那个 `{` 起截，
    救回来的是示例本身——而它的 id 完全合法，workflow 的 valid_ids 防线挡不住，
    于是静默把真实的 INCLUDE 覆盖成示例里的 EXCLUDE。
    这正是 workflow._parse_translation_response 的 docstring 记载的已知生产危害；
    用诚实的整批失败换一篇被投毒是严格退步。"""
    resp = ('示例：{"id": 1, "decision": "EXCLUDE", "one_line": "中文摘要占位"}\n\n'
            '实际裁决如下：\n'
            '[{"id": 1, "decision": "INCLUDE", "one_line": "MNAR 缺失机制的新识别条件"}, '
            '{"id": 2, "decision": "INCLUDE", "one_line": "影子变量"}]')
    got = loads_lenient(resp)
    assert isinstance(got, list), "必须取到真实载荷（数组），不是示例那个对象"
    assert got[0]["decision"] == "INCLUDE"


@pytest.mark.parametrize("resp", [
    '按 {id, decision} 格式输出如下：\n[{"id": 1, "decision": "INCLUDE"}]',
    '输出格式为 {"id": 0}：\n{"verdicts": [{"id": 1, "decision": "INCLUDE"}]}',
    '字段为 [id, decision]，结果：\n[{"id": 1, "decision": "INCLUDE"}]',
])
def test_前言自带括号时仍能救回真载荷(resp):
    """filter/翻译的 prompt 里全是 JSON 格式说明，模型复述格式时必然带括号。
    只认第一个括号会从那段格式说明起截，必然解析失败、整批降级。"""
    assert loads_lenient(resp) is not None


def test_闸门放行则抢救层必然拿得出东西():
    """这个不变式是整套设计的地基：闸门说「完整」就意味着不重采样，此时下游若
    一个字段都取不到，就是最坏组合（不重试 + 整批回退 + 还记成一次健康调用）。"""
    shapes = [
        '{"a": 1}', '[1, 2, 3]', '{"a": 1,}', '[{"a": 1} {"b": 2}]',
        '[{"id": 1}]\n\n以上是 1 条裁决。', '好的：\n[{"id": 1}]',
        '按 {id, decision} 格式：\n[{"id": 1}]',
        '这个请求缺少主题——请告知后我再输出 {"verdicts": [...]} 形式的结果。',
        '已完成合成，输出为合法 JSON。要点：……',
        '[{"id": 1}, {"id": 2, "t": "未闭', '', '   ', 'not json at all',
    ]
    for s in shapes:
        if looks_like_complete_json(s):
            assert loads_lenient(s) is not None, "闸门放行了但抢救层救不出: {!r}".format(s)


def test_示例比真载荷还长时也不得投毒():
    """`test_前言里回显的示例不得被当成载荷救回` 靠「取救出最多的那个起点」也能过，
    所以它测不出「严格解析先行」这道护栏在不在。这条专门构造示例**比真载荷更长**
    的情形：此时按体量取会选中示例，只有严格解析先行才救得对。"""
    long_placeholder = "中文摘要占位" * 40
    resp = ('示例：{{"id": 1, "decision": "EXCLUDE", "one_line": "{}"}}\n\n'
            '实际裁决：\n[{{"id": 1, "decision": "INCLUDE"}}]').format(long_placeholder)
    got = loads_lenient(resp)
    assert isinstance(got, list), "按体量取会选中示例；必须靠严格解析先行取到真载荷"
    assert got[0]["decision"] == "INCLUDE"
