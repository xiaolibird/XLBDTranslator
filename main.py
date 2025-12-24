#!/usr/bin/env python3
"""
主程序入口点。

该脚本负责：
1. 初始化配置和日志。
2. 获取用户输入（翻译模式、文档处理策略）。
3. 调用文档处理流水线（pipeline）生成结构化文本。
4. 启动翻译循环，处理文本并保存结果。
5. 统一的错误处理和程序退出逻辑。
"""
import os, time, json
import sys
import traceback
from pathlib import Path
from tqdm import tqdm
from dataclasses import asdict
from typing import List, Dict, Any

# 导入自定义模块
from src.config import Settings, modes
from src.errors import TranslationError
from src.logging_config import setup_logging
from src.ui import get_mode_selection, get_user_strategy
from src.file_io import get_last_checkpoint_id, create_output_directory, recover_context_from_file, is_likely_chinese
from src.translator import GEMINITranslator
from src.pipeline import ContentSegment, compile_structure, MarkdownRenderer


# 初始化日志记录器
logger = setup_logging()

def main():
    """主函数，协调整个翻译流程。"""
    try:
        logger.info("=" * 60)
        logger.info("📚 文档翻译系统启动")
        logger.info("=" * 60)
        
        # --- 1. 加载配置 ---
        # Settings() 会自动从 .env 文件和环境变量中加载配置
        settings = Settings()
        logger.info(f"📄 文档路径: {settings.document_path}")
        logger.info(f"🎭 默认翻译模式ID: {settings.translation_mode}")
        logger.info(f"📁 输出目录: {settings.output_base_dir}")
        
        # --- 2. 获取用户选择 ---
        selected_mode = get_mode_selection(modes)
        user_strategy = get_user_strategy(str(settings.document_path), settings)

        # --- 3. 组合最终配置 ---
        project_config = {
            **selected_mode.model_dump(),  # 使用 Pydantic V2 的 model_dump()
            **user_strategy
        }
        
        # --- 4. 统一处理流程 ---
        process_document_flow(settings, project_config)
        
        logger.info("=" * 60)
        logger.info("🎉 翻译任务成功完成！")
        logger.info("=" * 60)
        
    except TranslationError as e:
        logger.error(f"❌ 翻译流程出现已知错误: {e}", exc_info=True)
        logger.error(f"💡 建议: {e.suggestion}" if e.suggestion else "请检查上述错误详情。")
        sys.exit(1)
    except Exception as e:
        logger.critical(f"💥 发生未预期的严重错误: {e}", exc_info=True)
        logger.critical(traceback.format_exc())
        sys.exit(1)
    finally:
        logger.info("系统关闭。")

def process_document_flow(settings: Settings, project_config: dict):
    """
    协调文档从解析到翻译的整个流程。
    适配 ContentSegment 对象架构。
    """
    file_path = str(settings.document_path)
    logger.info(f"🚀 开始处理文档: {os.path.basename(file_path)}")
    logger.info(f"   - 翻译模式: {project_config['name']}")
    
    # --- 准备工作区 ---
    project_dir = create_output_directory(
        file_path, 
        project_config['name'],
        settings.output_base_dir
    )
    cache_path = os.path.join(project_dir, "structure_map.json")
    final_md_path = os.path.join(project_dir, "Full_Book.md")
    
    # --- 编译文档结构 (如果缓存不存在) ---
    all_segments: list[ContentSegment] = [] # 类型提示更新
    
    if settings.enable_cache and os.path.exists(cache_path):
        logger.info("📦 发现结构缓存，正在加载...")
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
                # 【关键修改】: 将字典列表转换回 ContentSegment 对象列表
                all_segments = [ContentSegment(**item) for item in raw_data]
            logger.info(f"   ✅ 成功加载 {len(all_segments)} 个文本片段。")
        except (json.JSONDecodeError, IOError, TypeError) as e:
            logger.warning(f"   ⚠️ 缓存文件损坏或格式不匹配: {e}。将重新编译文档。")
            all_segments = []

    if not all_segments:
        logger.info("⚙️ 未找到缓存或缓存已禁用，开始编译文档结构...")
        # compile_structure 现在直接返回 List[ContentSegment]
        all_segments = compile_structure(
            file_path=file_path,
            cache_path=cache_path,
            settings=settings,
            project_config=project_config
        )
    
    if not all_segments:
        raise TranslationError("文档编译后未生成任何文本片段，无法继续。")
    
    # --- 初始化输出文件 ---
    if not os.path.exists(final_md_path):
        logger.info(f"📝 创建新的输出文件: {final_md_path}")
        with open(final_md_path, "w", encoding="utf-8") as f:
            f.write(f"# 原文: {os.path.basename(file_path)}\n")
            f.write(f"> 使用 **{project_config['name']}** 模式翻译\n\n---\n\n")
    
    # --- 启动翻译循环 ---
    translator = GEMINITranslator(settings)
    pre_translate_chapter_titles(all_segments, translator, project_config)

    try:
        logger.info("💾 正在更新结构缓存（保存已翻译的章节标题）...")
        # 将对象列表转回字典列表
        data_to_save = [asdict(seg) for seg in all_segments]
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(data_to_save, f, ensure_ascii=False, indent=2)
        logger.info("✅ 缓存更新成功。")
    except Exception as e:
        logger.warning(f"⚠️ 无法更新缓存，但不影响后续流程: {e}")

    run_translation_loop(all_segments, 
        final_md_path, 
        translator,
        project_config)

