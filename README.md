# 🔄 XLBD Translator (新老笔电翻译器)

**一个超大文档 AI 翻译引擎。**

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

**XLBD Translator** 采用现代化的状态驱动架构，能够将复杂的 EPUB 和 PDF 文档（包括扫描件）通过高度可定制的“专家模式”（Persona）翻译成流畅、精准的中文，并最终生成带样式的 PDF 和 Markdown 文件。

## ✨ 核心特性

### 🎯 工业级稳定性
- **状态驱动架构**：整个翻译流程由内存中的数据结构驱动，并通过文件持久化。
- **断点续传**：支持意外中断后完美恢复，自动跳过已翻译片段，无需从头开始。
- **原子化保存**：每批次翻译完成后，立即保存进度，最大程度减少数据丢失风险。
- **健壮的错误处理**：内置 API 自动重试和 JSON 修复机制，智能处理网络波动和模型返回错误。

### 🤖 多模态翻译
- **文本模式 (Native)**：针对文本清晰的 PDF/EPUB，直接提取并翻译，速度快。
- **视觉模式 (Vision)**：针对扫描件或复杂排版的文档，自动渲染页面为图片并调用多模态模型进行翻译。
- **智能策略**：支持在交互式会话中选择自动检测、强制开启或强制关闭视觉模式。

### 🎭 专业翻译人格 (Persona)
- **高度可定制**：通过编辑 `config/modes.json`，您可以轻松修改或创建新的专家角色。
- **内置专家模式**：
    - **齐泽克专家**：擅长黑格尔哲学、拉康精神分析的学术翻译。
    - **社会学研究员**：精通批判理论、欧洲大陆哲学。
    - **传记记者**：文学性传记和历史非虚构作品专家。
    - **人工智能专家**： 精通 AI 和科技前沿领域。
    - **小说翻译家**：世情耽美言情小说翻译专家。
    - **尼采阐释者**：哲学隐喻的诗意翻译。

### ⚙️ 高度可配置
- **`.env` 驱动**：所有核心配置均通过 `config/.env` 文件管理，支持非交互式运行。
- **自定义目录 (TOC)**: 支持通过外部 CSV 文件为 PDF 注入章节结构，实现更精准的语义切分。
- **自定义 PDF 样式**: 通过修改 `config/pdf_style.css` 文件，您可以完全控制最终输出 PDF 的字体、边距、颜色等外观。

## 🚀 快速开始

### 1. 环境准备

- 克隆本仓库到本地:
  ```bash
  git clone https://github.com/developer/XLBDTranslator-dev.git
  cd XLBDTranslator-dev
  ```
- 推荐使用 `conda` 创建并激活一个 Python 虚拟环境:
  ```bash
  conda create -n xlbd-translator python=3.10
  conda activate xlbd-translator
  ```

### 2. 安装依赖

- 安装所有必要的 Python 库:
  ```bash
  pip install -r requirements.txt
  ```

### 3. 配置

核心配置均在 `config/.env` 文件中完成。请根据以下说明修改该文件：

1.  **API 密钥 (必需)**:
    ```dotenv
    # Google AI Studio 的 API Key
    API__GEMINI_API_KEY="YOUR_API_KEY_HERE"
    ```

2.  **文档路径 (必需)**:
    ```dotenv
    # 待翻译的 EPUB 或 PDF 文件的完整路径
    FILES__DOCUMENT_PATH="/path/to/your/document.pdf"
    ```

3.  **Gemini 模型 (可选)**:
    - 运行 `python check_models.py` 查看您可用的模型列表。
    ```dotenv
    # 默认为 gemini-2.5-flash
    API__GEMINI_MODEL="gemini-2.5-flash"
    ```

4.  **其他常用配置 (可选, 用于非交互式运行)**:
    - 以下配置如果在 `.env` 文件中设置，程序将直接使用这些值并跳过对应的交互式询问。
    ```dotenv
    # --- 文档特定策略 ---
    # 自定义目录文件路径
    DOCUMENT__CUSTOM_TOC_PATH="./test/my_toc.csv"
    # 页面范围 (例如, "10-50" 或 "[10, 50]")
    DOCUMENT__PAGE_RANGE="10,50"

    # --- 处理策略 ---
    # 是否保留原文，形成双语对照 (true/false)
    PROCESSING__RETAIN_ORIGINAL=true
    # 是否默认启用视觉模式 (true/false/不设置则为自动)
    PROCESSING__USE_VISION_MODE=true
    ```

### 4. 运行翻译

一切就绪后，直接运行主程序：

```bash
python main.py 
```

程序启动后，会通过交互式命令行引导您完成配置（任何已在 `.env` 中设置的步骤将被自动跳过）：

1.  **选择翻译模式 (Persona)**: 根据您的文本类型选择一个最合适的专家身份。
2.  **配置处理策略**:
    - **自定义目录 (TOC)**: (仅 PDF) 询问是否需要加载外部 CSV 格式的目录文件。
        - **格式要求**: CSV 文件必须包含 `Page`, `Title`, `Level` 三个表头。
    - **Vision 模式**: (仅 PDF) 选择自动检测、强制开启或强制关闭。
    - **页面范围**: (仅 PDF) 指定翻译的起始和结束页码。
    - **边距裁切**: (仅 Vision 模式) 设置裁切比例以移除页眉页脚。
    - **保留原文**: 设置是否生成双语对照的输出。

配置完成后，翻译流程将自动开始。您可以在终端看到实时的进度日志。

## 📁 输出文件

- **中间文件**: 所有缓存、图片和状态文件都保存在 `output/<文件哈希值>/` 目录下。
    - `structure_map.json`: 核心状态文件，记录了每个片段的原文、译文和元数据，是实现断点续传的关键。
- **最终文件**: 默认情况下，翻译完成的 `_Translated.md` 和 `_Translated.pdf` 文件会保存在与**源文件相同的目录**中。也可在 `.env` 中指定输出目录。

## 🎨 高级定制

- **修改翻译人格**: 直接编辑 `config/modes.json` 文件，调整现有角色的风格指南或添加全新的角色。
- **自定义 PDF 样式**: 编辑 `config/pdf_style.css` 文件来更改字体、字号、页边距、标题样式等，打造您专属的 PDF 阅读体验。

## 📜 开源协议

本项目采用 [MIT License](LICENSE) 开源协议。