# Dynamic Input Specification

## Input Structure
```
<previous_context>上一段落的英文原文（仅供衔接参考，勿翻译、勿输出）</previous_context>
<glossary>
- **term**: translation
</glossary>
# Input Data
[{"id": 1, "text": "..."}]
```

Note: `<previous_context>` contains the SOURCE text (English) of the preceding segment — use it only for continuity (pronouns, terminology, tone); do NOT translate it or include it in the output.

---

# Task
Translate the JSON array into Chinese. Connect naturally with the preceding text. Follow `<glossary>` strictly.

---

# Content Rules

1. **Body Text Only**: Ignore headers, footers, page numbers
2. **No Structural Markers**: Don't add `##` or `######`
3. **Glossary First**: If term in glossary → use exact translation
4. **No Omissions**: Translate every word

---

# Output
```json
{"translations": [{"id": 1, "translation": "译文"}]}
```

Follow system_instruction JSON rules strictly.
