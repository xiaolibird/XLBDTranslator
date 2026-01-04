# 🔄 XLBD Translator (新老笔电翻译器)

**一个超大文档 AI 翻译引擎。**

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

**XLBD Translator** 采用现代化的状态驱动架构，能够将复杂的 EPUB 和 PDF 文档（包括扫描件）通过高度可定制的“专家模式”（Persona）翻译成流畅、精准的中文，并最终生成带样式的 PDF 和 Markdown 文件。

## ✨ 核心特性

### 🎯 工业级稳定性
- **状态驱动架构**：整个翻译流程由内存中的数据结构驱动，并通过文件持久化。
- **断点续传**：支持意外中断后完美恢复，自动跳过已翻译片段，无需从头开始。
- **原子化保存**：每批次翻译完成后，立即保存进度，最大程度减少数据丢失风险。
- **健壮的错误处理**：内置 API 自动重试和 JSON 修复机制，智能处理网络波动和模型返回错误。
- **异步并发优化**：多批次并发翻译，保证结果顺序与原文精确匹配，避免分配错误。

### 🤖 多模态翻译
- **文本模式 (Native)**：针对文本清晰的 PDF/EPUB，直接提取并翻译，速度快。
- **视觉模式 (Vision)**：针对扫描件或复杂排版的文档，自动渲染页面为图片并调用多模态模型进行翻译。
- **智能策略**：支持在交互式会话中选择自动检测、强制开启或强制关闭视觉模式。

### 🌐 多 API 支持
- **Google Gemini**: 默认翻译引擎，支持多模态和长文本
- **DeepSeek API**: 支持 128K 上下文，成本效益高，特别优化中文翻译
  - 自动检测并启用长文本模式
  - 完整的 system + instruction + mode + context 合并为单 user message
  - 详见 [DeepSeek 使用指南](docs/DEEPSEEK_GUIDE.md)
