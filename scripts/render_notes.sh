#!/usr/bin/env bash
# 把 scholar 科研札记（含 [@citekey] 引用）用 pandoc --citeproc 渲染成 docx/pdf/latex。
#
# 用法:
#   scripts/render_notes.sh [路径] [格式] [参考文献] [CSL样式]
#     路径     : 单篇 .md 或札记目录（默认 output/scholar_notes）
#     格式     : docx | pdf | latex | tex（默认 docx；pdf 需装 xelatex）
#     参考文献 : .bib / .json（默认用札记目录里的 references.json；
#                长期用可指向 Zotero Better BibTeX 自动导出的 library.bib）
#     CSL样式  : 可选，.csl 文件（默认用 pandoc 内置 chicago-author-date）
#
# 示例:
#   scripts/render_notes.sh                                   # 渲染整目录为 docx
#   scripts/render_notes.sh output/scholar_notes/foo-abc.md pdf
#   scripts/render_notes.sh output/scholar_notes docx ~/Zotero/library.bib nature.csl
set -euo pipefail

INPUT="${1:-output/scholar_notes}"
FORMAT="${2:-docx}"
BIB="${3:-}"
CSL="${4:-}"

command -v pandoc >/dev/null 2>&1 || { echo "❌ 未找到 pandoc，请先安装（brew install pandoc）"; exit 1; }

case "$FORMAT" in
  tex) EXT="tex"; FMT="latex" ;;
  latex) EXT="tex"; FMT="latex" ;;
  pdf) EXT="pdf"; FMT="pdf" ;;
  docx) EXT="docx"; FMT="docx" ;;
  *) echo "❌ 不支持的格式: $FORMAT（docx|pdf|latex）"; exit 1 ;;
esac

# 收集待渲染的 markdown
if [[ -d "$INPUT" ]]; then
  NOTES_DIR="$INPUT"
  FILES=()  # 可移植（macOS 自带 bash 3.2 无 mapfile）
  while IFS= read -r line; do FILES+=("$line"); done < <(find "$INPUT" -maxdepth 1 -name '*.md' | sort)
else
  NOTES_DIR="$(dirname "$INPUT")"
  FILES=("$INPUT")
fi
[[ ${#FILES[@]} -gt 0 ]] || { echo "❌ 没找到 .md 文件: $INPUT"; exit 1; }

# 默认参考文献：札记目录的 references.json（模块自动生成，自包含）
if [[ -z "$BIB" ]]; then
  if [[ -f "$NOTES_DIR/references.json" ]]; then
    BIB="$NOTES_DIR/references.json"
  else
    echo "❌ 未指定参考文献，且 $NOTES_DIR/references.json 不存在"; exit 1
  fi
fi
[[ -f "$BIB" ]] || { echo "❌ 参考文献文件不存在: $BIB"; exit 1; }

PANDOC_ARGS=(--citeproc --bibliography="$BIB")
[[ -n "$CSL" ]] && PANDOC_ARGS+=(--csl="$CSL")
[[ "$FMT" == "pdf" ]] && PANDOC_ARGS+=(--pdf-engine=xelatex)

OUT_DIR="$NOTES_DIR/rendered"
mkdir -p "$OUT_DIR"

echo "参考文献: $BIB"
echo "输出格式: $FORMAT → $OUT_DIR/"
ok=0; fail=0
for f in "${FILES[@]}"; do
  base="$(basename "${f%.md}")"
  out="$OUT_DIR/$base.$EXT"
  if pandoc "$f" "${PANDOC_ARGS[@]}" -o "$out" 2>/dev/null; then
    ok=$((ok+1))
  else
    fail=$((fail+1)); echo "  ⚠️ 渲染失败: $f"
  fi
done
echo "完成: 成功 $ok / 失败 $fail → $OUT_DIR/"
