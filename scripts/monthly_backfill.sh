#!/usr/bin/env bash
# 按月历史回填科研札记:逐月从 Gmail + PubMed + arXiv(关键词+该月时间窗)拉取,
# 写入 Zotero(ScholarDigest)、拿 citekey、全文精读高优先级 top-N,产出每月一篇 md + 样式化 docx。
#
# 用法:
#   scripts/monthly_backfill.sh [--since YYYY-MM] [--until YYYY-MM] [--provider NAME] [--dry]
#     --since   起始月(默认 2022-01)      --until  结束月(默认当前月)
#     --provider LLM 提供商(默认 deepseek) --dry    只打印将执行的命令,不真跑
#
# 先验证近 2-3 月:  scripts/monthly_backfill.sh --since 2026-05
# 全量回填(备用):  scripts/monthly_backfill.sh --since 2022-01
#
# 前提(交互式跑,非无人值守):
#   - Zotero 桌面端开着,左栏选中 "ScholarDigest" 分类(护栏 PROCESSING__ZOTERO_COLLECTION 校验)
#   - translation-server 容器在跑(PROCESSING__ZOTERO_TRANSLATION_SERVER_URL 已配)
#   - config/scholar.env 里 PROCESSING__CLOSEREAD_ENABLED 由 --close-read 覆盖开启
set -euo pipefail

SINCE="2022-01"
UNTIL="$(date +%Y-%m)"
PROVIDER="deepseek"
DRY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --since) SINCE="$2"; shift 2 ;;
    --until) UNTIL="$2"; shift 2 ;;
    --provider) PROVIDER="$2"; shift 2 ;;
    --dry) DRY=1; shift ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

cd "$(dirname "$0")/.."

# 月份 → 整数(year*12+month-1),便于自增遍历
start=$(( ${SINCE%-*} * 12 + 10#${SINCE#*-} - 1 ))
end=$(( ${UNTIL%-*} * 12 + 10#${UNTIL#*-} - 1 ))
[[ $start -le $end ]] || { echo "❌ --since 晚于 --until"; exit 1; }

echo "按月回填: $SINCE ~ $UNTIL  (provider=$PROVIDER, 共 $((end-start+1)) 个月)"
echo "⚠️ 需 Zotero 开着并选中 ScholarDigest、translation-server 在跑。"
[[ $DRY -eq 1 ]] && echo "(dry-run: 只打印命令)"

ok=0; fail=0
for idx in $(seq "$start" "$end"); do
  yy=$(( idx / 12 )); mm=$(( idx % 12 + 1 ))
  M=$(printf "%04d-%02d" "$yy" "$mm")
  cmd=(python scholar_main.py digest --month "$M" --zotero --close-read --no-mark-read --provider "$PROVIDER")
  echo "──────── $M ────────"
  echo "  ${cmd[*]}"
  if [[ $DRY -eq 1 ]]; then continue; fi
  if "${cmd[@]}"; then
    ok=$((ok+1))
  else
    fail=$((fail+1)); echo "  ⚠️ $M 失败,继续下一月"
  fi
done
echo "完成: 成功 $ok / 失败 $fail  → 每月札记见 output/scholar_notes/digest_YYYY-MM.{md,docx}"
