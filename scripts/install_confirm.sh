#!/bin/bash
# ============================================================
# 安装/卸载 入库确认 launchd 用户代理（可信度加固 WO-8a / WO-8c）
#
# 用法:
#   bash scripts/install_confirm.sh              # 安装（每天 10:00 周 digest 自动筛选 + 每月 1 日 10:30 画像漂移提醒）
#   bash scripts/install_confirm.sh --uninstall  # 卸载
#
# 与既有 job 互不影响；复用同一 python 实体二进制 → TCC 完全磁盘访问无需再授权。
# 本脚本只读写 ~/Library/LaunchAgents 里它自己的 label，不碰 digest / weekly-ingest / monthly 的 plist。
#
# 安装后验证（会立刻触发一次，有待确认论文时会真的弹窗）:
#   launchctl kickstart gui/$(id -u)/com.xlbd.scholar-confirm
#   tail -f ~/Library/Logs/xlbd-scholar-digest/cron_confirm.log
# ============================================================
set -euo pipefail

LABELS=("com.xlbd.scholar-confirm" "com.xlbd.scholar-drift")
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${HOME}/Library/Logs/xlbd-scholar-digest"
GUI_DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "--uninstall" ]]; then
    for LABEL in "${LABELS[@]}"; do
        launchctl bootout "${GUI_DOMAIN}/${LABEL}" 2>/dev/null || true
        rm -f "${HOME}/Library/LaunchAgents/${LABEL}.plist"
        echo "✅ 已卸载 ${LABEL}"
    done
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

for LABEL in "${LABELS[@]}"; do
    TEMPLATE="${REPO_ROOT}/config/launchd/${LABEL}.plist"
    TARGET="${HOME}/Library/LaunchAgents/${LABEL}.plist"
    sed -e "s|__REPO_ROOT__|${REPO_ROOT}|g" \
        -e "s|__PYTHON_BIN__|${PYTHON_REAL}|g" \
        -e "s|__LOG_DIR__|${LOG_DIR}|g" \
        "${TEMPLATE}" > "${TARGET}"
    plutil -lint "${TARGET}" >/dev/null
    # 重新加载（幂等：先卸载旧的再装载）
    launchctl bootout "${GUI_DOMAIN}/${LABEL}" 2>/dev/null || true
    launchctl bootstrap "${GUI_DOMAIN}" "${TARGET}"
    echo "✅ 已安装 ${LABEL}"
done
echo ""
echo "   验证:   launchctl kickstart ${GUI_DOMAIN}/${LABELS[0]}"
echo "   看日志: tail -f ${LOG_DIR}/cron_confirm.log  （漂移 job：cron_drift.log）"
