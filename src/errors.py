"""
错误处理模块
定义结构化错误类型和错误处理装饰器
"""
from typing import Type, Optional, Callable, Any, TypeVar, Dict, List, Union
import time
import functools
from enum import Enum
import logging

# 类型变量
T = TypeVar('T')

class ErrorSeverity(str, Enum):
    """错误严重程度"""
    LOW = "low"        # 可恢复的轻微错误
    MEDIUM = "medium"  # 需要用户干预的错误
    HIGH = "high"      # 致命错误，程序终止

class TranslationError(Exception):
    """翻译系统基础错误类"""
    def __init__(self, 
                 message: Optional[str] = None,
                 severity: ErrorSeverity = ErrorSeverity.MEDIUM,
                 original_error: Optional[Exception] = None,
                 context: Optional[Dict[str, Any]] = None,
                 suggestion: Optional[str] = None):
        
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
            context_str = ', '.join(f"{k}={v}" for k, v in self.context.items())
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
            "original_error": str(self.original_error) if self.original_error else None
        }

# API 相关错误
class APIError(TranslationError):
    """API调用错误"""
    def __init__(self, message: Optional[str] = None, **kwargs):
        super().__init__(message, **kwargs)

class APIRateLimitError(APIError):
    """API速率限制错误"""
    def __init__(self, message: Optional[str] = None, retry_after: Optional[int] = None, **kwargs):
        message = message if message is not None else "API rate limit exceeded"
        if retry_after:
            message += f", retry after {retry_after} seconds"
        suggestion = kwargs.pop("suggestion", "Wait for the rate limit to reset or reduce request frequency.")
        super().__init__(message, suggestion=suggestion, **kwargs)
        self.retry_after = retry_after

class APITimeoutError(APIError):
    """API超时错误"""
    def __init__(self, message: Optional[str] = None, **kwargs):
        message = message if message is not None else "API request timed out"
        suggestion = kwargs.pop("suggestion", "Check your network connection or increase timeout settings.")
        super().__init__(message, ErrorSeverity.MEDIUM, suggestion=suggestion, **kwargs)
class APIQuotaExceededError(APIError):
    """API配额用尽错误"""
    def __init__(self, message: Optional[str] = None, **kwargs):
        message = message if message is not None else "API quota exceeded"
        suggestion = kwargs.pop("suggestion", "Check your API usage limits or upgrade your plan.")
        super().__init__(message, ErrorSeverity.HIGH, suggestion=suggestion, **kwargs)

class APIAuthenticationError(APIError):
    """API认证错误"""
    def __init__(self, message: Optional[str] = None, **kwargs):
        message = message if message is not None else "API authentication failed"
        suggestion = kwargs.pop("suggestion", "Check your API key and ensure it's valid.")
        super().__init__(message, ErrorSeverity.HIGH, suggestion=suggestion, **kwargs)
# 文档处理错误
class DocumentError(TranslationError):
    """文档处理错误"""
    pass

class DocumentParseError(DocumentError):
    """文档解析错误"""
    def __init__(self, message: Optional[str] = None, **kwargs):
        message = message if message is not None else "Failed to parse document"
        suggestion = kwargs.pop("suggestion", "Check if the document is corrupted or in an unsupported format.")
        super().__init__(message, ErrorSeverity.HIGH, suggestion=suggestion, **kwargs)
class DocumentFormatError(DocumentError):
    """文档格式错误"""
    def __init__(self, message: Optional[str] = None, **kwargs):
        message = message if message is not None else "Unsupported document format"
        suggestion = kwargs.pop("suggestion", "Ensure the document is in PDF or EPUB format.")
        super().__init__(message, ErrorSeverity.HIGH, suggestion=suggestion, **kwargs)
class DocumentNotFoundError(DocumentError):
    """文档未找到错误"""
    def __init__(self, message: Optional[str] = None, **kwargs):
        message = message if message is not None else "Document not found"
        suggestion = kwargs.pop("suggestion", "Check the file path and ensure the document exists.")
        super().__init__(message, ErrorSeverity.HIGH, suggestion=suggestion, **kwargs)
# 配置错误
class ConfigError(TranslationError):
    """配置错误"""
    pass

