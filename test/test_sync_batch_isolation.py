#!/usr/bin/env python3
"""
同步翻译逐批隔离测试

_run_sync_batches 此前对 translator.translate_batch 的调用没有 per-batch try——
tenacity 重试耗尽后的一次瞬态错误（429/超时/连接）会直接上抛，_run_sync_translation
的外层 except 把全部剩余未翻段打成 [Failed:...] 落盘后 raise，一次网络抖动即终止
整晚任务。修复后应与异步路径 _process_single_batch 语义对齐：单批失败只标记该批
failed、记日志、继续后续批次；只有全部批次都失败才整体抛错。

另覆盖两处复修点：
1) per-batch except 里覆写 translated_text 前须守卫——只覆写「本来没译文/带
   [Failed 标记」的段，不能销毁 checkpoint 重建后误收进 pending 的上轮好译文。
2) classify_fatal 判不出 'hard' 的错误（回退链耗尽、Ollama 断连等）连续多批
   触发时须靠 consecutive_failures 熔断，不能任其把剩余批次全部空转成 [Failed:]。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.core.exceptions import APIAuthenticationError, APIError
from src.core.schema import ContentSegment, SegmentList
from src.workflow.workflow import TranslationWorkflow


class _FakeCheckpoint:
    """与 test_async_batch_order.py 中同款的最小 checkpoint 假件"""

    def __init__(self):
        self.completed = set()
        self.failed = {}
        self.save_calls = 0

    def mark_segment_completed(self, segment_id):
        self.completed.add(segment_id)

    def mark_segment_failed(self, segment_id, reason):
        self.failed[segment_id] = reason

    def save_checkpoint(self):
        self.save_calls += 1


class _FakeSyncTranslatorReturnsStrings:
    """fake 同步翻译器：不抛异常，engine 把部分/全部段降级为 "[Failed: ...]"
    字符串正常返回（如 vision 逐段隔离、DeepSeek 缺失翻译兜底键等场景）——
    专门覆盖『0 成功批计入连续失败熔断』这条无真实异常对象的分支。"""

    def __init__(self, success_ids=frozenset()):
        # success_ids: 命中则该段返回真译文，否则返回 "[Failed: ...]" 字符串
        self.success_ids = success_ids
        self.calls = []

    def translate_batch(self, batch, context="", glossary=None):
        ids = [seg.segment_id for seg in batch]
        self.calls.append(ids)
        return [
            f"译文-{sid}" if sid in self.success_ids else f"[Failed: engine degraded {sid}]"
            for sid in ids
        ]


class _FakeSyncTranslator:
    """fake 同步翻译器：按批次内容决定成功/抛异常，记录每次调用的 segment_id 序列"""

    def __init__(self, fail_ids, fatal_ids=frozenset()):
        # fail_ids: 一批 segment_id 的集合——只要该批包含其中任一 id 就抛瞬态异常
        # fatal_ids: 命中则抛 APIAuthenticationError（classify_fatal=='hard'）
        self.fail_ids = fail_ids
        self.fatal_ids = fatal_ids
        self.calls = []

    def translate_batch(self, batch, context="", glossary=None):
        ids = [seg.segment_id for seg in batch]
        self.calls.append(ids)
        if self.fatal_ids & set(ids):
            raise APIAuthenticationError(f"simulated hard fatal error for batch {ids}")
        if self.fail_ids & set(ids):
            raise TimeoutError(f"simulated transient error for batch {ids}")
        return [f"译文-{sid}" for sid in ids]


def _make_workflow(segments, translator, batch_size=2, checkpoint_interval=1):
    """绕过 __init__ 搭最小 workflow：只挂 _run_sync_batches 需要的属性"""
    wf = TranslationWorkflow.__new__(TranslationWorkflow)
    wf.settings = SimpleNamespace(
        processing=SimpleNamespace(
            batch_size=batch_size,
            rate_limit_delay=0,
            max_context_length=0,
            checkpoint_interval=checkpoint_interval,
        )
    )
    wf.glossary = {}
    wf.all_segments = SegmentList(segments)
    wf.checkpoint = _FakeCheckpoint()
    wf.translator = translator
    wf._save_structure_map = lambda segs: None
    wf._get_context_from_memory = lambda seg, max_len: ""
    return wf


def test_middle_batch_transient_failure_isolated():
    """3 批共 6 段，第 2 批瞬态异常：第 1、3 批正常完成落盘，
    第 2 批标 failed，函数不整体 raise"""
    segments = [
        ContentSegment(segment_id=i, original_text=f"原文{i}", content_type="text")
        for i in range(1, 7)
    ]
    # batch_size=2 → 批次为 [1,2] [3,4] [5,6]；第2批（含3、4）触发异常
    translator = _FakeSyncTranslator(fail_ids={3, 4})
    wf = _make_workflow(segments, translator, batch_size=2)

    success_count = wf._run_sync_batches(SegmentList(segments), on_progress=None)

    # 第 1、3 批各 2 段成功
    assert success_count == 4
    seg_by_id = {seg.segment_id: seg for seg in segments}

    for sid in (1, 2, 5, 6):
        assert seg_by_id[sid].translated_text == f"译文-{sid}"
        assert sid in wf.checkpoint.completed

    for sid in (3, 4):
        assert seg_by_id[sid].translated_text.startswith("[Failed:")
        assert sid in wf.checkpoint.failed
        assert sid not in wf.checkpoint.completed

    # 三批都调用过 translate_batch（未因中间批异常而中断后续批次）
    assert translator.calls == [[1, 2], [3, 4], [5, 6]]


def test_all_batches_failed_raises():
    """全部批次都失败——才整体抛错"""
    segments = [
        ContentSegment(segment_id=i, original_text=f"原文{i}", content_type="text")
        for i in range(1, 5)
    ]
    # batch_size=2 → 批次为 [1,2] [3,4]；两批都在 fail_ids 覆盖范围内
    translator = _FakeSyncTranslator(fail_ids={1, 2, 3, 4})
    wf = _make_workflow(segments, translator, batch_size=2)

    with pytest.raises(TimeoutError):
        wf._run_sync_batches(SegmentList(segments), on_progress=None)

    # 即使整体抛错，已处理的批次也应先标记失败（不丢进度）
    for seg in segments:
        assert seg.translated_text.startswith("[Failed:")
    assert set(wf.checkpoint.failed.keys()) == {1, 2, 3, 4}


def test_hard_fatal_short_circuits_even_after_success():
    """裸引擎（无 fallback 链）遇 402/401/404 等硬性致命错误：即便首批已
    成功，也不能让剩余批次全部空转成 [Failed:] 后正常返回——必须整体
    raise，与异步路径 _process_single_batch 的 fatal_holder 短路语义对齐。"""
    segments = [
        ContentSegment(segment_id=i, original_text=f"原文{i}", content_type="text")
        for i in range(1, 7)
    ]
    # batch_size=2 → 批次为 [1,2] [3,4] [5,6]；第 2 批（含3、4）触发硬性致命错误
    translator = _FakeSyncTranslator(fail_ids=set(), fatal_ids={3, 4})
    wf = _make_workflow(segments, translator, batch_size=2)

    with pytest.raises(APIAuthenticationError):
        wf._run_sync_batches(SegmentList(segments), on_progress=None)

    # 第 1 批已成功落盘（fatal 短路不能丢掉已有进度）
    seg_by_id = {seg.segment_id: seg for seg in segments}
    assert seg_by_id[1].translated_text == "译文-1"
    assert seg_by_id[2].translated_text == "译文-2"
    assert {1, 2} <= wf.checkpoint.completed

    # 第 2 批标记失败
    assert 3 in wf.checkpoint.failed and 4 in wf.checkpoint.failed

    # 第 3 批不应被调用——fatal 短路必须在遇到硬性错误后立即停止后续批次
    assert translator.calls == [[1, 2], [3, 4]]
    assert seg_by_id[5].translated_text == ""
    assert seg_by_id[6].translated_text == ""


def test_transient_failure_does_not_overwrite_existing_good_translation():
    """checkpoint.json 丢失/损坏重建后，segment 可能带着上轮已付费的好译文
    却被 get_pending_segments 重新收进 pending——此时同批任一 segment 触发
    瞬态异常，不能无条件覆写整批 translated_text，否则销毁好译文并落盘。
    只应覆写「本来就没有译文/带 [Failed 标记」的段。"""
    segments = [
        ContentSegment(segment_id=1, original_text="原文1", content_type="text"),
        ContentSegment(segment_id=2, original_text="原文2", content_type="text"),
    ]
    segments[0].translated_text = "上轮好译文-1"  # 上轮已成功，checkpoint 重建后被误收
    segments[1].translated_text = ""  # 从未成功翻译过

    # 单批（batch_size=2 → 仅 1 批 [1,2]）整批触发瞬态异常
    translator = _FakeSyncTranslator(fail_ids={1, 2})
    wf = _make_workflow(segments, translator, batch_size=2)

    with pytest.raises(TimeoutError):
        wf._run_sync_batches(SegmentList(segments), on_progress=None)

    seg_by_id = {seg.segment_id: seg for seg in segments}
    # 已有好译文的段不应被覆写、也不应被标记失败
    assert seg_by_id[1].translated_text == "上轮好译文-1"
    assert 1 not in wf.checkpoint.failed

    # 本来就没有译文的段才应被标记失败
    assert seg_by_id[2].translated_text.startswith("[Failed:")
    assert 2 in wf.checkpoint.failed


def test_consecutive_transient_failures_trip_circuit_breaker():
    """回退链耗尽（fallback.py 纯中文 APIError）、本地 Ollama 断连
    （APITimeoutError）等错误 classify_fatal 判不出 'hard'——连续多批瞬态
    失败达到熔断阈值时必须整体短路，不能让剩余批次全部 continue 空转成
    [Failed:] 后正常返回、退出码 0。"""
    segments = [
        ContentSegment(segment_id=i, original_text=f"原文{i}", content_type="text")
        for i in range(1, 11)
    ]
    # batch_size=2 → 5 批：[1,2]成功 [3,4][5,6][7,8]连续瞬态失败(达阈值3) [9,10]不应被调用
    translator = _FakeSyncTranslator(fail_ids={3, 4, 5, 6, 7, 8})
    wf = _make_workflow(segments, translator, batch_size=2)

    with pytest.raises(TimeoutError):
        wf._run_sync_batches(SegmentList(segments), on_progress=None)

    # 第 1 批成功进度必须保住
    seg_by_id = {seg.segment_id: seg for seg in segments}
    assert seg_by_id[1].translated_text == "译文-1"
    assert seg_by_id[2].translated_text == "译文-2"

    # 连续 3 批失败后即熔断，第 5 批（[9,10]）不应被调用
    assert translator.calls == [[1, 2], [3, 4], [5, 6], [7, 8]]
    assert seg_by_id[9].translated_text == ""
    assert seg_by_id[10].translated_text == ""


# ---------------------------------------------------------------------------
# 熔断盲区回归测试：engine 部分失败不抛异常，而是把单段/整批降级为
# "[Failed: ...]" 字符串正常返回——上面几条用例全部靠 translator 抛真实
# 异常触发熔断，这条盲区分支（_run_sync_batches 里 batch_success_count==0
# 时的连续失败计数/整体 raise）此前零测试覆盖。
# ---------------------------------------------------------------------------


def test_all_failed_strings_without_exception_trips_circuit_breaker():
    """engine 全批降级为 "[Failed: ...]" 字符串、不抛异常：连续 3 批 0 成功
    仍须计入熔断并整体 raise，且第 4 批不应被调用——熔断盲区分支同样要
    在达到阈值后立即停止后续批次，不能任其空转成一本全 [Failed:] 的成品。"""
    segments = [
        ContentSegment(segment_id=i, original_text=f"原文{i}", content_type="text")
        for i in range(1, 9)
    ]
    # batch_size=2 → 4 批：[1,2][3,4][5,6][7,8]，engine 全部返回 [Failed:] 字符串
    translator = _FakeSyncTranslatorReturnsStrings(success_ids=frozenset())
    wf = _make_workflow(segments, translator, batch_size=2)

    with pytest.raises(APIError):
        wf._run_sync_batches(SegmentList(segments), on_progress=None)

    # 连续 3 批 0 成功即熔断，第 4 批（[7,8]）不应被调用
    assert translator.calls == [[1, 2], [3, 4], [5, 6]]
    seg_by_id = {seg.segment_id: seg for seg in segments}
    assert seg_by_id[7].translated_text == ""
    assert seg_by_id[8].translated_text == ""


def test_partial_success_batches_do_not_trip_circuit_breaker():
    """每批 3 段中恰 1 段成功（batch_success_count>=1）连续跑 4 批：不应
    熔断、不应 raise——熔断盲区的连续失败计数只应统计『本批 0 成功』的
    情形，绝不能被部分成功的批次污染进 failed_batches/consecutive_failures。"""
    segments = [
        ContentSegment(segment_id=i, original_text=f"原文{i}", content_type="text")
        for i in range(1, 13)
    ]
    # batch_size=3 → 4 批：[1,2,3][4,5,6][7,8,9][10,11,12]，每批仅首段成功
    translator = _FakeSyncTranslatorReturnsStrings(success_ids={1, 4, 7, 10})
    wf = _make_workflow(segments, translator, batch_size=3)

    success_count = wf._run_sync_batches(SegmentList(segments), on_progress=None)

    # 不 raise，四批全部跑完，每批贡献 1 个成功
    assert success_count == 4
    assert translator.calls == [[1, 2, 3], [4, 5, 6], [7, 8, 9], [10, 11, 12]]

    seg_by_id = {seg.segment_id: seg for seg in segments}
    for sid in (1, 4, 7, 10):
        assert seg_by_id[sid].translated_text == f"译文-{sid}"
        assert sid in wf.checkpoint.completed
    for sid in (2, 3, 5, 6, 8, 9, 11, 12):
        assert seg_by_id[sid].translated_text.startswith("[Failed:")
        assert sid in wf.checkpoint.failed


def test_single_batch_zero_success_raises_apierror_with_batch_number():
    """单批（仅 1 批）0 成功、engine 未抛异常：last_batch_exc 此前可能仍是
    None，收尾 `raise last_batch_exc` 会崩成 TypeError 掩盖真实故障；必须
    raise 带批次号的 APIError，让调用方看到的是可读的失败原因而非裸 TypeError。"""
    segments = [
        ContentSegment(segment_id=i, original_text=f"原文{i}", content_type="text")
        for i in range(1, 3)
    ]
    translator = _FakeSyncTranslatorReturnsStrings(success_ids=frozenset())
    wf = _make_workflow(segments, translator, batch_size=2)

    with pytest.raises(APIError) as exc_info:
        wf._run_sync_batches(SegmentList(segments), on_progress=None)

    assert "批次 1" in str(exc_info.value)
    assert translator.calls == [[1, 2]]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