def run_translation_loop(
    all_segments: list[ContentSegment], # 类型提示更新
    output_file: str,
    translator: GEMINITranslator,
    project_config: dict
):
    """
    执行翻译主循环。
    适配 ContentSegment 对象属性访问和新的 MarkdownRenderer。
    """
    # --- 0. 实例化渲染器 ---
    renderer = MarkdownRenderer(translator.settings)

    # --- 1. 断点续传 --- 
    last_id = get_last_checkpoint_id(output_file)
    
    # 【关键修改】: 使用 .segment_id 访问属性
    segments_to_do = [s for s in all_segments if s.segment_id > last_id]
    
    if not segments_to_do:
        logger.info("🎉 所有片段均已翻译完成！")
        return

    logger.info(f"🔄 从片段 ID {last_id + 1} 继续，剩余 {len(segments_to_do)} 个片段待处理。")
    
    # --- 2. 恢复上下文 ---
    context_length = translator.settings.max_context_length
    context_buffer = recover_context_from_file(output_file, context_length)

    # --- 3. 分批处理 ---
    batch_size = translator.settings.batch_size
    progress_bar = tqdm(range(0, len(segments_to_do), batch_size), desc="Translating Batches")
    
    for i in progress_bar:
        batch = segments_to_do[i : i + batch_size]
        
        # 【关键修改】: 使用 .segment_id
        progress_bar.set_postfix({
            "Batch": f"{i // batch_size + 1}/{len(progress_bar)}",
            "IDs": f"{batch[0].segment_id}-{batch[-1].segment_id}"
        })
        
        try:
            # --- 调用翻译 ---
            # 这里的 translate_batch 内部需要适配：它会接收 List[ContentSegment]
            # 如果你的 translator 还没改，可能需要在这里提取 batch_texts = [s.original_text for s in batch]
            translations = translator.translate_batch(batch, project_config, context=context_buffer)
            
            # --- 健壮性检查 ---
            if len(translations) != len(batch):
                logger.error(f"      ❌ 批次 {i // batch_size + 1} 数量不匹配 (Req: {len(batch)}, Res: {len(translations)})")
                continue

            # --- 实时写入 ---
            with open(output_file, "a", encoding="utf-8") as f:
                for idx, trans_text in enumerate(translations):
                    seg = batch[idx]
                    
                    # 【关键修改】: 将翻译结果填入对象
                    seg.translated_text = trans_text
                    
                    # 【关键修改】: 调用新的渲染器类
                    # 注意：Metadata (Chapter/Page) 已经在 seg 对象里了，渲染器会自动处理
                    markdown_chunk = renderer.render_segment(seg)
                    f.write(markdown_chunk)
                f.flush()
            
            # --- 更新上下文 ---
            if translations:
                full_translation_text = " ".join(t.replace('\n', ' ') for t in translations)
                context_buffer = full_translation_text[-context_length:]

        except Exception as e: # 捕获更宽泛的异常以防对象属性错误
            logger.error(f"      ❌ 批次处理失败: {e}", exc_info=True)
            continue
        
        # --- 速率控制 ---
        time.sleep(translator.settings.rate_limit_delay)

def pre_translate_chapter_titles(all_segments: List[ContentSegment], 
        translator, 
        project_config):
    
    """
    [预处理] 提取所有章节标题，批量翻译，并更新 Segment 对象。
    优化：只处理真正的章节开头 (is_new_chapter=True)。
    """
    logger.info("--- 开始章节标题预翻译 ---")
    
    # 1. 提取标题 (仅针对章节起始点)
    # 【优化点】增加 if seg.is_new_chapter 判断
    # raw_titles = [
    #     seg.chapter_title 
    #     for seg in all_segments 
    #     if seg.is_new_chapter and seg.chapter_title and seg.chapter_title.strip()
    # ]
    raw_titles = []
    for seg in all_segments:
        if seg.is_new_chapter and seg.chapter_title and seg.chapter_title.strip():
            # 【简单检测】如果标题里包含中文字符，大概率是已经翻译过了，跳过
            # 或者你可以根据自己的需求，决定是否要重新翻译
            if is_likely_chinese(seg.chapter_title):
                continue
            raw_titles.append(seg.chapter_title)
    
    # 2. 有序去重
    unique_titles = list(dict.fromkeys(raw_titles))
    
    if not unique_titles:
        logger.info("No new chapter headers found to translate.")
        return

    logger.info(f"Found {len(unique_titles)} unique headers. Translating...")

    # 3. 批量翻译
    translation_map = translator.translate_plain_text_list(unique_titles, project_config)
    
    # 4. 回填结果
    update_count = 0
    for seg in all_segments:
        # 【优化点】只修改作为新章节开头的那个 segment
        if seg.is_new_chapter and seg.chapter_title in translation_map:
            translated = translation_map[seg.chapter_title]
            if translated:
                seg.chapter_title = translated
                update_count += 1
    
    logger.info(f"Updated {update_count} chapter headers.")

if __name__ == "__main__":
    main()