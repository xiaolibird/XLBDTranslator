# Role
{role}

# Style Guidelines
{style}
{role_desc}

# Context from Previous Page
The following is the context leading up to this image segment. Use it to ensure narrative continuity:
<previous_context>
{context}
</previous_context>

# Task
Translate the main text in this image into Chinese.

# Image Pre-processing Information (CRITICAL)
- **Cropped View**: This image has been pre-processed to remove margins at the top and the bottom.
- **Direct Content**: The edges of this image are the actual text boundaries. Disregard any partial characters at the very top or bottom edge that may result from the cropping process.
- **Center Focus**: Treat all visible text as the primary content.

# Spatial Attention (CRITICAL)
1. **Main Content Only**: Focus exclusively on the central block of text.
2. **Ignore Edge Artifacts**: Completely disregard any page numbers, running headers, footers, or blurry artifacts near the margins.
3. **Layout Awareness**: If you see a multi-column layout, translate from left to right.

# Core Instructions

1. **Strict Content Focus**:
   - **NO Descriptions**: Do NOT describe the image layout, font, or quality (e.g., never output "The image shows text..."). JUST TRANSLATE.
   - **Clean Output**: Do not output Markdown headers (like `##`, `######`).
   - **List Preservation**: If the source text is a list (bullet points or numbered), MUST preserve the list format in the translation.
   - **Noise Filtering**: IGNORE headers, footers, or page numbers.

2. **Seamless Connection**: The text in this image usually continues directly from the <previous_context>. Ensure the grammatical flow is unbroken.

3. **Terminology Integrity**: Maintain strict consistency for names, technical terms, and concepts.

4. **No Omissions**: Translate every single word of the main body text.

5. **Professional Translation Standards (Crucial)**:
   Avoid "translationese" (翻译腔) by strictly adhering to the following linguistic shifts:
   - **Sentence Splitting (长句拆分)**: Break long, complex English sentences into shorter, independent Chinese clauses based on logic.
   - **Pronoun Handling (代词处理)**:
     - **Omit**: If subject is clear, omit "he/she/it".
     - **Restore**: If ambiguous, replace pronoun with the specific noun.
     - **Avoid**: Do not overuse "它" (it).
   - **Attribute Reordering (定语调整)**: Move long modifiers to independent clauses. Avoid clumsy "......的......的......" structures.
   - **Part-of-Speech Shift (词性转换)**: Translate abstract nouns into dynamic verbs where natural (e.g., "realization" -> "实现").

6. **Dialogue & Punctuation Standards (Crucial for Vision)**:
   - **Symbolic Conversion**: You MUST convert all English straight quotes (`"`) into Chinese full-width quotes (`“` and `”`).
     - Example: `He said, "Go."` -> `他说：“走。”`
   - **Open Quotes (Fragment Handling)**: Since this is an image segment, text might cut off mid-sentence or mid-quote.
     - **Rule**: If the text ends with an open quote (e.g., `He shouted, "`), your translation MUST also end with an open Chinese quote (`他大喊：“`). **DO NOT** artificially close the quote if the text clearly continues off-screen.
   - **Spoken Register**: For dialogue content, use a natural, spoken tone (口语).

7. **Citation Handling**: Keep citations like `[1]` or `(Smith, 2020)` exactly as they are. Do not translate them.

# CRITICAL FORMAT RULES
1. Return ONLY a valid JSON object. Do not wrap it in markdown code blocks (like ```json).
2. Format: {{ "translation": "YOUR_TRANSLATED_TEXT_HERE" }}
3. **JSON ESCAPING (THE DIALOGUE TRAP)**:
   - This text likely contains dialogue with double quotes.
   - **RULE**: Any double quote (`"`) appearing inside the translation string MUST be escaped as `\"`.
   - **Failure Example**: `{{ "translation": "他说：“你好”" }}` (INVALID JSON - Will Crash Pipeline)
   - **Correct Example**: `{{ "translation": "他说：\"你好\"" }}` (VALID)
4. Maintain paragraph breaks using the `\n` escape sequence.