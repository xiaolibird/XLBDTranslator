import io
from pathlib import Path

import pytest
import streamlit as st

from src.workflow.builder import SettingsBuilder
from web.components.model_selector import ModelSelector
from web.services.backend_adapter import BackendAdapter


def test_modelselector_no_api_key_returns_defaults():
    ModelSelector.clear_cache()
    models = ModelSelector.get_multimodal_models(api_key=None)
    assert isinstance(models, list)
    assert len(models) > 0


def test_settingsbuilder_unknown_preset_raises(tmp_path: Path):
    base = tmp_path / "doc.pdf"
    base.write_bytes(b"%PDF-1.4\n%fake")
    from src.core.schema import (APISettings, FileSettings, LoggingSettings,
                                 ProcessingSettings, Settings)
    api = APISettings.model_construct(gemini_api_key="k", gemini_model="m")
    files = FileSettings.model_construct(document_path=base, output_base_dir=tmp_path / "out", modes_config_path=Path("config/modes.json"))
    processing = ProcessingSettings.model_construct()
    logging = LoggingSettings.model_construct()
    settings = Settings.model_construct(api=api, files=files, processing=processing, logging=logging)

    builder = SettingsBuilder(settings)
    with pytest.raises(ValueError):
        builder.use_preset('this-does-not-exist')


def test_settings_document_path_validation_contract(tmp_path: Path):
    """document_path 走真实加载路径（from_env_file）的契约：
    - 未提供时允许为 None（main.py 先 from_env_file 构造、后由 builder 注入路径）
    - 指向不存在的文件 -> 校验失败（FileNotFoundError 包装为 ValidationError）
    - 不支持的扩展名 -> 校验失败
    """
    from pydantic import ValidationError
    from src.core.schema import Settings

    # Settings 的四个子配置均为必填：env 文件每一节至少要出现一个变量
    base_vars = (
        "API__GEMINI_API_KEY=fake-key\n"
        "FILES__OUTPUT_BASE_DIR=output\n"
        "PROCESSING__BATCH_SIZE=5\n"
        "LOGGING__LOG_LEVEL=INFO\n"
    )

    def env_file(extra: str = "") -> Path:
        p = tmp_path / f"case_{abs(hash(extra))}.env"
        p.write_text(base_vars + extra, encoding="utf-8")
        return p

    # 未提供 document_path 是合法初始状态
    s = Settings.from_env_file(env_file())
    assert s.files.document_path is None

    # 不存在的文件必须拒绝（before-validator 抛出的 FileNotFoundError 原样传播）
    with pytest.raises(FileNotFoundError):
        Settings.from_env_file(env_file(f"FILES__DOCUMENT_PATH={tmp_path / 'missing.pdf'}\n"))

    # 不支持的格式必须拒绝
    bad = tmp_path / "doc.docx"
    bad.write_bytes(b"fake")
    with pytest.raises(ValidationError):
        Settings.from_env_file(env_file(f"FILES__DOCUMENT_PATH={bad}\n"))


def test_backendadapter_missing_token_or_file_shows_none(monkeypatch):
    st.session_state.clear()
    # no api token
    st.session_state['uploaded_files'] = []
    adapter = BackendAdapter()
    result = adapter.build_settings_from_ui()
    assert result is None


# ---------------------------------------------------------------------------
# FIX-1: provider 致命错误（402 欠费等）必须从异步 gather 路径向上传导。
# 修复前 _process_single_batch 把所有异常吞成 [Failed:] 字符串并静默收尾，
# 全书几十个批次会逐一空转打成失败，调用方（execute/回退链）毫无感知。
# ---------------------------------------------------------------------------

from types import SimpleNamespace

from src.core.exceptions import APIError
from src.core.schema import ContentSegment, SegmentList
from src.translator.fallback import classify_fatal
from src.workflow.workflow import TranslationWorkflow


