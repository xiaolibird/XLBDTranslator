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
- **结构化输出**：Gemini 用 `response_schema`、DeepSeek/OpenAI 兼容用 `response_format=json_object` 从根上保证返回合法 JSON；本地 Ollama 仍保留轻量正则安全网。
- **译后质检回路**：翻译完成后自动扫描失败标记与术语违例段落并定向重译（上限 1 轮），残留失败写入 `quality_report.json` 显式报告，不再把 `[Failed]` 悄悄渲染进成品（可用 `ENABLE_QUALITY_CHECK=false` 关闭）。
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
  - 详见下文「3.2 编辑配置文件」中的 DeepSeek 配置示例
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
    - **摘要模式**：快速提炼与归纳文档要点。
    - **逻辑分析师**：论证结构与逻辑链条的精确翻译。

### ⚙️ 高度可配置
- **`.env` 驱动**：所有核心配置均通过 `config/config.env` 文件管理，支持非交互式运行。
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
# 使用默认配置文件 (config/config.env)
python main.py

# 指定要翻译的文档
python main.py /path/to/document.pdf

# 使用自定义配置文件
python main.py --config /path/to/custom.env document.epub
```

#### 4.2 命令行参数

程序支持通过命令行参数完全控制翻译流程，实现非交互式运行。参数优先级为：**命令行参数 > 配置文件 > 交互式询问**。

##### 基础参数

| 参数 | 说明 | 示例 |
|------|------|------|
| `file_path` | 要翻译的文档路径（位置参数）| `python main.py document.pdf` |
| `--config` | 配置文件路径 | `--config config/custom.env` |

##### 翻译模式参数

| 参数 | 说明 | 示例 |
|------|------|------|
| `--mode` | 翻译模式 ID（对应 modes.json 中的 key）| `--mode 1` |

##### PDF 专用参数

| 参数 | 说明 | 可选值/格式 | 示例 |
|------|------|-------------|------|
| `--vision-mode` | Vision 模式控制 | `auto`（自动检测）<br>`force`（强制启用）<br>`off`（仅文本） | `--vision-mode force` |
| `--page-range` | 页面范围 | `"起始,结束"` 或 `"起始-结束"` | `--page-range 10-50` |
| `--margins` | 裁切边距（移除页眉页脚） | `"上,下,左,右"`（0.0-1.0 的比例） | `--margins 0.1,0.05,0.05,0.05` |

##### 通用参数

| 参数 | 说明 | 示例 |
|------|------|------|
| `--retain-original` | 在输出中保留原文（双语对照） | `--retain-original` |
| `--no-retain-original` | 在输出中不保留原文 | `--no-retain-original` |

#### 4.3 使用示例

##### 完全非交互式运行

所有参数通过命令行指定，适合脚本化和批处理：

```bash
# 翻译 PDF，指定所有参数
python main.py document.pdf \
  --mode 1 \
  --vision-mode force \
  --page-range 10-50 \
  --margins 0.1,0.05,0.05,0.05 \
  --retain-original

# 翻译 EPUB，只指定模式和保留原文
python main.py novel.epub \
  --mode 5 \
  --retain-original

# 使用 DeepSeek API 翻译（config.env 中配置 API__TRANSLATOR_PROVIDER 等 DeepSeek 参数，见 3.2 节）
python main.py document.pdf \
  --config config/config.env \
  --mode 4 \
  --page-range 1-100
```

##### 混合模式

部分参数通过命令行，其余从配置文件读取或交互式询问：

```bash
# 仅指定模式和页面范围，其他参数从配置文件读取
python main.py document.pdf --mode 2 --page-range 1-100

# 仅指定文档和模式，其他参数通过交互式询问
python main.py document.pdf --mode 1
```

##### Docker/CI 环境

在容器或持续集成环境中，配合配置文件实现零交互：

```bash
# 所有配置都在 config.env 中预设
python main.py

# 或通过环境变量和命令行参数
export API__GEMINI_API_KEY="your-api-key"
python main.py /data/document.pdf \
  --mode 1 \
  --vision-mode off \
  --no-retain-original
