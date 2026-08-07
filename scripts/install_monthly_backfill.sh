#!/bin/bash
# ============================================================
# 安装/卸载 科研札记月度回填 的 launchd 用户代理
# （每月 1 日 21:30 出上一自然月札记 + 刷新文献索引）
#
# 用法:
#   bash scripts/install_monthly_backfill.sh              # 安装
#   bash scripts/install_monthly_backfill.sh --uninstall  # 卸载
#
# 与既有 weekly job（com.xlbd.scholar-digest）互补，互不影响；
# 复用同一 python 实体二进制 → TCC 完全磁盘访问无需再授权。
#
# 安装后验证:
#   launchctl kickstart gui/$(id -u)/com.xlbd.scholar-monthly-backfill
#   tail -f ~/Library/Logs/xlbd-scholar-digest/cron_monthly.log
# ============================================================
set -euo pipefail

LABEL="com.xlbd.scholar-monthly-backfill"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_TEMPLATE="${REPO_ROOT}/config/launchd/com.xlbd.scholar-monthly.plist"
PLIST_TARGET="${HOME}/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="${HOME}/Library/Logs/xlbd-scholar-digest"
GUI_DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "--uninstall" ]]; then
    launchctl bootout "${GUI_DOMAIN}/${LABEL}" 2>/dev/null || true
    rm -f "${PLIST_TARGET}"
    echo "✅ 已卸载 ${LABEL}"
    exit 0
fi

# 解析真实 python 二进制（穿透 symlink：TCC 授权认实际可执行文件）
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
if [[ -z "${PYTHON_BIN}" ]]; then
    echo "❌ 未找到 python3，请设置 PYTHON_BIN 环境变量" >&2
    exit 1
fi
PYTHON_REAL="$(readlink -f "${PYTHON_BIN}")"

echo "仓库路径: ${REPO_ROOT}"
echo "Python:   ${PYTHON_REAL}"
echo "日志目录: ${LOG_DIR}"

mkdir -p "${HOME}/Library/LaunchAgents" "${LOG_DIR}"

sed -e "s|__REPO_ROOT__|${REPO_ROOT}|g" \
    -e "s|__PYTHON_BIN__|${PYTHON_REAL}|g" \
    -e "s|__LOG_DIR__|${LOG_DIR}|g" \
    "${PLIST_TEMPLATE}" > "${PLIST_TARGET}"

# 重新加载（幂等：先卸载旧的再装载）
launchctl bootout "${GUI_DOMAIN}/${LABEL}" 2>/dev/null || true
launchctl bootstrap "${GUI_DOMAIN}" "${PLIST_TARGET}"

echo "✅ 已安装 ${LABEL}（每月 1 日 21:30 自动出上月札记 + 刷索引）"
echo ""
WEEKLY_PY="$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' \
    "${HOME}/Library/LaunchAgents/com.xlbd.scholar-digest.plist" 2>/dev/null || true)"
if [[ -n "${WEEKLY_PY}" && "${WEEKLY_PY}" == "${PYTHON_REAL}" ]]; then
    echo "ℹ️ 与 weekly job 使用同一 python（${PYTHON_REAL}），TCC 已授权，无需再操作。"
elif [[ "${REPO_ROOT}" == "${HOME}/Documents/"* || "${REPO_ROOT}" == "${HOME}/Desktop/"* || "${REPO_ROOT}" == "${HOME}/Downloads/"* ]]; then
    echo "⚠️ 本 job 的 python 与 weekly 不同（或 weekly 未装），需一次性授权："
    echo "   系统设置 > 隐私与安全性 > 完全磁盘访问权限 > ➕ 添加："
    echo "   ${PYTHON_REAL}"
    echo ""
fi
echo "   验证:   launchctl kickstart ${GUI_DOMAIN}/${LABEL}"
echo "   看日志: tail -f ${LOG_DIR}/cron_monthly.log"
