"""
配置管理模块
使用 Pydantic V2 和 pydantic-settings 进行配置验证和加载
"""
from pathlib import Path
from typing import Optional, Dict
from enum import Enum
from pydantic import Field, field_validator, BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

import json
import logging

# 将 BASE_DIR 定义在顶层，以便在整个模块中使用
BASE_DIR = Path(__file__).resolve().parent.parent

class ContextLength(str, Enum):
    """上下文长度枚举"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

class TranslationMode(BaseModel):
    """翻译模式配置 (数据模型)"""
    name: str = Field(..., description="模式名称")
    role_desc: str = Field(..., description="角色描述")
    style: str = Field(..., description="风格指南")
    context_len: ContextLength = Field(ContextLength.MEDIUM, description="上下文长度")
    
    model_config = {'use_enum_values': True}

class DocumentConfig(BaseModel):
    """文档处理配置 (数据模型)"""
    use_vision_mode: Optional[bool] = Field(None, description="是否使用视觉模式")
    margin_top: Optional[float] = Field(None, description="顶部边距比例 (0.0-1.0)")
    margin_bottom: Optional[float] = Field(None, description="底部边距比例 (0.0-1.0)")
    custom_toc_path: Optional[Path] = Field(None, description="自定义目录文件路径")
    page_range: Optional[tuple] = Field(None, description="页面范围 (start, end)")
    
    @field_validator('margin_top', 'margin_bottom')
    def validate_margin(cls, v):
        if v is not None and not (0 <= v <= 1):
            raise ValueError('Margin must be between 0.0 and 1.0')
        return v    

    @field_validator('page_range')
    def validate_page_range(cls, v):
        if v is not None:
            if len(v) != 2:
                raise ValueError('Page range must be a tuple of (start, end)')
            start, end = v
            if start < 0 or end < start:
                raise ValueError('Invalid page range')
        return v

class Settings(BaseSettings):
    """全局设置 (从 .env 加载)"""
    
    model_config = SettingsConfigDict(
        env_file="./config/.env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        validate_assignment=True,
        extra='ignore' # 允许 .env 里有多余的字段而不报错
    )
    
    # API 配置
    gemini_api_key: str = Field(..., validation_alias="GEMINI_API_KEY", description="Gemini API密钥")
    gemini_model: str = Field("gemini-1.5-flash-preview", validation_alias="GEMINI_MODEL", description="Gemini模型名称")
    
    # 文件路径
    document_path: Path = Field(..., validation_alias="DOCUMENT_PATH")
    output_base_dir: Path = Field(Path("outputs"), validation_alias="OUTPUT_DIR")
    custom_toc_path: Optional[Path] = Field(default=None, validation_alias="CUSTOM_TOC_PATH")

    # -----------------------------------------------------------------------
    # 1. PDF 处理设置 (修正：默认值设为 0.0，防止 NoneType 计算错误)
    # -----------------------------------------------------------------------
    margin_top: float = Field(0.0, validation_alias="MARGIN_TOP")
    margin_bottom: float = Field(0.0, validation_alias="MARGIN_BOTTOM")
    margin_left: float = Field(0.0, validation_alias="MARGIN_LEFT")
    margin_right: float = Field(0.0, validation_alias="MARGIN_RIGHT")

    # 翻译配置
    translation_mode: str = Field("1", validation_alias="TRANSLATION_MODE", description="翻译模式ID")
    batch_size: int = Field(5, validation_alias="BATCH_SIZE", description="批量翻译大小")
    max_retries: int = Field(3, validation_alias="MAX_RETRIES", description="最大重试次数")
    
    # 性能配置
    request_timeout: int = Field(30, validation_alias="REQUEST_TIMEOUT", description="API请求超时时间")
    rate_limit_delay: float = Field(1.0, validation_alias="RATE_LIMIT_DELAY", description="普通文本速率限制延迟")
    
    # -----------------------------------------------------------------------
    # 2. 新增：Vision 专用延时 (Translator 需要用到)
    # -----------------------------------------------------------------------
    vision_rate_limit_delay: float = Field(2.0, validation_alias="VISION_RATE_LIMIT_DELAY", description="Vision模式请求间隔(秒)")
    
    # 日志配置
    log_level: str = Field("INFO", validation_alias="LOG_LEVEL", description="日志级别")
    log_file: Optional[Path] = Field(None, validation_alias="LOG_FILE", description="日志文件路径")
    
    # 模式配置路径
    modes_config_path: Path = Field(BASE_DIR / "config" / "modes.json", validation_alias="MODES_CONFIG_PATH")
    
    # 功能开关
    enable_cache: bool = Field(True, validation_alias="ENABLE_CACHE", description="是否启用缓存")
    cache_ttl: int = Field(3600, validation_alias="CACHE_TTL", description="缓存生存时间（秒）")
    max_context_length: int = Field(4000, validation_alias="MAX_CONTEXT_LENGTH", description="最大上下文长度")
    min_chunk_size: int = Field(200, validation_alias="MIN_CHUNK_SIZE", description="最小分块大小")
    max_chunk_size: int = Field(2000, validation_alias="MAX_CHUNK_SIZE", description="最大分块大小")
    
    use_breadcrumb: bool = Field(True, validation_alias="USE_BREADCRUMB", description="是否使用面包屑导航") 

    # -----------------------------------------------------------------------
    # 3. 渲染开关 (修正：变量名改为复数，与 Renderer 代码一致)
    # -----------------------------------------------------------------------
    retain_original: bool = Field(False, validation_alias="RETAIN_ORIGINAL", description="是否在输出中保留原文")
    render_page_markers: bool = Field(True, validation_alias="RENDER_PAGE_MARKERS", description="是否显示页码标记")

    @field_validator('document_path')
    def validate_document_path(cls, v):
        if not v.exists():
            raise FileNotFoundError(f"Document not found: {v}")
        if v.suffix.lower() not in ['.pdf', '.epub']:
            raise ValueError(f"Unsupported file format: {v.suffix}. Only PDF and EPUB are supported.")
        return v

    @field_validator('batch_size')
    def validate_batch_size(cls, v):
        if v < 1 or v > 20:
            raise ValueError('Batch size must be between 1 and 20')
        return v
    
    @field_validator('max_retries')
    def validate_max_retries(cls, v):
        if v < 0 or v > 10:
            raise ValueError('Max retries must be between 0 and 10')
        return v
    
    @field_validator('log_level')
    def validate_log_level(cls, v):
        valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
        if v.upper() not in valid_levels:
            raise ValueError(f'Log level must be one of: {", ".join(valid_levels)}')
        return v.upper()

def get_default_modes() -> Dict[str, TranslationMode]:
    """返回一个硬编码的、安全的默认模式字典，作为紧急备用。"""
    default_modes_data = {
        "1": {
            "name": "Zizek Expert",
            "role_desc": "你是一位专门研究斯拉沃热·齐泽克、拉康精神分析和黑格尔哲学的顶级学者，同时也是一位酷酷的导师。",
            "style": "学术深度解析，擅长解释黑话和哲学梗，语言通俗幽默。",
            "context_len": "high"
        },
        "2": {
            "name": "Academic Translator",
            "role_desc": "你是一位专业的学术文献翻译专家，擅长翻译哲学、社会学和文学理论著作。",
            "style": "严谨准确，保持原文学术风格，术语统一。",
            "context_len": "medium"
        },
        "3": {
            "name": "Literary Translator",
            "role_desc": "你是一位文学翻译家，擅长翻译小说、散文和诗歌等文学作品。",
            "style": "优美流畅，注重文学性和可读性。",
            "context_len": "high"
        }
    }
    return {k: TranslationMode(**v) for k, v in default_modes_data.items()}

def load_modes_config(config_path: Path) -> Dict[str, TranslationMode]:
    """加载并验证翻译模式配置"""
    if not config_path.exists():
        logging.info(f"Modes config not found at {config_path}. Creating a default one.")
        default_modes = get_default_modes()
        default_modes_dict = {k: v.model_dump() for k, v in default_modes.items()}
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(default_modes_dict, f, ensure_ascii=False, indent=2)
            return default_modes
        except Exception as e:
            logging.error(f"Failed to create default modes config file: {e}. Using emergency fallback.")
            return get_default_modes()
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            modes_data = json.load(f)
        
        validated_modes = {}
        for mode_id, mode_config in modes_data.items():
            try:
                validated_modes[mode_id] = TranslationMode(**mode_config)
            except Exception as e:
                logging.warning(f"Skipping invalid mode configuration for mode {mode_id}: {e}")
                continue
        
        if not validated_modes:
            logging.error("No valid translation modes found in configuration. Using emergency fallback.")
            return get_default_modes()
            
        return validated_modes
        
    except Exception as e:
        logging.error(f"Failed to load or parse modes configuration from {config_path}: {e}. Using emergency fallback.")
        return get_default_modes()

# 全局配置实例
try:
    settings = Settings()
except Exception as e:
    logging.error(f"Failed to load settings: {e}")
    raise

# 全局模式配置
try:
    modes = load_modes_config(settings.modes_config_path)
except Exception as e:
    logging.error(f"Failed to load translation modes: {e}")
    modes = get_default_modes()