```

#### 4.4 交互式配置

当参数既未通过命令行指定，也未在配置文件中设置时，程序会通过交互式命令行引导您完成配置：

1.  **选择翻译模式 (Persona)**: 根据您的文本类型选择一个最合适的专家身份。
2.  **配置处理策略**:
    - **自定义目录 (TOC)**: (仅 PDF) 询问是否需要加载外部 CSV 格式的目录文件。
        - **格式要求**: CSV 文件必须包含 `Page`, `Title`, `Level` 三个表头。
    - **Vision 模式**: (仅 PDF) 选择自动检测、强制开启或强制关闭。
    - **页面范围**: (仅 PDF) 指定翻译的起始和结束页码。
    - **边距裁切**: (仅 Vision 模式) 设置裁切比例以移除页眉页脚。
    - **保留原文**: 设置是否生成双语对照的输出。

配置完成后，翻译流程将自动开始。您可以在终端看到实时的进度日志。

## 🎓 Scholar Digest（Google Scholar 邮件摘要）

除文档翻译外，项目内置一个独立的 Scholar Digest 模块：读取 Gmail 中的 Google Scholar 论文提醒邮件（可选并入 PubMed/arXiv 检索），经两级过滤后调用 LLM 生成中文论文摘要汇总。

**方法学审稿三态裁决**：黑名单保持零成本的确定性关键词匹配，只剔除无歧义的完全离题领域（保守，避免误杀对抗性证据）；相关性判断默认交给 LLM 做方法学审稿裁决（`PROCESSING__FILTER_MODE=llm`，可切回 `keyword`），输出三态 `INCLUDE / MAYBE / EXCLUDE` 加纳入维度（bucket）、危险信号（`THREAT`/`BENCHMARK`/`OVERCLAIM_PRECEDENT`）、角色（`MUST_ENGAGE` 等）与一句话用处。`MAYBE` 与 `INCLUDE` 一并进入翻译摘要，输出里用裁决徽章区分；标 `THREAT`/`MUST_ENGAGE` 的论文在优先级排序中置顶。每次裁决的判定、理由、模型与 prompt 版本连同被排除论文的完整元数据固化到 `{run_id}_excluded.json`，可审计、可回溯；LLM 调用失败时自动回退关键词匹配，不中断流程。审稿维度可在 `config/prompts/whitelist_filter_prompt.md` 定制，论文主题经 `PROCESSING__RESEARCH_INTERESTS` 注入。

**PubMed / arXiv 检索来源**：除 Gmail 外，可按检索式抓取 PubMed（E-utilities）与 arXiv（Atom API），与邮件结果去重后并入同一条筛选→翻译→摘要流水线，默认随每周 digest 自动运行（`PROCESSING__EXTERNAL_SOURCES_ENABLED=true`，`--external`/`--no-external` 覆盖）。全部公开接口、无需密钥；`--dry-run` 下自动跳过网络请求。检索式见 `PROCESSING__ARXIV_QUERY` / `PROCESSING__PUBMED_QUERY`。

**Zotero 联动 + pandoc 科研札记**：入选论文可一键写入 Zotero（本地连接器 `saveItems`），由 [Better BibTeX](https://retorque.re/zotero-better-bibtex/) 自动分配 citekey，随后生成 pandoc-ready 札记——每篇一份含 YAML front matter、正文 `[@citekey]` 引用、三态裁决徽章、摘要与 AI 归纳的 markdown，外加一份自包含 `references.json`（CSL-JSON）。配 Zotero 自动导出的 `references.bib/json`，可直接 `pandoc --citeproc` 转 LaTeX/docx。OA 全文（arXiv 直链 / Unpaywall，合法免费）作为附件交给 Zotero 自抓，非 OA 则退化为 abstract-only。默认关闭（写库需 Zotero 桌面端在线）；推荐「cron 出 digest → 人在时 `python scholar_main.py zotero --input <run_id>.json` 推送」的分离流程，或 digest 时加 `--zotero`。配置见 `PROCESSING__ZOTERO_*` / `PROCESSING__NOTES_*`。

### 配置

```bash
# 1. 从模板创建配置文件（包含密钥，已被 .gitignore 排除，不会入库）
cp config/scholar.env.template config/scholar.env

# 2. 编辑 config/scholar.env，填入 Gemini API Key 和 Gmail OAuth 凭据路径
#    注意：嵌套配置使用双下划线命名，如 LLM__GEMINI_API_KEY、GMAIL__CREDENTIALS_PATH

# 3. 安装 Scholar 专用依赖
pip install -r requirements_scholar.txt
```

Gmail OAuth 凭据（`config/credentials.json`）从 [Google Cloud Console](https://console.cloud.google.com/) 下载，首次运行时会引导完成授权并生成 `config/token.json`（两者均已被 .gitignore 排除）。

### 运行

```bash
# 生成论文摘要汇总（读取最近邮件 -> 过滤 -> LLM 摘要 -> 输出到 output/scholar_digest/）
python scholar_main.py digest

# 使用 DeepSeek v4 生成摘要（复用 config/config.env 的 DeepSeek 密钥）
python scholar_main.py digest --provider deepseek

# 补跑历史邮件（例如最近 151 天），跨月时自动按月份拆分 Markdown 输出
python scholar_main.py digest --days 151 --max-emails 0 --provider deepseek

# 使用传统关键词白名单（跳过 LLM 裁决）
python scholar_main.py digest --filter-mode keyword

# 并入 PubMed/arXiv 检索来源（默认已开启，可显式覆盖）
python scholar_main.py digest --external
python scholar_main.py digest --no-external   # 只用 Gmail