class _FatalCheckpoint:
    """最小 checkpoint stub：记录成功/失败标记，验证 fatal 后没有批次被写成成功"""

    def __init__(self):
        self.completed = set()
        self.failed = {}

    def mark_segment_completed(self, segment_id):
        self.completed.add(segment_id)

    def mark_segment_failed(self, segment_id, reason):
        self.failed[segment_id] = reason

    def save_checkpoint(self):
        pass


def _make_async_workflow(segments, batch_size=1):
    """绕过 __init__ 搭最小 workflow，只挂 _run_async_translation 依赖的属性"""
    wf = TranslationWorkflow.__new__(TranslationWorkflow)
    wf.settings = SimpleNamespace(
        processing=SimpleNamespace(
            batch_size=batch_size,
            async_max_workers=2,
            max_context_length=0,
        )
    )
    wf.glossary = {}
    wf.all_segments = SegmentList(segments)
    wf.checkpoint = _FatalCheckpoint()
    wf._save_structure_map = lambda segs: None
    wf._get_context_from_memory = lambda seg, max_len: ""
    return wf


def test_async_402_fatal_propagates_and_no_batch_marked_success(monkeypatch):
    """非 rich 路径：mock 异步翻译器抛 402（classify_fatal=='hard'）→
    _run_async_translation 必须向上 raise，且没有任何批次被写成成功"""
    # 强制走非 rich 的 _run_concurrent_batches 路径
    monkeypatch.setattr("src.workflow.workflow.RICH_AVAILABLE", False)

    fatal_exc = APIError("402 Payment Required: insufficient balance")
    # 前置校验：确保测试用的异常确实被分类为硬性致命，避免测试空转
    assert classify_fatal(fatal_exc) == "hard"

    class _InsolventAsyncTranslator:
        """欠费 provider：所有调用一律抛 402"""

        def __init__(self):
            self.calls = 0

        async def translate_text_batch_async(self, segments, context="", glossary=None):
            self.calls += 1
            raise fatal_exc

        async def translate_vision_batch_async(self, segments, context="", glossary=None):
            self.calls += 1
            raise fatal_exc

    segments = [
        ContentSegment(segment_id=i, original_text=f"原文{i}", content_type="text")
        for i in range(3)
    ]
    wf = _make_async_workflow(segments, batch_size=1)  # 3 个批次
    fake = _InsolventAsyncTranslator()
    wf.translator = SimpleNamespace(async_translator=fake)

    # 致命错误必须传导到调用方（execute/回退链），而非静默收尾
    with pytest.raises(APIError):
        wf._run_async_translation(SegmentList(segments))

    # 没有任何批次被写成成功：checkpoint 无 completed，各段均为失败标记
    assert wf.checkpoint.completed == set()
    assert set(wf.checkpoint.failed) == {0, 1, 2}
    assert all(
        (s.translated_text or "").startswith("[Failed:") for s in segments
    ), "fatal 后不允许把任何段写成成功译文"


def test_async_nonfatal_error_does_not_raise(monkeypatch):
    """守护测试：非致命异常仍走原降级语义（标失败但不上抛），防止短路过度扩大"""
    monkeypatch.setattr("src.workflow.workflow.RICH_AVAILABLE", False)

    nonfatal_exc = ValueError("transient parse hiccup")
    assert classify_fatal(nonfatal_exc) is None

    class _FlakyAsyncTranslator:
        async def translate_text_batch_async(self, segments, context="", glossary=None):
            raise nonfatal_exc

        async def translate_vision_batch_async(self, segments, context="", glossary=None):
            raise nonfatal_exc

    segments = [
        ContentSegment(segment_id=0, original_text="原文0", content_type="text"),
    ]
    wf = _make_async_workflow(segments, batch_size=1)
    wf.translator = SimpleNamespace(async_translator=_FlakyAsyncTranslator())

    # 不应 raise：非致命错误维持"标失败、继续收尾"的既有行为
    wf._run_async_translation(SegmentList(segments))

    assert wf.checkpoint.completed == set()
    assert 0 in wf.checkpoint.failed


