---
name: count-words-in-a-string
description: Use when the user asks to count words in a string, text, sentence, paragraph, or file.
---

# Count Words in a String

Count whitespace-separated words in a string or file.

## Inputs

- Inline string (quoted or pasted), OR
- File path to read.

If ambiguous (could be either), ask the user which one before counting.

## Steps

1. **Get the text.** Inline → use directly. File path → `Read` the file (or `wc -w <path>` via Bash if shell is faster).
2. **Split on whitespace.** Trim, split on any run of whitespace, drop empty tokens.
3. **Report.** Output exactly: `Word count: <N>`

## Rules

- Punctuation, hyphens, and contractions do not split words: `hello,`, `state-of-the-art`, `don't` each count as 1.
- Empty/whitespace-only input → `Word count: 0`.
- CJK or other non-whitespace-delimited scripts: warn that whitespace-splitting will undercount and offer a character count instead.
- Do not strip punctuation, lowercase, or otherwise transform the text unless asked.

## Output

Just `Word count: <N>`. Add character count or other detail only if the user asked.