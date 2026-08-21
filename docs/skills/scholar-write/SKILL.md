---
name: scholar-write
description: 用本机科研札记文献库支撑论文写作：按 role 轴(可引证据/可反驳观点/方法借鉴)取证 → 正文写 [@citekey] → pandoc 挂全局书目出 docx/LaTeX。当用户在写论文/综述/related work/discussion 并要"找证据支撑这个论点/找可引用的数字/找可反驳的观点/加引用/出参考文献/转 docx/转 latex/pandoc"时使用。区别于 scholar-notes(浏览查库)、scholar-search(临时检索不入库)、read-paper(精读入库)。
---

> 真相源：本文件在仓库 `docs/skills/scholar-write/SKILL.md`；改完须跑
> `bash scripts/install_skills.sh` 同步到 `~/.claude/skills/`。

# 札记库 → 论文写作

仓库：`/Users/xiaolibird/Documents/GitHub/XLBDTranslator-dev`（命令在仓库根运行）。
文献库：`output/scholar_notes/`（2000+ 条索引，含手动深读 keeper）。
全局书目：`output/scholar_notes/all_references.json`（CSL-JSON，索引更新时自动刷新，**勿手改**）。

## 三步写作流

### 1. 取证据 —— 按 role 轴查

```bash
/Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/notes_query.py <关键词...> --role citable|refutable|method [选项]
```

| role | 中文标记 | 什么时候用 |
|---|---|---|
| `citable` | 可引用证据 | 写 claim / results / intro 需要具体数字、效应量、可溯源结果 |
| `refutable` | 可反驳观点 | 写 discussion / critique / limitation，找可质疑的主张与靶子 |
| `method` | 方法论借鉴 | 写 related work / methods，找可迁移的方法思路 |

常用选项：`--tier high`（只看高优先级）、`--full-text-only`（只要有全文精读的）、
`--series manual`（只要手动深读 keeper，证据最厚）、`--month 2026-07`（也接受 `2026`）、
`--limit N`（0=不限）、`--cite`（吐可直接粘贴的 `[@a; @b]`）、`--json`（结构化，带 `total`）。

**匹配语义**：入选要求每个关键词都出现在 标题/一句话用处/任一 highlight 句中；但**传了
`--role` 时必须有该 role 的句子真含关键词**才入选——否则会拿到文不对题的句子当证据
（曾出现某篇 one_line 写着"未显式建模 MNAR"却在 `MNAR --role method` 里排第一）。
纯靠标题命中的条目会标注"（关键词命中标题/一句话用处，非句级证据）"并排在后面。

**退出码**：0=有命中，1=无命中，2=索引缺失/损坏（三种输出模式一致，可安全用于脚本）。

多词是 AND（每个词都要命中标题/一句话用处/highlights 任一）。命中太少就减词、去掉 `--role`。
输出末行 `↳ 科研札记_2026-07_手动精读.md:273` 是跳转位置——需要上下文时 `Read` 该文件那一段。
`notes_query.py` 靠 `__file__` 定位索引路径（不是 cwd 相对），换目录跑也不会找错文件——但仓库根
运行仍是约定写法，方便相对路径粘贴跳转行。

**取证路由（何时切到语义检索）**：

| 场景 | 用哪个 |
|---|---|
| 确切术语 / citekey / role 硬门槛取证（notes_query 有命中） | `notes_query.py`（原样，AND 精确匹配） |
| 中文概念找英文文献表述、换一种说法、或 notes_query 查询空手 | `scripts/notes_search.py`（语义检索，支持 `--role`/`--cite`/`--json`，参数面与 notes_query 对齐但 JSON schema 不同） |

```bash
/Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/notes_search.py <中文或英文查询...> --role citable|refutable|method [--cite] [--json]
```
`--mode` 默认 `hybrid`（dense 向量 + BM25 关键词 RRF 融合），也可 `--mode dense`/`--mode sparse`。
两路都跑过、结果按 citekey 去重合并即可。**注意覆盖面**：句级证据（highlights）只覆盖库内约三成
条目（精确数见 output/scholar_notes/AGENTS.md，实时数以 literature_index.json 为准）——`notes_search` 命中若标注"该篇无精读句级证据"，
只是语义命中了标题/一句话用处，不能当句级可引证据用；真要引用还得回 `notes_query`/原文核实有没有
对应句子。

### 2. 写稿

正文用 pandoc 引用语法：`……缺失本身携带信息 [@tan2023Informative]`；
多条并列 `[@a2024X; @b2025Y]`；带页码 `[@a2024X, p. 7]`；行内 `@a2024X 指出……`。

