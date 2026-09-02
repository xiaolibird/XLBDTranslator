#!/bin/bash
# ============================================================
# 安装/卸载「札记库 → iCloud 周快照」的 launchd 用户代理
#
# 用法:
#   bash scripts/install_backup.sh              # 装
#   bash scripts/install_backup.sh --uninstall
#
# 排程：周日 20:00 + RunAtLoad（登录补跑；脚本内 6 天守卫防刷屏）。
# 目的地：~/Library/Mobile Documents/com~apple~CloudDocs/XLBDBackups/
#
# ⚠️ macOS TCC 两点（比其它 job 多一条）：
#   1. 授权绑定 python 实体二进制。既有 5 个 job 的「完全磁盘访问权限」已覆盖
#      iCloud Drive 路径（FDA 覆盖全部受保护位置），**必须复用同一 python 实体**
#      ——用 PYTHON_BIN 换了二进制 = 无授权静默 EPERM（launchd 进程不弹窗）。
#   2. 备份写的是 ~/Library/Mobile Documents：手动终端能跑 ≠ launchd 能跑，
#      安装后务必用 kickstart + XLBD_BACKUP_FORCE=1 验证一次真产出快照
#      （守卫会让"手动测过后的 kickstart"秒退成空验——本仓库 PATH/no_proxy
#      两案都是"手动能跑、定时永远挂"模式漏掉的）。
#
# 安装后验证（必须看到新快照文件落盘）:
#   launchctl kickstart ${GUI_DOMAIN}/com.xlbd.scholar-backup   # 守卫可能秒退
#   XLBD_BACKUP_FORCE=1 PYTHONPATH=. python scripts/backup_snapshot.py  # 强制产出
#   ls -lh ~/Library/Mobile\ Documents/com~apple~CloudDocs/XLBDBackups/
# ============================================================
set -euo pipefail

LABEL="com.xlbd.scholar-backup"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_TEMPLATE="${REPO_ROOT}/config/launchd/${LABEL}.plist"
PLIST_TARGET="${HOME}/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="${HOME}/Library/Logs/xlbd-scholar-digest"   # 与其余 5 个 job 同一日志目录
GUI_DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "--uninstall" ]]; then
    launchctl bootout "${GUI_DOMAIN}/${LABEL}" 2>/dev/null || true
    rm -f "${PLIST_TARGET}"
    echo "✅ 已卸载 ${LABEL}（iCloud 里已有的快照不动）"
    exit 0
fi

# iCloud Drive 本体必须在（登出/未启用时装了也只会周周告警）
CLOUDDOCS="${HOME}/Library/Mobile Documents/com~apple~CloudDocs"
if [[ ! -d "${CLOUDDOCS}" ]]; then
    echo "❌ iCloud Drive 不可用：${CLOUDDOCS}" >&2
    echo "   请先在系统设置里登录 iCloud 并启用 iCloud Drive" >&2
    exit 1
fi

NOTES="${REPO_ROOT}/output/scholar_notes"
if [[ ! -d "${NOTES}" ]]; then
    echo "❌ 札记库不存在：${NOTES}" >&2
    exit 1
fi

# 解析真实 python 二进制（穿透 symlink：TCC 授权认实际可执行文件）
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"
if [[ -z "${PYTHON_BIN}" ]]; then
    echo "❌ 未找到 python3，请设置 PYTHON_BIN 环境变量" >&2
    exit 1
fi
PYTHON_REAL="$(readlink -f "${PYTHON_BIN}")"

echo "仓库路径: ${REPO_ROOT}"
echo "Python:   ${PYTHON_REAL}"
echo "目的地:   ${CLOUDDOCS}/XLBDBackups/"
echo "日志目录: ${LOG_DIR}"

mkdir -p "${HOME}/Library/LaunchAgents" "${LOG_DIR}"

sed -e "s|__REPO_ROOT__|${REPO_ROOT}|g" \
    -e "s|__PYTHON_BIN__|${PYTHON_REAL}|g" \
    -e "s|__LOG_DIR__|${LOG_DIR}|g" \
    "${PLIST_TEMPLATE}" > "${PLIST_TARGET}"
plutil -lint "${PLIST_TARGET}" >/dev/null   # 占位符替换出语法错时，早于 bootstrap 报出来

# 重新加载（幂等：先卸载旧的再装载）
launchctl bootout "${GUI_DOMAIN}/${LABEL}" 2>/dev/null || true
launchctl bootstrap "${GUI_DOMAIN}" "${PLIST_TARGET}"

echo ""
echo "✅ 已安装 ${LABEL}（周日 20:00 + 登录补跑；6 天守卫）"
echo ""
if [[ -f "${HOME}/Library/LaunchAgents/com.xlbd.scholar-digest.plist" ]]; then
    echo "   ℹ️ 检测到 weekly digest 已安装：TCC（完全磁盘访问）绑定 python 二进制，"
    echo "      iCloud Drive 亦在其覆盖内，无需重复授权——前提是同一 python 实体。"
else
    echo "   ⚠️ 需一次性授权完全磁盘访问权限（iCloud Drive 在覆盖内）："
    echo "      系统设置 > 隐私与安全性 > 完全磁盘访问权限 > ➕ ${PYTHON_REAL}"
fi
echo "   验证真产出（守卫会让紧跟手动测试的 kickstart 秒退，用 FORCE 绕过）:"
echo "      XLBD_BACKUP_FORCE=1 PYTHONPATH=. python scripts/backup_snapshot.py"
echo "   看日志:   tail -f ${LOG_DIR}/cron_backup.log"
echo "   恢复演练: PYTHONPATH=. python scripts/backup_snapshot.py --restore-to /tmp/restore_test"
echo "   卸载:     bash scripts/install_backup.sh --uninstall"