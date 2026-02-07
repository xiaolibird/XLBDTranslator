#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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


def run_digest(args, settings):
    """运行论文提取和摘要流程"""
    # 命令行参数覆盖配置
    if args.all:
        settings.processing.days_to_fetch = 0
        settings.processing.max_emails = 0 # 0 在客户端中表示不限制
        logger.info("模式切换: 获取历史上所有的邮件")
        
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

    # 设置黑白名单
    settings.processing.whitelist = [
        "EHR", "Electronic Health Record", "EMR", "Electronic Medical Record",
        "Clinical Prediction", "Predictive Model", "Risk Prediction",
        "GNN", "Graph Neural Network", "Graph Convolutional",
        "LLM", "Large Language Model", "NLP", "Transformer",
        "Semi-supervised", "Active Learning", "Clinical Decision Support"
    ]
    settings.processing.blacklist = [
        # 生物 & 基因
        "生物", "Biology", "Biological", "基因", "Gene", "Genetic", "Genomic",
        # 药物 & 分子
        "药", "Drug", "Pharmaceutical", "Pharmacology", "分子", "Molecule", "Molecular",
        # 视觉 & 图像
        "视觉", "Vision", "Visual", "Computer Vision", "CV", "图片", "图像", "Image",
        # 信号
        "信号", "Signal", "Signal Processing"
    ]
    
    # 打印启动信息
    logger.info("=" * 60)
    logger.info("Scholar Digest 启动")
    logger.info("=" * 60)
    logger.info("  获取天数: {}".format(settings.processing.days_to_fetch))
    logger.info("  最大邮件数: {}".format(settings.processing.max_emails))
    logger.info("  批次大小: {}".format(settings.processing.batch_size))
    logger.info("  翻译摘要: {}".format('是' if settings.processing.translate_abstracts else '否'))
    logger.info("  AI 总结: {}".format('是' if settings.processing.generate_summary else '否'))
    logger.info("  自动已读: {}".format('是' if settings.processing.auto_mark_read else '否'))
    logger.info("  输出目录: {}".format(settings.processing.output_dir))
    
    # 检查 API 密钥
    if settings.processing.translate_abstracts or settings.processing.generate_summary:
        if not settings.llm.api_key:
            logger.error("需要 LLM API 密钥进行翻译和摘要")
            logger.error("   请在配置文件中设置 GEMINI_API_KEY 或使用 --no-translate --no-summary")
            sys.exit(1)
    
    # Dry run 模式
    if args.dry_run:
        settings.processing.translate_abstracts = False
        settings.processing.generate_summary = False
        settings.processing.auto_mark_read = False
        logger.info("Dry Run 模式：仅获取和解析邮件")
    
    # 创建并执行工作流
    workflow = ScholarWorkflow(settings)
    output = workflow.execute()
    
    # 额外导出 CSV
    if args.export_csv:
        csv_path = workflow.export_to_csv()
        logger.info("CSV 已导出: {}".format(csv_path))
    
    # 打印完成信息
    logger.info("=" * 60)
    logger.info("Scholar Digest 完成!")
    logger.info("   处理邮件: {}".format(output.total_emails))
    logger.info("   提取论文: {}".format(output.total_papers))
    
    # 显示优先级最高的 5 篇论文
    if output.segments:
        logger.info("\n🔥 本次 Top 5 论文 (按优先级排序):")
        # 确保 workflow 已经排过序了
        sorted_papers = sorted(output.segments, key=lambda x: x.priority_score, reverse=True)
        for i, seg in enumerate(sorted_papers[:5], 1):
            logger.info("  {}. [{:.2f}] {}".format(
                i, seg.priority_score, seg.metadata.title[:80] + ("..." if len(seg.metadata.title) > 80 else "")
            ))
            logger.info("      理由: {}".format(seg.metadata.priority_reason or "N/A"))
            if seg.metadata.citation_count:
                logger.info("      引用数: {}".format(seg.metadata.citation_count))
    
    logger.info("\n   领域分布:")
    for field, count in sorted(output.fields_distribution.items(), key=lambda x: -x[1])[:5]:
        logger.info("     - {}: {}".format(field, count))
    logger.info("=" * 60)
    
    return output