# Zotero 联动：把已有 digest 写入 Zotero + 生成 pandoc 札记（需 Zotero 开着）
python scholar_main.py zotero --input output/scholar_digest/digest_xxx.json
# 或 digest 时一并推送（人在时）
python scholar_main.py digest --provider deepseek --zotero

# 深度研究模式
python scholar_main.py deep-research --papers output/scholar_digest/digest_xxx.json

# 批量翻译论文（需要环境变量 GEMINI_API_KEY 或 --key 参数）
python batch_translate_scholar.py --json <papers.json>
```

### 全文精读（句级角色标记 + 可调取 highlights）

对每月优先级 top-N（默认 5）论文做**全文级**精读：解析 OA 全文（arXiv 直链 / Unpaywall，合法免费）→ 下载 PDF → PyMuPDF 抽文本 → 强模型（`LLM__CLOSEREAD_MODEL`）输出结构化中文精读（研究问题 / 方法与数据 / 关键结论 / 可质疑点 / **对我研究的联想**），并按**对后续工作流的用途**给句子打角色标记：`〔可引用证据〕`墨绿（含数字/效应量，写作取证）/ `〔可反驳观点〕`紫（作者主张/可质疑处，写 critique 的靶子）/ `〔方法论借鉴〕`蓝（可迁移方法思路）（docx 版真三色着色）。这些句子聚合成索引条目的 `highlights[]`，工作流可按 role（citable/refutable/method）跨全库 `jq` 直取，无需打开 md。历史札记保留旧三色标记（方法学创新/重要发现/研究背景），索引层自动近似映射到新 role。候选层做 **OA 择优**：在高优先级候选里优先挑能拿到全文的，避免 top-N 恰好全是付费墙；无全文则降级为摘要级精读并明确标注。开关 `PROCESSING__CLOSEREAD_ENABLED` 或 CLI `--close-read`。

### 按月科研札记回填（headless）

```bash
# 单月 / 区间回填：Gmail+PubMed+arXiv → 三态筛选 → 权威元数据矫正 → top-5 全文精读
#   → output/scholar_notes/科研札记_YYYY-MM_全文精读.{md,docx,references.json,index.json}
python scripts/backfill_notes.py --since 2025-06 --until 2025-06
python scripts/backfill_notes.py --prev-month          # 上一自然月（月度 launchd 用）
```

headless 设计：不写 Zotero 库（元数据由 translation-server + Crossref 矫正；citekey 用「作者姓+年+标题词」人读兜底键，需要权威键时人在再 `zotero --input` 推库）。已存在的月份自动跳过（`--force` 覆盖）；**跨运行去重**——`seen` 集从文献索引恢复，新月份不会与历史月重复收录同一篇。并行分片跑历史时给每个进程独立 `--token-path`，避免并发刷新 Gmail token 写坏。

### 手动 PDF 深度精读（agent 亲读 + 脚本交叉核验）

自己手上有一篇 PDF、想做**比自动 top-N 更彻底的通读**并归档进文献库时用。三段式：

```bash
# 1) ingest：抽全文（不截断）+ 拉 Crossref/arXiv 权威元数据 + 分块逐块通读 → draft bundle
PYTHONPATH=. python scripts/read_pdf.py ingest paper.pdf            # 默认归档当月，可 --month YYYY-MM
# 2) agent 亲读：用 Read 工具读完整本 PDF，逐条核验脚本草稿的数字/结论/方法，
#    把合并终稿写回 bundle 的 close_reading_final + cross_check_report，status=final（协议见 skill: read-paper）
# 3) finalize：从当月全部 final bundle 重建手动精读四件套 + 刷索引（同月可多篇追加、幂等）
PYTHONPATH=. python scripts/read_pdf.py finalize <bundle.json>
```

产物为独立系列 `科研札记_YYYY-MM_手动精读.{md,docx,references.json,index.json}`，**不并入**自动
`_全文精读`（避免月度 launchd 见"当月已存在"而跳过整月）。在索引里手动深读是 keeper——论文写作
agent 检索时（过滤 `duplicate_of == null`）读到的就是最彻底那版精读。脚本深读与 Claude 亲读**交叉核验**：
每个效应量/数字回 PDF 原文核对，分歧以亲证为准，纠错与补漏记入札记末节「交叉核验记录」。触发 skill：
在本仓库对 Claude 说"精读这篇 PDF / 深读这篇论文"并给出 PDF 即可。

### 文献索引（论文写作 agent 的检索入口）

札记目录会维护一份机器可读总索引 `output/scholar_notes/literature_index.json`（+人读 `INDEX.md`、agent 使用说明 `AGENTS.md`），每篇一条：citekey/DOI/arXiv id、标题、裁决、一句话用处、优先级、是否全文精读、角色标记计数（`tag_counts`:citable/refutable/method）、**句级可调取 `highlights[]`**（{role,tag,section,text}）、所在札记文件+行号、跨月重复标记（`duplicate_of`）与 **citekey 撞键警告**（`citekey_collisions`，合并 bibliography 前必查）。

```bash
python scripts/notes_index.py                       # 增量（写札记时也会自动刷新）
python scripts/notes_index.py --full                # 全量重建
python scripts/notes_index.py --since 2025-01 --until 2025-12   # 强制重扫区间
```

数据源：新札记由 `write_notes` 自动写出无损 sidecar `{札记名}.index.json`；存量札记按 md 格式契约解析（`test/test_notes_index.py::test_roundtrip_md_parse` 锁格式）。**Agent 消费**：全局技能 `~/.claude/skills/scholar-notes/`（源码在 `docs/skills/scholar-notes/`，任何项目说"找文献/查札记"即可触发）；或直接读札记目录里的 `AGENTS.md`（源码 `docs/scholar_notes_AGENTS.md`，索引脚本自动部署）。检索四步法：查索引（过滤 `duplicate_of == null`）→ `grep '[@citekey]'` 定位精读节 → 正文 `[@citekey]` 引用 → 合并对应月 `references.json` 出 bibliography。

### 自动运行（launchd，两个互补 job）

```bash
bash scripts/install_weekly_digest.sh       # 每周一 09:00：digest 速览（不出札记）
bash scripts/install_monthly_backfill.sh    # 每月 1 日 21:30：出上月精读札记 + 刷新文献索引
# 卸载：各自加 --uninstall；手动触发：launchctl kickstart gui/$(id -u)/<label>
```

两个 job 时间错开且 Gmail token 物理隔离（monthly 用 `config/token.monthly.json` 副本），互不冲突；复用同一 python 实体二进制，TCC 完全磁盘访问只需授权一次。日志在 `~/Library/Logs/xlbd-scholar-digest/cron_digest.log` 与 `cron_monthly.log`；错过触发时点（睡眠/关机）唤醒后自动补跑。

> ⚠️ 仓库位于 `~/Documents` 等受 macOS 隐私保护的目录时，需一次性授权：系统设置 > 隐私与安全性 > **完全磁盘访问权限**，添加安装脚本打印的 python 二进制路径；否则后台任务会报 `Operation not permitted`。

## 📁 项目结构

```
XLBDTranslator/
├── main.py                 # 主入口文件
├── scholar_main.py         # Scholar Digest 入口（Google Scholar 邮件摘要）
├── batch_translate_scholar.py # Scholar 论文批量翻译
├── check_models.py         # 检查可用的 Gemini 模型
├── requirements.txt        # Python 依赖包
├── requirements_scholar.txt # Scholar Digest 依赖包
├── LICENSE                 # MIT 开源协议
├── README.md              # 中文说明文档
├── README.md.en           # 英文说明文档
├── config/                # 配置文件目录
│   ├── config.env.template # 环境变量模板
│   ├── scholar.env.template # Scholar Digest 配置模板
│   ├── modes.json         # 翻译人格定义
│   ├── pdf_style.css      # PDF 输出样式
│   └── prompts/           # 提示词模板
│       ├── system_instruction.md
│       ├── system_instruction_simple.md
│       ├── text_translation_prompt.md
│       ├── text_translation_prompt_simple.md
│       ├── vision_translation_prompt.md
│       ├── scholar_digest_prompt.md
│       ├── whitelist_filter_prompt.md  # 方法学审稿三态筛选 prompt
│       └── thesis_introduction_prompt.md
├── scripts/               # 部署运维脚本
│   ├── run_weekly_digest.sh          # 每周 digest 运行脚本
│   ├── install_weekly_digest.sh      # launchd 定时任务安装/卸载
│   └── com.xlbd.scholar-digest.plist # launchd 配置模板
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
│   ├── renderer/         # 渲染器（Markdown、PDF、EPUB）
│   │   ├── markdown.py   # Markdown 渲染
│   │   ├── pdf.py        # PDF 渲染
│   │   └── epub.py       # EPUB 渲染
│   ├── workflow/         # 工作流
│   │   ├── workflow.py   # 主工作流
│   │   ├── builder.py    # 配置构建器
│   │   └── tester.py     # 测试工具
│   ├── scholar/          # Scholar Digest（Gmail 邮件 + PubMed/arXiv 检索）
│   │   ├── academic_search.py # PubMed/arXiv 检索客户端
│   │   ├── fulltext.py   # OA 全文解析（arXiv 直链 / Unpaywall）
│   │   ├── zotero_sync.py # Zotero 连接器写库 + Better BibTeX citekey 解析
│   │   └── notes.py      # pandoc-ready 科研札记 + CSL-JSON 生成
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