#!/bin/bash
# ============================================================
# 安装/卸载「札记库索引 → 语义向量库」自动同步的 launchd 用户代理
#
# 用法:
#   bash scripts/install_embed_sync.sh              # 装
#   bash scripts/install_embed_sync.sh --uninstall  # 卸
#
# 触发方式：监视 output/scholar_notes/ 下的 literature_index.json 与 abstracts.json，
# 一变就增量同步（非定时）。abstracts.json 由 backfill_abstracts.py 产出且不碰索引，
# 不盯它的话摘要回填完 ab: 厚向量要等下一次无关索引变动才生效。
# 装它是为了堵住一个真实缺口：向量库会因为**入库之外**的原因变旧（改 citekey、改元数据、
# 手工重建索引），而在此之前唯一的自动同步入口是周度入库，且只在「本周有新论文」时才走到
# 同步——空窗周整段跳过。库一旧，notes_search --cite 就会吐出磁盘上已不存在的 citekey。
#
# ⚠️ macOS TCC：仓库在 ~/Documents 下时，launchd 后台任务需要一次性「完全磁盘访问权限」
#    授权。授权绑定 python 实体二进制、与参数无关，所以 weekly digest / vault sync 已授权
#    过的话这里无需再授权（脚本结尾会判断并提示）。
#
# 安装后验证:
#   launchctl kickstart gui/$(id -u)/com.xlbd.scholar-embed
#   tail -f ~/Library/Logs/xlbd-scholar-digest/cron_embed.log
# ============================================================
set -euo pipefail

LABEL="com.xlbd.scholar-embed"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_TEMPLATE="${REPO_ROOT}/config/launchd/${LABEL}.plist"
PLIST_TARGET="${HOME}/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="${HOME}/Library/Logs/xlbd-scholar-digest"   # 与 weekly/monthly/vault 同一日志目录
GUI_DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "--uninstall" ]]; then
    launchctl bootout "${GUI_DOMAIN}/${LABEL}" 2>/dev/null || true
    rm -f "${PLIST_TARGET}"
    echo "✅ 已卸载 ${LABEL}（向量库文件本身不动）"
    exit 0
fi

# 监视的是索引文件；它不存在说明札记库还没建索引，装了也只会空转
INDEX="${REPO_ROOT}/output/scholar_notes/literature_index.json"
if [[ ! -f "${INDEX}" ]]; then
    echo "❌ 找不到索引：${INDEX}" >&2
    echo "   先跑：PYTHONPATH=. python scripts/notes_index.py" >&2
    exit 1
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
plutil -lint "${PLIST_TARGET}" >/dev/null   # 占位符替换出语法错时，早于 bootstrap 报出来

# 重新加载（幂等：先卸载旧的再装载）
launchctl bootout "${GUI_DOMAIN}/${LABEL}" 2>/dev/null || true
launchctl bootstrap "${GUI_DOMAIN}" "${PLIST_TARGET}"

echo ""
echo "✅ 已安装 ${LABEL}（索引一变即增量同步向量库，节流 600s）"
echo ""
if [[ -f "${HOME}/Library/LaunchAgents/com.xlbd.scholar-digest.plist" || -f "${HOME}/Library/LaunchAgents/com.xlbd.scholar-vault.plist" ]]; then
    echo "   ℹ️ 检测到其它 scholar job 已安装：TCC 授权绑定 python 二进制，此处无需重复授权。"
elif [[ "${REPO_ROOT}" == "${HOME}/Documents/"* ]]; then
    echo "   ⚠️ 仓库位于受 macOS 隐私保护的目录，需一次性授权："
    echo "      系统设置 > 隐私与安全性 > 完全磁盘访问权限 > ➕ 添加："
    echo "      ${PYTHON_REAL}"
    echo "      （对话框中按 Cmd+Shift+G 粘贴上面的路径）"
fi
echo "   ⚠️ 本 job 需要 Ollama 常驻（bge-m3）。Ollama 没起时以退出码 3 收场，"
echo "      原因写在 ${LOG_DIR}/cron_embed.err.log，下次索引变动自动重试。"
echo "   验证:     launchctl kickstart ${GUI_DOMAIN}/${LABEL}"
echo "   看日志:   tail -f ${LOG_DIR}/cron_embed.log"
echo "   手工同步: PYTHONPATH=. python scripts/notes_embed.py"
echo "   看状态:   PYTHONPATH=. python scripts/notes_embed.py --stats"
echo "   卸载:     bash scripts/install_embed_sync.sh --uninstall"
