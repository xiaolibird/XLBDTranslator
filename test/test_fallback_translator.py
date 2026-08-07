# -*- coding: utf-8 -*-
"""FallbackTranslator 回退语义回归：

- 402/401 等硬性错误一次即切换到下一个 provider 并重放调用
- 429/quota 软性错误连续达到阈值才切换（期间原样抛出交给上层重试）
- 成功调用重置软性失败计数
- provider 构建失败（缺 key）自动跳过
- 链耗尽抛 FallbackExhaustedError（APIError 子类，classify_fatal 判 'hard'）；非致命错误不触发切换
"""
import asyncio

import pytest

from src.core.exceptions import APIError, APIAuthenticationError
from src.translator import fallback as fb
from src.translator.fallback import (
    FallbackTranslator,
    FallbackExhaustedError,
    classify_fatal,
    SOFT_SWITCH_THRESHOLD,
)
from src.core.schema import Settings, APISettings, FileSettings, ProcessingSettings, LoggingSettings


# ----------------------------------------------------------------------
# 测试替身
# ----------------------------------------------------------------------

class FakeTranslator:
    """可编程的假翻译器：按脚本依次抛错/返回。"""

    def __init__(self, name, script=None):
        self.name = name
        self.script = list(script or [])  # 每次调用弹出一个动作：Exception 或返回值
        self.calls = 0

    def translate_batch(self, segments, context="", glossary=None):
        self.calls += 1
        if self.script:
            action = self.script.pop(0)
            if isinstance(action, Exception):
                raise action
            return action
        return [f"{self.name}-ok"] * len(segments)

    def translate_titles(self, titles):
        return {t: f"{self.name}:{t}" for t in titles}

    def extract_glossary(self, segments):
        return {}

    def cleanup(self):
        pass


def _make_wrapper(monkeypatch, factories, settings=None):
    """factories: {provider_name: callable() -> FakeTranslator（可抛错）}"""
    created = {}

    def _fake_create(provider, settings):
        t = factories[provider]()
        created[provider] = t
        return t

    monkeypatch.setattr(fb, "_create_translator", _fake_create)
    # FallbackTranslator 只把 settings 透传给底层构造器（已被 mock），最小实例即可
    settings = settings if settings is not None else Settings.model_construct()
    wrapper = FallbackTranslator(settings, list(factories.keys()))
    return wrapper, created


def _real_settings(**api_overrides):
    """带真实 api/processing 配置的 Settings（async_translator 本地判定需要读
    settings.api.ollama_base_url / openai_base_url，model_construct() 拿不到）。
    APISettings 字段带 validation_alias（如 OPENAI_BASE_URL），直接构造后按
    python 字段名覆盖属性，绕开 alias-only 校验限制。
    注意：openai_base_url 的默认值同样是 'http://localhost:11434'（非仅
    ollama_base_url），所以任何要单独验证 ollama 分支的用例都必须显式传
    openai_base_url 指向远程地址，否则 deepseek（canonical→openai-compatible）
    会先于 ollama 命中 localhost 而让断言空转。"""
    api = APISettings()
    for key, value in api_overrides.items():
        setattr(api, key, value)
    return Settings(
        api=api,
        files=FileSettings(),
        processing=ProcessingSettings(),
        logging=LoggingSettings(),
    )


class _Seg:
    content_type = "text"
    segment_id = 1
    original_text = "hi"


# ----------------------------------------------------------------------
# classify_fatal
# ----------------------------------------------------------------------

def test_classify_fatal():
    assert classify_fatal(APIAuthenticationError("bad key")) == 'hard'
    assert classify_fatal(APIError("OpenAI-compatible HTTPError: 402 Payment Required Insufficient Balance")) == 'hard'
    assert classify_fatal(APIError("HTTPError: 429 too many requests")) == 'soft'
    assert classify_fatal(APIError("RESOURCE_EXHAUSTED: quota exceeded")) == 'soft'
    assert classify_fatal(APIError("connection reset by peer")) is None
    assert classify_fatal(TimeoutError("timed out")) is None


def test_classify_fatal_model_offline_404():
    # deepseek-chat 下线的真实场景：HTTP 404 + body "Model Not Exist"
    assert classify_fatal(APIError('OpenAI-compatible HTTPError: 404 Not Found {"error":{"message":"Model Not Exist"}}')) == 'hard'
    assert classify_fatal(APIError("model 'gemini-3.5-pro-preview' is not found")) == 'hard'