# ---------------------------------------------------------------------------
# FIX-2: Gemini 重试谓词直测。
# 新版 google-genai SDK 抛 genai.errors.APIError 体系（不再是 google.api_core），
# 谓词必须让瞬态错误（429/5xx/项目内 APIError 包装）进重试、
# 致命 4xx（401/402/403/404）与无关异常直接上抛，否则要么限流得不到重试、
# 要么欠费/认证错误被空转 3 轮才暴露。
# ---------------------------------------------------------------------------

from google.genai import errors as genai_errors

from src.translator.engine import _is_retryable_gemini_error


@pytest.mark.parametrize(
    "exc, expected",
    [
        # 429 限流：瞬态，必须重试
        (genai_errors.ClientError(429, {"error": {"message": "rate limited"}}), True),
        # 402 欠费：致命，重试只会空转，必须立即上抛给回退链
        (genai_errors.ClientError(402, {"error": {"message": "payment required"}}), False),
        # 5xx 服务端瞬态故障：必须重试
        (genai_errors.ServerError(503, {"error": {"message": "unavailable"}}), True),
        # 项目内 APIError（空响应/安全拦截包装）：保持既有重试行为
        (APIError("empty response"), True),
        # 无关异常（解析 bug 等）：不属于网络瞬态，不重试
        (ValueError("boom"), False),
    ],
    ids=["genai-429", "genai-402", "genai-503", "core-apierror", "valueerror"],
)
def test_is_retryable_gemini_error(exc, expected):
    assert _is_retryable_gemini_error(exc) is expected


# ---------------------------------------------------------------------------
# R1(fallback-book-r2-1): 同步翻译 catch-all 只标未成功段。
# 此前中途抛异常会把 pending_segments 里已成功批次的译文整体覆写为 [Failed:]
# 并随 structure_map 落盘销毁，下次运行 get_pending_segments 按失败标记
# 重新入队，等于成功批次全部重付费。与 scholar 链 944f0ab 的修法对齐。
#
# 语义已随逐批隔离升级（test_sync_batch_isolation.py）：非致命瞬态失败现在
# 按批隔离，不再整体上抛——只有 (a) 全部批次都失败，或 (b) 命中
# classify_fatal=='hard'（如回退链耗尽 FallbackExhaustedError）/连续失败
# 熔断阈值，才会整体 raise。下面第一条用例断言的是新语义：批次级瞬态失败
# 已完成段保留、失败批打 [Failed:] 标记、函数正常返回不 raise；第二条用例
# 补 FallbackExhaustedError 触发 hard 短路时仍整体 raise 的行为。
# ---------------------------------------------------------------------------


def _make_sync_workflow(segments, batch_size=3, use_rich=False):
    """绕过 __init__ 搭最小 workflow，只挂 _run_sync_translation 依赖的属性"""
    wf = TranslationWorkflow.__new__(TranslationWorkflow)
    wf.settings = SimpleNamespace(
        processing=SimpleNamespace(
            use_rich_progress=use_rich,
            batch_size=batch_size,
            checkpoint_interval=1000,  # 避免周期性落盘干扰断言
            max_context_length=0,
            rate_limit_delay=0,  # HEAD 上就存在的 fixture 漂移：_run_sync_batches
            # 批间限速判断会读该属性，SimpleNamespace 缺失时在真正调用
            # translate_batch 之前就抛 AttributeError，掩盖了下面的契约冲突
        )
    )
    wf.glossary = {}
    wf.all_segments = SegmentList(segments)
    wf.checkpoint = _FatalCheckpoint()
    wf._save_structure_map = lambda segs: None
    wf._get_context_from_memory = lambda seg, max_len: ""
    return wf


