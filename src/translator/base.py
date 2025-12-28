"""
翻译器抽象基类
定义通用接口以支持多供应商扩展
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Any

from ..core.schema import Settings, ContentSegment, SegmentList, TranslationMap


class BaseTranslator(ABC):
    """
    翻译器抽象基类
    
    定义所有翻译器必须实现的核心接口，以支持未来的多供应商扩展
    （如 OpenAI, Anthropic, Cohere 等）
    
    设计原则：
    1. 接口抽象：只定义"做什么"，不定义"怎么做"
    2. 供应商无关：不包含任何特定供应商的实现细节
    3. 可扩展性：子类可以添加特定供应商的额外功能
    """
    
    def __init__(self, settings: Settings):
        """
        初始化翻译器
        
        Args:
            settings: 全局设置对象
        """
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
        批量翻译文本段落
        
        Args:
            segments: 待翻译的段落列表
            context: 上下文文本（用于保持连贯性）
            glossary: 术语表（可选）
        
        Returns:
            翻译结果列表，顺序与输入一致
        """
        pass
    
    @abstractmethod
    def translate_titles(self, titles: List[str]) -> TranslationMap:
        """
        翻译标题列表
        
        Args:
            titles: 待翻译的标题列表
        
        Returns:
            翻译映射表 {原文: 译文}
        """
        pass
    
    @abstractmethod
    def extract_glossary(self, segments: SegmentList) -> Dict[str, str]:
        """
        从已翻译的片段中提取术语表
        
        Args:
            segments: 已翻译的段落列表
        
        Returns:
            术语表 {原文术语: 译文术语}
        """
        pass
    
    @property
    @abstractmethod
    def async_translator(self) -> Any:
        """
        获取异步翻译器实例
        
        Returns:
            异步翻译器对象（如果支持）
        """
        pass
    
    def cleanup(self):
        """
        清理资源（可选实现）
        
        子类可以重写此方法以清理特定资源
        """
        pass


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
