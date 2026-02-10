"""
日志配置工具
使用 loguru 进行现代化日志管理
"""


from loguru import logger

from ..core.schema import Settings


def setup_logging(settings: Settings) -> None:
    """设置日志配置"""
    # 清除默认处理器
    logger.remove()

    # 控制台处理器
    logger.add(
        lambda msg: print(msg, end=""),
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> <level>{level}</level> <cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level=settings.logging.log_level.upper(),
        colorize=True,
    )

    # 文件处理器
    if settings.files.log_file:
        settings.files.log_file.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            settings.files.log_file,
            format="{time:YYYY-MM-DD HH:mm:ss} {level} {name}:{line} - {message}",
            level="DEBUG",
            rotation="10 MB",
            retention="1 week",
            encoding="utf-8",
        )

    # 避免日志传播
    logger.disable("google")  # 禁用 Google 库的日志
    logger.disable("PIL")  # 禁用 PIL 库的日志


def get_logger(name: str) -> logger:
    """获取命名日志器"""
    return logger.bind(name=name)
