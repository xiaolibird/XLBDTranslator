# 审核文件契约

`reviews/glossary.json`：

```json
{"reviewer":"实际审核者","glossary":{"proper noun":"本书译名"},"remove_terms":[],"notes":["多义词按语境处理的规则"],"ambiguities":[]}
```

`glossary` 命令将其合并到 `cache/<源文件MD5>/glossary_merged.json`，并把 notes 加入本书模式。已开始翻译后变更术语，应暂停运行、记录变更并复核受影响段落，不假装旧译自动使用新表。

`reviews/sol_corrections.json`、`reviews/astra_corrections.json` 等：

```json
{
  "reviewer":"实际模型或审核者",
  "reviewed_segment_ids":[3,7],
  "chapter_title_updates":{"3":"统一后的中文章题"},
  "corrections":[{
    "segment_id":3,
    "original_translation_sha256":"对当前主译 translated_text UTF-8 字节求 SHA-256",
    "corrected_text":"完整修正译文，不只是差异片段",
    "reason":"影响意义的错误及依据"
  }]
}
```

段号统一为整数。每段只能有一条合并后的修正，不串行叠加两位审核者的整段版本。修正哈希必须对应仍在缓存中的主译；重译后需要重新核对。

`reviews/integrity.json` 用于解决审查候选，而非豁免硬失败：

```json
{"integrity_reviews":[{"segment_id":3,"translation_sha256":"审核当前最终译文的SHA-256","complete":true,"foreign_text_necessary":true,"reason":"经核对内容和段尾完整；外文是必要书目或URL"}]}
```

只填写实际确认的布尔项，不因脚本提示而批量放行。短译常见于英法到中文的压缩，但明显只剩开头、引语没收尾、脚注序列缺段、乱码都必须修复。

`reviews/render_policy.json` 可选：

```json
{
  "source_navigation":[{"segment_id":1,"reason":"原书目录由生成目录替代"}],
  "compact_segment_ids":[0],
  "toc_levels":{"16":2}
}
```

导航替换必须有理由，不能拿它跳过失败或缩短正文。完整审校数据仍保留所有源片段。

解析器的旧章首标记或继承章题不可靠时，可在 `render_policy.json` 写 `chapter_metadata_updates`：每项为 `segment_id`、布尔 `is_new_chapter`、字符串 `chapter_title` 与整数 `toc_level`（1–6）。仅更新阅读层元数据，不改缓存或文字；真实段内章目仍以哈希绑定的 `in_segment_headings` 切分。该字段不能覆盖删节提示或版本说明。

## 经用户选择的删节阅读版

确需删节时，先检查用户是否已有明确选择；已有授权直接执行，未选择时再让用户决定删节版或暂缓。审核完整场景、跨段人物与年龄信息、关联注释、回顾、附录直接引文及手稿图片，不能仅删除首次发现的两句就认定其他内容可发布。线索扫描用于定位，须以实际上下文作判断，并记录审查范围；非露骨评论不因提到被删场景便自动删除。

边界报告区分确认需隔离的内容、年龄或语境无法确认时的保守排除，以及为保持场景连续而连带省去的非限制上下文。不把处理片段数或省略字符比例说成限制内容的比例，不沿用某本书的固定段号、比例或图片数量。书前说明交代范围、保守处理和剩余限制，避免将删节阅读版包装为无删节全译。

`reviews/abridgement.json` 单独记录 `reviewed: true`、实际审核者、`user_approved: true`、用户原始选择 `approval_text`、源文件 `source_sha256`、书前 `reader_notice` 和 `notice_before_segment_id`。

`omissions` 每项包含整数 `segment_id`、原文 `source_sha256`、应用语义修正后译文 `translation_sha256`、非露骨 `reason` 及以 `【编者删节：` 开头的可见 `notice`。渲染前用说明替换整段，并从成品结构中清除被省略的原文、译文及关联图片；不改输入和主译缓存，不将省略段登记为 `complete: true`。`excluded_assets` 按图片 `path`、`sha256` 与理由列出排除项。

同一连续场景可用 `group_start_segment_id` 指向第一条省略记录，阅读层只显示一组提示，保留 `omitted_source_segment_ids` 追踪全部源片段，避免每个处理片段重复一遍说明。完整性报告仍列出全部省略源段；PDF/EPUB 验收按该分组核对，不能因此漏记省略或复用旧译文。图片省略也须在对应位置可见标注。

若已有 `delivery_restriction.json`，其 `restricted_source_segment_ids` 必须全部在删节方案中，否则禁止导出。审校报告明确标记 `edition: abridged` 和省略段号，完整性只针对保留正文；书名、文件名与版本说明均应清楚标识删节。验收还须确认书前说明及每处删节提示在 PDF、EPUB 中可见，排除的图片没有被打包。不要用 `source_navigation` 隐藏正文或借删节跳过无关的失败译文。

先隔离再生成，不能先做含限制内容的整书再遮盖。被省略的原文、旧译及图片不得进入成品结构或资源包，输入与缓存保留原状。文字省略处保持简短位置提示，不复述细节；连续范围只显示一组提示并追踪全部源段。相关图片先逐项核实，未能确认时只对该书、该版本记录保守排除和可见图版说明，已核实可保留的封面、图表照常保留。

作者原稿的划去文字、编辑方括号和本版编者删节是不同层次。源字符区间或符号数量不能可靠推出中文范围时，不按比例或成对符号数量自动补删除线；先逐处对齐，暂不能复原的在版本说明中如实注明。

## 索引与链接（有该结构才处理）

解析器只提取叶子块，嵌套 `li` 的父词目可能遗漏；电子索引的 `here` 是定位链接，不是正文。核查原 EPUB 的索引层级和 `epub:type="index-locator"` 锚点，逐个对应到原文段落所在的片段。不要仅从中文词频推测页码。

将恢复结果写 `reviews/index.json`：

```json
{"reviewed":true,"segment_ids":[158,159],"source_locator_count":3,"entries":[{"label":"审核后的中文父词目","depth":0,"targets":[3,7]},{"label":"子词目","depth":1,"targets":[8]}]}
```

`segment_ids` 必须是完整索引区域，目标必须存在且不能也在被替换索引中；`targets` 保留原锚点对应关系，可以合并同一片段的重复定位。对遗漏父词目补译并核验。渲染器生成 EPUB 内部链接和 PDF 可点击页码，标明定位精度是**对应译文片段起始处**，而非声称精确定位到原文某一个词。
