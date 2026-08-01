# Dynamic Input Specification

## Input Structure
```
<previous_context>前文翻译</previous_context>
<glossary>
- **term**: translation
</glossary>
# Input Data
[{"id": 1, "text": "..."}]
```

---

# Task
Translate the JSON array into Chinese. Connect with `<previous_context>`. Follow `<glossary>` strictly.

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
