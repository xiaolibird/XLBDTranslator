"""
核心数据结构定义
使用 Pydantic 2.0 进行数据验证和序列化
"""
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Any, Literal
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
import json

class ContextLength(str, Enum):
    """上下文长度枚举"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TranslationMode(BaseModel):
    """翻译模式配置"""
    name: str = Field(description="模式名称")
    role_desc: str = Field(description="角色描述")
    style: str = Field(description="风格指南")
    context_len: ContextLength = Field(default=ContextLength.MEDIUM, description="上下文长度")

    model_config = {'use_enum_values': True}


class DocumentConfig(BaseModel):
    """文档处理配置"""
    use_vision_mode: Optional[bool] = Field(None, description="是否使用视觉模式")
    margin_top: Optional[float] = Field(None, description="顶部边距比例 (0.0-1.0)")
    margin_bottom: Optional[float] = Field(None, description="底部边距比例 (0.0-1.0)")
    margin_left: Optional[float] = Field(None, description="左侧边距比例 (0.0-1.0)")
    margin_right: Optional[float] = Field(None, description="右侧边距比例 (0.0-1.0)")
    custom_toc_path: Optional[Path] = Field(None, validation_alias="CUSTOM_TOC_PATH", description="自定义目录CSV文件路径")
    page_range: Optional[tuple[int, int]] = Field(None, description="页面范围 (start, end)")
    # retain_original: Optional[bool] = Field(None, description="是否保留原文")

    @field_validator('margin_top', 'margin_bottom', 'margin_left', 'margin_right')
    @classmethod
    def validate_margin(cls, v):
        if v is not None and not (0 <= v <= 1):
            raise ValueError('Margin must be between 0.0 and 1.0')
        return v

    @field_validator('custom_toc_path', mode='before')
    def validate_existing_paths(cls, v):
        if v is None:
            return None
        p = Path(v)
        if not p.exists():
            raise FileNotFoundError(f"Path not found: {p}")
        return p

    @field_validator('page_range', mode='before') # 更改模式为 'before'
    @classmethod
    def validate_page_range(cls, v):
        if v is None or v == "": # 允许None或空字符串
            return None
        if isinstance(v, str):
            try:
                # 尝试解析 JSON 格式的字符串，如 "[1, 10]"
                parsed_list = json.loads(v)
                if not isinstance(parsed_list, list) or len(parsed_list) != 2:
                    raise ValueError('Page range string must be a list of 2 integers or a comma/hyphen separated string.')
                start, end = parsed_list
                v = (int(start), int(end))
            except json.JSONDecodeError:
                # 尝试解析逗号或短横线分隔的字符串，如 "1,10" 或 "1-10"
                parts = [p.strip() for p in v.replace('-', ',').split(',')]
                if len(parts) == 2:
                    start, end = map(int, parts)
                    v = (start, end)
                else:
                    raise ValueError('Page range string must be a list of 2 integers or a comma/hyphen separated string.')

        if len(v) != 2:
            raise ValueError('Page range must be a tuple of (start, end)')
        start, end = v
        if start < 0 or end < start:
            raise ValueError('Invalid page range (start cannot be negative, end cannot be less than start)')
        return v


class ContentSegment(BaseModel):
    """内容片段数据模型"""
    segment_id: int = Field(description="片段唯一ID")
    original_text: str = Field(default="", description="原始文本内容")
    translated_text: str = Field(default="", description="翻译后的文本内容")

    # 结构元数据
    is_new_chapter: bool = Field(default=False, description="是否为新章节开头")
    chapter_title: str = Field(default="", description="章节标题")
    page_index: int = Field(default=0, description="页码索引 (0-based)")
    toc_level: int = Field(default=1, description="目录层级")

    # 内容类型
    content_type: Literal["text", "image"] = Field(default="text", description="内容类型")
    image_path: Optional[str] = Field(default=None, description="图片路径 (仅当content_type为image时)")

    @model_validator(mode='after')
    def validate_image_path(self) -> 'ContentSegment':
        """验证图片路径"""
        if self.content_type == "image" and not self.image_path:
            raise ValueError("Image path is required when content_type is 'image'")
        if self.content_type == "text" and self.image_path:
            raise ValueError("Image path should not be set when content_type is 'text'")
        return self

    @property
    def is_translated(self) -> bool:
        """检查是否已翻译"""
        if not self.translated_text or not self.translated_text.strip():
            return False

        # 检查是否是失败标签
        failed_markers = [
            "[Translation Failed",
            "[Translation Failed - JSON Parse Error]",
            "[Translation Failed]"
        ]
        for marker in failed_markers:
            if marker in self.translated_text:
                return False

        return True

    def get_context_window(self, all_segments: list['ContentSegment'], window_size: int = 3) -> str:
        """获取上下文窗口"""
        if not all_segments:
            return ""

        # 找到当前片段在列表中的位置
        current_idx = next((i for i, seg in enumerate(all_segments) if seg.segment_id == self.segment_id), -1)
        if current_idx == -1:
            return ""

        # 获取前几个已翻译的片段
        context_parts = []
        for i in range(max(0, current_idx - window_size), current_idx):
            if i < len(all_segments) and all_segments[i].is_translated:
                context_parts.append(all_segments[i].translated_text)

        return " ".join(context_parts).strip()


class APISettings(BaseModel):
    """API 配置"""
    translator_provider: str = Field("gemini", validation_alias="TRANSLATOR_PROVIDER", description="翻译器提供商 (gemini, openai-compatible, ollama)")
    gemini_api_key: Optional[str] = Field(None, validation_alias="GEMINI_API_KEY", description="Gemini API密钥")
    gemini_model: str = Field("gemini-2.5-flash", validation_alias="GEMINI_MODEL", description="Gemini模型名称")
    
    openai_base_url: str = Field("http://localhost:11434", validation_alias="OPENAI_BASE_URL", description="OpenAI兼容API基础URL")
    openai_model: str = Field("gemma3", validation_alias="OPENAI_MODEL", description="OpenAI兼容模型名称")
    openai_api_key: Optional[str] = Field(None, validation_alias="OPENAI_API_KEY", description="OpenAI兼容API密钥")
    # Ollama配置
    # ollama_base_url: str = Field("http://localhost:11434", validation_alias="OLLAMA_BASE_URL", description="Ollama服务器地址")
    # ollama_model: str = Field("qwen2.5:14b", validation_alias="OLLAMA_MODEL", description="Ollama模型名称")



class FileSettings(BaseModel):
    """文件与路径配置"""
    document_path: Optional[Path] = Field(None, validation_alias="DOCUMENT_PATH", description="待翻译的文档路径")
    output_base_dir: Path = Field("output", validation_alias="OUTPUT_DIR", description="缓存和中间文件的主输出目录")
    final_output_dir: Optional[Path] = Field(None, validation_alias="FINAL_OUTPUT_DIR", description="最终翻译文件的输出目录 (默认与源文件同目录)")

    log_file: Optional[Path] = Field("logs/default.log", validation_alias="LOG_FILE", description="日志文件路径 (默认不输出到文件)")
    modes_config_path: Path = Field(Path("config/modes.json"), validation_alias="MODES_CONFIG_PATH", description="翻译模式配置文件路径")

    @field_validator('document_path', mode='before')
    def validate_existing_paths(cls, v):
        if v is None:
            return None
        p = Path(v)
        if not p.exists():
            raise FileNotFoundError(f"Path not found: {p}")
        return p


class ProcessingSettings(BaseModel):
    """翻译处理与性能配置"""
    # 核心
    translation_mode: str = Field("1", validation_alias="TRANSLATION_MODE", description="默认翻译模式ID")
    batch_size: int = Field(5, validation_alias="BATCH_SIZE", description="批量翻译大小")
    max_context_length: int = Field(4096, validation_alias="MAX_CONTEXT_LENGTH", description="最大上下文长度")
    
    # 性能与速率
    max_retries: int = Field(3, validation_alias="MAX_RETRIES", description="最大重试次数")
    request_timeout: int = Field(60, validation_alias="REQUEST_TIMEOUT", description="API请求超时时间")
    rate_limit_delay: float = Field(1.0, validation_alias="RATE_LIMIT_DELAY", description="普通文本请求间隔 (秒)")
    vision_rate_limit_delay: float = Field(2.0, validation_alias="VISION_RATE_LIMIT_DELAY", description="Vision模式请求间隔 (秒)")

    # 分块
    min_chunk_size: int = Field(200, validation_alias="MIN_CHUNK_SIZE", description="最小分块大小")
    max_chunk_size: int = Field(2000, validation_alias="MAX_CHUNK_SIZE", description="最大分块大小")

    # 异步/并发与缓存相关（Builder 会设置这些）
    enable_async: bool = Field(False, description="是否启用异步处理")
    async_threshold: int = Field(10, description="触发异步的最小段落数阈值")
    async_max_workers: int = Field(10, description="异步最大并发工作数")
    enable_gemini_caching: bool = Field(True, description="是否启用 Gemini 缓存")
    cache_ttl_hours: int = Field(1, description="缓存有效期（小时）")

    # 翻译模式实体（UI/Builder 可设置完整的 TranslationMode 对象）
    translation_mode_entity: Optional[TranslationMode] = Field(None, description="翻译模式实体（TranslationMode 对象）")

    # 断点续传
    enable_checkpoint: bool = Field(True, description="是否启用断点续传")
    checkpoint_interval: int = Field(1, description="断点保存间隔（段数）")

    # 功能开关
    enable_cache: bool = Field(True, validation_alias="ENABLE_CACHE", description="是否启用缓存")
    use_breadcrumb: bool = Field(True, validation_alias="USE_BREADCRUMB", description="是否为章节标题生成面包屑导航")
    render_page_markers: bool = Field(True, validation_alias="RENDER_PAGE_MARKERS", description="是否在Markdown中显示页码标记")
    use_vision_mode: Optional[bool] = Field(None, validation_alias="USE_VISION_MODE", description="是否默认启用视觉模式")
    retain_original: Optional[bool] = Field(None, validation_alias="RETAIN_ORIGINAL", description="是否保留原文")
    # 其他可选设置
    json_repair_retries: int = Field(0, description="JSON 修复重试次数")
    use_rich_progress: bool = Field(False, description="是否使用 rich 进度显示")

    @field_validator('batch_size')
    def validate_batch_size(cls, v):
        if v < 1 or v > 20:
            raise ValueError('Batch size must be between 1 and 20')
        return v


class LoggingSettings(BaseModel):
    """日志配置"""
    log_level: str = Field("INFO", validation_alias="LOG_LEVEL", description="日志级别")
    
    @field_validator('log_level')
    def validate_log_level(cls, v):
        valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
        level = str(v).upper()
        if level not in valid_levels:
            raise ValueError(f'Log level must be one of: {", ".join(valid_levels)}')
        return level


class Settings(BaseSettings):
    """全局设置 - 统一配置入口"""
    api: APISettings
    files: FileSettings
    processing: ProcessingSettings
    logging: LoggingSettings

    # 文档配置 (同样从.env加载默认值, 并可由UI动态覆盖)
    document: DocumentConfig = Field(default_factory=DocumentConfig)

    model_config = SettingsConfigDict(
        env_file=Path('config/.env'),
        env_file_encoding='utf-8',
        env_nested_delimiter='__',
        case_sensitive=False
    )

    @model_validator(mode='after')
    def validate_document_path_is_set(self) -> 'Settings':
        """验证文档路径已提供"""
        if self.files.document_path:
            if not self.files.document_path.exists():
                raise FileNotFoundError(f"Document not found: {self.files.document_path}")
            if self.files.document_path.suffix.lower() not in ['.pdf', '.epub']:
                raise ValueError(f"Unsupported file format: {self.files.document_path.suffix}. Only PDF and EPUB are supported.")
        return self

    @classmethod
    def from_env_file(cls, env_file_path: Path = Path('config/.env')) -> 'Settings':
        """
        从指定的 .env 文件路径加载设置。
        """
        return cls(_env_file=env_file_path)


# 便捷类型别名
SegmentList = list[ContentSegment]
TranslationMap = Dict[str, str]
