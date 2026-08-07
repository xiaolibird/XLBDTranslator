#!/bin/bash
# ============================================================
# 把 docs/skills/ 下的 SKILL.md 同步到 ~/.claude/skills/ 的安装副本
#
# docs/skills/ 是唯一真相源，改完 SKILL.md 必须跑本脚本才会生效到 agent 实际读到的
# ~/.claude/skills/ ——两侧曾经各自漂移（安装版多写、docs 版漏同步），全部靠这条脚本纠正。
#
# 用法:
#   bash scripts/install_skills.sh              # 同步全部五个 skill
#   bash scripts/install_skills.sh --dry-run    # 只看会同步什么，不动文件
#
# 只同步下面列出的五个目录，不用 --delete 整个 ~/.claude/skills/（那里还装着
# 其它与本仓库无关的 skill，删了会误伤）；每个目录内部用 --delete 是安全的，
# 因为该目录整个由 docs/skills/ 下同名目录定义，装的就该是 docs 版原样。
# ============================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCS_SKILLS="${REPO_ROOT}/docs/skills"
INSTALL_SKILLS="${HOME}/.claude/skills"

SKILLS=(read-paper scholar-notes scholar-search scholar-write screen-journal)

usage() {
    cat <<'USAGE'
用法: bash scripts/install_skills.sh [--dry-run] [-h|--help]

把 docs/skills/ 下的五个 SKILL.md 同步到 ~/.claude/skills/ 的安装副本。

选项:
  --dry-run    只打印会同步的内容，不落盘（透传给 rsync -n）
  -h, --help   显示本帮助并退出
USAGE
}

DRY_RUN=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN="--dry-run"
            echo "（--dry-run：只打印会同步的内容，不落盘）"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "❌ 未知参数: ${1}" >&2
            usage >&2
            exit 1
            ;;
    esac
done

mkdir -p "${INSTALL_SKILLS}"

for skill in "${SKILLS[@]}"; do
    src="${DOCS_SKILLS}/${skill}/"
    dst="${INSTALL_SKILLS}/${skill}/"

    if [[ ! -d "${src}" ]]; then
        echo "❌ 找不到源目录：${src}" >&2
        exit 1
    fi

    echo "同步: ${skill}"
    # --delete 只作用于这一个 skill 子目录内部（--delete 是 rsync 参数，随 src/dst 这对
    # 目录生效，不会波及 INSTALL_SKILLS 下其它 skill）；trailing slash 保证拷贝目录内容
    # 而不是把 src 目录本身嵌套进 dst。
    rsync -av --delete ${DRY_RUN} "${src}" "${dst}"
done

echo ""
echo "✅ 完成：docs/skills/ → ~/.claude/skills/（${#SKILLS[@]} 个 skill）"
if [[ -n "${DRY_RUN}" ]]; then
    echo "   （dry-run，未落盘；去掉 --dry-run 重跑以真正同步）"
fi