def test_classify_fatal_no_false_positives():
    # 数字串出现在 request id 等位置时不应整词命中
    assert classify_fatal(APIError("HTTPError: 500 internal error req-a402fb99")) is None
    # context（含模型输出回显）不参与分类：书籍正文里的 'billing'/'401' 不误杀 provider
    err = APIError("Model blocked content for translation",
                   context={"response": "...the billing department said 401 ways..."})
    assert classify_fatal(err) is None
    # 非 API 层错误（如 JSON 解析错）即使文本包含敏感词也不分类
    from src.core.exceptions import JSONParseError
    assert classify_fatal(JSONParseError("bad json: 402 billing quota")) is None


# ----------------------------------------------------------------------
# 切换语义
# ----------------------------------------------------------------------

def test_chain_dedupes_same_backend(monkeypatch):
    """别名（claude/claude-agent）与同配置的 openai 系 provider 视为同一后端，去重"""
    monkeypatch.setattr(fb, "_create_translator", lambda p, s: FakeTranslator(p))
    wrapper = FallbackTranslator(
        Settings.model_construct(),
        ["claude", "claude-agent", "deepseek", "openai-compatible"],
    )
    assert wrapper.providers == ["claude", "deepseek"]


def test_hard_error_switches_and_replays(monkeypatch):
    err402 = APIError("HTTPError: 402 Insufficient Balance")
    wrapper, created = _make_wrapper(monkeypatch, {
        "deepseek": lambda: FakeTranslator("deepseek", [err402]),
        "gemini": lambda: FakeTranslator("gemini"),
    })
    result = wrapper.translate_batch([_Seg()], context="")
    assert result == ["gemini-ok"]
    assert created["deepseek"].calls == 1  # 402 后没有再打 deepseek
    # 后续调用直接走 gemini
    assert wrapper.translate_batch([_Seg()]) == ["gemini-ok"]
    assert created["deepseek"].calls == 1


def test_soft_error_needs_threshold(monkeypatch):
    err429 = APIError("429 rate limit")
    wrapper, created = _make_wrapper(monkeypatch, {
        "deepseek": lambda: FakeTranslator("deepseek", [err429] * SOFT_SWITCH_THRESHOLD),
        "gemini": lambda: FakeTranslator("gemini"),
    })
    # 前 threshold-1 次：原样抛出，不切换
    for _ in range(SOFT_SWITCH_THRESHOLD - 1):
        with pytest.raises(APIError):
            wrapper.translate_batch([_Seg()])
    # 第 threshold 次：切换并重放到 gemini
    assert wrapper.translate_batch([_Seg()]) == ["gemini-ok"]


def test_success_resets_soft_counter(monkeypatch):
    err429 = APIError("429 rate limit")
    script = [err429] * (SOFT_SWITCH_THRESHOLD - 1) + [["ok"]] + [err429] * (SOFT_SWITCH_THRESHOLD - 1)
    wrapper, created = _make_wrapper(monkeypatch, {
        "deepseek": lambda: FakeTranslator("deepseek", script),
        "gemini": lambda: FakeTranslator("gemini"),
    })
    for _ in range(SOFT_SWITCH_THRESHOLD - 1):
        with pytest.raises(APIError):
            wrapper.translate_batch([_Seg()])
    assert wrapper.translate_batch([_Seg()]) == ["ok"]  # 成功，计数清零
    # 再来 threshold-1 次软错误仍不切换
    for _ in range(SOFT_SWITCH_THRESHOLD - 1):
        with pytest.raises(APIError):
            wrapper.translate_batch([_Seg()])
    assert created["deepseek"].calls == 2 * SOFT_SWITCH_THRESHOLD - 1


def test_constructor_failure_skips_provider(monkeypatch):
    def _boom():
        raise APIAuthenticationError("missing key")

    wrapper, created = _make_wrapper(monkeypatch, {
        "deepseek": _boom,
        "gemini": lambda: FakeTranslator("gemini"),
    })
    assert wrapper.translate_batch([_Seg()]) == ["gemini-ok"]
    assert "deepseek" not in created


def test_chain_exhausted_raises(monkeypatch):
    err402 = APIError("402 Insufficient Balance")
    err401 = APIAuthenticationError("invalid key")
    wrapper, _ = _make_wrapper(monkeypatch, {
        "deepseek": lambda: FakeTranslator("deepseek", [err402]),
        "gemini": lambda: FakeTranslator("gemini", [err401]),
    })
    with pytest.raises(APIError, match="均不可用"):
        wrapper.translate_batch([_Seg()])