**只用 query 真实吐出来的 citekey**，不要凭记忆编造键名——键不在书目里 pandoc 会渲染成 `[?]`。

### 3. 出稿

设 `BIB=/Users/xiaolibird/Documents/GitHub/XLBDTranslator-dev/output/scholar_notes/all_references.json`。

**Word**：
```bash
pandoc draft.md -f markdown-smart --citeproc --bibliography="$BIB" -o draft.docx
```

**PDF（真 LaTeX 排版，中文最佳）**——本机没装 TeX，但 docker 镜像 `zjuthesis:latest`
里有完整 TeX Live 2026 + ctex + Fandol 中文字族：
```bash
pandoc draft.md -f markdown-smart --citeproc --bibliography="$BIB" -s \
  --pdf-engine=xelatex -V documentclass=ctexart \
  -V geometry:margin=2.3cm -V fontsize=11pt -o latex_src.tex
docker run --rm -v "$PWD":/work -w /work --entrypoint sh zjuthesis:latest \
  -c "xelatex -interaction=nonstopmode latex_src.tex && xelatex -interaction=nonstopmode latex_src.tex"
open latex_src.pdf
```
跑两遍是为交叉引用/hyperref 的 PageLabels（少跑一遍会留 "Rerun to get..." 警告）。
容器不需要 bib 文件——`--citeproc` 在转换时已把书目渲染进 .tex（无 `\bibliography` 调用），
所以 `-v "$PWD":/work` 挂当前目录就够；产物在宿主机属当前用户，可直接改名移动。
想要中文段首缩进加 `-V indent=true`（ctexart 默认西式段落，靠段间距分隔）。

**PDF（无 docker 时的退路）**：pandoc 出 HTML 再用 Chrome headless 打印——
排版逊于 LaTeX（无连字符断词、中英间距不自动），但零依赖：
```bash
pandoc draft.md -f markdown-smart --citeproc --bibliography="$BIB" -s --css=paper.css --embed-resources -o draft.html
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless --disable-gpu \
  --no-pdf-header-footer --print-to-pdf=draft.pdf "file://$PWD/draft.html"
```

指定期刊样式加 `--csl=<style>.csl`（CSL 样式库：https://github.com/citation-style-language/styles）。
札记本身的渲染另有现成脚本 `scripts/render_notes.sh`。

### 中文出稿的三个坑（都踩过）

1. **必须加 `-f markdown-smart`**：否则 pandoc 把中文引号转成 LaTeX 的 `` `` '' ``，
   在 xeCJK 下渲染成西文引号。源文件里直接写中文弯引号 `“…”`，别写 ASCII `"`。
2. **`-o x.tex` 出的是片段**（无 `\documentclass`、无 CJK 设置），拿到 TeX 环境编不过；
   要独立编译必须加 `-s` 且指定 `documentclass=ctexart`。
3. **不要指定 macOS 字体名**（如 `-V CJKmainfont="Songti SC"`）：容器里没有，
   ctex 会自动选 Fandol。只有在本机装了 TeX 时才用系统字体名。
   另：YAML 头里的 `title:` 与正文 H1 只留一个，否则标题会印两遍。

## 硬规则

- **引用前确认无撞键**：`jq '.citekey_collisions' output/scholar_notes/literature_index.json` 应为 `[]`；
  非空先跑 `PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/notes_index.py --fix-collisions`（否则同键不同文会串引用）。
- **书目落后于札记时**先刷新：`PYTHONPATH=. /Users/xiaolibird/miniconda3/envs/env002_reader/bin/python3.12 scripts/notes_index.py`
  （会同时重建 `literature_index.json` 与 `all_references.json`，内容未变不落盘）。
- **全局书目只覆盖 keeper 键**（`duplicate_of == null`）：被判重条目自己的 citekey 不在其中。
  所以渲染**月度札记 md 本身**时仍用同月的 `科研札记_*.references.json`，别用全局书目。
- **不要编造论据**：highlights 里的句子是从 PDF 亲证过的，可直接改写引用；库里没有的主张不要替论文说。
- citekey 多为「作者+年份」兜底键而非 Zotero/BBT 权威键；跨系统对账以 **DOI** 为论文身份。
- 需要浏览式查库（按月/按优先级/读某篇精读全文）用 `scholar-notes`；
  库里没有、要临时搜 arXiv/PubMed 用 `scholar-search`；要把新 PDF 读进库用 `read-paper`。
