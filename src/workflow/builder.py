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
# 
# 参数完整定义（基于 schema.py ProcessingSettings）
# 
# 【不在预设中配置的参数 - 需要用户/环境指定】:
# - translator_provider: API 提供商
# - gemini_api_key / openai_api_key: API 密钥
# - gemini_model / openai_model: 模型名称
# - openai_base_url: API 地址
# - document_path: 文档路径
# - output_base_dir / final_output_dir: 输出目录
# - log_file / modes_config_path: 配置文件路径
# - custom_toc_path: 自定义目录
# - page_range: 页面范围
# - margin_* : 边距裁切
# - translation_mode: 翻译角色 ID
# - translation_mode_entity: 翻译角色对象

PRESETS: Dict[str, Dict[str, Any]] = {
    # ============================================================
    # 快速模式：最快翻译速度，适合快速预览
    # ============================================================
    "fast": {
        "description": "快速模式 - 最快翻译速度，适合快速预览",
        
        # --- 核心处理参数 ---
        "batch_size": 10,                    # 大批次，减少请求次数
        "max_context_length": 4096,          # 标准上下文长度
        
        # --- 生成参数 ---
        "temperature": 0.3,                  # 略高温度，允许一定灵活性
        "top_p": 0.95,                       # 标准 top-p
        "top_k": None,                       # 不限制 top-k
        "max_output_tokens": 16384,          # 标准输出长度
        
        # --- 术语表配置 ---
        "glossary_preamble_ratio": 0.08,     # 较小预翻译比例
        "glossary_min_terms": 5,             # 最少术语数
        "glossary_max_terms": 50,            # 较少最大术语数
        "enable_glossary_edit": False,       # 不编辑术语表
        "skip_pretranslate_if_glossary_exists": True,
        "reprocess_pretranslated": False,    # 不重新处理预翻译
        "enable_progressive_glossary": False, # 关闭渐进式术语提取
        "glossary_stop_threshold": 0.6,      # 较低饱和度阈值
        
        # --- 性能与速率 ---
        "max_retries": 2,                    # 较少重试
        "request_timeout": 45,               # 较短超时
        "rate_limit_delay": 0.5,             # 短请求间隔
        "vision_rate_limit_delay": 1.0,      # 短 Vision 间隔
        
        # --- 分块 ---
        "min_chunk_size": 300,               # 较大最小块
        "max_chunk_size": 2500,              # 较大最大块
        
        # --- 异步/并发 ---
        "enable_async": True,                # 启用异步
        "async_threshold": 5,                # 低阈值，更早触发异步
        "async_max_workers": 15,             # 高并发
        
        # --- 缓存 ---
        "enable_gemini_caching": True,       # 启用缓存
        "cache_ttl_hours": 1,                # 短缓存时间
        "enable_cache": True,                # 启用通用缓存
        
        # --- 断点续传 ---
        "enable_checkpoint": True,           # 启用断点
        "checkpoint_interval": 5,            # 较大保存间隔
        
        # --- 功能开关 ---
        "use_breadcrumb": False,             # 关闭面包屑（简化输出）
        "render_page_markers": True,         # 显示页码
        "use_vision_mode": None,             # 自动检测
        "retain_original": False,            # 不保留原文
        
        # --- 其他 ---
        "json_repair_retries": 1,            # 少量 JSON 修复
        "use_rich_progress": True,           # 使用 rich 进度
        "log_level": "INFO",                 # 标准日志级别
    },
    
    # ============================================================
    # 高质量模式：追求翻译质量，速度较慢
    # ============================================================
    "quality": {
        "description": "高质量模式 - 追求翻译质量，速度较慢",
        
        # --- 核心处理参数 ---
        "batch_size": 3,                     # 小批次，精细处理
        "max_context_length": 8192,          # 长上下文
        
        # --- 生成参数 ---
        "temperature": 0.1,                  # 低温度，稳定输出
        "top_p": 0.9,                        # 略低 top-p
        "top_k": 40,                         # 限制 top-k
        "max_output_tokens": 32768,          # 大输出长度
        
        # --- 术语表配置 ---
        "glossary_preamble_ratio": 0.15,     # 较大预翻译比例
        "glossary_min_terms": 15,            # 更多最少术语
        "glossary_max_terms": 150,           # 更多最大术语
        "enable_glossary_edit": True,        # 允许编辑术语表
        "skip_pretranslate_if_glossary_exists": False,  # 每次都预翻译
        "reprocess_pretranslated": True,     # 重新处理预翻译
        "enable_progressive_glossary": True, # 启用渐进式术语
        "glossary_stop_threshold": 0.9,      # 高饱和度阈值
        
        # --- 性能与速率 ---
        "max_retries": 5,                    # 更多重试
        "request_timeout": 120,              # 长超时
        "rate_limit_delay": 2.0,             # 长请求间隔
        "vision_rate_limit_delay": 3.0,      # 长 Vision 间隔
        
        # --- 分块 ---
        "min_chunk_size": 150,               # 较小最小块
        "max_chunk_size": 1500,              # 较小最大块（精细）
        
        # --- 异步/并发 ---
        "enable_async": False,               # 关闭异步，顺序处理
        "async_threshold": 50,               # 高阈值
        "async_max_workers": 5,              # 低并发
        
        # --- 缓存 ---
        "enable_gemini_caching": True,       # 启用缓存
        "cache_ttl_hours": 3,                # 长缓存时间
        "enable_cache": True,                # 启用通用缓存
        
        # --- 断点续传 ---
        "enable_checkpoint": True,           # 启用断点
        "checkpoint_interval": 1,            # 每段保存
        
        # --- 功能开关 ---
        "use_breadcrumb": True,              # 启用面包屑
        "render_page_markers": True,         # 显示页码
        "use_vision_mode": None,             # 自动检测
        "retain_original": False,            # 不保留原文
        
        # --- 其他 ---
        "json_repair_retries": 3,            # 更多 JSON 修复
        "use_rich_progress": True,           # 使用 rich 进度
        "log_level": "INFO",                 # 标准日志级别
    },
    
    # ============================================================
    # 平衡模式：速度和质量兼顾（默认推荐）
    # ============================================================
    "balanced": {
        "description": "平衡模式 - 速度和质量兼顾（默认推荐）",
        
        # --- 核心处理参数 ---
        "batch_size": 5,                     # 中等批次
        "max_context_length": 4096,          # 标准上下文
        
        # --- 生成参数 ---
        "temperature": 0.2,                  # 较低温度
        "top_p": 0.95,                       # 标准 top-p
        "top_k": None,                       # 不限制
        "max_output_tokens": 16384,          # 标准输出
        
        # --- 术语表配置 ---
        "glossary_preamble_ratio": 0.1,      # 标准预翻译比例
        "glossary_min_terms": 10,            # 标准最少术语
        "glossary_max_terms": 100,           # 标准最大术语
        "enable_glossary_edit": False,       # 不编辑
        "skip_pretranslate_if_glossary_exists": True,
        "reprocess_pretranslated": True,     # 重新处理
        "enable_progressive_glossary": True, # 启用渐进式
        "glossary_stop_threshold": 0.8,      # 标准饱和度
        
        # --- 性能与速率 ---
        "max_retries": 3,                    # 标准重试
        "request_timeout": 60,               # 标准超时
        "rate_limit_delay": 1.0,             # 标准间隔
        "vision_rate_limit_delay": 2.0,      # 标准 Vision 间隔
        
        # --- 分块 ---
        "min_chunk_size": 200,               # 标准最小块
        "max_chunk_size": 2000,              # 标准最大块
        
        # --- 异步/并发 ---
        "enable_async": True,                # 启用异步
        "async_threshold": 10,               # 标准阈值
        "async_max_workers": 10,             # 中等并发
        
        # --- 缓存 ---
        "enable_gemini_caching": True,       # 启用缓存
        "cache_ttl_hours": 1,                # 标准缓存时间
        "enable_cache": True,                # 启用通用缓存
        
        # --- 断点续传 ---
        "enable_checkpoint": True,           # 启用断点
        "checkpoint_interval": 1,            # 每段保存
        
        # --- 功能开关 ---
        "use_breadcrumb": True,              # 启用面包屑
        "render_page_markers": True,         # 显示页码
        "use_vision_mode": None,             # 自动检测
        "retain_original": False,            # 不保留原文
        
        # --- 其他 ---
        "json_repair_retries": 2,            # 标准 JSON 修复
        "use_rich_progress": True,           # 使用 rich 进度
        "log_level": "INFO",                 # 标准日志级别
    },
    
    # ============================================================
    # 调试模式：详细日志，小批次，便于问题定位
    # ============================================================
    "debug": {
        "description": "调试模式 - 详细日志，小批次，便于问题定位",
        
        # --- 核心处理参数 ---
        "batch_size": 2,                     # 极小批次
        "max_context_length": 2048,          # 短上下文
        
        # --- 生成参数 ---
        "temperature": 0.0,                  # 零温度，确定性输出
        "top_p": 1.0,                        # 不过滤
        "top_k": None,                       # 不限制
        "max_output_tokens": 8192,           # 较小输出
        
        # --- 术语表配置 ---
        "glossary_preamble_ratio": 0.05,     # 最小预翻译
        "glossary_min_terms": 5,             # 最少术语
        "glossary_max_terms": 30,            # 较少术语
        "enable_glossary_edit": True,        # 允许编辑（调试）
        "skip_pretranslate_if_glossary_exists": False,
        "reprocess_pretranslated": True,     # 重新处理
        "enable_progressive_glossary": False, # 关闭渐进式
        "glossary_stop_threshold": 0.5,      # 低饱和度
        
        # --- 性能与速率 ---
        "max_retries": 2,                    # 少量重试
        "request_timeout": 30,               # 短超时（快速失败）
        "rate_limit_delay": 0.5,             # 短间隔
        "vision_rate_limit_delay": 1.0,      # 短 Vision 间隔
        
        # --- 分块 ---
        "min_chunk_size": 100,               # 极小最小块
        "max_chunk_size": 1000,              # 极小最大块
        
        # --- 异步/并发 ---
        "enable_async": False,               # 关闭异步
        "async_threshold": 100,              # 极高阈值
        "async_max_workers": 3,              # 极低并发
        
        # --- 缓存 ---
        "enable_gemini_caching": False,      # 关闭缓存（每次新请求）
        "cache_ttl_hours": 0,                # 无缓存
        "enable_cache": False,               # 关闭通用缓存
        
        # --- 断点续传 ---
        "enable_checkpoint": True,           # 启用断点
        "checkpoint_interval": 1,            # 每段保存
        
        # --- 功能开关 ---
        "use_breadcrumb": True,              # 启用面包屑
        "render_page_markers": True,         # 显示页码
        "use_vision_mode": None,             # 自动检测
        "retain_original": True,             # 保留原文（调试对照）
        
        # --- 其他 ---
        "json_repair_retries": 5,            # 更多 JSON 修复
        "use_rich_progress": False,          # 关闭 rich（避免干扰日志）
        "log_level": "DEBUG",                # 详细日志
    },
    
    # ============================================================
    # 经济模式：最小化 token 消耗，降低成本
    # ============================================================
    "economy": {
        "description": "经济模式 - 最小化 token 消耗，降低成本",
        
        # --- 核心处理参数 ---
        "batch_size": 8,                     # 大批次减少请求
        "max_context_length": 2048,          # 短上下文省 token
        
        # --- 生成参数 ---
        "temperature": 0.3,                  # 略高温度
        "top_p": 0.9,                        # 略低 top-p
        "top_k": 30,                         # 限制 top-k
        "max_output_tokens": 8192,           # 较小输出
        
        # --- 术语表配置 ---
        "glossary_preamble_ratio": 0.05,     # 最小预翻译
        "glossary_min_terms": 5,             # 最少术语
        "glossary_max_terms": 30,            # 较少术语
        "enable_glossary_edit": False,       # 不编辑
        "skip_pretranslate_if_glossary_exists": True,
        "reprocess_pretranslated": False,    # 不重新处理
        "enable_progressive_glossary": False, # 关闭渐进式
        "glossary_stop_threshold": 0.5,      # 低饱和度
        
        # --- 性能与速率 ---
        "max_retries": 2,                    # 少量重试
        "request_timeout": 60,               # 标准超时
        "rate_limit_delay": 1.5,             # 略长间隔
        "vision_rate_limit_delay": 2.5,      # 略长 Vision 间隔
        
        # --- 分块 ---
        "min_chunk_size": 300,               # 较大最小块
        "max_chunk_size": 3000,              # 大最大块
        
        # --- 异步/并发 ---
        "enable_async": True,                # 启用异步
        "async_threshold": 10,               # 标准阈值
        "async_max_workers": 12,             # 较高并发
        
        # --- 缓存 ---
        "enable_gemini_caching": True,       # 启用缓存减少重复
        "cache_ttl_hours": 6,                # 长缓存时间
        "enable_cache": True,                # 启用通用缓存
        
        # --- 断点续传 ---
        "enable_checkpoint": True,           # 启用断点
        "checkpoint_interval": 3,            # 较大保存间隔
        
        # --- 功能开关 ---
        "use_breadcrumb": False,             # 关闭面包屑
        "render_page_markers": False,        # 关闭页码（省空间）
        "use_vision_mode": False,            # 关闭 Vision（省 token）
        "retain_original": False,            # 不保留原文
        
        # --- 其他 ---
        "json_repair_retries": 1,            # 少量 JSON 修复
        "use_rich_progress": True,           # 使用 rich 进度
        "log_level": "WARNING",              # 减少日志输出
    },
    
    # ============================================================
    # 双语模式：保留原文对照
    # ============================================================
    "bilingual": {
        "description": "双语模式 - 保留原文，适合学习和对照阅读",
        
        # --- 核心处理参数 ---
        "batch_size": 5,                     # 中等批次
        "max_context_length": 4096,          # 标准上下文
        
        # --- 生成参数 ---
        "temperature": 0.15,                 # 较低温度
        "top_p": 0.95,                       # 标准 top-p
        "top_k": None,                       # 不限制
        "max_output_tokens": 16384,          # 标准输出
        
        # --- 术语表配置 ---
        "glossary_preamble_ratio": 0.12,     # 略大预翻译
        "glossary_min_terms": 15,            # 更多术语
        "glossary_max_terms": 120,           # 更多术语
        "enable_glossary_edit": True,        # 允许编辑
        "skip_pretranslate_if_glossary_exists": True,
        "reprocess_pretranslated": True,     # 重新处理
        "enable_progressive_glossary": True, # 启用渐进式
        "glossary_stop_threshold": 0.85,     # 较高饱和度
        
        # --- 性能与速率 ---
        "max_retries": 3,                    # 标准重试
        "request_timeout": 60,               # 标准超时
        "rate_limit_delay": 1.0,             # 标准间隔
        "vision_rate_limit_delay": 2.0,      # 标准 Vision 间隔
        
        # --- 分块 ---
        "min_chunk_size": 200,               # 标准最小块
        "max_chunk_size": 1800,              # 略小最大块
        
        # --- 异步/并发 ---
        "enable_async": True,                # 启用异步
        "async_threshold": 10,               # 标准阈值
        "async_max_workers": 8,              # 中等并发
        
        # --- 缓存 ---
        "enable_gemini_caching": True,       # 启用缓存
        "cache_ttl_hours": 2,                # 中等缓存时间
        "enable_cache": True,                # 启用通用缓存
        
        # --- 断点续传 ---
        "enable_checkpoint": True,           # 启用断点
        "checkpoint_interval": 1,            # 每段保存
        
        # --- 功能开关 ---
        "use_breadcrumb": True,              # 启用面包屑
        "render_page_markers": True,         # 显示页码
        "use_vision_mode": None,             # 自动检测
        "retain_original": True,             # ★ 保留原文（核心）
        
        # --- 其他 ---
        "json_repair_retries": 2,            # 标准 JSON 修复
        "use_rich_progress": True,           # 使用 rich 进度
        "log_level": "INFO",                 # 标准日志级别
    },
}


