# PDF / EPUB 阅读版验收

## 运行环境

- `book.py` 使用本项目 Python 环境（`.conda_env` 仅是环境名提示，不硬编码用户目录）。需要项目依赖，包括 ebooklib、markdown、lxml、Pydantic。
- PDF 可在另一个带 ReportLab 的环境运行。优先用当前环境的 workspace dependencies 工具定位内置 Python、Poppler，别把某次绝对 runtime 路径写进 skill。
- 中文字体须可嵌入且涵盖文字；默认探测 macOS Songti，其他系统通过参数给 TrueType 字体/字体集合及字形索引。缺字先报错，不交付黑方块。必要外文字符可指定 `--fallback-font`，如支持希腊字母的字体。

`book.py render` 先保存修正快照和 audit 报告，再生成两种格式。PDF 生成失败时 Markdown 和 EPUB 可以保留，但不能说双格式已完成。更新修正后重新运行 render，避免 PDF 与 EPUB 内容漂移。

## 导航和内容

- 新建中文阅读封面、目录和章节导航；不依赖原 EPUB 文本替换是否覆盖每个 HTML 节点，避免漏出原文整段。
- 默认只读 `translated_text`，必要外文括注和中文注解原样保留。`original_text` 只进入内部结构与审校记录；双语输出仅在用户明确选择时开启。
- 目录页码替代只作用于已核实的导航片段。保留作者注释、诗歌、书信及正文；不为版式好看删掉不好处理的内容。
- 跨章节 EPUB 锚点须解析到实际 XHTML 文件。PDF 索引页码必须来自实际排版，不使用原书页码冒充新页码。

## 验收

1. `audit` 不得有硬失败；长度/外文候选的人工结论必须与当前最终译文哈希一致。
2. 检查 EPUB ZIP、各 XHTML 可解析、所有生成的 `#seg` 内链目标存在。
3. PDF 字体覆盖、页数、页码、书签和目录；用 `pypdf` 或 `pdfplumber` 抽取文本检查乱码与失败标记，但不能代替视觉检查。
4. 用 `pdftoppm -f N -l N -singlefile -png <PDF> <J/previews/page-N>` 渲染封面、目录、章首、正文、注释/诗歌/索引、末页，并实际查看 PNG。每次改了字体/分页/页眉后检查最新结果。
5. 章首页眉应显示本章，不沿用上一章；多轮目录排版之间不要保留已失效的页眉映射。
6. 有生成索引时，核对 EPUB 链接和 PDF 全部页码目标，说明片段起始定位精度；原索引父词目不能因叶子提取而无声消失。

`reviewed/quality_report.json` 记录机器检查。视觉检查和语义抽检由实际审核者另行记录；机器报告不可虚构“已经看图”或“全文精校”。

`render` 生成项目内的文件；完成上述验收后，按 [默认交付规则](../SKILL.md#默认交付到下载文件夹) 将最终 PDF、EPUB 复制到下载文件夹并校验，项目原件保留。

## 扫描件与原书图片

先判断每页是一张扫描图，还是独立插图。没有文字层时先做 OCR，并目视抽检竖排顺序、目录完整性、专名和跨页句；OCR 结果未通过检查前不启动全译。记录识别工具、版本、语言、页范围与无法辨认处。额外云端 OCR 供应商需要用户已有授权；主译供应商不能因此改变。

用户要求保留视觉内容时，抽取封面、章首插画、图表等并保持长宽比，不把整页外文扫描图作为插图混进单语正文。带外文标签的原图可以保留，配中文标签说明；难以识别处明确记录。新增 `reviews/visual_assets.json`：

```json
{"reviewed":true,"assets":[{"path":"assets/cover.jpg","sha256":"图片文件SHA256","kind":"cover","reason":"原书封面，保留原设计","caption":"原书封面"},{"path":"assets/figure-147.png","sha256":"图片文件SHA256","kind":"diagram","segment_id":56,"reason":"从原PDF第147页裁出独立图表，不含正文","caption":"图：个人自助、职业团体互助与国家公共救助的补充关系。"}]}
```

路径必须位于本书任务目录，类型为 cover／illustration／diagram；非封面须指向存在的正文片段。`render` 核对图片哈希，把图片复制到 `reviewed/assets/`，PDF 和 EPUB 从相同清单嵌入，Markdown 使用相对路径；验证 EPUB 图片已实际打包，不能只有本机文件链接。原文扫描页仍默认不渲染。生成后查看封面、代表性插图和图文跨页效果。
