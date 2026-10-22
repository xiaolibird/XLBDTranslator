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

## 索引与链接（有该结构才处理）

解析器只提取叶子块，嵌套 `li` 的父词目可能遗漏；电子索引的 `here` 是定位链接，不是正文。核查原 EPUB 的索引层级和 `epub:type="index-locator"` 锚点，逐个对应到原文段落所在的片段。不要仅从中文词频推测页码。

将恢复结果写 `reviews/index.json`：

```json
{"reviewed":true,"segment_ids":[158,159],"source_locator_count":3,"entries":[{"label":"审核后的中文父词目","depth":0,"targets":[3,7]},{"label":"子词目","depth":1,"targets":[8]}]}
```

`segment_ids` 必须是完整索引区域，目标必须存在且不能也在被替换索引中；`targets` 保留原锚点对应关系，可以合并同一片段的重复定位。对遗漏父词目补译并核验。渲染器生成 EPUB 内部链接和 PDF 可点击页码，标明定位精度是**对应译文片段起始处**，而非声称精确定位到原文某一个词。
