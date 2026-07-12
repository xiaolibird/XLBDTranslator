#!/usr/bin/env python3
"""
Google Gemini 模型能力检查脚本。

该脚本查询 Google Gemini API 中所有可用的模型，并详细列出它们的能力，
例如支持的输入类型、最大上下文窗口和关键特性。这有助于用户为 PDF 翻译项目选择最合适的模型。

API Key 读取顺序：环境变量 GEMINI_API_KEY -> config/config.env 的 API__GEMINI_API_KEY。
"""
import os
from pathlib import Path

from google import genai


def load_api_key() -> str | None:
    """读取 API Key：优先环境变量，其次 config/config.env（不打印任何密钥内容）"""
    key = os.getenv("GEMINI_API_KEY")
    if key:
        return key
    env_path = Path("config/config.env")
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("API__GEMINI_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


API_KEY = load_api_key()
if not API_KEY:
    print("❌ 错误: 未找到 API 密钥。")
    print('   请在 config/config.env 中设置 API__GEMINI_API_KEY="您的API密钥"，')
    print("   或设置环境变量 GEMINI_API_KEY。")
    exit(1)

client = genai.Client(api_key=API_KEY)


def analyze_model_capabilities(model) -> tuple[str, str]:
    """根据模型元数据推断输入类型与关键特性。"""
    input_type = "纯文本"
    key_features = []

    name = (model.name or "").lower()
    if "gemini" in name:
        input_type = "文本, 图像, 音频, 视频 (多模态)"
        key_features.append("大上下文")
    if "aqa" in name:
        key_features.append("事实核查/归因")

    return input_type, ", ".join(key_features) if key_features else "标准功能"


markdown_output_lines = []
markdown_output_lines.append("# Gemini 模型能力对比")
markdown_output_lines.append("\n该表格列出了所有支持内容生成的 Gemini 模型及其主要能力，帮助您选择合适的模型。\n")

print("🔍 正在查询可用模型及其能力列表...")

try:
    markdown_output_lines.append("\n| 模型名称                       | 输入类型                       | 输入 Tokens    | 输出 Tokens    | 主要特性     |")
    markdown_output_lines.append("| :----------------------------- | :----------------------------- | -------------: | -------------: | :----------- |")

    all_models = sorted(client.models.list(), key=lambda m: m.name or "")

    for m in all_models:
        actions = getattr(m, "supported_actions", None) or []
        if actions and "generateContent" not in actions:
            continue

        model_name = (m.name or "").replace("models/", "")
        input_type, features = analyze_model_capabilities(m)

        input_tokens = f"{m.input_token_limit:,}" if getattr(m, "input_token_limit", None) else "N/A"
        output_tokens = f"{m.output_token_limit:,}" if getattr(m, "output_token_limit", None) else "N/A"

        markdown_output_lines.append(
            f"| {model_name:<30} | {input_type:<30} | {input_tokens:>15} | {output_tokens:>15} | {features:<12} |"
        )

    markdown_output_lines.append("\n✅ 查询完成。")
    markdown_output_lines.append("   - '输入类型' 指示模型可以处理的数据类型，例如纯文本或多模态（文本、图像等）。")
    markdown_output_lines.append("   - '输入 Tokens' 是模型能够接受的最大上下文长度。")
    markdown_output_lines.append("   - '输出 Tokens' 是模型能够生成的最大响应长度。")
    markdown_output_lines.append("   - '主要特性' 突出显示了模型的额外功能，例如'大上下文'或'事实核查/归因'。")
    markdown_output_lines.append("   - **使用说明**: 请从上述表格中选择一个合适的模型名称，并更新 `config/config.env` 中的 `API__GEMINI_MODEL` 变量。")

except Exception as e:
    error_message = f"\n❌ 查询模型时发生错误: {e}\n   请检查您的 API Key 是否正确，以及网络连接是否正常。"
    print(error_message)
    markdown_output_lines.append(error_message)

print("\n".join(markdown_output_lines))