def test_sync_catchall_preserves_completed_segments_on_midway_failure():
    """10 段、batch_size=3：前 2 批成功后第 3 批抛非致命 APIError →
    与逐批隔离新语义对齐：前 6 段译文与 completed 状态保留，失败批打
    [Failed:] 标记，函数正常返回、不整体 raise（非致命瞬态失败已按批隔离，
    不再像旧契约那样整体上抛）。"""
    segments = [
        ContentSegment(segment_id=i, original_text=f"原文{i}", content_type="text")
        for i in range(10)
    ]
    wf = _make_sync_workflow(segments, batch_size=3, use_rich=False)

    call_count = {"n": 0}

    def fake_translate_batch(batch, context="", glossary=None):
        call_count["n"] += 1
        if call_count["n"] >= 3:
            # 非致命：classify_fatal 判不出 'hard'，也未连续达熔断阈值
            # （4 批里只失败 2 批），按批隔离语义应继续跑完剩余批次
            raise APIError("boom on batch 3")
        return [f"译文{s.segment_id}" for s in batch]

    wf.translator = SimpleNamespace(translate_batch=fake_translate_batch)

    # 捕获 structure_map 落盘内容，验证磁盘视图与内存断言一致
    structure_snapshots = []
    wf._save_structure_map = lambda segs: structure_snapshots.append(
        {s.segment_id: s.translated_text for s in segs}
    )

    # 非致命、未达熔断阈值：函数正常返回，不 raise
    wf._run_sync_translation(SegmentList(segments))

    # 前 2 批（seg 0-5）：译文保留、checkpoint 仍 completed、未被标失败
    for i in range(6):
        assert segments[i].translated_text == f"译文{i}", "已成功译文不许被覆写"
        assert i in wf.checkpoint.completed
        assert i not in wf.checkpoint.failed
    # 第 3、4 批抛异常的 seg 6-9：带 [Failed:] 标记并标失败
    for i in range(6, 10):
        assert (segments[i].translated_text or "").startswith("[Failed:")
        assert i in wf.checkpoint.failed
        assert i not in wf.checkpoint.completed

    # catch-all 落盘的 structure_map 与上述内存状态一致
    assert structure_snapshots, "应落盘 structure_map"
    final = structure_snapshots[-1]
    for i in range(6):
        assert final[i] == f"译文{i}", "落盘内容不许销毁已付费译文"
    for i in range(6, 10):
        assert (final[i] or "").startswith("[Failed:")


def test_sync_catchall_raises_when_fallback_chain_exhausted():
    """回退链耗尽（FallbackExhaustedError）时 classify_fatal 直接短路判 'hard'：
    即便前面批次已成功，也必须整体 raise，不能让剩余批次全部空转成 [Failed:]
    后正常返回、退出码 0。"""
    from src.translator.fallback import FallbackExhaustedError

    segments = [
        ContentSegment(segment_id=i, original_text=f"原文{i}", content_type="text")
        for i in range(10)
    ]
    wf = _make_sync_workflow(segments, batch_size=3, use_rich=False)

    call_count = {"n": 0}

    def fake_translate_batch(batch, context="", glossary=None):
        call_count["n"] += 1
        if call_count["n"] >= 3:
            raise FallbackExhaustedError(
                "所有翻译 provider 均不可用（回退链已耗尽）",
                context={"providers": ["deepseek", "gemini"]},
            )
        return [f"译文{s.segment_id}" for s in batch]

    wf.translator = SimpleNamespace(translate_batch=fake_translate_batch)

    with pytest.raises(FallbackExhaustedError):
        wf._run_sync_translation(SegmentList(segments))

    # 前 2 批（seg 0-5）已成功落盘的译文与 completed 状态必须保住
    for i in range(6):
        assert segments[i].translated_text == f"译文{i}"
        assert i in wf.checkpoint.completed
        assert i not in wf.checkpoint.failed

    # 第 3 批（触发 hard 短路）与从未被尝试的第 4 批（seg 9）均标为失败——
    # hard 短路必须在遇到致命错误后立即停止后续批次，不能任其空转
    for i in range(6, 10):
        assert (segments[i].translated_text or "").startswith("[Failed:")
        assert i in wf.checkpoint.failed
        assert i not in wf.checkpoint.completed

    # 第 4 批 [9] 从未被调用过 translate_batch（fatal 短路提前终止循环）
    assert call_count["n"] == 3
