from __future__ import annotations

import os
from typing import Dict, TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import TranslationMode

from .config import Settings

def get_user_strategy(file_path: str, settings: Settings) -> Dict[str, Any]:
    """
    交互式配置向导：根据文件类型和现有配置获取处理策略。
    
    现在会优先使用 settings 中的值，仅在缺失时才进行交互式询问。
    """
    ext = os.path.splitext(file_path)[1].lower()
    
    # 初始化默认策略
    strategy = {
        "use_vision_mode": None,
        "margin_top": settings.margin_top if settings.margin_top is not None else 0.08,
        "margin_bottom": settings.margin_bottom if settings.margin_bottom is not None else 0.05,
        "margin_left": settings.margin_left if settings.margin_left is not None else 0.0,
        "margin_right": settings.margin_right if settings.margin_right is not None else 0.0,
        "custom_toc_path": None,
        "retain_original": settings.retain_original if settings.retain_original is not None else True,
    }
    
    print("\n" + "="*60)
    print(f"🛠️  STRATEGY SETUP (项目策略配置)")
    print(f"   Target File: {os.path.basename(file_path)}")
    print("="*60)
    
    # ==========================================
    # 1. 章节目录 (TOC) 配置
    # ==========================================
    print("\n[1/4] 📚 Table of Contents (章节目录)")
    
    if ext == '.pdf':
        # 优先使用 .env 中的配置
        if settings.custom_toc_path and settings.custom_toc_path.exists():
            strategy["custom_toc_path"] = str(settings.custom_toc_path)
            print(f"      ✅ Found in settings: {os.path.basename(strategy['custom_toc_path'])}")
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
                        strategy["custom_toc_path"] = path
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
    print("\n[2/4] 👁️  Vision Mode (视觉/图片模式)")
    if ext == '.pdf':    
        print("      Auto  = Let code detect (Recommended for most files)")
        print("      Force = Force ENABLE (Best for scans, complex layouts)")
        print("      Off   = Force DISABLE (Only use text extraction)")
        v_choice = input("      Selection (a/f/o) [a]: ").strip().lower()
    
        if v_choice == 'f':
            strategy["use_vision_mode"] = True
            print("      🔵 Mode: FORCED VISION (Slower but more accurate)")
        elif v_choice == 'o':
            strategy["use_vision_mode"] = False
            print("      🔵 Mode: TEXT ONLY (Fast)")
        else:
            # strategy["use_vision_mode"] stays None
            print("      🔵 Mode: AUTO DETECT")
    
    if ext != '.pdf':
        print("      (Skipping Vision mode setup for non-PDF files)")

    # ==========================================
    # 3. 裁切/边距配置 (仅 PDF)
    # ==========================================
    print("\n[3/4] ✂️  Image Cropping (Remove Headers/Footers)")

    if ext != '.pdf' or strategy["use_vision_mode"] is False:
        print("      Skipped (Vision mode disabled or non-PDF file).")
    else:
        # 優先使用 .env 中的配置
        if all(val is not None for val in [settings.margin_top, settings.margin_bottom, settings.margin_left, settings.margin_right]):
            strategy["margin_top"] = settings.margin_top
            strategy["margin_bottom"] = settings.margin_bottom
            strategy["margin_left"] = settings.margin_left
            strategy["margin_right"] = settings.margin_right
            print(f"      ✅ Found in settings: Top={strategy['margin_top']*100:.1f}%, Bottom={strategy['margin_bottom']*100:.1f}%, Left={strategy['margin_left']*100:.1f}%, Right={strategy['margin_right']*100:.1f}%")
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
                            strategy.update({"margin_top": t, "margin_bottom": b, "margin_left": l, "margin_right": r})
                            print(f"      🔵 Manual Crop: T={t*100:.1f}%, B={b*100:.1f}%, L={l*100:.1f}%, R={r*100:.1f}%")
                        else:
                            print("      ⚠️ Values out of range (0-1). Using Defaults.")
                    else:
                        print("      ⚠️ Invalid format (expected 4 values). Using Defaults.")
                except ValueError:
                    print("      ⚠️ Invalid format. Using Defaults.")
            elif m_input in ("0", "0,0,0,0"):
                strategy.update({"margin_top": 0.0, "margin_bottom": 0.0, "margin_left": 0.0, "margin_right": 0.0})
                print("      🔵 Cropping: DISABLED")
            else:
                print("      🔵 Cropping: AUTO DEFAULTS")

    # ==========================================
    # 4. 保留原文配置
    # ==========================================
    print("\n[4/4] 📝 Retain Original Text (保留原文)")

    if settings.retain_original is not None:
        strategy["retain_original"] = settings.retain_original
        print(f"      ✅ Found in settings: {'是' if strategy['retain_original'] else '否'}")
        print("      (Skipping interactive retain original setup)")
    else:
        retain_original_choice = input("      是否在输出中保留原文? (y/n, 默认 n): ").strip().lower()
        strategy["retain_original"] = (retain_original_choice == 'y')
        print(f"      ✅ 保留原文设置: {'是' if strategy['retain_original'] else '否'}")

    print("="*60 + "\n")
    return strategy

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