class InvalidConfigError(ConfigError):
    """无效配置错误"""
    def __init__(self, message: Optional[str] = None, **kwargs):
        message = message if message is not None else "Invalid configuration"
        suggestion = kwargs.pop("suggestion", "Check your configuration file and environment variables.")
        super().__init__(message, ErrorSeverity.HIGH, suggestion=suggestion, **kwargs)

class MissingConfigError(ConfigError):
    """缺失配置错误"""
    def __init__(self, missing_key: str, message: Optional[str] = None, **kwargs):
        message = message if message is not None else f"Missing required configuration: {missing_key}"
        suggestion = kwargs.pop("suggestion", f"Set the {missing_key} environment variable or update your config file.")
        super().__init__(message, ErrorSeverity.HIGH, suggestion=suggestion, **kwargs)
# 翻译错误
class TranslationQualityError(TranslationError):
    """翻译质量错误"""
    def __init__(self, message: Optional[str] = None, **kwargs):
        message = message if message is not None else "Translation quality issue detected"
        suggestion = kwargs.pop("suggestion", "Review the translation output and adjust translation parameters.")
        super().__init__(message, ErrorSeverity.MEDIUM, suggestion=suggestion, **kwargs)
class JSONParseError(TranslationError):
    """JSON解析错误"""
    def __init__(self, message: Optional[str] = None, **kwargs):
        message = message if message is not None else "Failed to parse JSON response"
        suggestion = kwargs.pop("suggestion", "Check the API response format or enable JSON repair mode.")
        super().__init__(message, ErrorSeverity.MEDIUM, suggestion=suggestion, **kwargs)

# 文件系统错误
class FileSystemError(TranslationError):
    """文件系统错误"""
    pass

class DiskSpaceError(FileSystemError):
    """磁盘空间不足错误"""
    def __init__(self, **kwargs):
        message = "Insufficient disk space"
        suggestion = "Free up disk space or change output directory."
        super().__init__(message, ErrorSeverity.HIGH, suggestion=suggestion, **kwargs)

class PermissionError(FileSystemError):
    """权限错误"""
    def __init__(self, **kwargs):
        message = "Permission denied"
        suggestion = "Check file permissions or run with appropriate privileges."
        super().__init__(message, ErrorSeverity.HIGH, suggestion=suggestion, **kwargs)

# 错误处理装饰器
def retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exponential_base: float = 2.0,
    retry_on: tuple = (APIError,),
    no_retry_on: tuple = (DocumentNotFoundError, InvalidConfigError, DiskSpaceError),
    logger: Optional[logging.Logger] = None,
    on_retry: Optional[Callable[[int, Exception], None]] = None
):
    """
    带指数退避的重试装饰器
    
    Args:
        max_retries: 最大重试次数
        base_delay: 基础延迟时间（秒）
        max_delay: 最大延迟时间（秒）
        exponential_base: 指数基数
        retry_on: 需要重试的异常类型
        no_retry_on: 不需要重试的异常类型
        logger: 日志记录器
        on_retry: 重试时的回调函数 (接收 retry_attempt, exception)
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_exception = None
            
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                
                except no_retry_on as e:
                    if logger:
                        logger.error(f"Non-retriable error in {func.__name__}: {e}", exc_info=True)
                    raise
                    
                except retry_on as e:
                    last_exception = e
                    
                    if attempt < max_retries:
                        if on_retry:
                            on_retry(attempt + 1, last_exception)
                        
                        delay = min(base_delay * (exponential_base ** attempt), max_delay)
                        jitter = delay * 0.1 * (2 * (hash(str(attempt)) % 100) / 100 - 1)
                        effective_delay = delay + jitter
                        
                        if logger:
                            logger.warning(
                                f"Function {func.__name__} failed (attempt {attempt+1}/{max_retries+1}). "
                                f"Retrying in {effective_delay:.2f}s."
                            )
                        time.sleep(effective_delay)
                    
                except Exception as e:
                    if logger:
                        logger.error(f"Unexpected error in {func.__name__}: {e}", exc_info=True)
                    raise
            
            # If the loop completes, all retries have been exhausted
            if logger:
                logger.error(
                    f"Function {func.__name__} failed after {max_retries} retries.",
                    exc_info=last_exception
                )
            raise last_exception
        return wrapper
    return decorator

def safe_execute(
    default_return: Any = None,
    catch: tuple = (Exception,),
    logger: Optional[logging.Logger] = None,
    error_message: Optional[str] = None,
    reraise: bool = False,
    log_level: int = logging.ERROR
):
    """
    安全执行装饰器，捕获异常并返回默认值
    Args:
        default_return: 异常时返回的默认值
        catch: 需要捕获的异常类型
        logger: 日志记录器
        error_message: 自定义错误消息
        reraise: 是否重新抛出异常
        log_level: 日志级别
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            try:
                return func(*args, **kwargs)
            except catch as e:
                message = error_message or f"Error in {func.__name__}: {str(e)}"
                if logger:
                    logger.log(log_level, message, exc_info=True)
                else:
                    print(f"ERROR: {message}")

                if reraise:
                    raise
                return default_return
        return wrapper
    return decorator

