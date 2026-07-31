#!/usr/bin/env python3
"""
异步翻译批次顺序匹配测试
测试修复后的异步翻译是否能正确匹配翻译结果到原始 segments
"""

import asyncio
import sys
from pathlib import Path
from typing import List

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.core.schema import ContentSegment, SegmentList


async def simulate_async_translation_batch(batch: SegmentList, batch_id: int) -> List[str]:
    """模拟异步翻译批次，添加延迟来模拟并发执行"""
    import random
    delay = random.uniform(0.1, 1.0)  # 随机延迟 0.1-1.0 秒
    await asyncio.sleep(delay)

    # 返回每个 segment 的翻译结果，包含 segment_id 用于验证
    return [f"翻译-{seg.segment_id}-{batch_id}" for seg in batch]


def test_batch_order_preservation():
    """pytest 入口：批次顺序保持（无 asyncio 插件环境下用 asyncio.run 驱动）"""
    assert asyncio.run(_run_batch_order_preservation()), "asyncio.gather 未保持批次顺序"


def test_as_completed_problem():
    """pytest 入口：as_completed 乱序但结果完整性不丢失"""
    asyncio.run(_run_as_completed_problem())


async def _run_batch_order_preservation():
    """测试批次顺序是否被正确保持"""
    print("="*80)
    print("测试 1: 批次顺序保持")
    print("="*80)

    # 创建测试数据：10个批次，每个批次3个segments
    total_segments = 30
    batch_size = 3
    segments = SegmentList([
        ContentSegment(segment_id=i, original_text=f"原文{i}", content_type="text")
        for i in range(total_segments)
    ])

    # 按照批次大小分割
    batches = [
        segments[i:i+batch_size]
        for i in range(0, len(segments), batch_size)
    ]

    print(f"创建了 {len(batches)} 个批次，每个批次 {batch_size} 个 segments")
    print(f"原始 segments ID: {[s.segment_id for s in segments]}")

    # 并发执行所有批次（模拟异步翻译）
    tasks = [
        simulate_async_translation_batch(batch, batch_idx)
        for batch_idx, batch in enumerate(batches)
    ]

    # 使用 asyncio.gather 保持顺序（修复后的方法）
    print("\n使用 asyncio.gather (顺序保持)...")
    results_ordered = await asyncio.gather(*tasks)

    # 验证结果顺序
    print("\n验证结果顺序:")
    all_translations_ordered = []
    for batch_idx, (batch, batch_results) in enumerate(zip(batches, results_ordered)):
        print(f"  批次 {batch_idx}: segments={[s.segment_id for s in batch]} -> 结果={batch_results}")
        all_translations_ordered.extend(batch_results)

    # 验证每个翻译结果是否对应正确的 segment_id
    expected_translations = [f"翻译-{i}-{i//batch_size}" for i in range(total_segments)]
    print(f"\n期望的翻译结果: {expected_translations}")
    print(f"实际的翻译结果: {all_translations_ordered}")

    assert all_translations_ordered == expected_translations, (
        "批次顺序错误！翻译结果与原始 segments 不匹配"
    )
    print("✅ 批次顺序保持正确！翻译结果与原始 segments 正确匹配")
    return True


