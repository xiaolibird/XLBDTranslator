# Scholar Digest

从 Google Scholar 邮件提醒中自动提取论文信息并生成中文摘要。

## 功能特性

- 🔐 **Gmail OAuth 2.0 认证** - 安全访问邮箱
- 📧 **自动筛选 Scholar 邮件** - 精准识别 Google Scholar Alert 邮件
- 📄 **论文信息提取** - 提取标题、作者、摘要、DOI 等元数据
- 🤖 **AI 摘要生成** - 使用 Gemini/OpenAI 翻译摘要并生成总结
- 💾 **多格式输出** - JSON、Markdown、CSV 格式导出
- ✅ **邮件状态管理** - 自动标记已处理邮件为已读

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements_scholar.txt
```

### 2. 配置 Gmail API

1. 访问 [Google Cloud Console](https://console.cloud.google.com/)
2. 创建新项目或选择现有项目
3. 启用 Gmail API
4. 创建 OAuth 2.0 客户端凭据（桌面应用）
5. 下载凭据文件并保存为 `config/credentials.json`

详细步骤请参考: [Gmail API Python 快速入门](https://developers.google.com/gmail/api/quickstart/python)

### 3. 配置环境

复制配置模板并填写 API 密钥：

```bash
cp config/scholar.env.template config/scholar.env
```

编辑 `config/scholar.env`：

```env
GEMINI_API_KEY=your_gemini_api_key_here
```

### 4. 运行

```bash
python scholar_main.py
```

首次运行时会打开浏览器进行 Gmail 授权。

## 命令行参数

```bash
python scholar_main.py --help
```

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--config` | 配置文件路径 | `config/scholar.env` |
| `--days` | 获取最近 N 天的邮件 | 7 |
| `--max-emails` | 最大处理邮件数量 | 100 |
| `--batch-size` | LLM 批量处理大小 | 5 |
| `--no-translate` | 不翻译摘要 | - |
| `--no-summary` | 不生成 AI 总结 | - |
| `--no-mark-read` | 不标记邮件为已读 | - |
| `--output-dir` | 输出目录 | `output/scholar_digest` |
| `--export-csv` | 额外导出 CSV | - |
| `--dry-run` | 仅获取和解析，不调用 LLM | - |
| `--debug` | 调试模式 | - |

## 示例

```bash
# 获取最近 14 天的邮件
python scholar_main.py --days 14

# 仅提取论文信息，不翻译
python scholar_main.py --no-translate --no-summary

# Dry run 测试
python scholar_main.py --dry-run --debug

# 导出为 CSV
python scholar_main.py --export-csv
```

## 输出格式

### JSON 结构

```json
{
  "digest_id": "digest_20260108_120000",
  "title": "Scholar Digest - 2026-01-08",
  "total_papers": 25,
  "segments": [
    {
      "segment_id": 1,
      "paper_id": "abc123...",
      "original_abstract": "English abstract...",
      "translated_abstract": "中文摘要...",
      "summary": "本研究提出了...",
      "metadata": {
        "title": "Paper Title",
        "authors": ["Author 1", "Author 2"],
        "doi": "10.1234/xxxxx",
        "field": "Artificial Intelligence",
        ...
      }
    }
  ]
}
```

### Markdown 结构

生成的 Markdown 文件包含：
- 统计摘要（论文数量、领域分布）
- 按论文列表展示（标题、作者、摘要、AI总结）

## 数据模型

模仿原有翻译器的 `SegmentList` 设计：

- `PaperMetadata` - 论文元数据（标题、作者、DOI等）
- `PaperSegment` - 论文片段（原文、译文、总结）
- `DigestBatch` - 批量处理单元
- `DigestOutput` - 完整输出结果

## 项目结构

```
src/scholar/
├── __init__.py          # 模块入口
├── schema.py            # 数据结构定义
├── gmail_client.py      # Gmail API 客户端
├── paper_extractor.py   # 论文提取器
└── workflow.py          # 工作流编排

config/
├── scholar.env.template # 配置模板
└── credentials.json     # Gmail OAuth 凭据（需自行添加）

scholar_main.py          # 主入口
requirements_scholar.txt # 依赖列表
```

## 注意事项

1. **Gmail API 配额**: 每日请求有限制，请合理设置 `--max-emails`
2. **LLM 成本**: 翻译和摘要会消耗 API 配额
3. **隐私安全**: `credentials.json` 和 `token.json` 包含敏感信息，请勿提交到版本控制

## 故障排除

### 认证失败

```
Error: Could not find credentials.json
```

确保已从 Google Cloud Console 下载 OAuth 凭据并放置在正确位置。

### 没有找到 Scholar 邮件

1. 确认已订阅 Google Scholar Alert
2. 检查邮件是否在收件箱（不在垃圾邮件）
3. 尝试增加 `--days` 参数

### LLM 调用失败

1. 检查 API 密钥是否正确
2. 检查网络连接
3. 减小 `--batch-size` 以减少单次请求大小

## License

MIT License
