"""
回归测试：Gemini context cache 降级分支的错误分类。

修复前 `_generate_content` 的 `except Exception as e:` 对 cache_name 非空时
一律判定为「缓存使用失败」——429/401/402/超时等与缓存健康无关的瞬态/致命
错误都会：
  1) pop 掉内存中的 cache_refs['system']
  2) 调 cache_persistence.remove_invalid_cache() 把磁盘元数据也删掉
  3) 立刻用同一批内容无缓存重发一次（对 429 等于双倍烧配额）

而 create_full_cache 全程只在 begin_formal_translation 里调用一次，一旦
cache_refs 被清空、元数据被删，整本书剩余批次都会退化为无缓存全价发送，
且下次运行也无法复用 Google 侧仍存活的那个 cache。

修复后：只有错误文本明确指示 cache 本身失效（如 "CachedContent not found"）
才走清缓存+无缓存重发路径；其余错误原样上抛，交给 @retry / 回退链处理，
缓存保持不变。
"""
from unittest.mock import MagicMock

import pytest
from google.genai import errors as genai_errors
from google.genai import types

from src.translator.engine import GeminiTranslator, _looks_like_cache_invalidity


def _make_min_gemini_translator(side_effect) -> GeminiTranslator:
    """绕过 __init__（不建真实 genai.Client / 不需要 API key），只挂
    _generate_content 依赖的最小属性集。"""
    g = GeminiTranslator.__new__(GeminiTranslator)
    g.cache_refs = {"system": "cachedContents/abc123"}
    g.cache_persistence = MagicMock()
    g.settings = MagicMock()
    g.settings.api.gemini_model = "gemini-test-model"
    g._base_generation_config = types.GenerateContentConfig(
        system_instruction="BASE SYSTEM INSTRUCTION",
        temperature=0.3,
    )
    g._client = MagicMock()
    g._client.models.generate_content.side_effect = side_effect
    return g


def _fake_ok_response() -> MagicMock:
    resp = MagicMock()
    resp.prompt_feedback = None
    resp.candidates = [MagicMock()]
    return resp


class TestLooksLikeCacheInvalidity:
    def test_cache_specific_text_matches(self):
        assert _looks_like_cache_invalidity(
            Exception("404 NOT_FOUND. CachedContent not found (name=cachedContents/abc).")
        )

    def test_rate_limit_text_does_not_match(self):
        assert not _looks_like_cache_invalidity(Exception("429 RESOURCE_EXHAUSTED: rate limited"))

    def test_auth_text_does_not_match(self):
        assert not _looks_like_cache_invalidity(Exception("401 UNAUTHENTICATED: invalid api key"))


def test_transient_429_does_not_invalidate_cache():
    """[high] 回归：429 限流不是缓存问题，不能清缓存/删元数据/立即无缓存重发。"""
    err = genai_errors.ClientError(429, {"error": {"message": "rate limited, try again"}})
    g = _make_min_gemini_translator(side_effect=err)

    with pytest.raises(genai_errors.ClientError):
        g._generate_content(contents=["hi"], purpose="test")

    # 内存缓存引用必须原样保留
    assert g.cache_refs.get("system") == "cachedContents/abc123"
    # 磁盘元数据不能被清理
    g.cache_persistence.remove_invalid_cache.assert_not_called()
    # 不能立刻无缓存重发第二次（同一批 429 等于双倍烧配额）
    assert g._client.models.generate_content.call_count == 1


def test_hard_error_402_does_not_invalidate_cache():
    """[high] 回归：402 欠费同样是 provider 级致命错误，与缓存无关，必须原样上抛。"""
    err = genai_errors.ClientError(402, {"error": {"message": "payment required"}})
    g = _make_min_gemini_translator(side_effect=err)

    with pytest.raises(genai_errors.ClientError):
        g._generate_content(contents=["hi"], purpose="test")

    assert g.cache_refs.get("system") == "cachedContents/abc123"
    g.cache_persistence.remove_invalid_cache.assert_not_called()
    assert g._client.models.generate_content.call_count == 1


def test_cache_specific_error_still_degrades_and_resends():
    """守护路径未被误删：错误文本明确指示 cache 失效时，仍应清缓存 + 无缓存重发一次。"""
    cache_err = Exception("404 NOT_FOUND. CachedContent not found (name=cachedContents/abc123).")
    fake_response = _fake_ok_response()
    g = _make_min_gemini_translator(side_effect=[cache_err, fake_response])

    resp = g._generate_content(contents=["hi"], purpose="test")

    assert resp is fake_response
    assert "system" not in g.cache_refs
    g.cache_persistence.remove_invalid_cache.assert_called_once_with("cachedContents/abc123")
    assert g._client.models.generate_content.call_count == 2
