"""
配置加载和管理
"""
import json
from pathlib import Path
from typing import Dict

from .core.schema import TranslationMode
from .core.exceptions import ConfigError, MissingConfigError


def get_default_modes() -> Dict[str, TranslationMode]:
    """返回默认的翻译模式"""
    default_modes_data = {
        "1": {
            "name": "Zizek Expert",
            "role_desc": "你是一位专门研究斯拉沃热·齐泽克、拉康精神分析和黑格尔哲学的顶级学者，同时也是一位酷酷的导师。",
            "style": "学术深度解析，擅长解释黑话和哲学梗，语言通俗幽默。",
            "context_len": "high"
        },
        "2": {
            "name": "Biography Journalist",
            "role_desc": "你是一位拥有深厚历史学背景的资深文学翻译家，精通中文、英文和法文。你擅长翻译人物传记和历史非虚构作品。",
            "style": "流畅自然，拒绝翻译腔。使用地道的中文表达习惯。",
            "context_len": "medium"
        },
        "3": {
            "name": "Sociology Researcher",
            "role_desc": "你是一位拥有学术专家的资深学术翻译家，专精于批判理论、欧洲大陆哲学，以及社会学/文化评论领域。",
            "style": "严谨准确，保持原文学术风格，术语统一。",
            "context_len": "high"
        },
        "4": {
            "name": "AI Data Scientist",
            "role_desc": "你是一位顶尖的大数据科学家和人工智能研究员，同时非常理解脑科学和健康科学。",
            "style": "逻辑严谨，注重信息密度和模式识别。",
            "context_len": "high"
        },
        "5": {
            "name": "Novel Translator",
            "role_desc": "你是一位熟读各种英文世情/耽美/言情小说，精通英译中、日译中的资深翻译家。",
            "style": "情感细腻，注重生活细节和现代汉语表达。",
            "context_len": "low"
        },
        "6": {
            "name": "Nietzsche-ish Interpretive",
            "role_desc": "You are a profound Nietzschean scholar and a master literary translator.",
            "style": "庄重而直击人心，充满哲学隐喻的诗意翻译。",
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


# 全局模式配置
modes = load_modes_config(Path("config/modes.json"))