async def _run_as_completed_problem():
    """演示 as_completed 的问题"""
    print("\n" + "="*80)
    print("测试 2: 演示 as_completed 的问题")
    print("="*80)

    # 创建测试数据
    segments = SegmentList([
        ContentSegment(segment_id=i, original_text=f"原文{i}", content_type="text")
        for i in range(9)
    ])

    batches = [segments[i:i+3] for i in range(0, 9, 3)]
    tasks = [
        simulate_async_translation_batch(batch, batch_idx)
        for batch_idx, batch in enumerate(batches)
    ]

    print("使用 asyncio.as_completed (会打乱顺序)...")

    # 使用 as_completed（有问题的旧方法）
    results_unordered = [None] * len(tasks)
    for i, task in enumerate(asyncio.as_completed(tasks)):
        result = await task
        results_unordered[i] = result  # 这里会按完成顺序填充，而不是原始顺序

    print("\n使用 as_completed 的结果:")
    all_translations_unordered = []
    for batch_idx, batch_results in enumerate(results_unordered):
        batch = batches[batch_idx]  # 这里假设顺序正确，但实际不正确
        print(f"  批次 {batch_idx}: segments={[s.segment_id for s in batch]} -> 结果={batch_results}")
        all_translations_unordered.extend(batch_results)

    print(f"\n使用 as_completed 的最终结果: {all_translations_unordered}")
    print("❌ as_completed 会导致顺序混乱，翻译结果分配给错误的 segments")

    # as_completed 不保证顺序，但结果集合必须完整（每个批次恰好完成一次）
    expected_set = {f"翻译-{i}-{i // 3}" for i in range(9)}
    assert set(all_translations_unordered) == expected_set, (
        "as_completed 丢失或重复了批次结果"
    )


# ---------------------------------------------------------------------------
# FIX-6: 异步模式整批路由视觉批次（与同步 translate_batch 的 has_image 分流同构），
# 以及质检报告如实覆盖 image 失败段
# ---------------------------------------------------------------------------

import json
from types import SimpleNamespace

from src.workflow.workflow import TranslationWorkflow


class _RecordingAsyncTranslator:
    """fake 异步翻译器：记录被调方法与入参，返回可区分来源的译文"""

    def __init__(self):
        self.vision_calls: List[List[ContentSegment]] = []
        self.text_calls: List[List[ContentSegment]] = []

    async def translate_text_batch_async(self, segments, context="", glossary=None):
        self.text_calls.append(list(segments))
        return [f"文本译-{seg.segment_id}" for seg in segments]

    async def translate_vision_batch_async(self, segments, context="", glossary=None):
        self.vision_calls.append(list(segments))
        return [f"视觉译-{seg.segment_id}" for seg in segments]


class _FakeCheckpoint:
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


def _make_workflow(segments, batch_size=10):
    """绕过 __init__ 搭最小 workflow：只挂 _run_async_translation/_quality_check 需要的属性"""
    wf = TranslationWorkflow.__new__(TranslationWorkflow)
    wf.settings = SimpleNamespace(
        processing=SimpleNamespace(
            batch_size=batch_size,
            async_max_workers=2,
            max_context_length=0,
            enable_quality_check=True,
        )
    )
    wf.glossary = {}
    wf.all_segments = SegmentList(segments)
    wf.checkpoint = _FakeCheckpoint()
    wf._save_structure_map = lambda segs: None
    wf._get_context_from_memory = lambda seg, max_len: ""
    return wf


def test_async_mixed_batch_routes_whole_batch_to_vision():
    """混合批（2 text + 1 image）→ 整批走 translate_vision_batch_async，
    恰一次、入参为整批 3 段且顺序与输入一致，text 通路零调用，结果按序写回"""
    segments = [
        ContentSegment(segment_id=0, original_text="hello", content_type="text"),
        ContentSegment(
            segment_id=1, original_text="", content_type="image",
            image_path="/nonexistent/page1.png",
        ),
        ContentSegment(segment_id=2, original_text="world", content_type="text"),
    ]
    wf = _make_workflow(segments)
    fake = _RecordingAsyncTranslator()
    wf.translator = SimpleNamespace(async_translator=fake)

    wf._run_async_translation(SegmentList(segments))

    assert len(fake.vision_calls) == 1, "混合批应恰好调用一次 translate_vision_batch_async"
    assert len(fake.text_calls) == 0, "混合批不应调用 translate_text_batch_async"
    assert [s.segment_id for s in fake.vision_calls[0]] == [0, 1, 2], (
        "整批入参应为全部 3 段且顺序与输入一致（不拆分不重排）"
    )
    assert [s.translated_text for s in segments] == ["视觉译-0", "视觉译-1", "视觉译-2"], (
        "翻译结果应按输入顺序写回各 segment"
    )


