#!/usr/bin/env python3
"""
XLBD 翻译器主入口
基于状态驱动的现代化架构
"""
import os
import sys
import json
import time
from pathlib import Path
from typing import List, Dict, Any

from src.core.schema import Settings, ContentSegment, SegmentList, DocumentConfig
from src.core.exceptions import TranslationError, APIError, APITimeoutError, JSONParseError
from src.utils.logger import setup_logging, logger
from src.utils.file import create_output_directory, get_file_hash
from src.ui import get_mode_selection, get_user_strategy
from src.parsers.manager import compile_structure
from src.parsers.tools import is_likely_chinese
from src.translator.client import GeminiTranslator
from src.renderer.markdown import MarkdownRenderer
from src.config import modes

# 全局设置和日志初始化（在 main 函数中完成）


def process_document_flow(settings: Settings, translation_mode_config: dict):
    """
    状态驱动的文档处理流程

    核心架构：
    - Source of Truth: 内存中的 List[ContentSegment]
    - Load: 加载 structure_map.json 或解析文档
    - Gap Analysis: 找出未翻译片段
    - Translation Loop: 批量翻译 + 实时保存
    - Render: 生成最终文档
    """
    file_path = settings.files.document_path
    logger.info(f"🚀 开始处理文档: {file_path.name}")
    logger.info(f"   - 翻译模式: {translation_mode_config['name']}")
    
    # 基于文件内容的 MD5 哈希创建唯一的项目标识
    file_hash = get_file_hash(file_path)
    project_name = file_hash
    logger.info(f"   - 项目标识 (Hash): {project_name}")

    # 准备工作目录和路径
    project_dir = create_output_directory(
        project_name, settings.files.output_base_dir
    )
    structure_path = project_dir / "structure_map.json"

    # 1. Load: 加载文档结构到内存
    all_segments = load_document_structure(file_path, structure_path, settings)
    if not all_segments:
        raise TranslationError("文档解析失败，未生成任何内容片段")
    
    # 2. 预翻译章节标题
    translator = GeminiTranslator(settings)
    pre_translate_titles(all_segments, translator, translation_mode_config)

    # 保存标题翻译后的结构
    save_structure_map(structure_path, all_segments)

    # 3. Gap Analysis: 找出待翻译片段
    pending_segments = find_untranslated_segments(all_segments)

    if not pending_segments:
        logger.info("🎉 所有片段均已翻译完成！")
        # 生成最终文档
        render_final_document(all_segments, file_path.name, settings)
        return

    logger.info(f"🔄 发现 {len(pending_segments)} 个待翻译片段")
    
    # 4. Translation Loop: 状态驱动翻译
    run_state_driven_translation_loop(
        pending_segments, all_segments, structure_path, translator, translation_mode_config, settings
    )

    # 5. Render: 生成最终文档
    render_final_document(all_segments, file_path.name, settings)


def load_document_structure(file_path: Path, structure_path: Path, settings: Settings) -> SegmentList:
    """
    Load 阶段：加载文档结构到内存

    优先级：
    1. 从 structure_map.json 加载（包含翻译状态）
    2. 解析原始文档生成新结构
    """
    # 1. 尝试从 structure_map.json 加载
    if structure_path.exists() and settings.processing.enable_cache:
        try:
            with open(structure_path, 'r', encoding='utf-8') as f:
                raw_data = json.load(f)
                segments = [ContentSegment(**item) for item in raw_data]
                logger.info(f"📦 从结构文件加载 {len(segments)} 个片段")
                return segments
        except Exception as e:
            logger.warning(f"⚠️ structure_map.json 损坏，将重新解析: {e}")
            
    # 2. 重新解析文档
    logger.info("⚙️ 解析文档结构...")
    segments = compile_structure(file_path, structure_path, settings)

    if segments:
        logger.info(f"✅ 解析完成，生成 {len(segments)} 个片段")
        save_structure_map(structure_path, segments)
    else:
        logger.error("❌ 文档解析失败")

    return segments or []


def pre_translate_titles(segments, translator: GeminiTranslator, translation_mode_config: dict):
    """预翻译章节标题"""
    logger.info("📝 预翻译章节标题...")
    
    # 提取待翻译标题
    raw_titles = []
    for seg in segments:
        if (seg.is_new_chapter and seg.chapter_title and
            seg.chapter_title.strip() and not is_likely_chinese(seg.chapter_title)):
            raw_titles.append(seg.chapter_title)
    
    if not raw_titles:
        logger.info("   - 无需翻译的标题")
        return

    # 去重
    unique_titles = list(dict.fromkeys(raw_titles))
    logger.info(f"   - 发现 {len(unique_titles)} 个唯一标题")

    # 批量翻译
    translation_map = translator.translate_titles(unique_titles, translation_mode_config)
    
    # 回填结果
    update_count = 0
    for seg in segments:
        if seg.is_new_chapter and seg.chapter_title in translation_map:
            translated = translation_map[seg.chapter_title]
            if translated:
                seg.chapter_title = translated
                update_count += 1
    
    logger.info(f"   - 更新了 {update_count} 个标题")


