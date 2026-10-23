# 各 skill 的产物位置

本仓库自有 skill 与常用外部 skill 的落盘约定。新增或修改 skill 时同步更新本表。

通用规则：

- 一次运行产生多个文件的，**每次运行一个目录，目录名以 `YYYY-MM-DD-` 开头**（取运行当天），后接标题或论文名。
- 产物放在 `output/` 下（整体被 .gitignore 忽略，不进仓库）。需要随周备份的放 `output/scholar_notes/` 下：`scripts/backup_snapshot.py` 收这个目录的全部内容。
- 不要把产物放在仓库根目录、`~/Downloads` 或会话 scratchpad 里当作最终存档。scratchpad 只放过程文件，会话结束即清；Downloads 只放方便打开的副本，翻译成品统一在 `~/Downloads/翻译成果/{论文,书籍}/`，不散放在 Downloads 顶层。
- `output/scholar_notes/` 下新增的子目录不会被札记库误收：索引只读根目录的 `科研札记_*.md` 与 `manual/`、`books/` 下的 bundle，递归扫描只覆盖 `topics/`、`qa/`、`_inventory/`。

| skill | 产物位置 | 写入方 | 备注 |
|---|---|---|---|
| read-paper | `output/scholar_notes/manual/<桶>/<paper_id>.paper.json`，归档后生成 `科研札记_<桶>_手动精读.{md,docx,references.json,index.json}`；PDF 归位到 `~/Desktop/Lab/Reading/<桶>/` | `scripts/read_pdf.py` | 桶名 `YYYY-MM-DD-<主题>`，按主题分桶 |
| read-book | `output/scholar_notes/books/<Slug>/` | `scripts/book_digest.py` | |
| ask-notes | `output/scholar_notes/topics/qa/qa-<slug>.md` | `scripts/ask_notes.py` | 同一问题原地更新 |
| scholar-notes | 只读 | — | 概念页由 `build_topics.py` 维护在 `topics/` |
| scholar-write | 只读札记库；稿件放在用户自己的稿件目录 | — | 书目用 `output/scholar_notes/all_references.json` |
| scholar-search | 不落盘 | — | 临时检索 |
| screen-journal | `output/journal_screen/`（如 `npj_digit_med_candidates.json`） | `scripts/screen_journal.py` | 尚未按运行分目录，平铺 |
| paper-audit | `output/scholar_notes/audits/<YYYY-MM-DD>-<论文>_<venue>/report.md` + 配套材料 | agent 按 SKILL.md 写 | 2026-10-21 起；此前根目录的旧报告已迁入 |
| manuscript-selfcheck | `output/scholar_notes/selfchecks/<YYYY-MM-DD>-<稿件名>/{report.md,gates.json}` | agent 按 SKILL.md 写 | 2026-10-21 起必须落盘；重跑另开新日期目录 |
| translate-paper | `output/translations/papers/<YYYY-MM-DD>-<arXiv号>_<标题>/`（PDF、`src/`、`checks/`、`meta.json`）；验收后另复制最终 PDF 到 `~/Downloads/翻译成果/论文/`（核对 SHA-256） | agent 按 SKILL.md 第 8 步复制 | 过程目录在 scratchpad |
| translate-book | `output/translations-book/<书籍标识>/`（`cache/` 断点、`reviews/` 术语与校对、`reviewed/` 成品 PDF/EPUB/MD、`previews/` 排版检查图）；清单 `output/translations-book/translation_projects.json`；最终阅读版另复制到 `~/Downloads/翻译成果/书籍/` | `docs/skills/translate-book/scripts/book.py` 调 `main.py` | 书籍标识不带日期（一本书一个长期目录）；2026-08 及更早的旧作业在 `output/translations/_archive/jobs/`，成品在原书旁 `*_Translated.*` |
| deep-research（Anthropic 官方 skill，非本仓库） | `output/scholar_notes/deep_research/<YYYY-MM-DD>-<标题>/{report.md,notes/}` | 调用时把路径传给子代理 | skill 本身随账号同步、不可改，约定见该目录 README |
