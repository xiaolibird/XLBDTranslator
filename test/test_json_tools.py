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
