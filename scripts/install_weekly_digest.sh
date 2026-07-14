#!/bin/bash
# ============================================================
# 安装/卸载 Scholar Digest 每周自动运行的 launchd 用户代理
#
# 用法:
#   bash scripts/install_weekly_digest.sh              # 安装（默认每周一 09:00）
#   bash scripts/install_weekly_digest.sh --uninstall  # 卸载
#
# ⚠️ macOS TCC：仓库在 ~/Documents 下时，launchd 后台任务访问它
#    需要一次性授权（脚本结尾会打印具体操作），否则运行报
#    "Operation not permitted"。
#
# 安装后验证:
#   launchctl kickstart gui/$(id -u)/com.xlbd.scholar-digest
#   tail -f ~/Library/Logs/xlbd-scholar-digest/cron_digest.log
# ============================================================
set -euo pipefail

LABEL="com.xlbd.scholar-digest"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_TEMPLATE="${REPO_ROOT}/scripts/${LABEL}.plist"
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

# 替换占位符生成实际 plist
sed -e "s|__REPO_ROOT__|${REPO_ROOT}|g" \
    -e "s|__PYTHON_BIN__|${PYTHON_REAL}|g" \
    -e "s|__LOG_DIR__|${LOG_DIR}|g" \
    "${PLIST_TEMPLATE}" > "${PLIST_TARGET}"

# 重新加载（幂等：先卸载旧的再装载）
launchctl bootout "${GUI_DOMAIN}/${LABEL}" 2>/dev/null || true
launchctl bootstrap "${GUI_DOMAIN}" "${PLIST_TARGET}"

echo "✅ 已安装 ${LABEL}（每周一 09:00 自动运行）"
echo ""
if [[ "${REPO_ROOT}" == "${HOME}/Documents/"* || "${REPO_ROOT}" == "${HOME}/Desktop/"* || "${REPO_ROOT}" == "${HOME}/Downloads/"* ]]; then
    echo "⚠️ 仓库位于受 macOS 隐私保护的目录，还需一次性授权（否则后台运行会被系统拒绝）："
    echo "   系统设置 > 隐私与安全性 > 完全磁盘访问权限 > ➕ 添加："
    echo "   ${PYTHON_REAL}"
    echo "   （对话框中按 Cmd+Shift+G 粘贴上面的路径）"
    echo ""
fi
echo "   验证:     launchctl kickstart ${GUI_DOMAIN}/${LABEL}"
echo "   看日志:   tail -f ${LOG_DIR}/cron_digest.log"
