"""Core components: data schemas and exception types."""

from .exceptions import (
    APIAuthenticationError,
    APIError,
    APIRateLimitError,
    APITimeoutError,
    ConfigError,
    DocumentFormatError,
    DocumentParseError,
    JSONParseError,
    MissingConfigError,
    TranslationError,
)
from .schema import (
    APISettings,
    ContentSegment,
    ContextLength,
    DocumentConfig,
    FileSettings,
    LoggingSettings,
    ProcessingSettings,
    SegmentList,
    Settings,
    TranslationMap,
    TranslationMode,
)

__all__ = [
    # schema.py
    "Settings",
    "APISettings",
    "FileSettings",
    "ProcessingSettings",
    "LoggingSettings",
    "DocumentConfig",
    "ContentSegment",
    "TranslationMode",
    "ContextLength",
    "SegmentList",
    "TranslationMap",
    # exceptions.py
    "TranslationError",
    "APIError",
    "APIRateLimitError",
    "APITimeoutError",
    "APIAuthenticationError",
    "DocumentParseError",
    "DocumentFormatError",
    "ConfigError",
    "MissingConfigError",
    "JSONParseError",
]
