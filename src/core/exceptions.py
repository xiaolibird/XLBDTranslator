"""
自定义异常类
继承自 TranslationError 的具体异常类型
"""

from enum import Enum
from typing import Any, Dict, Optional


class ErrorSeverity(str, Enum):
    """错误严重程度"""

    LOW = "low"  # 可恢复的轻微错误
    MEDIUM = "medium"  # 需要用户干预的错误
    HIGH = "high"  # 致命错误，程序终止


class TranslationError(Exception):
    """翻译系统基础错误类"""

    def __init__(
        self,
        message: Optional[str] = None,
        severity: ErrorSeverity = ErrorSeverity.MEDIUM,
        original_error: Optional[Exception] = None,
        context: Optional[Dict[str, Any]] = None,
        suggestion: Optional[str] = None,
    ):

        if message is None:
            if original_error:
                self.message = f"发生未知错误: {type(original_error).__name__} - {str(original_error)}"
            else:
                self.message = "发生未知错误，未提供具体信息。"
        else:
            self.message = message

        self.severity = severity
        self.original_error = original_error
        self.context = context or {}
        self.suggestion = suggestion
        super().__init__(self.message)

    def __str__(self):
        base = f"[{self.severity.value.upper()}] {self.message}"
        if self.original_error:
            base += f" (Caused by: {type(self.original_error).__name__}: {str(self.original_error)})"
        if self.context:
            context_str = ", ".join(f"{k}={v}" for k, v in self.context.items())
            base += f" [Context: {context_str}]"
        if self.suggestion:
            base += f"\n💡 Suggestion: {self.suggestion}"
        return base

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式，便于日志记录"""
        return {
            "type": type(self).__name__,
            "message": self.message,
            "severity": self.severity.value,
            "context": self.context,
            "suggestion": self.suggestion,
            "original_error": str(self.original_error) if self.original_error else None,
        }


# API 相关错误
class APIError(TranslationError):
    """API调用错误"""

    pass


class APIRateLimitError(APIError):
    """API速率限制错误"""

    def __init__(
        self, message: Optional[str] = None, retry_after: Optional[int] = None, **kwargs
    ):
        message = message if message is not None else "API rate limit exceeded"
        if retry_after:
            message += f", retry after {retry_after} seconds"
        suggestion = kwargs.pop(
            "suggestion",
            "Wait for the rate limit to reset or reduce request frequency.",
        )
        super().__init__(message, suggestion=suggestion, **kwargs)
        self.retry_after = retry_after


class APITimeoutError(APIError):
    """API超时错误"""

    def __init__(self, message: Optional[str] = None, **kwargs):
        message = message if message is not None else "API request timed out"
        suggestion = kwargs.pop(
            "suggestion", "Check your network connection or increase timeout settings."
        )
        super().__init__(message, ErrorSeverity.MEDIUM, suggestion=suggestion, **kwargs)


class APIQuotaExceededError(APIError):
    """API配额用尽错误"""

    def __init__(self, message: Optional[str] = None, **kwargs):
        message = message if message is not None else "API quota exceeded"
        suggestion = kwargs.pop(
            "suggestion", "Check your API usage limits or upgrade your plan."
        )
        super().__init__(message, ErrorSeverity.HIGH, suggestion=suggestion, **kwargs)


class APIAuthenticationError(APIError):
    """API认证错误"""

    def __init__(self, message: Optional[str] = None, **kwargs):
        message = message if message is not None else "API authentication failed"
        suggestion = kwargs.pop(
            "suggestion", "Check your API key and ensure it's valid."
        )
        super().__init__(message, ErrorSeverity.HIGH, suggestion=suggestion, **kwargs)


# 文档处理错误（保留常用的）
class DocumentParseError(TranslationError):
    """文档解析错误"""

    def __init__(self, message: Optional[str] = None, **kwargs):
        message = message if message is not None else "Failed to parse document"
        suggestion = kwargs.pop(
            "suggestion",
            "Check if the document is corrupted or in an unsupported format.",
        )
        super().__init__(message, ErrorSeverity.HIGH, suggestion=suggestion, **kwargs)


class DocumentFormatError(TranslationError):
    """文档格式错误"""

    def __init__(self, message: Optional[str] = None, **kwargs):
        message = message if message is not None else "Unsupported document format"
        suggestion = kwargs.pop(
            "suggestion", "Ensure the document is in PDF or EPUB format."
        )
        super().__init__(message, ErrorSeverity.HIGH, suggestion=suggestion, **kwargs)


# 配置错误（保留常用的）
class ConfigError(TranslationError):
    """配置错误"""

    def __init__(self, message: Optional[str] = None, **kwargs):
        message = message if message is not None else "Configuration error"
        suggestion = kwargs.pop(
            "suggestion", "Check your configuration file and environment variables."
        )
        super().__init__(message, ErrorSeverity.HIGH, suggestion=suggestion, **kwargs)


class MissingConfigError(ConfigError):
    """缺失配置错误"""

    def __init__(self, missing_key: str, message: Optional[str] = None, **kwargs):
        message = (
            message
            if message is not None
            else f"Missing required configuration: {missing_key}"
        )
        suggestion = kwargs.pop(
            "suggestion",
            f"Set the {missing_key} environment variable or update your config file.",
        )
        super().__init__(message, ErrorSeverity.HIGH, suggestion=suggestion, **kwargs)


class JSONParseError(TranslationError):
    """JSON解析错误"""

    def __init__(self, message: Optional[str] = None, **kwargs):
        message = message if message is not None else "Failed to parse JSON response"
        suggestion = kwargs.pop(
            "suggestion", "Check the API response format or enable JSON repair mode."
        )
        super().__init__(message, ErrorSeverity.MEDIUM, suggestion=suggestion, **kwargs)
