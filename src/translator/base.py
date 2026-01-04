"""
翻译器抽象基类
定义通用接口以支持多供应商扩展
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Any, TYPE_CHECKING

from ..core.schema import Settings, ContentSegment, SegmentList, TranslationMap

if TYPE_CHECKING:
    from typing import Protocol


class BaseTranslator(ABC):
    """
    翻译器抽象基类
    
    定义所有翻译器必须实现的核心接口，以支持未来的多供应商扩展
    （如 Gemini, OpenAI, DeepSeek, Anthropic 等）
    
    设计原则：
    1. 接口抽象：只定义"做什么"，不定义"怎么做"
    2. 供应商无关：不包含任何特定供应商的实现细节
    3. 可扩展性：子类可以添加特定供应商的额外功能
    4. 类型安全：使用抽象方法强制子类实现必需接口
    5. 文档完整：每个方法都有清晰的契约说明
    
    派生类实现要求：
    ====================================
    1. 必须实现所有@abstractmethod标记的方法
    2. translate_batch必须保证返回列表长度与输入segments长度一致
    3. translate_titles必须返回字典，键为原标题，值为翻译后标题
    4. extract_glossary必须返回字典，键为原术语，值为翻译术语
    5. async_translator可返回None表示不支持异步
    6. 所有方法应实现异常处理，抛出core.exceptions中定义的异常
    7. 建议实现__enter__/__exit__用于资源管理
    
    异常处理契约：
    ====================================
    - API错误 → 抛出 APIError 或其子类
    - 认证失败 → 抛出 APIAuthenticationError
    - 超时 → 抛出 APITimeoutError
    - JSON解析失败 → 抛出 JSONParseError
    - 配置错误 → 抛出 ConfigError
    """
    
    def __init__(self, settings: Settings):
        """
        初始化翻译器
        
        Args:
            settings: 全局设置对象，包含API配置、处理配置等
            
        Raises:
            APIAuthenticationError: API密钥无效或缺失
            ConfigError: 配置参数无效
        
        派生类注意事项：
            - 必须先调用super().__init__(settings)
            - 建议在初始化时验证API连接
            - 可选：初始化缓存管理器、Prompt管理器等
        """
        if not isinstance(settings, Settings):
            raise TypeError(f"settings must be Settings instance, got {type(settings)}")
        
        self.settings = settings
        self.doc_hash: Optional[str] = None
        
        # 从 settings 自动计算 doc_hash（用于缓存/断点等特性）
        try:
            if getattr(settings.files, 'document_path', None):
                from ..utils.file import get_file_hash
                self.doc_hash = get_file_hash(settings.files.document_path)
        except Exception:
            # 计算失败不应阻断翻译器初始化；相关功能自然降级。
            self.doc_hash = None
    
    @abstractmethod
    def translate_batch(
        self,
        segments: SegmentList,
        context: str = "",
        glossary: Optional[Dict[str, str]] = None
    ) -> List[str]:
        """
        批量翻译文本段落（核心接口）
        
        Args:
            segments: 待翻译的段落列表（SegmentList = List[ContentSegment]）
            context: 上下文文本（用于保持连贯性），通常是前文摘要
            glossary: 术语表（可选），格式为 {"原术语": "译术语"}
        
        Returns:
            翻译结果列表，长度必须与segments一致，顺序对应
            
        Raises:
            APIError: API调用失败
            APITimeoutError: 请求超时
            JSONParseError: 无法解析API响应
            TranslationError: 其他翻译相关错误
            
        实现要求：
            1. 返回列表长度必须等于len(segments)
            2. 返回顺序必须与输入segments顺序一致
            3. 单个segment翻译失败时返回"[Failed: reason]"，不中断整体
            4. 支持自动分流：文本/图像内容使用不同处理逻辑
            5. 应实现重试机制处理临时性故障
            6. 建议支持批量处理以提升效率
        """
        pass
    
    @abstractmethod
    def translate_titles(self, titles: List[str]) -> TranslationMap:
        """
        翻译标题列表（章节标题、目录等）
        
        Args:
            titles: 待翻译的标题列表，通常是章节标题或文档元数据
        
        Returns:
            翻译映射表 {原文标题: 译文标题}，TranslationMap = Dict[str, str]
            
        Raises:
            APIError: API调用失败
            JSONParseError: 无法解析API响应为字典格式
            
        实现要求：
            1. 返回字典的键必须覆盖所有输入标题
            2. 翻译失败的标题可映射为原文或"[Failed]"标记
            3. 标题翻译应更注重简洁性和一致性
            4. 建议批量翻译以提升效率
        """
        pass
    
    @abstractmethod
    def extract_glossary(self, segments: SegmentList) -> Dict[str, str]:
        """
        从已翻译的片段中提取术语表（自动术语识别）
        
        Args:
            segments: 已翻译的段落列表（必须包含original_text和translated_text）
        
        Returns:
            术语表 {原文术语: 译文术语}，用于后续翻译的一致性
            
        Raises:
            APIError: API调用失败
            TranslationError: 术语提取失败
            
        实现要求：
            1. 返回空字典表示未提取到术语（不应抛出异常）
            2. 应识别专有名词、技术术语、人名地名等
            3. 建议分析原文与译文的对应关系
            4. 可选：支持用户自定义术语优先级
            5. 如不支持术语提取，返回空字典即可
        """
        pass
    
    @property
    @abstractmethod
    def async_translator(self) -> Optional['BaseAsyncTranslator']:
        """
        获取异步翻译器实例（可选特性）
        
        Returns:
            异步翻译器对象，如不支持异步则返回None
            
        实现要求：
            1. 返回None表示不支持异步翻译
            2. 返回的异步翻译器应继承自BaseAsyncTranslator
            3. 应实现懒加载，避免不必要的资源占用
            4. 异步翻译器应支持上下文管理器（with/async with）
            5. 异步翻译器生命周期由主翻译器管理
            
        示例：
            @property
            def async_translator(self):
                if self._async_translator is None:
                    self._async_translator = AsyncGeminiTranslator(self)
                return self._async_translator
        """
        pass
    
    def cleanup(self):
        """
        清理资源（可选实现）
        
        子类可以重写此方法以清理特定资源
        """
        pass
    
    def __enter__(self):
        """上下文管理器入口（可选实现）"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器退出（可选实现）"""
        return False


class BaseAsyncTranslator(ABC):
    """
    异步翻译器抽象基类
    
    定义异步翻译的核心接口
    """
    
    def __init__(self, base_translator: BaseTranslator):
        """
        初始化异步翻译器
        
        Args:
            base_translator: 基础翻译器实例
        """
        self.base = base_translator
        self.settings = base_translator.settings
    
    @abstractmethod
    async def translate_text_batch_async(
        self,
        segments: SegmentList,
        context: str,
        glossary: Optional[Dict[str, str]] = None
    ) -> List[str]:
        """
        异步批量翻译文本段落
        
        Args:
            segments: 待翻译的段落列表
            context: 上下文文本
            glossary: 术语表（可选）
        
        Returns:
            翻译结果列表
        """
        pass
    
    @abstractmethod
    async def translate_vision_batch_async(
        self,
        segments: SegmentList,
        context: str,
        glossary: Optional[Dict[str, str]] = None
    ) -> List[str]:
        """
        异步批量翻译包含图像的段落
        
        Args:
            segments: 待翻译的段落列表（包含图像）
            context: 上下文文本
            glossary: 术语表（可选）
        
        Returns:
            翻译结果列表
        """
        pass
    
    def cleanup(self):
        """
        清理资源（可选实现）
        """
        pass
