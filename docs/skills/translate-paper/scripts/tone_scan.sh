#!/bin/bash
# 扫描译文里残留的 AI 腔/翻译腔触发标记（lieflat-less-ai-tone 第 1、4、9、10 条）。
# 只做定位，不自动改：命中处由编排者对照原文判断（原文本身的 not…but、证明里的「因此」通常要保留）。
#
# 用法: bash tone_scan.sh <译文 .tex 文件...>
set -uo pipefail
[ $# -ge 1 ] || { echo "用法: bash tone_scan.sh <译文.tex...>"; exit 2; }

ALL="$(cat "$@" | grep -v '^\s*%')"
scan() {
    local label="$1" pat="$2"
    local n
    n=$(printf '%s\n' "$ALL" | grep -o "$pat" | wc -l | tr -d ' ')
    printf '%-24s %s\n' "$label" "$n"
    if [ "$n" -gt 0 ]; then
        printf '%s\n' "$ALL" | grep -n "$pat" | cut -c1-160 | head -8 | sed 's/^/    /'
    fi
}
scan "破折号 ——"            '——'
scan "当……时，"             '\(^\|[，。；：]\)当[^前然中地下即初][^。，；]\{1,40\}时，'
scan "对于/就……来说/而言"   '\(对于\|就\)[^。，]\{1,20\}\(来说\|而言\)'
scan "这意味着/这表明"       '这\(意味着\|表明\|说明\)'
scan "句首 然而/此外/因此"   '\(^\|。\)\(然而\|此外\|因此\|与此同时\|换言之\|总而言之\)，'
scan "不是……而是"           '不是[^。]\{1,30\}而是'
scan "说白了/说穿了"         '说\(白\|穿\)了'
scan "英文直引号"            "\`\`\|''"
