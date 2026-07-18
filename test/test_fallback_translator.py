# -*- coding: utf-8 -*-
"""FallbackTranslator 回退语义回归：

- 402/401 等硬性错误一次即切换到下一个 provider 并重放调用
- 429/quota 软性错误连续达到阈值才切换（期间原样抛出交给上层重试）
- 成功调用重置软性失败计数
- provider 构建失败（缺 key）自动跳过
- 链耗尽抛 APIError；非致命错误不触发切换
"""
import pytest

from src.core.exceptions import APIError, APIAuthenticationError
from src.translator import fallback as fb
from src.translator.fallback import FallbackTranslator, classify_fatal, SOFT_SWITCH_THRESHOLD
from src.core.schema import Settings


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


def _make_wrapper(monkeypatch, factories):
    """factories: {provider_name: callable() -> FakeTranslator（可抛错）}"""
    created = {}

    def _fake_create(provider, settings):
        t = factories[provider]()
        created[provider] = t
        return t

    monkeypatch.setattr(fb, "_create_translator", _fake_create)
    # FallbackTranslator 只把 settings 透传给底层构造器（已被 mock），最小实例即可
    settings = Settings.model_construct()
    wrapper = FallbackTranslator(settings, list(factories.keys()))
    return wrapper, created


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
