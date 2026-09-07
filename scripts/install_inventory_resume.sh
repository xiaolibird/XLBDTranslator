#!/bin/bash
# ============================================================
# 安装/卸载「无标识 keeper 盘点」的每日续跑 launchd 用户代理
#
# 用法:
#   bash scripts/install_inventory_resume.sh              # 装（每日 09:10）
#   bash scripts/install_inventory_resume.sh --uninstall  # 卸
#
# 为什么要它：OpenAlex 2026-08 起改按日额度计费（$0.001/请求、免费约 $0.1/日、
# UTC 零点重置），全库一天问不完。每天补问一批，2~3 天问完。
#
# ⚠️ 这是**临时任务**，不是常驻服务。盘点跑完（`--resume-from latest --dry-run`
#    显示待重查 0 条）后请手动卸载——留着也只是每天空跑一次、无副作用，但没有意义。
#
# 与其它 6 个 job 的差别：**不设 RunAtLoad**。漏跑一天没有任何损失（第二天补上），
# 而 RunAtLoad 会让每次登录都白烧一次当日额度。
#
# 安装后验证:
#   launchctl kickstart gui/$(id -u)/com.xlbd.scholar-inventory
#   tail -f ~/Library/Logs/xlbd-scholar-digest/cron_inventory.log
# ============================================================
set -euo pipefail

LABEL="com.xlbd.scholar-inventory"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_TEMPLATE="${REPO_ROOT}/config/launchd/${LABEL}.plist"
PLIST_TARGET="${HOME}/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="${HOME}/Library/Logs/xlbd-scholar-digest"
GUI_DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "--uninstall" ]]; then
    launchctl bootout "${GUI_DOMAIN}/${LABEL}" 2>/dev/null || true
    rm -f "${PLIST_TARGET}"
    echo "✅ 已卸载 ${LABEL}（已产出的盘点报告不动）"
    exit 0
fi

# 礼貌池邮箱：Crossref/OpenAlex 都认它。取 config/scholar.env 里已有的那个，
# 免得把邮箱写死进仓库文件。
EMAIL="${INVENTORY_EMAIL:-}"
if [[ -z "${EMAIL}" && -f "${REPO_ROOT}/config/scholar.env" ]]; then
    EMAIL="$(grep -E '^PROCESSING__(EXTERNAL|ZOTERO)_EMAIL=' "${REPO_ROOT}/config/scholar.env" \
             | head -1 | cut -d= -f2- | tr -d '"'"'"' ' || true)"
fi
if [[ -z "${EMAIL}" ]]; then
    echo "❌ 没找到礼貌池邮箱。设 INVENTORY_EMAIL=you@x.org 再跑，" >&2
    echo "   或在 config/scholar.env 里配 PROCESSING__EXTERNAL_EMAIL。" >&2
    exit 1
fi

# 没有基线报告就不装：--resume-from latest 会直接退 2，装了也只是每天报错
INV_DIR="${REPO_ROOT}/output/scholar_notes/_inventory"
if ! ls "${INV_DIR}"/missing_ids_*.json >/dev/null 2>&1; then
    echo "❌ ${INV_DIR} 下没有盘点报告，本 job 是**续跑**用的。" >&2
    echo "   先跑一次全量：PYTHONPATH=. python scripts/inventory_missing_ids.py --email ${EMAIL}" >&2
    exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
if [[ -z "${PYTHON_BIN}" ]]; then
    echo "❌ 未找到 python3，请设置 PYTHON_BIN 环境变量" >&2
    exit 1
fi
PYTHON_REAL="$(readlink -f "${PYTHON_BIN}")"

echo "仓库路径: ${REPO_ROOT}"
echo "Python:   ${PYTHON_REAL}"
echo "邮箱:     ${EMAIL}"

mkdir -p "${HOME}/Library/LaunchAgents" "${LOG_DIR}"

sed -e "s|__REPO_ROOT__|${REPO_ROOT}|g" \
    -e "s|__PYTHON_BIN__|${PYTHON_REAL}|g" \
    -e "s|__LOG_DIR__|${LOG_DIR}|g" \
    -e "s|__EMAIL__|${EMAIL}|g" \
    "${PLIST_TEMPLATE}" > "${PLIST_TARGET}"
plutil -lint "${PLIST_TARGET}" >/dev/null

launchctl bootout "${GUI_DOMAIN}/${LABEL}" 2>/dev/null || true
launchctl bootstrap "${GUI_DOMAIN}" "${PLIST_TARGET}"

REMAIN="$(cd "${REPO_ROOT}" && PYTHONPATH=. "${PYTHON_REAL}" scripts/inventory_missing_ids.py \
          --resume-from latest --dry-run 2>/dev/null | grep -oE '待重查 [0-9]+ 条' || echo '待重查 ? 条')"

echo ""
echo "✅ 已安装 ${LABEL}（每日 09:10 续跑一批；当前${REMAIN}）"
echo ""
echo "   ℹ️ 这是**临时任务**：盘点问完后请手动卸载。"
echo "      判断跑完了没有：PYTHONPATH=. python scripts/inventory_missing_ids.py --resume-from latest --dry-run"
echo "   验证:   launchctl kickstart ${GUI_DOMAIN}/${LABEL}"
echo "   看日志: tail -f ${LOG_DIR}/cron_inventory.log"
echo "   卸载:   bash scripts/install_inventory_resume.sh --uninstall"
