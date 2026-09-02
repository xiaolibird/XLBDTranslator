# -*- coding: utf-8 -*-
"""翻译引擎门面（阶段 4a 拆分后的稳定 import 路径）。

实现已按后端拆到 engine_common / engine_gemini(+_async) / engine_openai 平级模块
（原 2329 行里 Gemini 侧与 OpenAI 侧零符号交叉，共享面只有 6 个 helper +
GLOSSARY_WINDOW_SIZE）。本文件只做显式 re-export：外部（7 个测试文件、agent.py、
fallback.py、__init__.py）的既有 import 路径与 monkeypatch 语义零破坏。

纪律（PRD 审定，拆分后必须守住）：
- 经门面调用的符号不得改为实现模块内直调——测试对门面属性的 monkeypatch 靠
  调用点的晚绑定才生效；
- 下划线符号显式列入 __all__ 是门面契约的一部分（它们被测试直接 import）；
- 新增对外符号先加到实现模块，再显式加进这里，别让调用方直连实现模块。
"""
from .engine_common import (                                              # noqa: F401
    GLOSSARY_WINDOW_SIZE,
    _glossary_extraction_prompt,
    _iter_glossary_windows,
    _normalize_translation_list,
    _reraise_if_provider_fatal,
    _stop_by_settings,
    _VISION_TRANSIENT_RETRIES,
)
from .engine_gemini import (                                              # noqa: F401
    GeminiTranslator,
    _CACHE_INVALID_PATTERNS,
    _ResponseValidationError,
    _TRANSLATION_RESPONSE_SCHEMA,
    _is_retryable_gemini_error,
    _looks_like_cache_invalidity,
)
from .engine_gemini_async import AsyncGeminiTranslator                    # noqa: F401
from .engine_openai import (                                              # noqa: F401
    AsyncOpenAICompatibleTranslator,
    OpenAICompatibleTranslator,
    _is_retryable_openai_error,
)

__all__ = [
    "GeminiTranslator", "AsyncGeminiTranslator",
    "OpenAICompatibleTranslator", "AsyncOpenAICompatibleTranslator",
    "GLOSSARY_WINDOW_SIZE",
    "_glossary_extraction_prompt", "_iter_glossary_windows",
    "_normalize_translation_list", "_reraise_if_provider_fatal",
    "_stop_by_settings", "_VISION_TRANSIENT_RETRIES",
    "_CACHE_INVALID_PATTERNS", "_ResponseValidationError",
    "_TRANSLATION_RESPONSE_SCHEMA", "_is_retryable_gemini_error",
    "_looks_like_cache_invalidity", "_is_retryable_openai_error",
]