# ==================== 设置路由表 ====================
#
# key（PRESETS / builder 链式方法使用的名字） -> (Settings 子对象属性名, 子对象内实际字段名)
#
# 数据驱动写法：所有能路由到 Settings 的键都在此列出一次；_apply_setting 只需查表，
# 不必再维护一长串 if/elif（后者曾导致 temperature/top_p/glossary_* 等键被遗漏而静默丢弃）。
# 绝大多数键在子对象里同名，只有极少数（如 model_name -> gemini_model）需要改名，
# 直接在表里写清楚即可，无需额外的"改名映射"。
_SETTING_ROUTES: Dict[str, tuple] = {
    # --- processing（ProcessingSettings） ---
    "translation_mode": ("processing", "translation_mode"),
    "translation_mode_entity": ("processing", "translation_mode_entity"),
    "batch_size": ("processing", "batch_size"),
    "max_context_length": ("processing", "max_context_length"),
    "temperature": ("processing", "temperature"),
    "top_p": ("processing", "top_p"),
    "top_k": ("processing", "top_k"),
    "max_output_tokens": ("processing", "max_output_tokens"),
    "glossary_preamble_ratio": ("processing", "glossary_preamble_ratio"),
    "glossary_min_terms": ("processing", "glossary_min_terms"),
    "glossary_max_terms": ("processing", "glossary_max_terms"),
    "enable_glossary_edit": ("processing", "enable_glossary_edit"),
    "skip_pretranslate_if_glossary_exists": ("processing", "skip_pretranslate_if_glossary_exists"),
    "reprocess_pretranslated": ("processing", "reprocess_pretranslated"),
    "enable_progressive_glossary": ("processing", "enable_progressive_glossary"),
    "glossary_stop_threshold": ("processing", "glossary_stop_threshold"),
    "max_retries": ("processing", "max_retries"),
    "request_timeout": ("processing", "request_timeout"),
    "rate_limit_delay": ("processing", "rate_limit_delay"),
    "vision_rate_limit_delay": ("processing", "vision_rate_limit_delay"),
    "min_chunk_size": ("processing", "min_chunk_size"),
    "max_chunk_size": ("processing", "max_chunk_size"),
    "enable_async": ("processing", "enable_async"),
    "async_threshold": ("processing", "async_threshold"),
    "async_max_workers": ("processing", "async_max_workers"),
    "enable_gemini_caching": ("processing", "enable_gemini_caching"),
    "cache_ttl_hours": ("processing", "cache_ttl_hours"),
    "enable_checkpoint": ("processing", "enable_checkpoint"),
    "checkpoint_interval": ("processing", "checkpoint_interval"),
    "enable_cache": ("processing", "enable_cache"),
    "use_breadcrumb": ("processing", "use_breadcrumb"),
    "render_page_markers": ("processing", "render_page_markers"),
    "use_vision_mode": ("processing", "use_vision_mode"),
    "retain_original": ("processing", "retain_original"),
    "json_repair_retries": ("processing", "json_repair_retries"),
    "use_rich_progress": ("processing", "use_rich_progress"),
    "enable_quality_check": ("processing", "enable_quality_check"),
    "qc_semantic": ("processing", "qc_semantic"),

    # --- files（FileSettings） ---
    "document_path": ("files", "document_path"),
    "output_base_dir": ("files", "output_base_dir"),
    "final_output_dir": ("files", "final_output_dir"),
    "log_file": ("files", "log_file"),
    "modes_config_path": ("files", "modes_config_path"),

    # --- api（APISettings）---
    "translator_provider": ("api", "translator_provider"),
    "gemini_api_key": ("api", "gemini_api_key"),
    "model_name": ("api", "gemini_model"),  # 对外名沿用历史命名，实际落到 gemini_model 字段
    "openai_api_key": ("api", "openai_api_key"),
    "openai_base_url": ("api", "openai_base_url"),
    "openai_model": ("api", "openai_model"),
    "fallback_providers": ("api", "fallback_providers"),
    "agent_model": ("api", "agent_model"),

    # --- logging（LoggingSettings） ---
    "log_level": ("logging", "log_level"),
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
    
    # ========== API 设置 ==========
    
    def api_key(self, key: str) -> 'SettingsBuilder':
        """设置 API 密钥"""
        self._modifications['gemini_api_key'] = key
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
    
    def translation_provider(self, provider: str) -> 'SettingsBuilder':
        """设置翻译供应商"""
        self._modifications['translator_provider'] = provider
        return self
    
    def gemini_api_key(self, api_key: str) -> 'SettingsBuilder':
        """设置Gemini API Key"""
        self._modifications['gemini_api_key'] = api_key
        return self
    
    def gemini_model(self, model: str) -> 'SettingsBuilder':
        """设置Gemini模型名称"""
        self._modifications['model_name'] = model
        return self
    
    def openai_api_key(self, api_key: str) -> 'SettingsBuilder':
        """设置OpenAI兼容API Key"""
        self._modifications['openai_api_key'] = api_key
        return self
    
    def openai_base_url(self, base_url: str) -> 'SettingsBuilder':
        """设置OpenAI兼容API基础URL"""
        self._modifications['openai_base_url'] = base_url
        return self
    
    def openai_model(self, model: str) -> 'SettingsBuilder':
        """设置OpenAI兼容模型名称"""
        self._modifications['openai_model'] = model
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
        """应用单个设置项到 Settings 对象

        路由表 _SETTING_ROUTES 是数据驱动的唯一真源：key -> (子对象名, 子对象内实际字段名)。
        之前用一长串 if/elif 手工枚举，PRESETS 里新增的 temperature/top_p/glossary_* 等
        十几个键根本没被枚举到，落到 else 分支被 logger.debug 静默丢弃——设了等于没设。
        改为路由表后，只要 Settings 子对象里有对应字段，键就一定能落位；新增字段时
        也只需在表里加一行，不必再改这段逻辑。
        """
        route = _SETTING_ROUTES.get(key)
        if route is not None:
            sub_obj_name, field_name = route
            setattr(getattr(self._settings, sub_obj_name), field_name, value)
        else:
            # 未知设置：Settings 中确实找不到归属，明确告警而非静默调试日志，
            # 避免"配置项拼错/改名后没人发现"的问题再次发生
            logger.warning(f"⚠️ 未知设置项，未生效: {key} = {value}")
    
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
