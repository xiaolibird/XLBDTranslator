#!/usr/bin/env python3
"""
XLBD 翻译器主入口
基于状态驱动的现代化架构
"""
import os
import sys
import argparse
from pathlib import Path
import traceback

from src.core.schema import Settings
from src.core.exceptions import TranslationError, APIError, APITimeoutError, JSONParseError, ConfigError
from src.utils.logger import setup_logging, logger
from src.utils.ui import get_mode_selection, get_user_strategy, load_modes_config
from src.workflow import TranslationWorkflow
from src.workflow.builder import SettingsBuilder


def main():
    """主函数，协调整个翻译流程"""
    try:
        # 解析命令行参数
        parser = argparse.ArgumentParser(
            description="XLBD 文档翻译系统",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
                示例用法:
                python main.py                          # 使用.env配置的文档路径
                python main.py path/to/document.pdf     # 翻译指定文档
                python main.py document.epub            # 支持PDF和EPUB格式
            """
        )
        parser.add_argument(
            'file_path',
            nargs='?',
            type=str,
            help='要翻译的文档路径（可选，未指定则使用.env中的配置）'
        )
        args = parser.parse_args()
        
        # 初始化设置（从 env 读取）
        base_settings = Settings.from_env_file()

        # 使用 Builder 统一构建最终 Settings（避免在 main 中直接改 settings 字段）
        builder = SettingsBuilder(base_settings)

        # 命令行参数覆盖文档路径
        if args.file_path:
            file_path = Path(args.file_path)
            if not file_path.exists():
                logger.error(f"❌ 指定的文档不存在: {file_path}")
                sys.exit(1)
            if file_path.suffix.lower() not in ['.pdf', '.epub']:
                logger.error(f"❌ 不支持的文档格式: {file_path.suffix}（仅支持 .pdf 和 .epub）")
                sys.exit(1)
            builder.document_path(file_path)

        # 构建一个可用的 settings（用于日志/UI/模式加载等）
        settings = builder.build()

        setup_logging(settings)

        logger.info("=" * 60)
        logger.info("📚 XLBD 文档翻译系统启动")
        logger.info("=" * 60)

        # --- 1. 加载配置 ---
        logger.info(f"📄 文档路径: {settings.files.document_path}")
        logger.info(f"🎭 默认翻译模式ID: {settings.processing.translation_mode}")
        logger.info(f"📁 项目目录: {settings.files.output_base_dir}")
        
        # --- 1.5 从配置文件加载 modes ---
        try:
            modes = load_modes_config(settings.files.modes_config_path)
            if not modes:
                logger.error("❌ 没有加载到任何有效的翻译模式！")
                raise ConfigError("无法加载翻译模式配置，请检查 modes.json 文件。")
            logger.info(f"✅ 已加载 {len(modes)} 个翻译模式")
        except Exception as e:
            logger.error(f"❌ 加载翻译模式失败: {e}")
            raise ConfigError(f"无法加载翻译模式: {e}")

        # --- 2. 获取用户选择 ---
        # 检查是否在交互环境中
        is_interactive = os.isatty(0)  # 检查 stdin 是否连接到终端

        if is_interactive:
            selected_mode = get_mode_selection(modes)  # 现在返回 TranslationMode 对象
            get_user_strategy(settings)
        else:
            # 非交互模式：使用默认值
            logger.info("🔄 非交互模式，使用env文件配置")
            mode_id = settings.processing.translation_mode
            if mode_id not in modes:
                logger.warning(f"⚠️  配置的模式 ID '{mode_id}' 不存在，使用第一个可用模式")
                mode_id = list(modes.keys())[0]
            selected_mode = modes[mode_id]
            logger.info(f"✅ 使用翻译模式: {selected_mode.name}")

        # --- 3. 组合最终配置 ---
        # 所有配置都通过 Builder 汇总到 final_settings
        builder.translation_mode_entity(selected_mode)
        final_settings = builder.build()
        
        logger.info(f"✅ 翻译模式已设置: {selected_mode.name}")

        # --- 4. 统一处理流程 ---
        # 所有配置都在 final_settings 中
        workflow = TranslationWorkflow(final_settings)
        workflow.execute()
        logger.info("=" * 60)
        logger.info("🎉 翻译任务成功完成！")
        logger.info("=" * 60)

    except TranslationError as e:
        logger.critical(f"💥 翻译错误: {e}", exc_info=True)
        sys.exit(1)
    except APIError as e:
        logger.critical(f"💥 API 错误: {e}", exc_info=True)
        sys.exit(1)
    except APITimeoutError as e:
        logger.critical(f"💥 API 超时: {e}", exc_info=True)
        sys.exit(1)
    except JSONParseError as e:
        logger.critical(f"💥 JSON 解析错误: {e}", exc_info=True)
        sys.exit(1)
    except Exception as e:
        logger.critical(f"💥 发生未预期的严重错误: {e}", exc_info=True)
        logger.critical(traceback.format_exc())
        sys.exit(1)
    finally:
        logger.info("系统关闭。")


if __name__ == "__main__":
    main()
