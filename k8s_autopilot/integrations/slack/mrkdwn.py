r"""Markdown → Slack mrkdwn converter.

Slack uses ``mrkdwn`` — a proprietary formatting dialect that differs
from standard Markdown in several critical ways:

- Bold:           ``**bold**``   → ``*bold*``
- Italic:         ``*italic*``   → ``_italic_``
- Strikethrough:  ``~~strike~~`` → ``~strike~``
- Links:          ``[text](url)``→ ``<url|text>``
- Headings:       ``# Title``    → ``*Title*`` (bold text)
- Tables:         Pipe syntax    → column-aligned code blocks
- HR:             ``---``        → ``───────────`` (unicode line)

Slack *does* support triple-backtick code blocks and inline code —
those are preserved as-is.

Usage::

    from k8s_autopilot.integrations.slack.mrkdwn import md_to_mrkdwn

    slack_text = md_to_mrkdwn("## Status\\n**All systems** go!")
    # → "*Status*\\n*All systems* go!"

Ref: https://api.slack.com/reference/surfaces/formatting
Ref: https://docs.slack.dev/messaging/composing/formatting
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Pre-compiled regex patterns
# ---------------------------------------------------------------------------

# Code blocks — triple backticks with optional language tag.
# These MUST be extracted *first* to avoid mangling their content.
_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```", re.DOTALL)

# Inline code — single backtick pairs.  Also extracted first.
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")

# Markdown headings — lines starting with 1-6 '#' characters.
# Convert to bold text since Slack mrkdwn has no heading support.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

# Bold: **text** → *text*  (must run BEFORE italic conversion)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")

# Italic: single unescaped * → _ (only for *word* patterns, not ** or * alone)
# We handle this after bold so **bold** has already been converted to *bold*.
# Matches *text* but NOT **text** (already handled) and NOT * inside code.
_ITALIC_SINGLE_STAR_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")

# Strikethrough: ~~text~~ → ~text~
_STRIKETHROUGH_RE = re.compile(r"~~(.+?)~~")

# Links: [text](url) → <url|text>
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

# Image links: ![alt](url) → <url|alt> (degrade gracefully)
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

# Markdown table detection — lines with pipe characters and separator rows.
# A table starts when we see a row like |---|---|
_TABLE_SEPARATOR_RE = re.compile(r"^\|?\s*[-:]+[-|\s:]+\s*\|?\s*$")

# Horizontal rule: ---, ***, ___ (alone on a line)
_HR_RE = re.compile(r"^(\s*[-*_]{3,}\s*)$", re.MULTILINE)

# Bullet list items: line-start * or - optionally preceded by spaces
_LIST_RE = re.compile(r"^(\s*)[*-]\s+", re.MULTILINE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def md_to_mrkdwn(text: str) -> str:
    """Convert standard Markdown text to Slack mrkdwn format.

    Preserves code blocks and inline code — everything inside backtick
    delimiters is left untouched.

    Args:
        text: Markdown-formatted string.

    Returns:
        Slack mrkdwn-formatted string.
    """
    if not text:
        return text

    # ── Step 1: Convert tables to code blocks first ───────────────────
    # This allows us to strip markdown formatting (bold, inline code, etc.)
    # directly from table cells before code blocks are protected.
    result = _convert_tables(text)

    # ── Step 2: Extract code blocks & inline code to protect them ─────
    # Replace with placeholder tokens and restore after all transformations.
    code_blocks: list[str] = []
    inline_codes: list[str] = []

    def _save_code_block(match: re.Match) -> str:
        code_blocks.append(match.group(0))
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    def _save_inline_code(match: re.Match) -> str:
        inline_codes.append(match.group(0))
        return f"\x00INLINECODE{len(inline_codes) - 1}\x00"

    result = _CODE_BLOCK_RE.sub(_save_code_block, result)
    result = _INLINE_CODE_RE.sub(_save_inline_code, result)

    # ── Step 3: Convert inline formatting ─────────────────────────────

    # Images → link fallback (must run before link conversion)
    result = _IMAGE_RE.sub(r"<\2|\1>", result)

    # Links: [text](url) → <url|text>
    result = _LINK_RE.sub(r"<\2|\1>", result)

    # Bullet lists: * or - followed by space -> unicode bullet
    result = _LIST_RE.sub(r"\1• ", result)

    # Headings: # Title → *Title*
    result = _HEADING_RE.sub(lambda m: f"*{m.group(2)}*", result)

    # Bold: **text** → *text*
    result = _BOLD_RE.sub(r"*\1*", result)

    # Strikethrough: ~~text~~ → ~text~
    result = _STRIKETHROUGH_RE.sub(r"~\1~", result)

    # Horizontal rules: --- → unicode line
    result = _HR_RE.sub("\u2500" * 30, result)

    # Note: We intentionally do NOT convert single-star italic (*text*)
    # because after bold conversion, *text* in mrkdwn already means bold.
    # Slack uses _text_ for italic, but blindly converting * to _ would
    # break the bold text we just created.  Markdown italic using _text_
    # syntax is already compatible with Slack mrkdwn natively.

    # ── Step 4: Restore code blocks & inline code ─────────────────────
    for i, block in enumerate(code_blocks):
        result = result.replace(f"\x00CODEBLOCK{i}\x00", block)
    for i, code in enumerate(inline_codes):
        result = result.replace(f"\x00INLINECODE{i}\x00", code)

    return result


# ---------------------------------------------------------------------------
# Internal: table conversion
# ---------------------------------------------------------------------------


def _convert_tables(text: str) -> str:
    """Detect Markdown tables and wrap them in column-aligned code blocks.

    Slack does not support Markdown table syntax (pipe ``|``).
    Wrapping them in triple-backtick code blocks with proper column
    alignment provides a clean, readable table.
    """
    lines = text.split("\n")
    result_lines: list[str] = []
    table_lines: list[list[str]] = []  # list of cell-lists
    in_table = False

    for line in lines:
        stripped = line.strip()

        if in_table:
            # Continue collecting table rows
            if stripped.startswith("|") or _TABLE_SEPARATOR_RE.match(stripped):
                # Skip separator rows (|---|---|) — they clutter monospace view
                if not _TABLE_SEPARATOR_RE.match(stripped):
                    table_lines.append(_parse_table_row(stripped))
                continue
            else:
                # End of table — flush
                result_lines.append(_flush_table(table_lines))
                table_lines = []
                in_table = False
                result_lines.append(line)
        else:
            # Detect table start: current line has pipes AND next could
            # be a separator, OR this line itself looks like a header row.
            if stripped.startswith("|") and "|" in stripped[1:]:
                in_table = True
                table_lines.append(_parse_table_row(stripped))
            else:
                result_lines.append(line)

    # Flush any trailing table
    if table_lines:
        result_lines.append(_flush_table(table_lines))

    return "\n".join(result_lines)


def _strip_markdown(text: str) -> str:
    """Strip markdown formatting markers for plain text cells."""
    if not text:
        return text
    # Strip bold/italic/code markers
    text = text.replace("**", "").replace("__", "")
    text = text.replace("*", "").replace("_", "")
    text = text.replace("`", "")
    # Strip markdown links [text](url) -> text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return text.strip()


def _parse_table_row(row: str) -> list[str]:
    """Parse a markdown table row into a list of cell values."""
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [_strip_markdown(cell) for cell in row.split("|")]


def _flush_table(rows: list[list[str]]) -> str:
    """Render table rows as a column-aligned code block.

    Computes the maximum width for each column across all rows,
    then pads each cell to that width for clean alignment.
    Adds a separator line under the header row.
    """
    if not rows:
        return ""

    # Compute max width per column
    num_cols = max(len(row) for row in rows)
    col_widths = [0] * num_cols
    for row in rows:
        for i, cell in enumerate(row):
            if i < num_cols:
                col_widths[i] = max(col_widths[i], len(cell))

    # Build aligned rows
    aligned_lines: list[str] = []
    for idx, row in enumerate(rows):
        cells = []
        for i in range(num_cols):
            cell = row[i] if i < len(row) else ""
            cells.append(cell.ljust(col_widths[i]))
        aligned_lines.append("  ".join(cells).rstrip())

        # Add separator after header row
        if idx == 0 and len(rows) > 1:
            sep = "  ".join("\u2500" * w for w in col_widths)
            aligned_lines.append(sep)

    table_text = "\n".join(aligned_lines)
    return f"```\n{table_text}\n```"
