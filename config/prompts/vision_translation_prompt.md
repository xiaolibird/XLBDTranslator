# Vision Translation Task

## Context
<previous_context>
{context}
</previous_context>

---

# IMAGE-SPECIFIC RULES (图像专用规则)

## Pre-processing Awareness
- Image is **cropped** (margins removed)
- Edge artifacts = ignore
- Multi-column: left→right, top→bottom

## Content Rules
1. **JUST TRANSLATE** - No descriptions ("The image shows...")
2. **No Structural Markers** - No `##`, `######`
3. **Preserve Lists** - Keep bullet/numbered format

## Open Quote Handling
If text cuts mid-quote:
- Source: `He said, "Truth is not`
- ✅ Output: `他说："真理并非` (open quote)
- ❌ NOT: `他说："真理并非……"` (wrongly closed)

---

# Output Format
```json
{ "translation": "译文内容" }
```

**Escape Rules**: `"` → `\"`, newline → `\n`

Follow system_instruction for all other translation rules.
