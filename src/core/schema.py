"""
核心数据结构定义
使用 Pydantic 2.0 进行数据验证和序列化
"""
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Any, Literal
from pydantic import AliasChoices, BaseModel, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
import json

from .exceptions import ConfigError

# 统一的翻译失败/不完整标记词表。
# 此前 is_translated（本文件）只认 "[Translation Failed"，而 checkpoint 的 get_pending_segments
# 认 "[Failed"/"Failed]"，engine 两种都会产出——导致 "[Failed: ...]" 段被 is_translated 误判为已译、
# 逃过质检与续译。以下常量是全链路（is_translated、get_pending_segments、质检 step）的唯一真源。
FAILED_MARKERS = (
    "[Translation Failed",
    "[Failed",
    "[Fallback Failed",
    "[...翻译被截断]",
)


def contains_failed_marker(text: Optional[str]) -> bool:
    """文本是否含任一失败/不完整标记（大小写敏感，标记本身固定为英文/中文字面量）。"""
    if not text:
        return False
    return any(marker in text for marker in FAILED_MARKERS)


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
    few_shot: Optional[str] = Field(None, description="示范译例（源文→理想译文），随模式注入 system instruction")
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
        """检查是否已翻译（含失败/不完整标记的视为未完成，交给质检与续译）"""
        if not self.translated_text or not self.translated_text.strip():
            return False
        return not contains_failed_marker(self.translated_text)

class APISettings(BaseModel):
    """API 配置"""
    translator_provider: str = Field("gemini", validation_alias="TRANSLATOR_PROVIDER", description="翻译器提供商 (gemini, openai-compatible, ollama)")
    gemini_api_key: Optional[str] = Field(None, validation_alias="GEMINI_API_KEY", description="Gemini API密钥")
    gemini_model: str = Field("gemini-2.5-flash", validation_alias="GEMINI_MODEL", description="Gemini模型名称")
    
    openai_base_url: str = Field("http://localhost:11434", validation_alias="OPENAI_BASE_URL", description="OpenAI兼容API基础URL")
    openai_model: str = Field("gemma3", validation_alias="OPENAI_MODEL", description="OpenAI兼容模型名称")
    openai_api_key: Optional[str] = Field(None, validation_alias="OPENAI_API_KEY", description="OpenAI兼容API密钥")
    fallback_providers: str = Field("", validation_alias="FALLBACK_PROVIDERS", description="回退供应商链，逗号分隔（如 'claude-agent' 或 'gemini,claude-agent'）。主 provider 欠费/认证失败/配额耗尽时自动切换")
    agent_model: str = Field("sonnet", validation_alias="AGENT_MODEL", description="claude-agent provider 使用的模型别名（sonnet/opus/haiku）")
    # Ollama 独立配置组：让 'ollama' 能与 deepseek 同链共存（二者都走
    # OpenAICompatibleTranslator，但 provider='ollama' 时读这组而非 openai_*）
    ollama_base_url: str = Field("http://localhost:11434/v1", validation_alias="OLLAMA_BASE_URL", description="Ollama服务器地址（OpenAI兼容端点）")
    ollama_model: str = Field("qwen3.5:35b", validation_alias="OLLAMA_MODEL", description="Ollama模型名称")



