# 精读 PDF 目录搬家 + 入库时间线 xlsx 随索引更新（2026-09-01）

## 搬家

`~/Downloads/` 下按日期命名的论文精读目录（`YYYY-MM[-DD][-批次]`）全部挪到 **`~/Desktop/Lab/Reading/`**：
32 个目录、294 个 PDF；只留 `2026-01-16 Amazon Ebook deDRM`（非论文）与 wanna-read / 文献整理 / hyliang 等归档树。
另把 `~/Downloads/scholar_pdf`（08-24 为周报缺全文的 6 篇手动补下载）搬为 `Reading/2026-08-24-周报缺全文补下载`。

踩坑：两个目录名结尾带空格（`2026-07 `、`2026-07-27 `），`^\d{4}-\d{2}(-\d{2})?(-.*)?$` 会漏掉，
第一遍盘点就漏了 `2026-07 `（14 篇 bundle 在引用）。搬过去时把空格去掉了。

## 项目侧改路径

- manual/books bundle 的 `pdf_path`：277 个文件重写（原件备份 `output/scholar_notes/_archive/pdfpath_remap_20260901/`，
  备份 vs 现文件逐字段比对只有 pdf_path 变）。顺手修回 12 条早已断掉的路径（影子变量批次指向 `wanna-read/论文/2026-08-17-影子变量/`
  但文件在日期目录里；`wanna-read/论文/` 下 4 篇被挪进 `_已入库/`；nihms/pcbi 两篇在 `2026-08-31-补充精读/`）。
  仍死的 3 条指向 `/private/tmp`（atc_garg2022 / che2018_grud / agniel2018_bmj），原件已不在。
- md 里的引用：撤稿案例_DynaGraph、复现路线图_DynaGraph对照臂、需下载全文清单。
- `scripts/locate_pdf.py` 搜索根加 Reading；`scripts/relink_manual_pdfs.py` 用法示例；全局 skill read-paper 加归档位置约定。
- literature_index / sidecar / vault 不存 PDF 路径，不用重建。

## 时间线 xlsx

《札记库入库时间线.xlsx》原是 08-19 一次性用 openpyxl 生成后扔在 ~/Downloads 的（仓库里没有生成脚本），索引一变就过期。
现在挪到 `~/Desktop/Lab/Reading/` 根目录，改为由索引派生、随索引更新：
- `scripts/export_timeline_xlsx.py`：literature_index.json → 三张表（时间线 / 按月汇总 / 明细）。**零依赖**手写
  SpreadsheetML（launchd 用的 env002_reader 没装 openpyxl；表头加粗反白、冻结首行、明细自动筛选、列宽），
  旁边 `.札记库入库时间线.xlsx.meta.json` 记来源 generated_at 做陈旧判定。
- 挂在 `scripts/sync_vault.py`（launchd `com.xlbd.scholar-vault` WatchPaths=literature_index.json）里，best-effort：
  失败只记日志 + 系统通知，不影响 vault。
- 口径与老表逐行对过：2026-08 之前 71 个批次里 67 行完全一致；差异 4 行都有解释——2024-03 索引后来多判了 1 条重复；
  老表按「批次标签」合并同月的自动+手动文件为一行（2026-06 / 2026-07-27），新表**一份札记文件一行**；
  2026-07 的 `reading_source=manual-pdf-agent-cross-check` 老表算脚本通读，新表算手动精读（它就是 agent 亲读）。
  新表多一列「书籍精读」（08-26 才有书籍系列）和明细的「重复于」。
