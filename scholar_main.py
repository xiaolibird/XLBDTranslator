#!/usr/bin/env python3
"""
Scholar Digest 主入口
从 Google Scholar 邮件中提取论文并生成摘要
"""
import os
import sys
import argparse
from pathlib import Path
import traceback

from src.scholar.schema import ScholarSettings
from src.scholar.workflow import ScholarWorkflow
from src.utils.logger import setup_logging, logger


def main():
    """主函数，协调整个 Scholar Digest 流程"""
    try:
        # 解析命令行参数
        parser = argparse.ArgumentParser(
            description="Scholar Digest - Google Scholar 邮件论文摘要工具",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
示例用法:
    python scholar_main.py                              # 使用默认配置
    python scholar_main.py --days 14                    # 获取最近14天的邮件
    python scholar_main.py --max-emails 50              # 最多处理50封邮件
    python scholar_main.py --no-translate               # 不翻译摘要
    python scholar_main.py --no-mark-read               # 不标记邮件为已读
    python scholar_main.py --config custom.env          # 使用自定义配置文件
    python scholar_main.py --output-dir ./my_output     # 指定输出目录
            """
        )
        
        # 配置文件
        parser.add_argument(
            '--config',
            type=str,
            default='config/scholar.env',
            help='配置文件路径（默认: config/scholar.env）'
        )
        
        # 邮件获取参数
        parser.add_argument(
            '--days',
            type=int,
            default=None,
            help='获取最近 N 天的邮件（默认: 7）'
        )
        parser.add_argument(
            '--max-emails',
            type=int,
            default=None,
            help='最大处理邮件数量（默认: 100）'
        )
        
        # 处理参数
        parser.add_argument(
            '--batch-size',
            type=int,
            default=None,
            help='LLM 批量处理大小（默认: 5）'
        )
        parser.add_argument(
            '--no-translate',
            action='store_true',
            help='不翻译摘要，仅提取论文信息'
        )
        parser.add_argument(
            '--no-summary',
            action='store_true',
            help='不生成 AI 总结'
        )
        parser.add_argument(
            '--no-mark-read',
            action='store_true',
            help='不自动标记邮件为已读'
        )
        
        # 输出参数
        parser.add_argument(
            '--output-dir',
            type=str,
            default=None,
            help='输出目录路径'
        )
        parser.add_argument(
            '--export-csv',
            action='store_true',
            help='额外导出 CSV 格式'
        )
        
        # 调试参数
        parser.add_argument(
            '--debug',
            action='store_true',
            help='启用调试模式'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='仅获取和解析邮件，不进行 LLM 处理'
        )
        
        args = parser.parse_args()
        
        # 加载配置
        config_path = Path(args.config)
        if config_path.exists():
            settings = ScholarSettings.from_env_file(config_path)
            logger.info(f"📂 已加载配置文件: {config_path}")
        else:
            # 使用默认配置
            settings = ScholarSettings()
            logger.warning(f"⚠️ 配置文件不存在: {config_path}，使用默认配置")
        
        # 命令行参数覆盖配置
        if args.days is not None:
            settings.processing.days_to_fetch = args.days
        if args.max_emails is not None:
            settings.processing.max_emails = args.max_emails
        if args.batch_size is not None:
            settings.processing.batch_size = args.batch_size
        if args.no_translate:
            settings.processing.translate_abstracts = False
        if args.no_summary:
            settings.processing.generate_summary = False
        if args.no_mark_read:
            settings.processing.auto_mark_read = False
        if args.output_dir:
            settings.processing.output_dir = Path(args.output_dir)
        if args.debug:
            settings.log_level = "DEBUG"
        
        # 设置日志
        if args.debug:
            import logging
            logging.basicConfig(level=logging.DEBUG)
        
        # 打印启动信息
        logger.info("=" * 60)
        logger.info("📚 Scholar Digest 启动")
        logger.info("=" * 60)
        logger.info(f"  📅 获取天数: {settings.processing.days_to_fetch}")
        logger.info(f"  📧 最大邮件数: {settings.processing.max_emails}")
        logger.info(f"  📦 批次大小: {settings.processing.batch_size}")
        logger.info(f"  🌐 翻译摘要: {'是' if settings.processing.translate_abstracts else '否'}")
        logger.info(f"  🤖 AI 总结: {'是' if settings.processing.generate_summary else '否'}")
        logger.info(f"  ✅ 自动已读: {'是' if settings.processing.auto_mark_read else '否'}")
        logger.info(f"  📁 输出目录: {settings.processing.output_dir}")
        
        # 检查 API 密钥
        if settings.processing.translate_abstracts or settings.processing.generate_summary:
            if not settings.llm.api_key:
                logger.error("❌ 需要 LLM API 密钥进行翻译和摘要")
                logger.error("   请在配置文件中设置 GEMINI_API_KEY 或使用 --no-translate --no-summary")
                sys.exit(1)
        
        # Dry run 模式
        if args.dry_run:
            settings.processing.translate_abstracts = False
            settings.processing.generate_summary = False
            settings.processing.auto_mark_read = False
            logger.info("⚠️ Dry Run 模式：仅获取和解析邮件")
        
        # 创建并执行工作流
        workflow = ScholarWorkflow(settings)
        output = workflow.execute()
        
        # 额外导出 CSV
        if args.export_csv:
            csv_path = workflow.export_to_csv()
            logger.info(f"📊 CSV 已导出: {csv_path}")
        
        # 打印完成信息
        logger.info("=" * 60)
        logger.info("🎉 Scholar Digest 完成!")
        logger.info(f"   处理邮件: {output.total_emails}")
        logger.info(f"   提取论文: {output.total_papers}")
        logger.info("   领域分布:")
        for field, count in sorted(output.fields_distribution.items(), key=lambda x: -x[1])[:5]:
            logger.info(f"     - {field}: {count}")
        logger.info("=" * 60)
        
    except FileNotFoundError as e:
        logger.error(f"❌ 文件未找到: {e}")
        logger.error("   请确保 Gmail API 凭据文件存在")
        sys.exit(1)
    except Exception as e:
        logger.critical(f"💥 发生错误: {e}")
        if args.debug:
            logger.critical(traceback.format_exc())
        sys.exit(1)
    finally:
        logger.info("系统关闭。")


if __name__ == "__main__":
    main()
