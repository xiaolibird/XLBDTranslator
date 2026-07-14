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
from datetime import date, timedelta
import traceback


def _parse_month_range(args):
    """从 --month YYYY-MM 或 --since/--until YYYY-MM-DD 解析 [start,end]（含端点）。无则 None。"""
    month = getattr(args, 'month', None)
    since = getattr(args, 'since', None)
    until = getattr(args, 'until', None)
    if month:
        y, m = [int(x) for x in month.split('-')[:2]]
        start = date(y, m, 1)
        end = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
        return (start, end)
    if since or until:
        start = date.fromisoformat(since) if since else date(2000, 1, 1)
        end = date.fromisoformat(until) if until else date.today()
        return (start, end)
    return None

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
    if getattr(args, 'provider', None):
        settings.llm.provider = args.provider
        # 切换到 DeepSeek 时若模型仍是 gemini 默认值，则换用 DeepSeek 默认模型
        if args.provider != 'gemini' and settings.llm.model.startswith('gemini'):
            settings.llm.model = 'deepseek-v4-flash'
            logger.info("提供商切换为 {}，默认模型: deepseek-v4-flash".format(args.provider))
    if getattr(args, 'model', None):
        settings.llm.model = args.model
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
    if getattr(args, 'filter_mode', None):
        settings.processing.filter_mode = args.filter_mode

    # 外部检索来源开关（--external / --no-external 覆盖配置）
    if getattr(args, 'external', False):
        settings.processing.external_sources_enabled = True
    if getattr(args, 'no_external', False):
        settings.processing.external_sources_enabled = False

    # Zotero 联动开关（--zotero / --no-zotero 覆盖配置）
    if getattr(args, 'zotero', False):
        settings.processing.zotero_enabled = True
    if getattr(args, 'no_zotero', False):
        settings.processing.zotero_enabled = False

    # 全文精读开关
    if getattr(args, 'close_read', False):
        settings.processing.closeread_enabled = True

    # 黑白名单词表来自配置链路：schema 默认值 <- env (PROCESSING__WHITELIST/BLACKLIST)
    # <- CLI。不再在这里硬编码覆盖（曾导致 env 配置静默失效）。

    # Dry run 不发任何 LLM 调用，过滤强制回退关键词模式，且跳过外部网络检索
    if args.dry_run:
        if settings.processing.filter_mode == 'llm':
            settings.processing.filter_mode = 'keyword'
            logger.info("Dry Run 模式：过滤模式回退为 keyword（不调用 LLM）")
        settings.processing.external_sources_enabled = False
        settings.processing.zotero_enabled = False
        logger.info("Dry Run 模式：跳过 PubMed/arXiv 外部检索与 Zotero 写库（不发网络请求）")

    # 打印启动信息
    logger.info("=" * 60)
    logger.info("Scholar Digest 启动")
    logger.info("=" * 60)
    logger.info("  获取天数: {}".format(settings.processing.days_to_fetch))
    logger.info("  最大邮件数: {}".format(settings.processing.max_emails))
    logger.info("  批次大小: {}".format(settings.processing.batch_size))
    logger.info("  过滤模式: {} (黑名单 {} 词 / 白名单 {} 词)".format(
        settings.processing.filter_mode,
        len(settings.processing.blacklist),
        len(settings.processing.whitelist)
    ))
    logger.info("  外部检索: {} (PubMed/arXiv)".format('是' if settings.processing.external_sources_enabled else '否'))
    logger.info("  Zotero 联动: {}".format('是（写库+citekey+札记）' if settings.processing.zotero_enabled else '否'))
    logger.info("  翻译摘要: {}".format('是' if settings.processing.translate_abstracts else '否'))
    logger.info("  AI 总结: {}".format('是' if settings.processing.generate_summary else '否'))
    logger.info("  自动已读: {}".format('是' if settings.processing.auto_mark_read else '否'))
    logger.info("  输出目录: {}".format(settings.processing.output_dir))
    
    # 检查 API 密钥（按提供商区分；llm.api_key 是 Gemini 专用字段）
    if settings.processing.translate_abstracts or settings.processing.generate_summary:
        if settings.llm.provider.lower() == 'gemini' and not settings.llm.api_key:
            logger.error("需要 LLM API 密钥进行翻译和摘要")
            logger.error("   请在配置文件中设置 LLM__GEMINI_API_KEY 或使用 --no-translate --no-summary")
            sys.exit(1)
        # deepseek / openai-compatible 的密钥校验交给 workflow._create_llm_client
        #（支持回退复用 config/config.env 的 API__OPENAI_API_KEY）
    
    # Dry run 模式
    if args.dry_run:
        settings.processing.translate_abstracts = False
        settings.processing.generate_summary = False
        settings.processing.auto_mark_read = False
        logger.info("Dry Run 模式：仅获取和解析邮件")
    
    # 按月/区间回填：显式日期区间，且默认不标已读历史邮件
    month_range = _parse_month_range(args)
    if month_range and not args.no_mark_read:
        settings.processing.auto_mark_read = False
        logger.info("回填模式：日期区间 {} ~ {}，自动关闭标已读".format(
            month_range[0], month_range[1]))

    # 创建并执行工作流
    workflow = ScholarWorkflow(settings)
    workflow.date_range = month_range
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


