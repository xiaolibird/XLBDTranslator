#!/usr/bin/env python3
"""
异步翻译熔断盲区回归测试。

同步路径 _run_sync_batches 有两道熔断闸：classify_fatal(e)=='hard' 和
consecutive_failures>=CONSECUTIVE_FAILURE_THRESHOLD（后者专门兜住『engine
不抛异常、只把结果降级成 "[Failed:...]" 字符串』的持续性故障）。异步路径
_process_single_batch 此前只有 fatal_holder（仅 hard 分类），跨批次的
连续 0 成功计数根本不存在——engine 持续返回结构不对的 JSON 或回退链耗尽
后返回占位字符串都不抛异常，classify_fatal 永远拿不到异常对象，fatal_holder
永不置位，224 批全部照常发真实请求跑完，直到 _quality_check 才发现整本书
都是 [Failed:]。

本文件覆盖修复：stats["consecutive_zero_success"] 达阈值时补一个代表性
APIError 写入 fatal_holder，复用现成的短路分支跳过后续批次。

注意：asyncio.as_completed 内部对传入的任务先做 `set(fs)` 再逐个
ensure_future——协程对象按 id() 哈希落桶，导致批次实际发起顺序是被打乱的
（与 enumerate 时的批次号无关，且随进程内存布局变化）。本文件的 fake
translator 内部无任何真实 await 挂起点，各批次仍严格顺序执行、不会互相
交叉（验证见下方 test_all_failed_strings_without_exception_trips_circuit_breaker
的注释），但『哪几个具体批次』先跑是不确定的——因此断言只依赖『发起了几次
真实请求』『还有多少段被短路』这类与顺序无关的不变量，不依赖具体的批次号。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.core.exceptions import APIError
from src.core.schema import ContentSegment, SegmentList
from src.workflow.workflow import TranslationWorkflow


class _FakeCheckpoint:
    """与 test_async_batch_order.py 中同款的最小 checkpoint 假件"""

    def __init__(self):
        self.completed = set()
        self.failed = {}

    def mark_segment_completed(self, segment_id):
        self.completed.add(segment_id)

    def mark_segment_failed(self, segment_id, reason):
        self.failed[segment_id] = reason

    def remove_from_completed(self, segment_id):
        self.completed.discard(segment_id)

    def save_checkpoint(self):
        pass


class _FakeAsyncTranslatorReturnsStrings:
    """fake 异步翻译器：不抛异常，engine 把部分/全部段降级为 "[Failed: ...]"
    字符串正常返回（如结构化输出缺失 id 兜底键、Gemini 重试耗尽后返回占位
    字符串等场景）——专门覆盖『异步路径连续 0 成功批熔断』这条此前完全没有
    的分支。"""

    def __init__(self, success_ids=frozenset()):
        self.success_ids = success_ids
        self.text_calls = []

    async def translate_text_batch_async(self, segments, context="", glossary=None):
        ids = [seg.segment_id for seg in segments]
        self.text_calls.append(ids)
        return [
            f"译文-{sid}" if sid in self.success_ids else f"[Failed: engine degraded {sid}]"
            for sid in ids
        ]

    async def translate_vision_batch_async(self, segments, context="", glossary=None):
        ids = [seg.segment_id for seg in segments]
        return [f"译文-{sid}" for sid in ids]


def _make_workflow(segments, batch_size=2, async_max_workers=2):
    """绕过 __init__ 搭最小 workflow：只挂 _run_async_translation 需要的属性"""
    wf = TranslationWorkflow.__new__(TranslationWorkflow)
    wf.settings = SimpleNamespace(
        processing=SimpleNamespace(
            batch_size=batch_size,
            async_max_workers=async_max_workers,
            max_context_length=0,
            enable_quality_check=True,
            checkpoint_interval=1,
        )
    )
    wf.glossary = {}
    wf.all_segments = SegmentList(segments)
    wf.checkpoint = _FakeCheckpoint()
    wf._save_structure_map = lambda segs: None
    wf._get_context_from_memory = lambda seg, max_len: ""
    return wf


def test_all_failed_strings_without_exception_trips_circuit_breaker():
    """engine 全批降级为 "[Failed: ...]" 字符串、不抛异常：连续 3 批 0 成功
    仍须整体 raise，且最后一批不应发起真实请求——与同步路径熔断盲区分支
    （test_sync_batch_isolation.py::test_all_failed_strings_without_exception_
    trips_circuit_breaker）语义对齐。

    fake translator 内部无真实 await 挂起点：单个批次一旦被事件循环调度就会
    运行到完成（release 信号量）后才轮到下一个批次的 __step，不会出现两个
    批次交叉执行的情况——因此『发起了几次真实请求』这个计数是确定的，只是
    『具体是哪个批次号』不确定（取决于 asyncio.as_completed 内部 set(fs)
    的哈希顺序）。"""
    segments = [
        ContentSegment(segment_id=i, original_text=f"原文{i}", content_type="text")
        for i in range(1, 9)
    ]
    # batch_size=2 → 4 批，engine 全部返回 [Failed:] 字符串（0 成功）
    fake = _FakeAsyncTranslatorReturnsStrings(success_ids=frozenset())
    wf = _make_workflow(segments, batch_size=2)
    wf.translator = SimpleNamespace(async_translator=fake)

    with pytest.raises(APIError, match="连续 3 批翻译 0 成功"):
        wf._run_async_translation(SegmentList(segments))

    # 连续 3 批 0 成功即熔断：4 批里只有 3 批真正发起过请求，第 4 批（哪个
    # 批次号不确定）被短路拦掉，不再对已知报废的 provider 发真实请求
    assert len(fake.text_calls) == 3

    degraded = [s for s in segments if "engine degraded" in s.translated_text]
    circuit_broken = [s for s in segments if "熔断" in s.translated_text]
    assert len(degraded) == 6, "3 批×2段=6 段应带 engine 降级失败标记"
    assert len(circuit_broken) == 2, "被短路的最后 1 批（2 段）应带熔断标记，未发起真实请求"
    for s in circuit_broken:
        assert s.translated_text.startswith("[Failed:")
        assert s.segment_id in wf.checkpoint.failed


def test_partial_success_batches_do_not_trip_circuit_breaker():
    """每批 2 段中恰 1 段成功（batch_success>=1，对每个批次内容恒成立、与
    批次发起顺序无关）：连续 0 成功的计数永远不会离开 0，不应熔断、不应
    raise，且全部 4 批都应发起过真实请求。"""
    segments = [
        ContentSegment(segment_id=i, original_text=f"原文{i}", content_type="text")
        for i in range(1, 9)
    ]
    # batch_size=2 → 4 批：[1,2][3,4][5,6][7,8]，每批恰好首段成功
    fake = _FakeAsyncTranslatorReturnsStrings(success_ids={1, 3, 5, 7})
    wf = _make_workflow(segments, batch_size=2)
    wf.translator = SimpleNamespace(async_translator=fake)

    wf._run_async_translation(SegmentList(segments))  # 不应 raise

    # 四批全部发起过真实请求（不论顺序）
    assert len(fake.text_calls) == 4
    assert sorted(fake.text_calls) == [[1, 2], [3, 4], [5, 6], [7, 8]]

    seg_by_id = {seg.segment_id: seg for seg in segments}
    for sid in (1, 3, 5, 7):
        assert seg_by_id[sid].translated_text == f"译文-{sid}"
        assert sid in wf.checkpoint.completed
    for sid in (2, 4, 6, 8):
        assert seg_by_id[sid].translated_text.startswith("[Failed:")
        assert sid in wf.checkpoint.failed


def test_zero_success_batches_below_threshold_do_not_trip_circuit_breaker():
    """0 成功批总数低于阈值 3（本例仅 2 批）：无论 as_completed 打乱后的
    实际发起顺序如何排列，连续 0 成功的计数都不可能达到 3（鸽笼原理：
    streak 长度不可能超过 0 成功批的总数）——用来确认『连续』计数不会被
    与 0 成功无关的其它批次污染进熔断阈值。"""
    segments = [
        ContentSegment(segment_id=i, original_text=f"原文{i}", content_type="text")
        for i in range(1, 11)
    ]
    # batch_size=2 → 5 批：仅 [1,2][3,4] 两批 0 成功，其余 3 批都成功
    fake = _FakeAsyncTranslatorReturnsStrings(success_ids={5, 6, 7, 8, 9, 10})
    wf = _make_workflow(segments, batch_size=2)
    wf.translator = SimpleNamespace(async_translator=fake)

    wf._run_async_translation(SegmentList(segments))  # 不应 raise

    assert len(fake.text_calls) == 5, "0 成功批数低于阈值，全部 5 批都应正常发起请求"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
