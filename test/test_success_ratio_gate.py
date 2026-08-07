# -*- coding: utf-8 -*-
"""_run_translation_loop 整体成功率下限回归测试。

背景：provider 退化成"每批恰 1 段成功"时，_run_sync_batches/
_process_single_batch 各自的连续失败熔断（CONSECUTIVE_FAILURE_THRESHOLD）
永不触发——因为每批都有 >=1 成功，consecutive_failures 每次都被清零。这会
产出一本大半 [Failed:] 的成品，且 execute() 退出码 0，用户很可能完全不
知道成品质量已经烂掉。

修复：_run_translation_loop 在同步/异步两条路径收尾处统一检查整体成功率
（本轮实际成功段数 / 本轮 pending 总数），低于 settings.processing.
min_success_ratio（默认 0.5）则 raise APIError（消息含实际成功率），阻断
产出降级成品。

关键：分母必须是"本轮 pending 总数"而非全书段数——否则纯增量续跑（全书
仅剩 3 段失败 1 段）会被全书基数误判成"大面积失败"。本文件通过 mock
_run_sync_translation 的返回值直接驱动比率判定，不依赖真实批次/翻译器
细节（那部分已由 test_sync_batch_isolation.py / test_async_zero_success_
circuit_breaker.py 覆盖）。

二次修复（最小样本量下限 MIN_GATE_SAMPLE=20）：增量续跑场景下 pending 可能
只有个位数（如全书仅剩 5 段因某种原因失败重入队），此时 1 段偶发失败就把
比率拉到 20%，若照样按比率 raise，会让整本 99% 完成的书渲染/清理全部跳过，
且 raise 前 checkpoint 已经落盘，重跑会直接自锁在同一 pending 集合上。故本
轮尝试段数 < MIN_GATE_SAMPLE 时豁免比率检查；本文件下方用例分母凡是要验证
比率判定本身的，均改为 >= MIN_GATE_SAMPLE 的样本量以保持原语义，另补两条
分别覆盖"小样本豁免"与"大样本仍拦截"。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.core.exceptions import APIError
from src.core.schema import ContentSegment, SegmentList
from src.workflow.workflow import MIN_GATE_SAMPLE, TranslationWorkflow


def _make_workflow(pending_count, success_count, min_success_ratio=None):
    """绕过 __init__ 搭最小 workflow：跳过术语表预翻译清理块
    （reprocess_pretranslated=False）与异步分流（enable_async=False），
    只保留 _run_translation_loop 自身的编排逻辑；_run_sync_translation
    被 mock 为直接返回给定的 success_count，隔离真实批次机制。"""
    segments = [
        ContentSegment(segment_id=i, original_text=f"原文{i}", content_type="text")
        for i in range(pending_count)
    ]

    wf = TranslationWorkflow.__new__(TranslationWorkflow)
    processing_kwargs = dict(
        reprocess_pretranslated=False,
        enable_async=False,
        async_threshold=10,
    )
    if min_success_ratio is not None:
        processing_kwargs["min_success_ratio"] = min_success_ratio
    wf.settings = SimpleNamespace(processing=SimpleNamespace(**processing_kwargs))
    wf.all_segments = SegmentList(segments)
    wf.glossary = {}
    wf.translator = SimpleNamespace(begin_formal_translation=lambda glossary: None)
    wf.checkpoint = SimpleNamespace(
        get_pending_segments=lambda segs: SegmentList(segments),
        save_checkpoint=lambda: None,
    )
    wf._save_structure_map = lambda segs: None

    sync_calls = []

    def _fake_run_sync_translation(pending):
        sync_calls.append(list(pending))
        return success_count

    wf._run_sync_translation = _fake_run_sync_translation
    return wf, sync_calls


def test_forty_percent_success_raises():
    """20 段 pending（>= MIN_GATE_SAMPLE），仅 8 段成功（40%）—— 低于默认
    阈值 0.5，必须 raise，且消息中含实际成功率数字。"""
    wf, calls = _make_workflow(pending_count=20, success_count=8)

    with pytest.raises(APIError, match="8/20"):
        wf._run_translation_loop()

    assert len(calls) == 1 and len(calls[0]) == 20


def test_sixty_percent_success_does_not_raise():
    """20 段 pending，12 段成功（60%）—— 高于默认阈值 0.5，正常返回不 raise。"""
    wf, calls = _make_workflow(pending_count=20, success_count=12)

    wf._run_translation_loop()  # 不应 raise

    assert len(calls) == 1


def test_all_success_does_not_raise():
    """全部成功（100%，样本量 >= MIN_GATE_SAMPLE）—— 显然不应 raise，防止
    阈值判定方向写反。"""
    wf, calls = _make_workflow(pending_count=20, success_count=20)

    wf._run_translation_loop()  # 不应 raise


def test_exact_threshold_boundary_does_not_raise():
    """成功率恰好等于阈值（50% == 默认 0.5，样本量 >= MIN_GATE_SAMPLE）——
    边界不应 raise（严格小于才触发），避免把"刚好卡线"的正常波动错杀。"""
    wf, calls = _make_workflow(pending_count=20, success_count=10)

    wf._run_translation_loop()  # 不应 raise


def test_custom_min_success_ratio_from_settings():
    """settings.processing.min_success_ratio 可自定义：70% 成功但阈值设为
    0.8 时应 raise；同样的 70% 在默认阈值 0.5 下不应 raise（对照组）。
    样本量 20（>= MIN_GATE_SAMPLE）确保比率检查本身生效。"""
    wf_strict, _ = _make_workflow(pending_count=20, success_count=14, min_success_ratio=0.8)
    with pytest.raises(APIError, match="14/20"):
        wf_strict._run_translation_loop()

    wf_default, _ = _make_workflow(pending_count=20, success_count=14)
    wf_default._run_translation_loop()  # 默认阈值 0.5 下不应 raise


def test_missing_min_success_ratio_setting_falls_back_to_default():
    """settings.processing 缺 min_success_ratio 字段（如旧测试用的 SimpleNamespace
    fixture）时按 getattr 默认值 0.5 兜底，不应因 AttributeError 崩溃。
    样本量 20（>= MIN_GATE_SAMPLE）确保比率检查本身生效。"""
    wf, _ = _make_workflow(pending_count=20, success_count=6)  # 30% < 0.5 默认阈值

    with pytest.raises(APIError):
        wf._run_translation_loop()


def test_small_sample_below_gate_does_not_raise():
    """pending=5（< MIN_GATE_SAMPLE=20），仅 1 段成功（20%，远低于默认阈值
    0.5）—— 小样本豁免：增量续跑场景下几段的偶发失败没有统计意义，不应
    raise 阻断整本书渲染。"""
    assert 5 < MIN_GATE_SAMPLE
    wf, calls = _make_workflow(pending_count=5, success_count=1)

    wf._run_translation_loop()  # 不应 raise（样本量不足，豁免比率检查）

    assert len(calls) == 1 and len(calls[0]) == 5


def test_large_sample_at_gate_still_raises():
    """pending=20（恰好等于 MIN_GATE_SAMPLE，仍在检查范围内），9 段成功
    （45% < 默认阈值 0.5）—— 大样本下真正的持续性退化仍必须被拦截，不能
    被"小样本豁免"误伤放过。"""
    wf, calls = _make_workflow(pending_count=20, success_count=9)

    with pytest.raises(APIError, match="9/20"):
        wf._run_translation_loop()

    assert len(calls) == 1 and len(calls[0]) == 20


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