class FileSettings(BaseModel):
    """文件与路径配置"""
    document_path: Optional[Path] = Field(None, validation_alias="DOCUMENT_PATH", description="待翻译的文档路径")
    output_base_dir: Path = Field("output", validation_alias=AliasChoices("OUTPUT_DIR", "OUTPUT_BASE_DIR"), description="缓存和中间文件的主输出目录")
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
    
    # ========== API 生成参数 ==========
    # 控制模型输出的随机性和多样性
    temperature: float = Field(0.2, validation_alias="TEMPERATURE", description="生成温度 (0.0-2.0)，越低越确定")
    top_p: float = Field(0.95, validation_alias="TOP_P", description="核采样概率 (0.0-1.0)，控制多样性")
    top_k: Optional[int] = Field(None, validation_alias="TOP_K", description="Top-K 采样 (1-100)，None 表示不限制")
    max_output_tokens: int = Field(16384, validation_alias="MAX_OUTPUT_TOKENS", description="最大输出 token 数（不同模型限制: 8192-65535）")
    
    # ========== 术语表配置 ==========
    glossary_preamble_ratio: float = Field(0.1, validation_alias="GLOSSARY_PREAMBLE_RATIO", description="预翻译比例 (0.05-0.3)，用于生成术语表")
    glossary_min_terms: int = Field(10, validation_alias="GLOSSARY_MIN_TERMS", description="术语表最少条目数")
    glossary_max_terms: int = Field(100, validation_alias="GLOSSARY_MAX_TERMS", description="术语表最多条目数")
    enable_glossary_edit: bool = Field(False, validation_alias="ENABLE_GLOSSARY_EDIT", description="是否在生成后允许编辑术语表")
    skip_pretranslate_if_glossary_exists: bool = Field(True, validation_alias="SKIP_PRETRANSLATE_IF_GLOSSARY_EXISTS", description="如果术语表已存在，跳过预翻译")
    reprocess_pretranslated: bool = Field(True, validation_alias="REPROCESS_PRETRANSLATED", description="是否在正式翻译时重新处理预翻译部分")
    enable_progressive_glossary: bool = Field(True, validation_alias="ENABLE_PROGRESSIVE_GLOSSARY", description="是否启用渐进式术语表提取（每个 batch 提取一次）")
    glossary_stop_threshold: float = Field(0.8, validation_alias="GLOSSARY_STOP_THRESHOLD", description="术语表饱和度阈值 (0.5-1.0)，达到后停止预翻译")
    
    # 性能与速率
    max_retries: int = Field(3, validation_alias="MAX_RETRIES", description="最大重试次数")
    request_timeout: int = Field(60, validation_alias="REQUEST_TIMEOUT", description="API请求超时时间")
    rate_limit_delay: float = Field(1.0, validation_alias="RATE_LIMIT_DELAY", description="普通文本请求间隔 (秒)")
    vision_rate_limit_delay: float = Field(2.0, validation_alias="VISION_RATE_LIMIT_DELAY", description="Vision模式请求间隔 (秒)")

    # 分块
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
    json_repair_retries: int = Field(0, description="(deprecated, unused) JSON 修复已由结构化输出+正则兜底取代；字段保留仅防旧 env 配置炸启动")
    use_rich_progress: bool = Field(False, description="是否使用 rich 进度显示")

    # 质检回路：翻译完成后扫描失败/术语违例段落并定向重译（上限 1 轮），生成 quality_report.json
    enable_quality_check: bool = Field(True, validation_alias="ENABLE_QUALITY_CHECK", description="是否启用译后质检与定向重译")
    qc_semantic: bool = Field(False, validation_alias="QC_SEMANTIC", description="是否启用廉价模型语义漏译抽检（默认关，控成本，预留）")

    # 整体成功率下限：provider 退化成"每批恰 1 段成功"时，_run_sync_batches/
    # _process_single_batch 的连续失败熔断永不触发（每批都有 >=1 成功，
    # consecutive_failures 每次都被清零）——会产出大半 [Failed:] 的成品且
    # 退出码 0。_run_translation_loop 用本轮 pending 总数做分母校验整体成功率，
    # 低于此阈值判定 provider 已退化，raise APIError 阻断静默降级完工。
    min_success_ratio: float = Field(0.5, validation_alias="MIN_SUCCESS_RATIO", description="本轮翻译成功率下限 (0.0-1.0)，低于此值判定 provider 退化并中止")

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
        env_file=Path('config/config.env'),
        env_file_encoding='utf-8',
        env_nested_delimiter='__',
        case_sensitive=False
    )

    @model_validator(mode='after')
    def validate_document_path_is_set(self) -> 'Settings':
        """校验已提供的文档路径（存在性与格式）。

        document_path 允许为 None：main.py 先从 env 构造 Settings，
        再由 SettingsBuilder 注入 CLI 传入的文档路径。
        """
        if self.files.document_path:
            if not self.files.document_path.exists():
                raise FileNotFoundError(f"Document not found: {self.files.document_path}")
            if self.files.document_path.suffix.lower() not in ['.pdf', '.epub']:
                raise ValueError(f"Unsupported file format: {self.files.document_path.suffix}. Only PDF and EPUB are supported.")
            
            # 对于 EPUB 文件，禁用页码标记（页码对EPUB无意义）
            if self.files.document_path.suffix.lower() == '.epub':
                self.processing.render_page_markers = False
        return self

    @classmethod
    def _env_sections(cls) -> dict:
        """SECTION 前缀 → 对应 pydantic 子模型，供未知键校验复用（_known_env_keys /
        from_env_file 共用同一份，避免两处 section 列表各写一遍导致漂移）。"""
        return {
            'API': APISettings, 'FILES': FileSettings, 'PROCESSING': ProcessingSettings,
            'LOGGING': LoggingSettings, 'DOCUMENT': DocumentConfig,
        }

    @classmethod
    def _known_env_keys(cls) -> set:
        """枚举全部合法的 env 键（SECTION__FIELD 大写 + 各字段 validation_alias）。"""
        keys = set()
        for prefix, model in cls._env_sections().items():
            for name, field in model.model_fields.items():
                keys.add(f"{prefix}__{name}".upper())
                alias = getattr(field, 'validation_alias', None)
                if isinstance(alias, str):
                    keys.add(f"{prefix}__{alias}".upper())
        return keys

    @classmethod
    def _bare_field_hints(cls) -> dict:
        """裸字段名（无 SECTION__ 前缀，大写）→ 正确 SECTION__FIELD 写法的提示
        映射，从各 Settings 子模型的字段名/validation_alias 动态生成。用于致命
        ConfigError 消息里给出「XXX → SECTION__XXX」这类具体修正建议，而不是
        只说"缺少合法前缀"却不指明该缺的前缀是什么。"""
        hints: dict = {}
        for prefix, model in cls._env_sections().items():
            for name, field in model.model_fields.items():
                correct = f"{prefix}__{name}".upper()
                hints.setdefault(name.upper(), correct)
                alias = getattr(field, 'validation_alias', None)
                if isinstance(alias, str):
                    hints.setdefault(alias.upper(), correct)
        return hints

    @classmethod
    def from_env_file(cls, env_file_path: Path = Path('config/config.env')) -> 'Settings':
        """
        从指定的 .env 文件路径加载设置。

        Settings 的 extra 是默认的 'forbid'，不是「静默忽略」，但哪些键会真正
        触发 extra_forbidden 由 pydantic-settings 的 dotenv provider 自己一套
        豁免规则决定（sources/providers/dotenv.py __call__），不是"有无合法
        SECTION__ 前缀"这么简单——实测两类键 pydantic 从不报错：
        - 空值键：`FOO=`、`PROXY_URL=""`、裸 `FOO`（无等号）在
          `if not env_value: continue` 处直接跳过，从不进入 extra 判定；
        - 键名前缀撞上任一顶层字段的 env 名（哪怕没有 `__`，如 `API_KEY`/
          `APIX`/`FILESY`/`DOCUMENTATION_URL` 分别撞上 api/files/document 字段
          的 env 名 API/FILES/DOCUMENT）：复杂字段走的是
          `env_name.startswith(field_env_name)` 纯前缀匹配，同样不会报错，
          只是静默丢弃这个键（不生效，但也不致命）。
        这些豁免是 pydantic-settings 的内部实现细节，随版本升级会漂移，本地
        复刻等于自己预测另一个库的行为——预测错了就会把原本能正常启动的
        config.env 判成假阳性致命错误。因此这里不再对"无合法 SECTION__ 前缀"
        直接下判断；只保留"合法前缀但字段名手滑"这一条确定性强的 warning
        提示（如曾经的 PROCESSING__MAX_CONTEXT_LENGT，pydantic 确认会静默
        忽略），真正的致命性交给下面 cls(_env_file=...) 权威裁决——捕获它抛出
        的 extra_forbidden ValidationError 后转成可读的 ConfigError，零假阳性。
        转换时附带 _bare_field_hints() 生成的「XXX → SECTION__XXX」修正建议
        （从各子模型字段名/alias 动态生成），而不是只说"缺前缀"却不指明具体
        该写成哪个 SECTION__FIELD。

        键的枚举用 python-dotenv 的 dotenv_values 而非手写 `line.split('=', 1)`：
        后者不认 `export KEY=VAL` 前缀（会把整行第一个词错判成键名 "EXPORT
        API__XXX"），也不处理带换行的引号取值（`KEY="a\nfoo=bar\n"` 会被当成
        两行、"foo" 被误判为未知键）——这两种在裸 pydantic/dotenv 加载下本就合法
        的写法，手写解析会把它们错误地升级成致命 ConfigError。dotenv_values 与
        下面 `cls(_env_file=...)` 走的是同一套解析器，因此这里枚举出的键集合与
        pydantic-settings 实际看到的键集合始终一致。
        """
        try:
            from dotenv import dotenv_values
            raw_keys = dotenv_values(env_file_path)
        except OSError:
            raw_keys = None  # 文件读不到时交给下面的 pydantic 正常报错路径

        if raw_keys is not None:
            known = cls._known_env_keys()
            known_prefixes = tuple(f"{p}__" for p in cls._env_sections())
            for raw_key in raw_keys:
                key = raw_key.strip().upper()
                if key in known:
                    continue
                if key.startswith(known_prefixes):
                    import logging
                    logging.getLogger(__name__).warning(
                        f"⚠️ 配置文件中的未知键将被忽略: {key}（检查拼写，格式为 SECTION__FIELD 双下划线）"
                    )
                # 无合法 SECTION__ 前缀的键不在这里预判致命——上面的豁免规则
                # 证明"前缀不合法"不等于"pydantic 会报错"，交给下面权威裁决。

        try:
            return cls(_env_file=env_file_path)
        except ValidationError as e:
            unknown_keys = sorted({
                '.'.join(str(part) for part in err['loc']).upper()
                for err in e.errors()
                if err.get('type') == 'extra_forbidden'
            })
            if unknown_keys:
                hints = cls._bare_field_hints()
                described = [
                    f"{key} → {hints[key]}" if key in hints else key
                    for key in unknown_keys
                ]
                raise ConfigError(
                    f"配置文件中存在非法键: {'; '.join(described)}（缺少合法的 SECTION__ "
                    f"前缀，会导致程序启动失败，请按提示改为 SECTION__FIELD 格式）",
                    context={"keys": unknown_keys, "env_file": str(env_file_path)},
                ) from e
            raise  # 其他类型的校验错误（类型不匹配等）保持原样上抛，不在此处转换


# 便捷类型别名
SegmentList = list[ContentSegment]
TranslationMap = Dict[str, str]
