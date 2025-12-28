"""
Settings Builder - 使用 Builder Pattern 构建配置

提供预设模式和链式调用自定义配置
"""
from pathlib import Path
from typing import Optional, Dict, Any
from copy import deepcopy

from ..core.schema import Settings
from ..utils.logger import logger


# ==================== 配置预设 ====================

PRESETS: Dict[str, Dict[str, Any]] = {
    # 快速模式：最快翻译速度，适合快速预览
    "fast": {
        "description": "快速模式 - 最快翻译速度，适合快速预览",
        "batch_size": 10,
        "enable_async": True,
        "async_threshold": 5,
        "async_max_workers": 15,
        "enable_gemini_caching": True,
        "cache_ttl_hours": 1,
        "enable_checkpoint": True,
        "checkpoint_interval": 5,
        "max_retries": 2
    },
    
    # 高质量模式：追求翻译质量，速度较慢
    "quality": {
        "description": "高质量模式 - 追求翻译质量，速度较慢",
        "batch_size": 3,
        "enable_async": False,  # 关闭异步，确保顺序翻译
        "async_threshold": 20,
        "async_max_workers": 5,
        "enable_gemini_caching": True,
        "cache_ttl_hours": 2,
        "enable_checkpoint": True,
        "checkpoint_interval": 1,
        "max_retries": 5
    },
    
    # 平衡模式：速度和质量兼顾（默认推荐）
    "balanced": {
        "description": "平衡模式 - 速度和质量兼顾（默认推荐）",
        "batch_size": 5,
        "enable_async": True,
        "async_threshold": 10,
        "async_max_workers": 10,
        "enable_gemini_caching": True,
        "cache_ttl_hours": 1,
        "enable_checkpoint": True,
        "checkpoint_interval": 1,
        "max_retries": 3
    },
    
    # 调试模式：详细日志，小批次，便于问题定位
    "debug": {
        "description": "调试模式 - 详细日志，小批次，便于问题定位",
        "batch_size": 2,
        "enable_async": False,  # 关闭异步，便于调试
        "async_threshold": 50,
        "async_max_workers": 3,
        "enable_gemini_caching": False,  # 关闭缓存，确保每次都是新请求
        "enable_checkpoint": True,
        "checkpoint_interval": 1,
        "max_retries": 2
    },
    
    # 经济模式：最小化 token 消耗，降低成本
    "economy": {
        "description": "经济模式 - 最小化 token 消耗，降低成本",
        "batch_size": 8,  # 较大批次减少请求次数
        "enable_async": True,
        "async_threshold": 10,
        "async_max_workers": 12,
        "enable_gemini_caching": True,  # 启用缓存减少重复请求
        "cache_ttl_hours": 3,  # 更长的缓存时间
        "enable_checkpoint": True,
        "checkpoint_interval": 3,
        "max_retries": 2
    }
}


