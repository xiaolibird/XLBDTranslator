# Dynamic Input Specification

> 定位：仅供本地 Ollama 等 token 预算受限的场景（PromptManager 的 simple 路径）。
> 云端 provider（含 claude-agent）一律用完整版 text_translation_prompt.md，勿再往
> 本文件添加与完整版重复/冲突的规则——引用编号、引号、标点规则以 system
> instruction 为唯一出处。

## Input Structure
The user message contains, in order:
1. `<previous_context>` — the source text (English) of the PRECEDING segment, for continuity reference only; do NOT translate it or include it in output
2. Optional `<glossary>` — terms as `- **term**: translation`
3. `# Input Data` — a JSON array `[{"id": 1, "text": "..."}]` of segments to translate

# Task
Translate each segment in `# Input Data` into Chinese. Connect naturally with the previous context. If a term appears in the glossary, use the specified translation exactly.

# Instructions
1. Translate ONLY the body text. Ignore headers, footers, or page numbers.
2. Translate every word of the main body text; no omissions, no summaries.
3. Use natural Chinese sentence structures.
4. Follow the citation and punctuation rules in the system instruction.

# Output Format
Return one top-level JSON object:
```json
{"translations": [
  {"id": 1, "translation": "翻译内容1"},
  {"id": 2, "translation": "翻译内容2"}
]}
```

Rules: ids must match input exactly; escape `"` as `\"` and newlines as `\n`; no extra content outside the JSON object.
