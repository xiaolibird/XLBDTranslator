# -*- coding: utf-8 -*-
"""翻译线升级回归测试：结构化输出、统一失败标记、术语抽取分窗、Vision 注入术语表、质检回路。

不发真实 API 请求（纯函数或 monkeypatch）。
"""
from src.core.schema import (
    ContentSegment,
    FAILED_MARKERS,
    contains_failed_marker,
)


# ==================== 统一失败标记 ====================

def test_contains_failed_marker_covers_all_variants():
    """引擎产出的各类失败/截断标记都应被统一判定为未完成"""
    for text in (
        "[Failed: Missing translation]",
        "[Translation Failed - JSON Parse Error]",
        "[Fallback Failed]",
        "正常译文[...翻译被截断]",
    ):
        assert contains_failed_marker(text) is True
    assert contains_failed_marker("正常完整的译文。") is False
    assert contains_failed_marker("") is False
    assert contains_failed_marker(None) is False


def test_is_translated_and_pending_agree_on_failed_marker():
    """is_translated 与 checkpoint get_pending_segments 对失败标记判定一致（此前不一致会漏判 [Failed:]）"""
    from src.translator.support import CheckpointManager

    seg_ok = ContentSegment(segment_id=1, original_text="a", translated_text="完整译文。")
    seg_failed = ContentSegment(segment_id=2, original_text="b", translated_text="[Failed: Missing translation]")
    seg_trunc = ContentSegment(segment_id=3, original_text="c", translated_text="半句[...翻译被截断]")

    assert seg_ok.is_translated is True
    assert seg_failed.is_translated is False
    assert seg_trunc.is_translated is False

    ckpt = CheckpointManager.__new__(CheckpointManager)
    # 直接调用判定逻辑：把三段喂给 get_pending_segments，需要 completed_ids 为空
    ckpt.checkpoint_data = {"completed_segments": [], "failed_segments": [], "title_translations": {}}
    # get_completed_segment_ids 读 completed_segments
    pending = ckpt.get_pending_segments([seg_ok, seg_failed, seg_trunc])
    pending_ids = {s.segment_id for s in pending}
    # seg_ok 已完成内容但不在 completed_ids → 仍会入 pending（条件1）；
    # 关键是 failed / trunc 一定在 pending（内容失败），验证不漏判
    assert 2 in pending_ids and 3 in pending_ids


# ==================== 术语抽取分窗（去 8000 截断） ====================

def test_glossary_windows_cover_full_content():
    """分窗覆盖全文，不再只取前 8000 字符"""
    from src.translator.engine import _iter_glossary_windows, GLOSSARY_WINDOW_SIZE

    pairs = ["X" * 5000 for _ in range(5)]  # 25000 字符，远超单窗 8000
    windows = list(_iter_glossary_windows(pairs, window_size=GLOSSARY_WINDOW_SIZE))
    assert len(windows) >= 3  # 必须切成多窗
    # 全部内容都被覆盖（字符总量不丢）
    total = sum(len(w) for w in windows)
    assert total >= sum(len(p) for p in pairs)


# ==================== Vision 注入术语表 ====================

def test_normalize_translation_list_unwraps_object():
    """DeepSeek json_object 可能把数组包成对象或退化成 {id: 译文}，需统一解包，避免整批误判缺失"""
    from src.translator.engine import _normalize_translation_list

    arr = [{"id": 1, "translation": "译文1"}]
    assert _normalize_translation_list(arr) == arr
    # 包成 {"translations": [...]}
    assert _normalize_translation_list({"translations": arr}) == arr
    # 包成 {"data": [...]}
    assert _normalize_translation_list({"data": arr}) == arr
    # 退化成 {id: 译文} 映射
    got = _normalize_translation_list({"1": "译文1", "2": "译文2"})
    assert {g["id"]: g["translation"] for g in got} == {"1": "译文1", "2": "译文2"}
    # 无法识别 → 空列表（不崩）
    assert _normalize_translation_list({"foo": {"bar": 1}}) == []
    assert _normalize_translation_list(None) == []


def test_vision_prompt_injects_glossary():
    """format_vision_prompt 传入 glossary 时应在 prompt 中出现术语约束"""
    from src.translator.support import PromptManager

    pm = PromptManager.__new__(PromptManager)
    pm.mode_entity = None  # 无模式前缀
    glossary_text = "- **MNAR**: Must be translated as **非随机缺失**"
    prompt = pm.format_vision_prompt("previous ctx", glossary=glossary_text)
    assert "MANDATORY GLOSSARY" in prompt
    assert "非随机缺失" in prompt
    # 不传 glossary 时不出现该段
    prompt2 = pm.format_vision_prompt("ctx")
    assert "MANDATORY GLOSSARY" not in prompt2


# ==================== 同步翻译循环透传 glossary（R1/fallback-book-r2-4） ====================
# 此前 _run_sync_translation 两处 translate_batch 均不传 glossary，
# 非 Gemini 缓存 provider（DeepSeek/OpenAI 等，prompt 里术语表退化为 "N/A"）
# 在花钱生成术语表后整本不用；质检重译也走同函数，术语违例永远修不掉。

import pytest


@pytest.mark.parametrize("use_rich", [True, False], ids=["rich", "plain"])
def test_sync_translate_batch_receives_glossary(use_rich):
    """rich 与非 rich 两条同步路径都必须把 self.glossary 透传给 translate_batch"""
    from types import SimpleNamespace

    from src.core.schema import SegmentList
    from src.workflow.workflow import TranslationWorkflow

    segments = [
        ContentSegment(segment_id=i, original_text=f"原文{i}", content_type="text")
        for i in range(4)
    ]
    wf = TranslationWorkflow.__new__(TranslationWorkflow)
    wf.settings = SimpleNamespace(
        processing=SimpleNamespace(
            use_rich_progress=use_rich,
            batch_size=2,
            checkpoint_interval=1000,
            max_context_length=0,
        )
    )
    wf.glossary = {"MNAR": "非随机缺失"}
    wf.all_segments = SegmentList(segments)
    wf.checkpoint = SimpleNamespace(
        mark_segment_completed=lambda sid: None,
        mark_segment_failed=lambda sid, reason: None,
        save_checkpoint=lambda: None,
    )
    wf._save_structure_map = lambda segs: None
    wf._get_context_from_memory = lambda seg, max_len: ""

    received = []

    def fake_translate_batch(batch, context="", glossary=None):
        received.append(glossary)
        return [f"译{s.segment_id}" for s in batch]

    wf.translator = SimpleNamespace(translate_batch=fake_translate_batch)

    wf._run_sync_translation(SegmentList(segments))

    assert received, "translate_batch 应被调用"
    # 每一批都必须收到同一个 glossary 对象（is 校验防止传了空 dict 冒充）
    assert all(g is wf.glossary for g in received), (
        "同步路径必须透传 self.glossary，收到: {}".format(received)
    )