class SettingsBuilder:
    """
    Settings Builder - 使用 Builder Pattern 构建配置
    
    支持预设模式和链式调用自定义
    
    示例用法:
        # 使用预设
        settings = SettingsBuilder().use_preset("fast").build()
        
        # 预设 + 自定义
        settings = (SettingsBuilder()
                    .use_preset("balanced")
                    .document_path("test.pdf")
                    .batch_size(10)
                    .build())
        
        # 完全自定义
        settings = (SettingsBuilder()
                    .document_path("test.pdf")
                    .batch_size(5)
                    .enable_cache()
                    .build())
    """
    
    def __init__(self, base_settings: Optional[Settings] = None):
        """
        初始化 Builder
        
        Args:
            base_settings: 基础设置对象（可选）。如果提供，将作为起点进行修改
        """
        if base_settings:
            self._settings = deepcopy(base_settings)
        else:
            self._settings = Settings()
        
        # 存储待应用的修改
        self._modifications: Dict[str, Any] = {}
        self._preset_name: Optional[str] = None
    
    # ========== 预设相关 ==========
    
    def use_preset(self, preset_name: str) -> 'SettingsBuilder':
        """
        应用预设模式
        
        Args:
            preset_name: 预设名称 (fast/quality/balanced/debug/economy)
        """
        if preset_name not in PRESETS:
            available = ", ".join(PRESETS.keys())
            raise ValueError(f"未知预设: '{preset_name}'。可用: {available}")
        
        self._preset_name = preset_name
        preset = PRESETS[preset_name]
        
        # 将预设配置添加到修改列表（不包括 description）
        for key, value in preset.items():
            if key != "description":
                self._modifications[key] = value
        
        logger.info(f"🎯 使用预设: {preset_name} - {preset['description']}")
        return self
    
    @classmethod
    def list_presets(cls) -> Dict[str, str]:
        """列出所有可用预设及其描述"""
        return {name: config["description"] for name, config in PRESETS.items()}
    
    @classmethod
    def print_presets(cls) -> None:
        """打印所有预设信息"""
        print("\n" + "=" * 60)
        print("可用配置预设")
        print("=" * 60)
        for name, desc in cls.list_presets().items():
            print(f"\n{name:12s} - {desc}")
        print("\n" + "=" * 60 + "\n")
    
    # ========== 性能设置 ==========
    
    def batch_size(self, size: int) -> 'SettingsBuilder':
        """设置批处理大小"""
        self._modifications['batch_size'] = size
        return self
    
    def cache_ttl_hours(self, hours: int) -> 'SettingsBuilder':
        """设置缓存有效期（小时）"""
        self._modifications['cache_ttl_hours'] = hours
        return self
    
    def enable_cache(self, enabled: bool = True) -> 'SettingsBuilder':
        """启用或禁用缓存"""
        self._modifications['enable_gemini_caching'] = enabled
        return self
    
    def enable_async(self, enabled: bool = True) -> 'SettingsBuilder':
        """启用或禁用异步模式"""
        self._modifications['enable_async'] = enabled
        return self
    
    def async_threshold(self, threshold: int) -> 'SettingsBuilder':
        """设置异步模式触发阈值"""
        self._modifications['async_threshold'] = threshold
        return self
    
    def async_max_workers(self, workers: int) -> 'SettingsBuilder':
        """设置异步并发数"""
        self._modifications['async_max_workers'] = workers
        return self
    
    def enable_checkpoint(self, enabled: bool = True) -> 'SettingsBuilder':
        """启用或禁用断点续传"""
        self._modifications['enable_checkpoint'] = enabled
        return self
    
    def checkpoint_interval(self, interval: int) -> 'SettingsBuilder':
        """设置检查点保存间隔"""
        self._modifications['checkpoint_interval'] = interval
        return self
    
    # ========== 翻译设置 ==========
    
    def translation_mode(self, mode: str) -> 'SettingsBuilder':
        """
        设置翻译模式
        
        Args:
            mode: 翻译模式名称 (如 "text", "vision")
        """
        self._modifications['translation_mode'] = mode
        return self
    
    def translation_mode_entity(self, mode_entity: Any) -> 'SettingsBuilder':
        """
        设置翻译模式实体对象
        
        Args:
            mode_entity: TranslationMode 对象
        """
        self._modifications['translation_mode_entity'] = mode_entity
        return self
    
    def use_vision_mode(self, enabled: bool = True) -> 'SettingsBuilder':
        """启用或禁用视觉模式"""
        self._modifications['use_vision_mode'] = enabled
        return self
    
    # ========== 文件设置 ==========
    
    def document_path(self, file_path: str | Path) -> 'SettingsBuilder':
        """设置源文档路径"""
        self._modifications['document_path'] = Path(file_path)
        return self
    
    def output_base_dir(self, output_dir: str | Path) -> 'SettingsBuilder':
        """设置输出目录"""
        self._modifications['output_base_dir'] = Path(output_dir)
        return self
    
    def final_output_dir(self, output_dir: str | Path) -> 'SettingsBuilder':
        """设置最终输出目录"""
        self._modifications['final_output_dir'] = Path(output_dir)
        return self
    
    # ========== API 设置 ==========
    
    def gemini_api_key(self, api_key: str) -> 'SettingsBuilder':
        """设置 API Key"""
        self._modifications['gemini_api_key'] = api_key
        return self
    
    def gemini_model(self, model: str) -> 'SettingsBuilder':
        """设置模型名称"""
        self._modifications['model_name'] = model
        return self
    
    def max_retries(self, retries: int) -> 'SettingsBuilder':
        """设置最大重试次数"""
        self._modifications['max_retries'] = retries
        return self
    
    def request_timeout(self, timeout: int) -> 'SettingsBuilder':
        """设置请求超时时间（秒）"""
        self._modifications['request_timeout'] = timeout
        return self
    
    # ========== 日志设置 ==========
    
    def log_level(self, level: str) -> 'SettingsBuilder':
        """设置日志级别 (DEBUG/INFO/WARNING/ERROR)"""
        self._modifications['log_level'] = level
        return self
    
    def log_file(self, log_file: str | Path) -> 'SettingsBuilder':
        """设置日志文件路径"""
        self._modifications['log_file'] = Path(log_file)
        return self
    
    # ========== 通用设置 ==========
    
    def custom(self, key: str, value: Any) -> 'SettingsBuilder':
        """设置自定义配置项"""
        self._modifications[key] = value
        return self
    
    # ========== 构建 ==========
    
    def build(self) -> Settings:
        """
        构建最终的 Settings 对象
        
        Returns:
            Settings: 配置完成的设置对象
        """
        # 应用所有修改到设置对象
        for key, value in self._modifications.items():
            self._apply_setting(key, value)
        
        # 验证设置
        self._validate_settings()
        
        return self._settings
    
    def _apply_setting(self, key: str, value: Any) -> None:
        """应用单个设置项到 Settings 对象"""
        # Processing 相关设置
        if key in ['batch_size', 'enable_gemini_caching', 'enable_async', 
                   'async_threshold', 'async_max_workers', 'translation_mode', 
                   'enable_checkpoint', 'checkpoint_interval', 'cache_ttl_hours', 
                   'max_retries', 'max_context_length', 'json_repair_retries', 
                   'request_timeout', 'rate_limit_delay', 'enable_cache',
                   'use_breadcrumb', 'render_page_markers', 'use_vision_mode',
                   'retain_original', 'use_rich_progress', 'translation_mode_entity',
                   'vision_rate_limit_delay']:
            setattr(self._settings.processing, key, value)
        
        # Files 相关设置
        elif key in ['document_path', 'output_base_dir', 'final_output_dir', 
                     'log_file', 'modes_config_path']:
            setattr(self._settings.files, key, value)
        
        # API 相关设置
        elif key in ['gemini_api_key', 'model_name']:
            # 注意：schema中是 gemini_model 而不是 model_name
            if key == 'model_name':
                setattr(self._settings.api, 'gemini_model', value)
            elif key == 'gemini_api_key':
                setattr(self._settings.api, 'gemini_api_key', value)
        
        # Logging 相关设置
        elif key in ['log_level']:
            setattr(self._settings.logging, key, value)
        
        # 未知设置（静默忽略）
        else:
            logger.debug(f"自定义设置: {key} = {value}")
    
    def _validate_settings(self) -> None:
        """验证设置的有效性"""
        # 验证必需的文件路径
        if self._settings.files.document_path:
            doc_path = self._settings.files.document_path
            if not doc_path.exists():
                logger.warning(f"⚠️ 源文档不存在: {doc_path}")
        
        # 验证批大小
        if self._settings.processing.batch_size <= 0:
            raise ValueError("批大小必须大于 0")
        
        # 验证异步阈值
        if self._settings.processing.async_threshold < 0:
            raise ValueError("异步阈值不能为负数")

        # ========== 配置冲突/矛盾处理 ==========
        # 1) 总缓存关闭时，Gemini Context Caching 也应关闭（避免用户误以为仍在使用 Gemini 缓存）
        if (not self._settings.processing.enable_cache) and self._settings.processing.enable_gemini_caching:
            logger.warning("⚠️ enable_cache=False 时将禁用 enable_gemini_caching（避免缓存配置矛盾）")
            self._settings.processing.enable_gemini_caching = False

        # 2) 异步关闭时，异步相关参数仅提示（最终是否忽略由 ProcessingSettings validator 决定）
        if not self._settings.processing.enable_async:
            if self._settings.processing.async_max_workers != 10 or self._settings.processing.async_threshold != 10:
                logger.warning("⚠️ enable_async=False：async_max_workers/async_threshold 仅保留配置但会被忽略")

        # 3) Gemini caching 关闭时，TTL 仅提示
        if not self._settings.processing.enable_gemini_caching and self._settings.processing.cache_ttl_hours != 1:
            logger.warning("⚠️ enable_gemini_caching=False：cache_ttl_hours 配置将被忽略")
