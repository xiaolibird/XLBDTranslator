"""
测试工作流模块 - 预设配置和测试支持
"""
from pathlib import Path
from typing import Optional

from ..core.schema import Settings
from ..utils.logger import logger
from .workflow import TranslationWorkflow
from .builder import SettingsBuilder, PRESETS


class TestWorkflow:
    """
    测试工作流 - 简化测试配置和执行
    
    提供预设测试方法和自定义测试支持
    """
    
    def __init__(self, settings: Settings):
        """初始化测试工作流"""
        self.settings = settings
        self.workflow: Optional[TranslationWorkflow] = None
    
    def run(self) -> None:
        """执行测试工作流"""
        try:
            logger.info("🧪 开始执行测试工作流...")
            logger.info(f"   - 源文件: {self.settings.files.document_path}")
            logger.info(f"   - 翻译模式: {self.settings.processing.translation_mode}")
            logger.info(f"   - 批大小: {self.settings.processing.batch_size}")
            
            # 创建并执行翻译工作流
            self.workflow = TranslationWorkflow(self.settings)
            self.workflow.execute()
            
            logger.info("✅ 测试工作流执行成功!")
            
        except Exception as e:
            logger.error(f"❌ 测试工作流执行失败: {e}")
            raise
    
    # ========== 预设测试方法 ==========
    
    @classmethod
    def fast_test(cls, source_file: str | Path, output_dir: Optional[str | Path] = None) -> None:
        """快速模式测试"""
        logger.info("🚀 执行快速模式测试...")
        builder = SettingsBuilder().use_preset("fast").set_source_file(source_file)
        if output_dir:
            builder.set_output_dir(output_dir)
        test = cls(builder.build())
        test.run()
    
    @classmethod
    def quality_test(cls, source_file: str | Path, output_dir: Optional[str | Path] = None) -> None:
        """高质量模式测试"""
        logger.info("💎 执行高质量模式测试...")
        builder = SettingsBuilder().use_preset("quality").set_source_file(source_file)
        if output_dir:
            builder.set_output_dir(output_dir)
        test = cls(builder.build())
        test.run()
    
    @classmethod
    def balanced_test(cls, source_file: str | Path, output_dir: Optional[str | Path] = None) -> None:
        """平衡模式测试"""
        logger.info("⚖️ 执行平衡模式测试...")
        builder = SettingsBuilder().use_preset("balanced").set_source_file(source_file)
        if output_dir:
            builder.set_output_dir(output_dir)
        test = cls(builder.build())
        test.run()
    
    @classmethod
    def debug_test(cls, source_file: str | Path, output_dir: Optional[str | Path] = None) -> None:
        """调试模式测试"""
        logger.info("🐛 执行调试模式测试...")
        builder = SettingsBuilder().use_preset("debug").set_source_file(source_file)
        if output_dir:
            builder.set_output_dir(output_dir)
        test = cls(builder.build())
        test.run()
    
    @classmethod
    def economy_test(cls, source_file: str | Path, output_dir: Optional[str | Path] = None) -> None:
        """经济模式测试"""
        logger.info("💰 执行经济模式测试...")
        builder = SettingsBuilder().use_preset("economy").set_source_file(source_file)
        if output_dir:
            builder.set_output_dir(output_dir)
        test = cls(builder.build())
        test.run()


# Compatibility alias expected by web.workflow
TranslationTester = TestWorkflow
