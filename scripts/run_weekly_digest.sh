#!/bin/bash
# ============================================================
# Scholar Digest 每周自动运行脚本（由 launchd 调度，也可手动执行）
#
# 用法:
#   bash scripts/run_weekly_digest.sh            # 正常运行
#   bash scripts/run_weekly_digest.sh --dry-run  # 透传额外参数
#
# 环境变量:
#   PYTHON_BIN  python 解释器路径（默认 python3；launchd 场景由
#               install_weekly_digest.sh 烘焙绝对路径进 plist）
# ============================================================
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_DIR="${REPO_ROOT}/logs"
LOG_FILE="${LOG_DIR}/cron_digest.log"

mkdir -p "${LOG_DIR}"
cd "${REPO_ROOT}"

{
    echo "============================================================"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Weekly Scholar Digest 启动"
    echo "  python: ${PYTHON_BIN} ($(${PYTHON_BIN} --version 2>&1))"

    # --days 8: 每周运行留 1 天重叠防漏，run 内 DOI/paper_id 去重兜底
    "${PYTHON_BIN}" scholar_main.py digest --days 8 "$@"
    exit_code=$?

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 运行结束，退出码: ${exit_code}"
    echo ""
    exit "${exit_code}"
} >> "${LOG_FILE}" 2>&1
