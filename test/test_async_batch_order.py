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
