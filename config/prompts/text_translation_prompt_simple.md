# Context
<previous_context>
{context}
</previous_context>

# Task
Translate the provided JSON array of text segments into Chinese. Each segment must be translated with precision while maintaining the global flow of the book.

# Instructions
1. Translate ONLY the body text. Ignore headers, footers, or page numbers.
2. Connect naturally with the previous context.
3. Maintain consistent terminology.
4. Translate every word of the main body text.
5. Use natural Chinese sentence structures.
6. Convert English quotes to Chinese quotes.
7. For citations at sentence end, use [1] format.

# Output Format
Return a valid JSON array:
```json
[
  {"id": 1, "translation": "翻译内容1"},
  {"id": 2, "translation": "翻译内容2"}
]
```

**Rules:**
- Output must be valid JSON array
- Each object has "id" (integer) and "translation" (string)
- Escape double quotes as \"
- Escape newlines as \n
- IDs must match input exactly
- No extra content or comments

# Input
{input_json}