def find_untranslated_segments(all_segments: SegmentList) -> SegmentList:
    """
    Gap Analysis: 找出所有未翻译的片段

    基于内存状态分析，不依赖文件检查
    """
    untranslated = [seg for seg in all_segments if not seg.is_translated]
    logger.info(f"🔍 分析结果: {len(untranslated)}/{len(all_segments)} 片段待翻译")
    return untranslated


def run_state_driven_translation_loop(
    pending_segments: SegmentList,
    all_segments: SegmentList,
    structure_path: Path,
    translator: GeminiTranslator,
    translation_mode_config: dict,
    settings: Settings
):
    """
    Translation Loop: 状态驱动翻译主循环

    核心特征：
    - 只遍历待翻译列表
    - 通过内存索引获取上下文（不读文件）
    - 每批翻译后立即保存完整状态
    - 不实时写入 Markdown（等全部完成后渲染）
    """
    from tqdm import tqdm

    batch_size = settings.processing.batch_size
    total_batches = (len(pending_segments) + batch_size - 1) // batch_size

    logger.info(f"🔄 开始翻译循环: {total_batches} 批次，批大小 {batch_size}")

    progress_bar = tqdm(
        range(0, len(pending_segments), batch_size),
        desc="翻译进度",
        unit="批"
    )

    for batch_start in progress_bar:
        batch_end = min(batch_start + batch_size, len(pending_segments))
        current_batch = pending_segments[batch_start:batch_end]

        # 更新进度条
        batch_num = batch_start // batch_size + 1
        progress_bar.set_postfix({
            "批次": f"{batch_num}/{total_batches}",
            "片段": f"{current_batch[0].segment_id}-{current_batch[-1].segment_id}",
            "进度": f"{batch_end}/{len(pending_segments)}"
        })

        try:
            # 获取上下文：直接从内存中获取前文翻译
            context_text = get_context_from_memory(current_batch[0], all_segments, settings.processing.max_context_length)

            # 执行翻译
            translations = translator.translate_batch(current_batch, translation_mode_config, context_text)

            # 验证翻译结果
            if len(translations) != len(current_batch):
                logger.error(f"❌ 批次翻译结果数量不匹配: 期望 {len(current_batch)}, 得到 {len(translations)}")
                continue

            # 更新内存中的片段状态
            for seg, trans_text in zip(current_batch, translations):
                seg.translated_text = trans_text

            # Save: 立即保存完整状态到 structure_map.json
            save_structure_map(structure_path, all_segments)

            logger.debug(f"✅ 批次 {batch_num} 完成，已保存状态")

        except (APIError, APITimeoutError) as e:
            logger.error(f"❌ 批次 {batch_num} 发生 API 错误: {e}")
            logger.info("   将继续下一个批次。")
            continue
        except JSONParseError as e:
            logger.error(f"❌ 批次 {batch_num} 发生 JSON 解析错误: {e}")
            logger.info("   将继续下一个批次。")
            continue
        except Exception as e:
            logger.error(f"❌ 批次 {batch_num} 发生未知错误: {e}")
            # 继续下一批，不中断整个流程
            continue

        # 速率控制
        time.sleep(settings.processing.rate_limit_delay)


def get_context_from_memory(current_segment: ContentSegment, all_segments: SegmentList, max_length: int) -> str:
    """
    从内存中获取翻译上下文

    通过 segment_id 在 all_segments 中查找前文已翻译片段
    """
    # 找到当前片段的位置
    current_idx = next((i for i, seg in enumerate(all_segments) if seg.segment_id == current_segment.segment_id), -1)
    if current_idx == -1:
        return ""

    # 获取前几个已翻译的片段内容
    context_parts = []
    context_length = 0

    # 向前查找已翻译的片段
    for i in range(current_idx - 1, -1, -1):
        seg = all_segments[i]
        if seg.is_translated and seg.translated_text:
            # 估算长度（中文字符按2字节算）
            text_length = len(seg.translated_text.encode('utf-8'))
            if context_length + text_length > max_length:
                break

            context_parts.insert(0, seg.translated_text)  # 保持顺序
            context_length += text_length

    return " ".join(context_parts).strip()


