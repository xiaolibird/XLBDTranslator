你是翻译子代理。论文 {ARXIV_ID} 按 {LICENSE} 许可发布，允许翻译。先完整读规范文件 {GUIDE_PATH}，以及其中提到的 SKILL.md。

输入：{INPUT_PATH}（{SCOPE}）
输出：{OUTPUT_PATH}（用 Write 写入），完整覆盖输入的每一行。
{EXTRA}

约束：只写这一个输出文件，不得改动其他任何文件；禁止任何 git 操作（add/commit/checkout/stash/reset 等一律不许）；不要编译。
写完后自查：
  python3 -I {SKILL_DIR}/scripts/verify_chunk.py {INPUT_PATH} {OUTPUT_PATH}
必须全部 ✅；有 ❌ 就改到通过为止（数学片段只许顺序不同，内容不许变）。
最后用中文简短回报：完成情况、verify_chunk 结果、术语表之外你新定的译名（若有）。
