# 🔄 XLBD Translator (新老笔电翻译器)

**超大文档AI翻译引擎**

[![Python](https://img.shields.io/badge/Python-3.9+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

**XLBD Translator** 是一个为深度阅读和研究者设计的、由 AI 驱动的强大文档翻译引擎。它能够将复杂的 EPUB 和 PDF 文档（包括扫描件）通过高度可定制的“专家模式”（Persona）翻译成流畅、精准的中文。

## ✨ 核心特性

### 🎯 工业级稳定性
- **断点续传**：支持意外中断后继续翻译，无需从头开始
- **结构化输出**：生成带目录的 Markdown 文件，支持章节跳转
- **错误恢复**：自动重试 API 失败，智能降级处理

### 🤖 多模态翻译
- **Native 模式**：针对文本清晰的 PDF/EPUB，直接提取并翻译
- **Vision 模式**：针对扫描件或复杂排版的文档，自动渲染图片后翻译
- **自动检测**：智能识别文档类型，自动选择最优模式

### 🎭 专业翻译人格
- **齐泽克专家**：擅长黑格尔哲学、拉康精神分析的学术翻译
- **社会学研究员**：精通批判理论、欧洲大陆哲学
- **传记记者**：文学性传记和历史非虚构作品专家
- **小说翻译家**：世情耽美言情小说翻译专家
- **尼采阐释者**：哲学隐喻的诗意翻译

### ⚙️ 高度可定制
- **模型可定制**：默认使用 `gemini-2.5-pro`，可轻松更换为 `gemini-1.5-flash` 等其他模型，以平衡成本与性能。
- **自定义目录 (TOC)**: 支持通过外部 CSV 文件为 PDF 注入章节结构，实现更精准的语义切分。

## 🚀 快速开始

### 1. 环境准备

- 克隆本仓库到本地:
  
  git clone https://github.com/developer/XLBD-Translator.git
  
  cd XLBD-Translator
  
- 推荐使用 `conda` 创建并激活一个 Python 虚拟环境:
  
  conda create -n xlbd-translator python=3.9
  
  conda activate xlbd-translator
  
### 2. 安装依赖

- 安装所有必要的 Python 库:
  
  pip install -r requirements.txt
  
- 下载 `spacy` 的英文语言模型，用于文本语义切分:
  
  python -m spacy download en_core_web_sm
  
### 3. 配置

1.  **API Key**: 打开 `main.py` 文件，在第 11 行 `API_KEY = " "` 处，填入您的 Google AI Studio API Key。
    例如: `API_KEY = "YOUR_GOOGLE_API_KEY_HERE"`
2.  **文件路径**: 打开 `main.py` 文件，在第 12 行 `FILE_PATH = ' '` 处，填入您想要翻译的 EPUB 或 PDF 文件的完整路径。
    例如: `FILE_PATH = '/path/to/your/document.pdf'`
3.  **(可选) 更换 Gemini 模型**:
    - 项目默认使用 `gemini-2.5-pro`。您可以根据需求（例如追求更快的速度或更低的成本）更换为其他模型，如 `gemini-3.0-flash`。
    - **修改位置**: 打开 `translator.py` 文件，在第 7 行 `def __init__(self, api_key, model_name="gemini-2.5-pro"):` 中，修改 `model_name` 的值。
      例如，修改为 `def __init__(self, api_key, model_name="gemini-3.0-flash"):`

### 4. 运行翻译

一切就绪后，直接运行主程序：

python main.py 

程序启动后，会通过交互式命令行引导您完成后续步骤：

1.  **选择翻译模式 (Persona)**: 根据您的文本类型选择一个最合适的专家身份。
2.  **配置翻译策略**:
    - **自定义目录 (TOC)**: （仅 PDF）询问是否需要加载外部 CSV 格式的目录文件。
        - **格式要求**: CSV 文件必须包含 `Page`, `Title`, `Level` 这三个表头。
        - **`Page`**: 章节标题对应的 **PDF 阅读器中的实际页码**。
        - **`Title`**: 章节的完整标题。
        - **`Level`**: 章节层级（例如：1 代表章，2 代表节）。
        - [示例 toc](toc_example.csv)
        - **Vision 模式**: （仅 PDF）选择自动检测、强制开启或强制关闭图片模式。对于扫描件，建议强制开启。
        - **边距裁切**: （仅 PDF）设置页眉页脚的裁切比例，或使用自动检测的默认值。
        

配置完成后，翻译流程将自动开始。您可以在终端看到实时的进度日志。翻译结果会保存在一个以 `日期_文件名_模式名` 命名的文件夹中。


## 📜 开源协议

本项目采用 [MIT License](LICENSE) 开源协议。