def save_structure_map(structure_path: Path, segments: SegmentList):
    """
    Save: 保存完整的文档结构状态到 JSON 文件

    这是单一真理源的持久化
    """
    try:
        structure_path.parent.mkdir(parents=True, exist_ok=True)

        # 序列化为字典列表
        data = [seg.model_dump() for seg in segments]

        with open(structure_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        logger.debug(f"💾 结构状态已保存: {len(segments)} 个片段")
    except Exception as e:
        logger.error(f"❌ 保存结构状态失败: {e}")
        raise


def render_final_document(segments: SegmentList, doc_name: str, settings: Settings):
    """Render: 生成最终文档（Markdown + PDF）"""
    logger.info("📄 生成最终文档...")

    # 决定最终输出目录
    if settings.files.final_output_dir:
        final_dir = settings.files.final_output_dir
        final_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"   - 自定义输出目录: {final_dir}")
    else:
        # 默认输出到源文件所在目录
        final_dir = settings.files.document_path.parent
        logger.info(f"   - 输出到源文件目录: {final_dir}")

    # 1. 生成 Markdown
    md_renderer = MarkdownRenderer(settings)
    md_output_path = final_dir / f"{Path(doc_name).stem}_Translated.md"
    md_renderer.render_to_file(segments, md_output_path, f"原文: {doc_name}")
    logger.info(f"✅ Markdown 已保存到: {md_output_path}")

    # 2. 生成 PDF（可选，如果依赖可用）
    try:
        from src.renderer.pdf import PDFRenderer
        pdf_renderer = PDFRenderer(settings)

        pdf_path = final_dir / f"{Path(doc_name).stem}_Translated.pdf"
        pdf_renderer.render_to_file(segments, pdf_path, f"原文: {doc_name}")
        logger.info(f"✅ PDF 已保存到: {pdf_path}")

    except OSError as e:
        if "cannot load library" in str(e) or "cannot open shared object file" in str(e) or "no library called" in str(e).lower():
            logger.error("❌ PDF 生成失败：缺少 WeasyPrint 运行所需的系统依赖库。")
            logger.info("   - Windows: 请访问 https://doc.courtbouillon.org/weasyprint/stable/first_steps.html#windows 安装 GTK3。")
            logger.info("   - macOS: 请运行 `brew install pango cairo gdk-pixbuf libffi`。")
            logger.info("   - Debian/Ubuntu: 请运行 `sudo apt-get install libpango-1.0-0 libcairo2 libpangoft2-1.0-0 libgdk-pixbuf2.0-0 libffi-dev`。")
            logger.warning("⚠️ PDF 生成已跳过，但 Markdown 文件已成功生成。")
        else:
            logger.warning(f"⚠️ PDF 生成过程中发生文件错误: {e}")
            logger.info("📄 Markdown 文件已成功生成")
    except Exception as e:
        logger.warning(f"⚠️ PDF 生成跳过: {e}")
        logger.info("📄 Markdown 文件已成功生成")


def main():
    """主函数，协调整个翻译流程"""
    try:
        # 初始化设置和日志
        settings = Settings.from_env_file()
        setup_logging(settings)

        logger.info("=" * 60)
        logger.info("📚 XLBD 文档翻译系统启动")
        logger.info("=" * 60)

        # --- 1. 加载配置 ---
        logger.info(f"📄 文档路径: {settings.files.document_path}")
        logger.info(f"🎭 默认翻译模式ID: {settings.processing.translation_mode}")
        logger.info(f"📁 输出目录: {settings.files.output_base_dir}")

        # --- 2. 获取用户选择 ---
        # 检查是否在交互环境中
        is_interactive = os.isatty(0)  # 检查 stdin 是否连接到终端

        if is_interactive:
            selected_mode = get_mode_selection(modes)
            get_user_strategy(settings)
        else:
            # 非交互模式：使用默认值
            logger.info("🔄 非交互模式，使用默认配置")
            selected_mode = modes.get(settings.processing.translation_mode, modes["1"])
            logger.info(f"✅ 使用翻译模式: {selected_mode.name}")

        # --- 3. 组合最终配置 ---
        # 使用来自 UI 的策略更新 settings.document，使其成为文档处理的单一事实来源
        # 这样，所有下游函数都可以通过 settings 对象访问到最终的、有效的配置

        
        # 现在只包含翻译模式相关信息
        translation_mode_config = selected_mode.model_dump()

        # --- 4. 统一处理流程 ---
        # 传入更新后的 settings 对象
        process_document_flow(settings, translation_mode_config)
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
        import traceback
        logger.critical(traceback.format_exc())
        sys.exit(1)
    finally:
        logger.info("系统关闭。")


if __name__ == "__main__":
    main()