def run_zotero_sync(args, settings):
    """独立 Zotero 联动：读已有 digest JSON → 写库 → 回查 citekey → 生成 pandoc 札记。

    用于「cron 无人值守出 digest，人在时再交互式推 Zotero」的分离流程。
    """
    from src.scholar.schema import DigestOutput
    from src.scholar.zotero_sync import sync_segments_to_zotero
    from src.scholar.notes import write_notes

    proc = settings.processing
    if args.base_url:
        proc.zotero_base_url = args.base_url
    if args.notes_dir:
        proc.notes_dir = Path(args.notes_dir)
    if args.no_pdf:
        proc.zotero_attach_pdf = False
    if args.no_enrich:
        proc.zotero_enrich_crossref = False
    if args.collection:
        proc.zotero_collection = args.collection

    input_path = Path(args.input)
    if not input_path.exists():
        logger.error("digest JSON 不存在: {}".format(input_path))
        sys.exit(1)

    output = DigestOutput.load_from_file(input_path)
    segments = output.segments
    logger.info("从 {} 加载 {} 篇入选论文".format(input_path, len(segments)))
    if not segments:
        logger.warning("无论文可同步，退出")
        return

    if args.translation_server is not None:
        proc.zotero_translation_server_url = args.translation_server
    if getattr(args, 'close_read', False):
        proc.closeread_enabled = True

    email = proc.zotero_email or proc.external_email or ""
    results = sync_segments_to_zotero(
        segments,
        base_url=proc.zotero_base_url,
        email=email,
        attach_pdf=proc.zotero_attach_pdf,
        enrich_crossref=proc.zotero_enrich_crossref,
        require_collection=proc.zotero_collection,
        write_to_zotero=not args.notes_only,
        translation_server_url=proc.zotero_translation_server_url,
    )
    citekeys = {pid: r.citekey for pid, r in results.items()}

    # 全文精读（高优先级 top-N）
    if proc.closeread_enabled:
        from src.scholar.closereading import close_read_segments
        from src.scholar.llm_client import LLMClient
        close_read_segments(
            segments, proc.research_interests, LLMClient(settings.llm),
            top_n=proc.closeread_top_n, email=email,
            model=(settings.llm.closeread_model or settings.llm.model),
        )

    # 聚合札记：一个时间窗一篇，文件名取 digest 输入名
    summary = write_notes(
        segments, citekeys, out_dir=proc.notes_dir,
        instruction=proc.notes_instruction,
        digest_title="Scholar Digest — {}".format(input_path.stem),
        filename=input_path.stem,
        emit_docx=proc.notes_emit_docx,
        cjk_font=proc.notes_docx_cjk_font,
    )

    saved = sum(1 for r in results.values() if r.saved)
    resolved = sum(1 for r in results.values() if r.citekey)
    oa_hit = sum(1 for r in results.values() if r.pdf_url)
    logger.info("=" * 60)
    logger.info("Zotero 联动完成: 写库 {} 篇 / citekey {} 篇 / OA 命中 {} 篇".format(
        saved, resolved, oa_hit))
    logger.info("聚合札记: {}".format(summary.get("note_path")))
    logger.info("references.json: {}".format(summary.get("references_json")))
    logger.info("=" * 60)


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
        digest_parser.add_argument('--month', type=str, default=None, help='按月回填 YYYY-MM（Gmail/PubMed/arXiv 都按此月区间检索）')
        digest_parser.add_argument('--since', type=str, default=None, help='起始日期 YYYY-MM-DD（区间回填）')
        digest_parser.add_argument('--until', type=str, default=None, help='结束日期 YYYY-MM-DD（区间回填）')
        
        # LLM 参数
        digest_parser.add_argument('--provider', type=str, default=None,
                                   choices=['gemini', 'deepseek', 'openai-compatible'],
                                   help='LLM 提供商（覆盖配置文件）')
        digest_parser.add_argument('--model', type=str, default=None, help='LLM 模型名称（覆盖配置文件）')

        # 处理参数
        digest_parser.add_argument('--batch-size', type=int, default=None, help='LLM 批量处理大小')
        digest_parser.add_argument('--filter-mode', type=str, default=None,
                                   choices=['llm', 'keyword'],
                                   help='白名单过滤模式：llm=LLM相关性裁决（默认），keyword=关键词匹配')
        digest_parser.add_argument('--no-translate', action='store_true', help='不翻译摘要')
        digest_parser.add_argument('--no-summary', action='store_true', help='不生成 AI 总结')
        digest_parser.add_argument('--no-mark-read', action='store_true', help='不标记邮件为已读')
        digest_parser.add_argument('--external', action='store_true', help='启用 PubMed/arXiv 外部检索来源')
        digest_parser.add_argument('--no-external', action='store_true', help='禁用 PubMed/arXiv 外部检索来源')
        digest_parser.add_argument('--zotero', action='store_true', help='digest 后写入 Zotero 并生成 pandoc 札记（需 Zotero 在线）')
        digest_parser.add_argument('--no-zotero', action='store_true', help='禁用 Zotero 联动')
        digest_parser.add_argument('--close-read', action='store_true', help='对高优先级 top-N 做全文精读（句级三色联想）')

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

        # ========== zotero 命令 ==========
        zt_parser = subparsers.add_parser(
            'zotero',
            help='把已有 digest 写入 Zotero 并生成 pandoc 札记',
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
示例用法:
    python scholar_main.py zotero --input output/scholar_digest/<run_id>.json
    # 先用 cron 无人值守出 digest，人在（Zotero 开着）时再执行本命令推送
            """
        )
        zt_parser.add_argument('--config', type=str, default='config/scholar.env', help='配置文件路径')
        zt_parser.add_argument('--input', type=str, required=True, help='digest 输出的 JSON 文件路径')
        zt_parser.add_argument('--base-url', type=str, default=None, help='Zotero 连接器地址（默认 http://localhost:23119）')
        zt_parser.add_argument('--notes-dir', type=str, default=None, help='札记输出目录')
        zt_parser.add_argument('--collection', type=str, default=None, help='防呆：要求 Zotero 里选中同名分类才写入（如 P4）')
        zt_parser.add_argument('--no-pdf', action='store_true', help='不解析/附加 OA PDF')
        zt_parser.add_argument('--no-enrich', action='store_true', help='不做 Crossref 元数据增强')
        zt_parser.add_argument('--notes-only', action='store_true', help='只重生成聚合札记（不写 Zotero，仅读 BBT 解析 citekey）')
        zt_parser.add_argument('--translation-server', type=str, default=None,
                               help='Zotero translation-server 地址（如 http://localhost:1969），交它做权威解析')
        zt_parser.add_argument('--close-read', action='store_true',
                               help='对高优先级 top-N 做全文精读（下 PDF 抽全文 → 强模型 → 句级三色联想）')
        zt_parser.add_argument('--debug', action='store_true', help='启用调试模式')
        
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
            legacy_parser.add_argument('--month', type=str, default=None)
            legacy_parser.add_argument('--since', type=str, default=None)
            legacy_parser.add_argument('--until', type=str, default=None)
            legacy_parser.add_argument('--batch-size', type=int, default=None)
            legacy_parser.add_argument('--filter-mode', type=str, default=None, choices=['llm', 'keyword'])
            legacy_parser.add_argument('--no-translate', action='store_true')
            legacy_parser.add_argument('--no-summary', action='store_true')
            legacy_parser.add_argument('--no-mark-read', action='store_true')
            legacy_parser.add_argument('--external', action='store_true')
            legacy_parser.add_argument('--no-external', action='store_true')
            legacy_parser.add_argument('--zotero', action='store_true')
            legacy_parser.add_argument('--no-zotero', action='store_true')
            legacy_parser.add_argument('--close-read', action='store_true')
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
        elif args.command == 'zotero':
            run_zotero_sync(args, settings)
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
