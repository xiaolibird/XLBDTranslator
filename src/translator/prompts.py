"""
翻译提示模板管理
"""
from pathlib import Path


def load_prompt_template(template_name: str) -> str:
    """从文件加载 Prompt 模板"""
    # 修正路径：从项目根目录的 config/prompts/ 目录加载
    path = Path(__file__).parent.parent.parent / "config" / "prompts" / template_name
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        # 返回基本模板作为兜底
        print(f"Warning: Prompt template not found: {path}, using fallback")
        return "Translate the following text: {input_json}"


# 加载所有模板
SYSTEM_INSTRUCTION = load_prompt_template("system_instruction.md")
TEXT_TRANSLATION_PROMPT = load_prompt_template("text_translation_prompt.md")
VISION_TRANSLATION_PROMPT = load_prompt_template("vision_translation_prompt.md")


def format_text_prompt(role: str, style: str, role_desc: str, context: str, input_json: str) -> str:
    """格式化文本翻译提示"""
    return TEXT_TRANSLATION_PROMPT.format(
        role=role,
        style=style,
        role_desc=role_desc,
        context=context,
        input_json=input_json
    )


def format_vision_prompt(role: str, style: str, role_desc: str, context: str) -> str:
    """格式化视觉翻译提示"""
    return VISION_TRANSLATION_PROMPT.format(
        role=role,
        style=style,
        role_desc=role_desc,
        context=context
    )


def format_title_prompt(text_list: str, style: str) -> str:
    """格式化标题翻译提示"""
    return f"""You are a professional translator. Translate the following list of document headers/titles into Chinese.

Your style: {style}

Input JSON: {text_list}

**You MUST OBEY THE FOLLOWING RULE!!!!!!**
Output JSON format: A flat JSON Dictionary where keys are the source text and values are the translation.
Example: {{"Chapter 1": "第一章", "Index": "索引"}}

Return ONLY the JSON object."""
