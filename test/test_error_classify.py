"""Gemini 重试谓词直测（自 test_error_paths.py 迁入，原 FIX-2 组）。

新版 google-genai SDK 抛 genai.errors.APIError 体系（不再是 google.api_core），
谓词必须让瞬态错误（429/5xx/项目内 APIError 包装）进重试、
致命 4xx（401/402/403/404）与无关异常直接上抛，否则要么限流得不到重试、
要么欠费/认证错误被空转 3 轮才暴露。
"""
import pytest
from google.genai import errors as genai_errors

from src.core.exceptions import APIError
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