- **Ollama 本地**: 支持本地部署模型，适合离线或隐私要求高的场景
- **OpenAI 兼容**: 支持任何 OpenAI 兼容的 API

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
  git clone https://github.com/xiaolibird/XLBDTranslator.git
  cd XLBDTranslator
  ```
- 推荐使用 `conda` 创建并激活一个 Python 虚拟环境:
  ```bash
  conda create -n xlbd-translator python=3.12
  conda activate xlbd-translator
  ```

### 2. 安装依赖

- 安装所有必要的 Python 库:
  ```bash
  pip install -r requirements.txt
  ```

- **PDF 输出支持** (推荐):
  
  项目默认输出 **Markdown (.md)** 和 **PDF (.pdf)** 两种格式。PDF 生成依赖 `weasyprint` 库。
  
  - **macOS 用户**: 需要额外安装系统依赖
    ```bash
    brew install cairo pango gdk-pixbuf
    ```
  
  - **Ubuntu/Debian 用户**:
    ```bash
    sudo apt-get install libpango1.0-dev libcairo2-dev libgdk-pixbuf2.0-dev
    ```
  
  - **Windows 用户**: 推荐使用 GTK3 运行时
    1. 下载并安装 [GTK3 Runtime](https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer/releases)
    2. 或使用 conda 安装相关依赖：
       ```bash
       conda install -c conda-forge weasyprint
       ```
  
  - 如果 `weasyprint` 安装失败，程序会自动跳过 PDF 生成，仍可正常输出 Markdown 文件。

### 3. 配置

#### 3.1 创建配置文件

核心配置通过环境变量文件管理。项目提供了配置模板 `config/config.env.template`，首次使用前需要创建实际配置文件：

```bash
# 从模板创建配置文件
cp config/config.env.template config/config.env
```

**注意**: `config/config.env` 文件包含敏感信息（如API密钥），已被 `.gitignore` 排除，不会提交到版本库。

#### 3.2 编辑配置文件

打开 `config/config.env` 文件，根据以下说明修改：

1.  **选择翻译引擎**:
    ```dotenv
    # 可选: gemini (默认), openai-compatible (支持 DeepSeek/OpenAI/Ollama)
    API__TRANSLATOR_PROVIDER=gemini
    ```

2.  **API 密钥 (必需)**:
    
    **使用 Gemini**:
    ```dotenv
    # Google AI Studio 的 API Key
    API__GEMINI_API_KEY="YOUR_API_KEY_HERE"
    ```
    
    **使用 DeepSeek** (推荐用于中文翻译):
    ```dotenv
    # DeepSeek API (OpenAI 兼容)
    API__TRANSLATOR_PROVIDER=openai-compatible
    API__OPENAI_API_KEY="sk-your-deepseek-api-key"
    API__OPENAI_BASE_URL="https://api.deepseek.com"
    API__OPENAI_MODEL="deepseek-chat"
    ```
    > 📖 详细配置请参考 [DeepSeek 使用指南](docs/DEEPSEEK_GUIDE.md)

3.  **文档路径 (必需)**:
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

#### 4.1 基本用法

一切就绪后，直接运行主程序：

```bash
python main.py
```

#### 4.2 指定自定义配置文件（可选）

如果需要使用不同的配置文件（例如针对不同项目），可以通过命令行参数指定：

```bash
python main.py --config /path/to/your/custom.env
```

这对于管理多个翻译项目非常有用，每个项目可以有独立的配置文件。

#### 4.3 交互式配置

程序启动后，会通过交互式命令行引导您完成配置（任何已在配置文件中设置的步骤将被自动跳过）：

1.  **选择翻译模式 (Persona)**: 根据您的文本类型选择一个最合适的专家身份。
2.  **配置处理策略**:
    - **自定义目录 (TOC)**: (仅 PDF) 询问是否需要加载外部 CSV 格式的目录文件。
        - **格式要求**: CSV 文件必须包含 `Page`, `Title`, `Level` 三个表头。
    - **Vision 模式**: (仅 PDF) 选择自动检测、强制开启或强制关闭。
    - **页面范围**: (仅 PDF) 指定翻译的起始和结束页码。
    - **边距裁切**: (仅 Vision 模式) 设置裁切比例以移除页眉页脚。
    - **保留原文**: 设置是否生成双语对照的输出。

配置完成后，翻译流程将自动开始。您可以在终端看到实时的进度日志。

## 📁 项目结构

```
XLBDTranslator/
├── main.py                 # 主入口文件
├── check_models.py         # 检查可用的 Gemini 模型
├── requirements.txt        # Python 依赖包
├── LICENSE                 # MIT 开源协议
├── README.md              # 中文说明文档
├── README.md.en           # 英文说明文档
├── config/                # 配置文件目录
│   ├── config.env.template # 环境变量模板
│   ├── modes.json         # 翻译人格定义
│   ├── pdf_style.css      # PDF 输出样式
│   └── prompts/           # 提示词模板
│       ├── system_instruction.md
│       ├── text_translation_prompt.md
│       ├── vision_translation_prompt.md
│       └── json_repair_prompt.md
├── src/                   # 源代码目录
│   ├── core/             # 核心模块（数据结构、异常）
│   │   ├── schema.py     # Pydantic 数据模型
│   │   └── exceptions.py # 自定义异常
│   ├── parser/           # 文档解析器（PDF、EPUB）
│   │   ├── loader.py     # 文档加载器
│   │   ├── formats.py    # 格式处理
│   │   └── helpers.py    # 辅助函数
│   ├── translator/       # 翻译引擎（同步/异步）
│   │   ├── base.py       # 基础翻译器
│   │   ├── engine.py     # 翻译引擎
│   │   └── support.py    # 支持模块
│   ├── renderer/         # 渲染器（Markdown、PDF）
│   │   ├── markdown.py   # Markdown 渲染
│   │   └── pdf.py        # PDF 渲染
│   ├── workflow/         # 工作流
│   │   ├── workflow.py   # 主工作流
│   │   ├── builder.py    # 配置构建器
│   │   └── tester.py     # 测试工具
│   └── utils/            # 工具函数
│       ├── file.py       # 文件操作
│       ├── logger.py     # 日志系统
│       └── ui.py         # 用户界面
├── output/               # 翻译输出（自动生成）
│   └── <file_md5>/      # 每个文档的缓存和结果
├── logs/                 # 日志文件
└── test/                 # 测试脚本和数据
```

## 📁 输出文件

- **中间文件**: 所有缓存、图片和状态文件都保存在 `output/<文件哈希值>/` 目录下。
    - `structure_map.json`: 核心状态文件，记录了每个片段的原文、译文和元数据，是实现断点续传的关键。
    - `checkpoint.json`: 恢复检查点数据
    - `glossary.json`: 提取的术语表，确保一致性
- **最终文件**: 默认情况下，翻译完成的 `_Translated.md` 和 `_Translated.pdf` 文件会保存在与**源文件相同的目录**中。

## 🎨 高级定制

### 修改翻译人格 (Persona)

编辑 `config/modes.json` 文件来调整现有角色或添加新的专家模式：

```json
{
  "custom_expert": {
    "name": "自定义专家",
    "role_desc": "您是一位...",
    "style": "您的风格指南...",
    "context_len": "medium"
  }
}
```

### 自定义 PDF 样式

编辑 `config/pdf_style.css` 文件来更改字体、字号、页边距、颜色等：

```css
body {
  font-family: '您偏好的字体';
  font-size: 12pt;
  line-height: 1.8;
}
```

## � 贡献

欢迎贡献！请随时提交 Pull Request。

## 📜 开源协议

本项目采用 [MIT License](LICENSE) 开源协议。

---

**注意**：本工具需要有效的 Google Gemini API 密钥。根据您的 API 计划，使用可能会产生费用。