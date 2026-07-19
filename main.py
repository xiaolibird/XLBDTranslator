#!/usr/bin/env python3
"""
XLBD 翻译器主入口
基于状态驱动的现代化架构
"""
import os
import sys
import argparse
from pathlib import Path

from src.core.schema import Settings
from src.core.exceptions import TranslationError, APIError, APITimeoutError, JSONParseError, ConfigError
from src.utils.logger import setup_logging, logger
from src.utils.ui import get_mode_selection, get_user_strategy, load_modes_config
from src.workflow import TranslationWorkflow
from src.workflow.builder import SettingsBuilder, PRESETS

 
def main():
    """主函数，协调整个翻译流程"""
    try:
        # 解析命令行参数
        parser = argparse.ArgumentParser(
            description="XLBD 文档翻译系统",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
                示例用法:
                python main.py document.pdf                                     # 使用默认配置，交互式选择模式
                python main.py document.pdf --mode 1                            # 指定翻译模式
                python main.py document.pdf --vision-mode force                 # 强制使用 Vision 模式
                python main.py document.pdf --page-range 10-50                  # 翻译指定页面范围
                python main.py document.pdf --retain-original                   # 保留原文
                python main.py --config custom.env document.epub                # 使用自定义配置文件
            """
        )
        parser.add_argument(
            'file_path',
            nargs='?',
            type=str,
            help='要翻译的文档路径（可选，未指定则使用配置文件中的设置）'
        )
        parser.add_argument(
            '--config',
            type=str,
            default='config/config.env',
            help='配置文件路径（默认: config/config.env）'
        )

        # 配置预设参数（覆盖 config.env 中的性能/质量默认值；其他显式 CLI 参数仍最后生效并覆盖预设）
        preset_help = "；".join(f"{name}={cfg['description']}" for name, cfg in PRESETS.items())
        parser.add_argument(
            '--preset',
            type=str,
            choices=list(PRESETS.keys()),
            help=f'配置预设（覆盖 config.env 默认值，其他显式命令行参数最后生效）: {preset_help}'
        )

        # 翻译模式参数
        parser.add_argument(
            '--mode',
            type=str,
            help='翻译模式 ID（如 1, 2, 3 等）'
        )
        
        # Vision 模式参数（仅 PDF）
        parser.add_argument(
            '--vision-mode',
            type=str,
            choices=['auto', 'force', 'off'],
            help='Vision 模式: auto=自动检测, force=强制启用, off=仅文本提取'
        )
        
        # 页面范围参数（仅 PDF）
        parser.add_argument(
            '--page-range',
            type=str,
            help='页面范围，格式: "10,50" 或 "10-50"'
        )
        
        # 裁切边距参数（仅 PDF）
        parser.add_argument(
            '--margins',
            type=str,
            help='裁切边距，格式: "top,bottom,left,right"（0.0-1.0 的比例，如 "0.1,0.05,0.05,0.05"）'
        )
        
        # 保留原文参数
        parser.add_argument(
            '--retain-original',
            action='store_true',
            help='在输出中保留原文'
        )
        parser.add_argument(
            '--no-retain-original',
            action='store_true',
            help='在输出中不保留原文'
        )
        
        args = parser.parse_args()
        
        # 初始化设置（从指定的配置文件读取）
        config_path = Path(args.config)
        if not config_path.exists():
            logger.error(f"❌ 配置文件不存在: {config_path}")
            sys.exit(1)
        base_settings = Settings.from_env_file(config_path)

        # 使用 Builder 统一构建最终 Settings（避免在 main 中直接改 settings 字段）
        builder = SettingsBuilder(base_settings)

        # 应用预设（须在下方各显式 CLI 参数之前调用，保证预设覆盖 config.env 默认值，
        # 而用户显式传入的其他 CLI flag——如 --mode/--retain-original 等——仍在 build() 之后
        # 直接作用于 settings 对象，最后生效、覆盖预设）
        if args.preset:
            builder.use_preset(args.preset)

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

        # --- 2. 应用命令行参数到 settings ---
        ext = settings.files.document_path.suffix.lower()
        
        # 2.1 处理 Vision 模式参数（仅 PDF）
        if args.vision_mode:
            if ext != '.pdf':
                logger.warning("⚠️  --vision-mode 参数仅适用于 PDF 文件，将被忽略")
            else:
                if args.vision_mode == 'force':
                    settings.processing.use_vision_mode = True
                    logger.info("✅ Vision 模式: 强制启用")
                elif args.vision_mode == 'off':
                    settings.processing.use_vision_mode = False
                    logger.info("✅ Vision 模式: 禁用")
                else:  # auto
                    settings.processing.use_vision_mode = None
                    logger.info("✅ Vision 模式: 自动检测")
        
        # 2.2 处理页面范围参数（仅 PDF）
        if args.page_range:
            if ext != '.pdf':
                logger.warning("⚠️  --page-range 参数仅适用于 PDF 文件，将被忽略")
            else:
                try:
                    parts = [p.strip() for p in args.page_range.replace('-', ',').split(',')]
                    if len(parts) == 2:
                        start, end = map(int, parts)
                        if start > 0 and end >= start:
                            settings.document.page_range = (start, end)
                            logger.info(f"✅ 页面范围: {start}-{end}")
                        else:
                            logger.error("❌ 页面范围无效（起始页必须 > 0，结束页必须 >= 起始页）")
                            sys.exit(1)
                    else:
                        logger.error("❌ 页面范围格式错误（应为 'start,end' 或 'start-end'）")
                        sys.exit(1)
                except ValueError:
                    logger.error("❌ 页面范围格式错误（应为数字）")
                    sys.exit(1)
        else:
            # 如果没有指定页面范围，默认使用所有页面
            if ext == '.pdf' and not settings.document.page_range:
                logger.info("📄 页面范围: 翻译所有页面（未指定范围）")
        
        # 2.3 处理裁切边距参数（仅 PDF）
        if args.margins:
            if ext != '.pdf':
                logger.warning("⚠️  --margins 参数仅适用于 PDF 文件，将被忽略")
            elif settings.processing.use_vision_mode is False:
                logger.warning("⚠️  Vision 模式已禁用，--margins 参数将被忽略")
            else:
                try:
                    parts = [p.strip() for p in args.margins.split(',')]
                    if len(parts) == 4:
                        t, b, l, r = map(float, parts)
                        if all(0 <= val < 1.0 for val in [t, b, l, r]):
                            settings.document.margin_top = t
                            settings.document.margin_bottom = b
                            settings.document.margin_left = l
                            settings.document.margin_right = r
                            logger.info(f"✅ 裁切边距: Top={t*100:.1f}%, Bottom={b*100:.1f}%, Left={l*100:.1f}%, Right={r*100:.1f}%")
                        else:
                            logger.error("❌ 边距值必须在 0.0-1.0 范围内")
                            sys.exit(1)
                    else:
                        logger.error("❌ 边距格式错误（应为 'top,bottom,left,right'）")
                        sys.exit(1)
                except ValueError:
                    logger.error("❌ 边距格式错误（应为数字）")
                    sys.exit(1)
        
        # 2.4 处理保留原文参数
        if args.retain_original:
            settings.processing.retain_original = True
            logger.info("✅ 保留原文: 是")
        elif args.no_retain_original:
            settings.processing.retain_original = False
            logger.info("✅ 保留原文: 否")

        # --- 3. 获取翻译模式（优先级：命令行 > 配置文件 > 交互式）---
        selected_mode = None
        
        # 3.1 尝试从命令行参数获取
        if args.mode:
            if args.mode in modes:
                selected_mode = modes[args.mode]
                logger.info(f"✅ 使用命令行指定的翻译模式: {selected_mode.name}")
            else:
                logger.error(f"❌ 指定的翻译模式 '{args.mode}' 不存在")
                logger.error(f"   可用模式: {', '.join(modes.keys())}")
                sys.exit(1)
        
        # 3.2 尝试从配置文件获取
        if not selected_mode and settings.processing.translation_mode:
            mode_id = settings.processing.translation_mode
            if mode_id in modes:
                selected_mode = modes[mode_id]
                logger.info(f"✅ 使用配置文件中的翻译模式: {selected_mode.name}")
            else:
                logger.warning(f"⚠️  配置的模式 ID '{mode_id}' 不存在")
        
        # 3.3 如果都没有，进行交互式选择（如果在交互环境中）
        need_interactive_mode = selected_mode is None
        need_interactive_strategy = any([
            # Vision 模式需要交互（仅 PDF）
            ext == '.pdf' and settings.processing.use_vision_mode is None and not args.vision_mode,
            # 边距需要交互（仅 PDF 且 Vision 模式未禁用）
            ext == '.pdf' and settings.processing.use_vision_mode is not False and 
            all(val is None for val in [settings.document.margin_top, settings.document.margin_bottom, 
                                       settings.document.margin_left, settings.document.margin_right]) and not args.margins,
            # 保留原文需要交互
            settings.processing.retain_original is None and not args.retain_original and not args.no_retain_original
        ])
        
        # 检查是否在交互环境中
        is_interactive = os.isatty(0)  # 检查 stdin 是否连接到终端

        if is_interactive and (need_interactive_mode or need_interactive_strategy):
            # 交互式模式
            if need_interactive_mode:
                selected_mode = get_mode_selection(modes)
            if need_interactive_strategy:
                get_user_strategy(settings)
        else:
            # 非交互模式
            if need_interactive_mode:
                # 如果翻译模式仍未确定，使用第一个可用模式
                selected_mode = modes[list(modes.keys())[0]]
                logger.info(f"🔄 非交互模式，使用第一个可用翻译模式: {selected_mode.name}")
            
            if not is_interactive:
                logger.info("🔄 非交互模式，使用配置文件和命令行参数中的设置")

        # --- 4. 组合最终配置 ---
        # 所有配置都通过 Builder 汇总到 final_settings
        builder.translation_mode_entity(selected_mode)
        final_settings = builder.build()
        
        logger.info(f"✅ 翻译模式已设置: {selected_mode.name}")

        # --- 5. 统一处理流程 ---
        # 所有配置都在 final_settings 中
        workflow = TranslationWorkflow(final_settings)
        workflow.execute()
        logger.info("=" * 60)
        logger.info("🎉 翻译任务成功完成！")
        logger.info("=" * 60)

    # loguru 会对 message.format(*args, **kwargs) 二次格式化：错误信息里带花括号
    # （如 API 返回的 JSON body）时 f-string 直接内插会触发 KeyError 掩盖真实错误，
    # 必须用 "{}" 占位符让 loguru 安全替换
    except TranslationError as e:
        logger.opt(exception=True).critical("💥 翻译错误: {}", str(e))
        sys.exit(1)
    except APIError as e:
        logger.opt(exception=True).critical("💥 API 错误: {}", str(e))
        sys.exit(1)
    except APITimeoutError as e:
        logger.opt(exception=True).critical("💥 API 超时: {}", str(e))
        sys.exit(1)
    except JSONParseError as e:
        logger.opt(exception=True).critical("💥 JSON 解析错误: {}", str(e))
        sys.exit(1)
    except Exception as e:
        logger.opt(exception=True).critical("💥 发生未预期的严重错误: {}", str(e))
        sys.exit(1)
    finally:
        logger.info("系统关闭。")


if __name__ == "__main__":
    main()