def validate_input(
    validation_func: Callable[[Any], bool],
    error_message: str,
    error_type: Type[Exception] = ValueError
):
    """
    输入验证装饰器
    
    Args:
        validation_func: 验证函数，接收包含args和kwargs的元组，返回True表示验证通过
        error_message: 验证失败时的错误消息
        error_type: 抛出的异常类型
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            if not validation_func((args, kwargs)):
                raise error_type(error_message)
            return func(*args, **kwargs)
        return wrapper
    return decorator

class ErrorContext:
    """错误上下文管理器"""
    def __init__(self,
                 context: Dict[str, Any],
                 logger: Optional[logging.Logger] = None,
                 suppress: bool = False,
                 default_return: Any = None):
        self.context = context
        self.logger = logger
        self.suppress = suppress
        self.default_return = default_return
        self.original_error = None
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_val is not None:
            self.original_error = exc_val
            # 如果是我们的错误类型，添加上下文
            if isinstance(exc_val, TranslationError):
                exc_val.context.update(self.context)
            elif self.logger:
                # 记录错误上下文
                context_str = ', '.join(f"{k}={v}" for k, v in self.context.items())
                self.logger.error(f"Error context: {context_str}", exc_info=True)

            # 如果设置了抑制异常，返回默认值
            if self.suppress:
                return True  # 抑制异常

        return False  # 不抑制异常

def error_handler(
    error_types: Union[Type[Exception], tuple] = Exception,
    handler: Optional[Callable[[Exception], Any]] = None,
    default_return: Any = None,
    logger: Optional[logging.Logger] = None
):
    """
    通用错误处理器装饰器

    Args:
        error_types: 要处理的异常类型
        handler: 自定义处理函数
        default_return: 默认返回值
        logger: 日志记录器
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            try:
                return func(*args, **kwargs)
            except error_types as e:
                if handler:
                    return handler(e)
                if logger:
                    logger.error(f"Error in {func.__name__}: {e}", exc_info=True)
                return default_return
        return wrapper
    return decorator

# 错误恢复策略
def create_fallback_strategy(
    primary_func: Callable,
    fallback_funcs: List[Callable],
    logger: Optional[logging.Logger] = None
) -> Callable:
    """
    创建降级策略

    Args:
        primary_func: 主函数
        fallback_funcs: 降级函数列表
        logger: 日志记录器

    Returns:
        带降级策略的函数
    """
    def fallback_wrapper(*args, **kwargs):
        # 尝试主函数
        try:
            return primary_func(*args, **kwargs)
        except Exception as e:
            if logger:
                logger.warning(f"Primary function failed: {e}. Trying fallbacks...")

            # 尝试降级函数
            for i, fallback_func in enumerate(fallback_funcs):
                try:
                    if logger:
                        logger.info(f"Trying fallback {i+1}/{len(fallback_funcs)}")
                    return fallback_func(*args, **kwargs)
                except Exception as fallback_error:
                    if logger:
                        logger.warning(f"Fallback {i+1} failed: {fallback_error}")
                    continue

            # 所有降级都失败
            raise TranslationError(
                "All fallback strategies failed",
                severity=ErrorSeverity.HIGH,
                original_error=e
            )

    return fallback_wrapper