def run_deep_research(args, settings):
    """运行 Deep Research 生成论文绪论"""
    from src.scholar.deep_research import DeepResearchClient
    import json
    
    # 加载论文数据
    papers_file = Path(args.papers_file)
    if not papers_file.exists():
        logger.error("论文文件不存在: {}".format(papers_file))
        sys.exit(1)
    
    with open(papers_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # 转换为 PaperSegment 对象
    from src.scholar.schema import PaperMetadata, PaperSegment
    segments = []
    for seg_data in data.get('segments', []):
        try:
            meta_data = seg_data.get('metadata', {})
            # 处理日期字段
            if 'publication_date' in meta_data and meta_data['publication_date']:
                from datetime import date
                if isinstance(meta_data['publication_date'], str):
                    meta_data['publication_date'] = date.fromisoformat(meta_data['publication_date'])
            if 'email_received_at' in meta_data and meta_data['email_received_at']:
                from datetime import datetime
                if isinstance(meta_data['email_received_at'], str):
                    meta_data['email_received_at'] = datetime.fromisoformat(meta_data['email_received_at'])
            if 'extracted_at' in meta_data and meta_data['extracted_at']:
                from datetime import datetime
                if isinstance(meta_data['extracted_at'], str):
                    meta_data['extracted_at'] = datetime.fromisoformat(meta_data['extracted_at'])
            
            meta = PaperMetadata(**meta_data)
            seg = PaperSegment(
                segment_id=seg_data.get('segment_id', 0),
                paper_id=seg_data.get('paper_id', ''),
                metadata=meta,
                original_abstract=seg_data.get('original_abstract', ''),
                translated_abstract=seg_data.get('translated_abstract', ''),
                priority_score=seg_data.get('priority_score', 0.0)
            )
            segments.append(seg)
        except Exception as e:
            logger.warning("跳过无效论文数据: {}".format(str(e)))
            continue
    
    logger.info("从 {} 加载了 {} 篇论文".format(papers_file, len(segments)))
    
    # 运行 Deep Research
    client = DeepResearchClient(settings)
    result = client.generate_thesis_introduction(
        papers=segments,
        research_topic=args.topic,
        target_words=args.target_words,
        save_output=True
    )
    
    if result.get('success'):
        logger.info("\n" + "=" * 60)
        logger.info("SUCCESS! 论文绪论已生成")
        logger.info("输出文件: {}".format(result.get('output_file')))
        logger.info("=" * 60)
    else:
        logger.error("生成失败: {}".format(result.get('error')))
        sys.exit(1)


def main():
    """主函数，协调整个 Scholar Digest 流程"""
    try:
        # 创建主解析器
        parser = argparse.ArgumentParser(
            description="Scholar Digest - Google Scholar 邮件论文摘要工具",
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        
        # 创建子命令
        subparsers = parser.add_subparsers(dest='command', help='可用命令')
        
        # ========== digest 命令 ==========
        digest_parser = subparsers.add_parser(
            'digest',
            help='提取论文并生成摘要',
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
示例用法:
    python scholar_main.py digest                       # 使用默认配置
    python scholar_main.py digest --days 14             # 获取最近14天的邮件
    python scholar_main.py digest --all                 # 获取所有历史邮件
    python scholar_main.py digest --no-translate        # 不翻译摘要
            """
        )
        
        # 配置文件
        digest_parser.add_argument('--config', type=str, default='config/scholar.env', help='配置文件路径')
        
        # 邮件获取参数
        digest_parser.add_argument('--days', type=int, default=None, help='获取最近 N 天的邮件')
        digest_parser.add_argument('--max-emails', type=int, default=None, help='最大处理邮件数量')
        digest_parser.add_argument('--all', action='store_true', help='获取所有历史邮件')
        
        # 处理参数
        digest_parser.add_argument('--batch-size', type=int, default=None, help='LLM 批量处理大小')
        digest_parser.add_argument('--no-translate', action='store_true', help='不翻译摘要')
        digest_parser.add_argument('--no-summary', action='store_true', help='不生成 AI 总结')
        digest_parser.add_argument('--no-mark-read', action='store_true', help='不标记邮件为已读')
        
        # 输出参数
        digest_parser.add_argument('--output-dir', type=str, default=None, help='输出目录路径')
        digest_parser.add_argument('--export-csv', action='store_true', help='额外导出 CSV 格式')
        
        # 调试参数
        digest_parser.add_argument('--debug', action='store_true', help='启用调试模式')
        digest_parser.add_argument('--dry-run', action='store_true', help='仅获取和解析邮件')
        
        # ========== deep-research 命令 ==========
        dr_parser = subparsers.add_parser(
            'deep-research',
            help='使用 Deep Research 生成论文绪论',
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
示例用法:
    python scholar_main.py deep-research --papers output/digest.json
    python scholar_main.py deep-research --papers output/digest.json --topic "EHR and GNN"
    python scholar_main.py deep-research --papers output/digest.json --target-words 20000
            """
        )
        
        dr_parser.add_argument('--config', type=str, default='config/scholar.env', help='配置文件路径')
        dr_parser.add_argument('--papers', dest='papers_file', type=str, required=True, help='论文 JSON 文件路径')
        dr_parser.add_argument('--topic', type=str, default='EHR Data Mining and Clinical Prediction using Graph Neural Networks and Large Language Models', help='研究主题')
        dr_parser.add_argument('--target-words', type=int, default=15000, help='目标字数')
        dr_parser.add_argument('--debug', action='store_true', help='启用调试模式')
        
        # 解析参数
        args = parser.parse_args()
        
        # 如果没有提供子命令，默认使用 digest
        if args.command is None:
            # 为了向后兼容，解析旧格式的参数
            legacy_parser = argparse.ArgumentParser()
            legacy_parser.add_argument('--config', type=str, default='config/scholar.env')
            legacy_parser.add_argument('--days', type=int, default=None)
            legacy_parser.add_argument('--max-emails', type=int, default=None)
            legacy_parser.add_argument('--all', action='store_true')
            legacy_parser.add_argument('--batch-size', type=int, default=None)
            legacy_parser.add_argument('--no-translate', action='store_true')
            legacy_parser.add_argument('--no-summary', action='store_true')
            legacy_parser.add_argument('--no-mark-read', action='store_true')
            legacy_parser.add_argument('--output-dir', type=str, default=None)
            legacy_parser.add_argument('--export-csv', action='store_true')
            legacy_parser.add_argument('--debug', action='store_true')
            legacy_parser.add_argument('--dry-run', action='store_true')
            args = legacy_parser.parse_args()
            args.command = 'digest'
        
        # 加载配置
        config_path = Path(args.config)
        if config_path.exists():
            settings = ScholarSettings.from_env_file(config_path)
            logger.info("已加载配置文件: {}".format(config_path))
        else:
            settings = ScholarSettings()
            logger.warning("配置文件不存在: {}，使用默认配置".format(config_path))
        
        # 设置调试日志
        if args.debug:
            import logging
            logging.basicConfig(level=logging.DEBUG)
        
        # 执行对应命令
        if args.command == 'digest':
            run_digest(args, settings)
        elif args.command == 'deep-research':
            run_deep_research(args, settings)
        else:
            parser.print_help()
            sys.exit(1)
        
    except FileNotFoundError as e:
        logger.error("文件未找到: {}".format(e))
        logger.error("   请确保 Gmail API 凭据文件存在")
        sys.exit(1)
    except Exception as e:
        logger.critical("发生错误: {}".format(e))
        traceback.print_exc()
        sys.exit(1)
    finally:
        logger.info("系统关闭。")


if __name__ == "__main__":
    main()