def test_async_pure_text_batch_routes_to_text():
    """纯文本批 → 只调 translate_text_batch_async，vision 通路零调用"""
    segments = [
        ContentSegment(segment_id=0, original_text="foo", content_type="text"),
        ContentSegment(segment_id=1, original_text="bar", content_type="text"),
    ]
    wf = _make_workflow(segments)
    fake = _RecordingAsyncTranslator()
    wf.translator = SimpleNamespace(async_translator=fake)

    wf._run_async_translation(SegmentList(segments))

    assert len(fake.text_calls) == 1
    assert len(fake.vision_calls) == 0
    assert [s.translated_text for s in segments] == ["文本译-0", "文本译-1"]


def test_quality_report_covers_failed_image_segment(tmp_path):
    """含 [Failed:] 的 image 段 → still_failed 如实计入；重译队列不含 image 段"""
    text_failed = ContentSegment(
        segment_id=0, original_text="broken", content_type="text",
        translated_text="[Failed: timeout]",
    )
    image_failed = ContentSegment(
        segment_id=1, original_text="", content_type="image",
        image_path="/nonexistent/page2.png",
        translated_text="[Failed: vision error]",
    )
    text_ok = ContentSegment(
        segment_id=2, original_text="fine", content_type="text",
        translated_text="好的译文",
    )
    wf = _make_workflow([text_failed, image_failed, text_ok])
    wf.project_dir = tmp_path
    wf.file_path = tmp_path / "book.pdf"

    retry_calls = []

    def _fake_retry(segs):
        retry_calls.append(list(segs))
        for seg in segs:
            seg.translated_text = "重译成功"

    wf._run_sync_translation = _fake_retry

    wf._quality_check()

    # 重译队列只含 text 失败段，不含 image 段（不新增 image 重译行为）
    assert len(retry_calls) == 1
    assert [s.segment_id for s in retry_calls[0]] == [0]

    report = json.loads((tmp_path / "quality_report.json").read_text(encoding="utf-8"))
    # 全类型如实统计：image 失败段进 failed_before_retry 与 still_failed
    assert 1 in report["failed_before_retry"]
    still_failed_ids = [item["segment_id"] for item in report["still_failed_after_retry"]]
    assert 1 in still_failed_ids, "image 失败段必须计入 still_failed"
    assert 0 not in still_failed_ids, "text 段重译成功后不应留在 still_failed"


def test_quality_report_image_only_failure_no_retry(tmp_path):
    """只有 image 失败时：不触发重译，但报告仍如实覆盖（不打 ✅ 全干净）"""
    image_failed = ContentSegment(
        segment_id=0, original_text="", content_type="image",
        image_path="/nonexistent/page3.png",
        translated_text="[Failed: vision error]",
    )
    wf = _make_workflow([image_failed])
    wf.project_dir = tmp_path
    wf.file_path = tmp_path / "book.pdf"

    retry_calls = []
    wf._run_sync_translation = lambda segs: retry_calls.append(list(segs))

    wf._quality_check()

    assert retry_calls == [], "image 失败段不应进入重译队列"
    report = json.loads((tmp_path / "quality_report.json").read_text(encoding="utf-8"))
    assert report["failed_before_retry"] == [0]
    assert [item["segment_id"] for item in report["still_failed_after_retry"]] == [0]


async def main():
    """主测试函数"""
    print("异步翻译批次顺序匹配测试\n")

    try:
        # 测试修复后的正确方法
        success = await _run_batch_order_preservation()

        # 演示有问题的方法
        await _run_as_completed_problem()

        print("\n" + "="*80)
        if success:
            print("🎉 测试通过！修复成功")
            print("现在异步翻译可以正确匹配翻译结果到原始 segments")
        else:
            print("❌ 测试失败！修复需要进一步检查")
        print("="*80)

    except Exception as e:
        print(f"\n❌ 测试执行失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
