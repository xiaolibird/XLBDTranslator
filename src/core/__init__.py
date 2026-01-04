"""Core components: data schemas and exception types."""
from .schema import (
    Settings,
    APISettings,
    FileSettings,
    ProcessingSettings,
    LoggingSettings,
    DocumentConfig,
    ContentSegment,
    TranslationMode,
    ContextLength,
    SegmentList,
    TranslationMap,
)
from .exceptions import (
    TranslationError,
    APIError,
    APIRateLimitError,
    APITimeoutError,
    APIAuthenticationError,
    DocumentParseError,
    DocumentFormatError,
    ConfigError,
    MissingConfigError,
    JSONParseError,
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
