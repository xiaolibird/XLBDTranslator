#!/bin/bash
# 用 zjuthesis 镜像里的 xelatex 编译译文，然后汇报日志里的问题。
#
# 用法: bash compile.sh <build 目录> [主文件名，默认 main_zh]
#
# 镜像入口脚本改 UID 会报 "UID '0' already exists"，所以必须 --entrypoint sh + -u 当前用户。
# 本机若有 xelatex 就直接用本机的。
set -uo pipefail

BUILD="$(cd "$1" && pwd)"
MAIN="${2:-main_zh}"
IMAGE="zjuthesis:latest"

RUN='xelatex -interaction=nonstopmode MAIN.tex >/dev/null 2>&1
if [ -f MAIN.bcf ]; then biber MAIN >bib.log 2>&1; elif grep -q "\\\\bibdata" MAIN.aux; then bibtex MAIN >bib.log 2>&1; fi
xelatex -interaction=nonstopmode MAIN.tex >/dev/null 2>&1
xelatex -interaction=nonstopmode MAIN.tex >/dev/null 2>&1
echo compiled'
RUN="${RUN//MAIN/$MAIN}"

if command -v xelatex >/dev/null 2>&1; then
    (cd "$BUILD" && sh -c "$RUN")
else
    docker run --rm --entrypoint sh -u "$(id -u):$(id -g)" -e HOME=/tmp \
        -v "$BUILD":/work -w /work "$IMAGE" -c "$RUN"
fi

LOG="$BUILD/$MAIN.log"
PDF="$BUILD/$MAIN.pdf"
[ -f "$LOG" ] || { echo "❌ 没有生成日志 ${LOG}"; exit 1; }

echo "== 致命错误（! 开头）"
grep -n '^!' "$LOG" | head -20 || true
echo "== 未定义引用/文献"
grep -E "Reference .* undefined|Citation .* undefined|There were undefined" "$LOG" | sort | uniq -c | head -20 || true
echo "== 缺字（Missing character）"
grep 'Missing character' "$LOG" | sort | uniq -c | head -20 || true
echo "== 字体回退（Font shape undefined）"
grep "Font shape .* undefined" "$LOG" | head -10 || true
echo "== Overfull 行数: $(grep -c 'Overfull' "$LOG")"
[ -f "$BUILD/bib.log" ] && { echo "== bib.log 尾部"; tail -3 "$BUILD/bib.log"; }

if [ -f "$PDF" ]; then
    echo "✅ PDF: ${PDF}（$(du -h "${PDF}" | cut -f1)）"
else
    echo "❌ 没有生成 PDF"; exit 1
fi
