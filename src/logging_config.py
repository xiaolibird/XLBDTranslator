import logging
import sys
from pathlib import Path
from typing import Optional

from .config import settings

class ColoredFormatter(logging.Formatter):
    """
    一个自定义的日志格式化器，用于为不同的日志级别添加颜色。
    """
    LOG_COLORS = {
        "DEBUG": "\x1b[36m",    # Cyan
        "INFO": "\x1b[32m",     # Green
        "WARNING": "\x1b[33m",  # Yellow
        "ERROR": "\x1b[31m",    # Red
        "CRITICAL": "\x1b[31;1m", # Bold Red
        "RESET": "\x1b[0m"
    }

    def format(self, record):
        """
        重写 format 方法以应用颜色。
        """
        log_color = self.LOG_COLORS.get(record.levelname, self.LOG_COLORS["RESET"])
        
        # 创建一个新的 format 字符串，将 levelname 包裹在颜色代码中
        log_fmt = (
            f"{log_color}[%(levelname)s]{self.LOG_COLORS['RESET']} "
            "[%(asctime)s] [%(name)s:%(lineno)d]: "
            "%(message)s"
        )
        
        formatter = logging.Formatter(log_fmt, datefmt="%Y-%m-%d %H:%M:%S")
        return formatter.format(record)

def setup_logging(
    log_level: Optional[str] = None,
    log_file: Optional[Path] = None,
    module_name: Optional[str] = None
) -> logging.Logger:
    """
    设置日志配置
    
    Args:
        log_level: 日志级别
        log_file: 日志文件路径
        module_name: 模块名称
    
    Returns:
        配置好的日志记录器
    """
    # 使用配置或参数
    level = log_level or settings.log_level
    log_file_path = log_file or settings.log_file
    
    # 创建日志记录器
    logger_name = module_name or __name__.split('.')[0]
    logger = logging.getLogger(logger_name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    
    # 清除已有的处理器
    logger.handlers.clear()
    
    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
    
    # 控制台格式化器
    console_format = '%(asctime)s - %(levelname)s - %(name)s - %(message)s'
    console_formatter = ColoredFormatter(console_format)
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)
    
    # 文件处理器（如果指定了日志文件）
    if log_file_path:
        # 确保日志目录存在
        log_file_path.parent.mkdir(parents=True, exist_ok=True)
        
        file_handler = logging.FileHandler(log_file_path, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)  # 文件记录所有级别
        
        # 文件格式化器（更详细）
        file_format = '%(asctime)s - %(levelname)s - %(name)s - %(filename)s:%(lineno)d - %(message)s'
        file_formatter = logging.Formatter(file_format)
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)
    
    # 避免日志传播到根日志记录器
    logger.propagate = False
    
    return logger

def get_logger(module_name: Optional[str] = None) -> logging.Logger:
    """
    获取日志记录器（单例模式）
    
    Args:
        module_name: 模块名称
    
    Returns:
        日志记录器
    """
    name = module_name or __name__.split('.')[0]
    logger = logging.getLogger(name)
    
    # 如果还没有配置处理器，进行配置
    if not logger.handlers:
        logger = setup_logging(module_name=name)
    
    return logger

# 创建默认日志记录器
logger = get_logger("translation_system")

# 使用示例
if __name__ == "__main__":
    logger.debug("Debug message")
    logger.info("Info message")
    logger.warning("Warning message")
    logger.error("Error message")
    logger.critical("Critical message")