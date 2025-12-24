#!/usr/bin/env python3
"""
Google Gemini 模型能力检查脚本。

该脚本查询 Google Gemini API 中所有可用的模型，并详细列出它们的能力，
例如支持的输入类型、最大上下文窗口和关键特性。这有助于用户为 PDF 翻译项目选择最合适的模型。
"""
import os
import google.generativeai as genai
from dotenv import load_dotenv

# --- 配置 --- 
# 尝试从 .env 文件加载 API Key
load_dotenv()
API_KEY = os.getenv("GEMINI_API_KEY")

# 如果 API Key 不存在，则打印错误并退出
if not API_KEY:
    print("❌ 错误: 未找到 API 密钥。")
    print("   请在项目根目录创建 .env 文件，并添加 GEMINI_API_KEY=\"您的API密钥\"。")
    exit(1)

# 配置 Gemini API
genai.configure(api_key=API_KEY)

# --- 模型能力分析函数 (更健壮) ---
def analyze_model_capabilities(model: genai.GenerativeModel) -> tuple[str, str]:
    """
    分析模型的支持生成方法，推断其能力。这比简单地基于名称的启发式方法更健壮。
    """
    input_type = "纯文本"  # 默认输入类型
    key_features = []         # 关键特性列表

    # 检查是否支持 generateContent 方法，这是我们关注的核心
    if 'generateContent' in model.supported_generation_methods:
        # 对于通用的 Gemini 多模态模型（如 gemini-1.5-pro, gemini-2.5-pro, gemini-flash），
        # 它们天生就支持多种输入类型，通常名称中会包含版本或指示词。
        if 'gemini' in model.name and (
            'vision' in model.name or 
            '1.5' in model.name or 
            '2.5' in model.name or 
            'flash' in model.name
        ):
            input_type = "文本, 图像, 音频, 视频 (多模态)"
            key_features.append("大上下文")
        # 特定的视觉模型（旧版本或专用版本）
        elif 'vision' in model.name:
            input_type = "文本, 图像"

    # 如果是 Attributed Question Answering (AQA) 模型
    if 'aqa' in model.name:
        key_features.append("事实核查/归因")

    # 返回输入类型和关键特性
    return input_type, ", ".join(key_features) if key_features else "标准功能"


# --- 主脚本 --- 
# 存储 Markdown 输出的列表
markdown_output_lines = []

markdown_output_lines.append("# Gemini 模型能力对比")
markdown_output_lines.append("\n该表格列出了所有支持内容生成的 Gemini 模型及其主要能力，帮助您选择合适的模型。\n")

print("🔍 正在查询可用模型及其能力列表...") # 依然在终端打印进度

try:
    # --- 表格头部 (Markdown 格式) ---
    markdown_output_lines.append("\n| 模型名称                       | 输入类型                       | 输入 Tokens    | 输出 Tokens    | 主要特性     |")
    markdown_output_lines.append("| :----------------------------- | :----------------------------- | -------------: | -------------: | :----------- |")

    # --- 模型迭代与分析 ---
    # 获取所有模型并按名称排序
    all_models = sorted(genai.list_models(), key=lambda m: m.name)
    
    for m in all_models:
        # 只处理支持 generateContent 的模型
        if 'generateContent' in m.supported_generation_methods:
            
            # 移除模型名称前缀 "models/"
            model_name = m.name.replace("models/", "")
            
            # 使用我们更健壮的分析函数
            input_type, features = analyze_model_capabilities(m) # 传递完整的模型对象
            
            # 格式化 Token 限制，提高可读性
            input_tokens = f"{m.input_token_limit:,}" if m.input_token_limit else "N/A"
            output_tokens = f"{m.output_token_limit:,}" if m.output_token_limit else "N/A"
            
            # --- 添加表格行 (Markdown 格式) ---
            markdown_output_lines.append(f"| {model_name:<30} | {input_type:<30} | {input_tokens:>15} | {output_tokens:>15} | {features:<12} |")

    markdown_output_lines.append("\n✅ 查询完成。")
    markdown_output_lines.append("   - '输入类型' 指示模型可以处理的数据类型，例如纯文本或多模态（文本、图像等）。")
    markdown_output_lines.append("   - '输入 Tokens' 是模型能够接受的最大上下文长度。")
    markdown_output_lines.append("   - '输出 Tokens' 是模型能够生成的最大响应长度。")
    markdown_output_lines.append("   - '主要特性' 突出显示了模型的额外功能，例如'大上下文'或'事实核查/归因'。")
    markdown_output_lines.append("   - **使用说明**: 请从上述表格中选择一个合适的模型名称，并更新您项目根目录下的 `.env` 文件中的 `GEMINI_MODEL` 变量。")

except Exception as e:
    error_message = f"\n❌ 查询模型时发生错误: {e}\n   请检查您的 API Key 是否正确，以及网络连接是否正常。"
    print(error_message) # 错误信息依然打印到终端
    markdown_output_lines.append(error_message)

# 最终将所有 Markdown 内容打印到标准输出
print("\n".join(markdown_output_lines))

