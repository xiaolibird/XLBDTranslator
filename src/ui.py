from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .core.schema import TranslationMode

from .core.schema import Settings

def get_user_strategy(settings: Settings):
    """
    交互式配置向导：根据文件类型和现有配置获取处理策略。
    
    现在会优先使用 settings 中的值，仅在缺失时才进行交互式询问。
    """
    file_path = settings.files.document_path
    ext = os.path.splitext(file_path)[1].lower()
    
    print("\n" + "="*60)
    print(f"🛠️  STRATEGY SETUP (项目策略配置)")
    print(f"   Target File: {os.path.basename(file_path)}")
    print("="*60)
    
    # ==========================================
    # 1. 章节目录 (TOC) 配置
    # ==========================================
    print("\n[1/5] 📚 Table of Contents (章节目录)")
    
    if ext == '.pdf':
        # 优先使用 .env 中的配置
        if settings.document.custom_toc_path and settings.document.custom_toc_path.exists():
            print(f"      ✅ Found in settings: {os.path.basename(str(settings.document.custom_toc_path))}")
            print("      (Skipping interactive TOC setup)")
        else:
            # 如果配置中没有，再进行交互式询问
            print("      PDFs often lack a readable TOC. Do you have a CSV mapping?")
            print("      (Format: 'Page,Title,Level')")
            
            use_toc = input("      Load custom TOC CSV? (y/n) [n]: ").strip().lower()
            if use_toc == 'y':
                while True:
                    path = input("      Enter CSV path: ").strip().strip("'").strip('"')
                    if os.path.exists(path):
                        settings.document.custom_toc_path = Path(path)
                        print(f"      ✅ Loaded: {os.path.basename(path)}")
                        break
                    else:
                        print("      ❌ File not found. Please try again.")
    else:
        # EPUB 逻辑
        print(f"      ✅ Detected {ext.upper()} format. Using internal structure.")
        print("      (Skipping custom TOC setup)")

    # ==========================================
    # 2. Vision 模式配置 (仅 PDF)
    # ==========================================
    # 对于非 PDF 文件，Vision 和 Cropping 步骤将被跳过，但 Retain Original Text 仍适用。
    print("\n[2/5] 👁️  Vision Mode (视觉/图片模式)")
    if ext == '.pdf':    
        # 优先使用 .env 中的配置
        if settings.processing.use_vision_mode is not None:
            status = "已启用" if settings.processing.use_vision_mode else "已禁用"
            print(f"      ✅ Found in settings: {status}")
            print("      (Skipping interactive Vision mode setup)")
        else:
            print("      Auto  = Let code detect (Recommended for most files)")
            print("      Force = Force ENABLE (Best for scans, complex layouts)")
            print("      Off   = Force DISABLE (Only use text extraction)")
            v_choice = input("      Selection (a/f/o) [a]: ").strip().lower()
        
            if v_choice == 'f':
                settings.processing.use_vision_mode = True
                print("      🔵 Mode: FORCED VISION (Slower but more accurate)")
            elif v_choice == 'o':
                settings.processing.use_vision_mode = False
                print("      🔵 Mode: TEXT ONLY (Fast)")
            else:
                settings.processing.use_vision_mode = None # Directly update settings for Auto Detect
                print("      🔵 Mode: AUTO DETECT")
    
    if ext != '.pdf':
        print("      (Skipping Vision mode setup for non-PDF files)")

    # ==========================================
    # 3. 页面范围配置 (仅 PDF)
    # ==========================================
    print("\n[3/5] 📄 Page Range (页面范围)")

    if ext == '.pdf':
        # 优先使用 .env 中的配置
        if settings.document.page_range:
            print(f"      ✅ Found in settings: Pages {settings.document.page_range[0]} to {settings.document.page_range[1]}")
            print("      (Skipping interactive page range setup)")
        else:
            # 如果配置中没有，再进行交互式询问
            print("      指定翻译页面范围 (例如, '10,50' 或 '10-50').")
            print("      直接按 ENTER 键则翻译整个文档。")
            
            pr_input = input("      页面范围: ").strip()
            
            if pr_input:
                try:
                    # 支持逗号和短横线作为分隔符
                    parts = [p.strip() for p in pr_input.replace('-', ',').split(',')]
                    if len(parts) == 2:
                        start, end = map(int, parts)
                        if start > 0 and end >= start:
                            # 假设用户输入的是 1-based，Pydantic 模型内部处理
                            settings.document.page_range = (start, end)
                            print(f"      🔵 范围设定: Pages {start} to {end}")
                        else:
                            print("      ⚠️ 无效范围。将翻译整个文档。")
                            settings.document.page_range = None
                    else:
                        print("      ⚠️ 格式错误。将翻译整个文档。")
                        settings.document.page_range = None
                except ValueError:
                    print("      ⚠️ 格式错误。将翻译整个文档。")
                    settings.document.page_range = None
            else:
                print("      🔵 将翻译整个文档。")
    else:
        print("      (Skipping page range setup for non-PDF files).")


    # ==========================================
    # 4. 裁切/边距配置 (仅 PDF)
    # ==========================================
    print("\n[4/5] ✂️  Image Cropping (Remove Headers/Footers)")

    if ext != '.pdf' or settings.processing.use_vision_mode is False:
        print("      Skipped (Vision mode disabled or non-PDF file).")
    else:
        # 優先使用 .env 中的配置
        if all(val is not None for val in [settings.document.margin_top, settings.document.margin_bottom, settings.document.margin_left, settings.document.margin_right]):
            print(f"      ✅ Found in settings: Top={settings.document.margin_top*100:.1f}%, Bottom={settings.document.margin_bottom*100:.1f}%, Left={settings.document.margin_left*100:.1f}%, Right={settings.document.margin_right*100:.1f}%")
            print("      (Skipping interactive margin setup)")
        else:
            # 如果配置中没有，再进行交互式询问
            print("      CRITICAL for Vision to avoid translating running titles.")
            print("      Format: 'top,bottom,left,right' ratio (0.0 to 1.0)")
            print("      Example: '0.1,0.05,0.05,0.05' (Crops all sides)")
            print("      Enter '0' to disable all cropping.")
            print("      Press ENTER to use Defaults (Top~8%, Bottom~5%, L/R 0%)")
            
            m_input = input("      Margins: ").strip()
            
            if "," in m_input:
                try:
                    parts = [p.strip() for p in m_input.split(",")]
                    if len(parts) == 4:
                        t, b, l, r = map(float, parts)
                        if all(0 <= val < 1.0 for val in [t, b, l, r]):
                            settings.document.margin_top = t
                            settings.document.margin_bottom = b
                            settings.document.margin_left = l
                            settings.document.margin_right = r
                            print(f"      🔵 Manual Crop: T={t*100:.1f}%, B={b*100:.1f}%, L={l*100:.1f}%, R={r*100:.1f}%")
                        else:
                            print("      ⚠️ Values out of range (0-1). Using Defaults.")
                    else:
                        print("      ⚠️ Invalid format (expected 4 values). Using Defaults.")
                except ValueError:
                    print("      ⚠️ Invalid format. Using Defaults.")
            elif m_input in ("0", "0,0,0,0"):
                settings.document.margin_top = 0.0
                settings.document.margin_bottom = 0.0
                settings.document.margin_left = 0.0
                settings.document.margin_right = 0.0
                print("      🔵 Cropping: DISABLED")
            else:
                print("      🔵 Cropping: AUTO DEFAULTS")

    # ==========================================
    # 5. 保留原文配置
    # ==========================================
    print("\n[5/5] 📝 Retain Original Text (保留原文)")

    if settings.processing.retain_original is not None:
        print(f"      ✅ Found in settings: {'是' if settings.processing.retain_original else '否'}")
        print("      (Skipping interactive retain original setup)")
    else:
        retain_original_choice = input("      是否在输出中保留原文? (y/n, 默认 n): ").strip().lower()
        settings.processing.retain_original = (retain_original_choice == 'y')
        print(f"      ✅ 保留原文设置: {'是' if settings.processing.retain_original else '否'}")

    print("="*60 + "\n")
    return

def get_mode_selection(modes: Dict[str, 'TranslationMode']) -> 'TranslationMode':
    """交互式地从用户那里获取翻译模式选择。"""
    print("\n🎭 请选择翻译模式 (Personas):")

    for key, mode_obj in modes.items():
        print(f"  [{key}] {mode_obj.name}")  # 使用 .name 访问属性
    
    choice = input("\n请输入数字 (默认 1): ").strip()
    if not choice or choice not in modes:
        choice = "1"
    
    selected_mode = modes[choice]
    print(f"✅ 已选择: {selected_mode.name}\n") # 使用 .name 访问属性
    return selected_mode
