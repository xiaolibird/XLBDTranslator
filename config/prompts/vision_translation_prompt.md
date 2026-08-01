# Vision Translation Task

The user message provides the page context (`<previous_context>`, the source text of the preceding page — reference only) and the image to translate.

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
If the source text cuts off mid-quote, keep the quote open in the translation.
Correct: `He said, "Truth is not` → `他说："真理并非` (quote left open).
Wrong: closing it artificially as `他说："真理并非……"`.

---

# Output Format
```json
{ "translation": "译文内容" }
```

**Escape Rules**: `"` → `\"`, newline → `\n`

Follow system_instruction for all other translation rules.
