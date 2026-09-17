from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Dict, TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.core.schema import TranslationMode

from src.core.schema import Settings, TranslationMode
from src.core.exceptions import ConfigError, MissingConfigError


def get_default_modes() -> Dict[str, TranslationMode]:
    """返回默认的翻译模式（通用学术翻译）"""
    default_modes_data = {
        "999": {
            "name": "Academic Researcher",
            "role_desc": "你是一位具有计算机科学和人工智能背景的研究人员，专注于数据挖掘和大模型应用。你对AI技术、自然语言处理、文本分析有深入理解。",
            "style": "保持学术严谨性，用中文表达时追求精准和专业，避免翻译腔。特别关注技术术语、学术概念的准确性。",
            "context_len": "high"
        }
    }
    return {k: TranslationMode(**v) for k, v in default_modes_data.items()}


def load_modes_config(config_path: Path) -> Dict[str, TranslationMode]:
    """加载翻译模式配置"""
    if not config_path.exists():
        print(f"Modes config not found at {config_path}. Creating default one.")
        default_modes = get_default_modes()
        default_modes_dict = {k: v.model_dump() for k, v in default_modes.items()}

        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(default_modes_dict, f, ensure_ascii=False, indent=2)
            return default_modes
        except Exception as e:
            print(f"Failed to create default modes config: {e}")
            return get_default_modes()
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            modes_data = json.load(f)
        
        validated_modes = {}
        for mode_id, mode_config in modes_data.items():
            try:
                validated_modes[mode_id] = TranslationMode(**mode_config)
            except Exception as e:
                print(f"Skipping invalid mode configuration for mode {mode_id}: {e}")
                continue
        
        if not validated_modes:
            print("No valid translation modes found. Using defaults.")
            return get_default_modes()
            
        return validated_modes
        
    except Exception as e:
        print(f"Failed to load modes config: {e}. Using defaults.")
        return get_default_modes()


# ========================================================================
# UI 交互函数
# ========================================================================

def get_user_strategy(settings: Settings):
    """
    交互式配置向导：根据文件类型和现有配置获取处理策略。
    
    现在会优先使用 settings 中的值，仅在缺失时才进行交互式询问。
    注意：TOC 和页面范围已改为使用默认值，不再询问。
    """
    file_path = settings.files.document_path
    ext = os.path.splitext(file_path)[1].lower()
    
    print("\n" + "="*60)
    print(f"🛠️  STRATEGY SETUP (项目策略配置)")
    print(f"   Target File: {os.path.basename(file_path)}")
    print("="*60)
    
    # ==========================================
    # 1. 章节目录 (TOC) 配置 - 跳过，使用默认值
    # ==========================================
    print("\n[1/5] 📚 Table of Contents (章节目录)")
    
    if ext == '.pdf':
        if settings.document.custom_toc_path and settings.document.custom_toc_path.exists():
            print(f"      ✅ Using custom TOC: {os.path.basename(str(settings.document.custom_toc_path))}")
        else:
            print("      ✅ Using PDF's built-in TOC (or per-page segmentation)")
    else:
        # EPUB 逻辑
        print(f"      ✅ Detected {ext.upper()} format. Using internal structure.")

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
    # 3. 页面范围配置 (仅 PDF) - 跳过，使用默认值
    # ==========================================
    print("\n[3/5] 📄 Page Range (页面范围)")

    if ext == '.pdf':
        if settings.document.page_range:
            print(f"      ✅ Using specified range: Pages {settings.document.page_range[0]} to {settings.document.page_range[1]}")
        else:
            print("      ✅ Translating all pages (no range specified)")
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
    """
    交互式地从用户那里获取翻译模式选择。
    
    Args:
        modes: 可用的翻译模式字典 {mode_id: TranslationMode}
    
    Returns:
        TranslationMode: 用户选择的翻译模式对象
    
    Raises:
        ValueError: 如果 modes 为空或无效
    """
    if not modes:
        raise ValueError("❌ 没有可用的翻译模式！请检查 modes.json 配置文件。")
    
    print("\n🎭 请选择翻译模式 (Personas):")

    for key, mode_obj in modes.items():
        print(f"  [{key}] {mode_obj.name}")  # 使用 .name 访问属性
    
    choice = input("\n请输入数字 (默认 1): ").strip()
    if not choice or choice not in modes:
        choice = "1"
        if choice not in modes:
            # 如果默认值 "1" 也不存在，使用第一个可用的模式
            choice = list(modes.keys())[0]
            print(f"⚠️  模式 '1' 不存在，使用第一个可用模式。")
    
    selected_mode_obj = modes[choice]
    print(f"✅ 已选择: {selected_mode_obj.name}\n")
    return selected_mode_obj