def test_chain_exhausted_raises_dedicated_exception_classified_hard(monkeypatch):
    """链耗尽必须抛专用的 FallbackExhaustedError（APIError 子类，不破坏现有
    except APIError 调用方），且 classify_fatal 对它直接短路判 'hard'——
    这样同步路径的 hard 短路立即生效，不必等连续失败熔断凑够阈值；异步路径
    _process_single_batch 只判 classify_fatal=='hard' 的 fatal_holder 逻辑
    也自动覆盖到这个场景。"""
    err402 = APIError("402 Insufficient Balance")
    err401 = APIAuthenticationError("invalid key")
    wrapper, _ = _make_wrapper(monkeypatch, {
        "deepseek": lambda: FakeTranslator("deepseek", [err402]),
        "gemini": lambda: FakeTranslator("gemini", [err401]),
    })
    with pytest.raises(FallbackExhaustedError) as excinfo:
        wrapper.translate_batch([_Seg()])
    assert classify_fatal(excinfo.value) == 'hard'


def test_non_fatal_error_does_not_switch(monkeypatch):
    err = APIError("connection reset")
    wrapper, created = _make_wrapper(monkeypatch, {
        "deepseek": lambda: FakeTranslator("deepseek", [err]),
        "gemini": lambda: FakeTranslator("gemini"),
    })
    with pytest.raises(APIError, match="connection reset"):
        wrapper.translate_batch([_Seg()])
    # 未切换：下一次调用仍走 deepseek
    assert wrapper.translate_batch([_Seg()]) == ["deepseek-ok"]
    assert "gemini" not in created


# ----------------------------------------------------------------------
# async_translator 本地保护：链中含 ollama 必须强制同步（返回 None）
# ----------------------------------------------------------------------

def test_async_translator_none_when_chain_has_ollama(monkeypatch):
    """回退链 deepseek→ollama→gemini：ollama_base_url 走独立配置组，
    不能只靠 openai_base_url 探测，否则线程池会以 async_max_workers 并发压垮本地 Ollama。

    openai_base_url 显式指向远程地址，确保 True 只能由 ollama_base_url 分支
    产生——若误用默认 _real_settings()，deepseek 会先于 ollama 命中
    localhost（openai_base_url 默认值同样是 localhost），令本用例空转。"""
    wrapper, _ = _make_wrapper(
        monkeypatch,
        {
            "deepseek": lambda: FakeTranslator("deepseek"),
            "ollama": lambda: FakeTranslator("ollama"),
            "gemini": lambda: FakeTranslator("gemini"),
        },
        settings=_real_settings(openai_base_url="https://api.deepseek.com/v1"),
    )
    assert wrapper.async_translator is None


def test_async_translator_available_when_chain_lacks_ollama(monkeypatch):
    """与上一条同 settings、只是链去掉 ollama（deepseek→gemini），构成真正的差分：
    async_translator 不应再被强制置 None。"""
    wrapper, _ = _make_wrapper(
        monkeypatch,
        {
            "deepseek": lambda: FakeTranslator("deepseek"),
            "gemini": lambda: FakeTranslator("gemini"),
        },
        settings=_real_settings(openai_base_url="https://api.deepseek.com/v1"),
    )
    assert wrapper.async_translator is not None


def test_async_translator_none_when_openai_compatible_localhost(monkeypatch):
    wrapper, _ = _make_wrapper(
        monkeypatch,
        {"deepseek": lambda: FakeTranslator("deepseek")},
        settings=_real_settings(openai_base_url="http://localhost:11434"),
    )
    assert wrapper.async_translator is None


def test_async_translator_available_when_chain_all_remote(monkeypatch):
    """链中无本地 provider（deepseek 指向远程地址、gemini 无本地概念）时应正常给出异步实例"""
    wrapper, _ = _make_wrapper(
        monkeypatch,
        {
            "deepseek": lambda: FakeTranslator("deepseek"),
            "gemini": lambda: FakeTranslator("gemini"),
        },
        settings=_real_settings(openai_base_url="https://api.deepseek.com/v1"),
    )
    assert wrapper.async_translator is not None


# ----------------------------------------------------------------------
# AsyncFallbackTranslator.cleanup() 后禁止复用（否则会静默换用默认线程池）
# ----------------------------------------------------------------------

def test_async_batch_after_cleanup_raises(monkeypatch):
    wrapper, _ = _make_wrapper(
        monkeypatch,
        {
            "deepseek": lambda: FakeTranslator("deepseek"),
            "gemini": lambda: FakeTranslator("gemini"),
        },
        settings=_real_settings(openai_base_url="https://api.deepseek.com/v1"),
    )
    async_t = wrapper.async_translator
    assert async_t is not None
    async_t.cleanup()

    with pytest.raises(RuntimeError):
        asyncio.run(async_t.translate_text_batch_async([_Seg()], context=""))
