"""Canonical metadata headers and conservative HTML-to-text conversion."""
from __future__ import annotations
import html
import re


def header_key(key: str) -> str:
    key = re.sub(r"[^A-Za-z0-9_]+", "_", key.strip()).strip("_")
    if not key:
        raise ValueError("empty capture metadata key")
    return key


def render_document(title: str, fields: dict, body: str) -> str:
    """Use one-line values: source content cannot inject a header boundary."""
    clean = lambda value: " ".join(str(value).splitlines()).strip()
    lines = [f"# {clean(title)}", ""]
    lines.extend(f"{header_key(k)}: {clean(v)}" for k, v in fields.items()
                 if v is not None)
    return "\n".join(lines) + "\n\n---\n\n" + body


def render_header_lines(lines: list[str], body: str) -> str:
    """Compatibility bridge for adapters whose fields are assembled conditionally."""
    fields = {}
    for line in lines[1:]:
        if line.strip():
            key, sep, value = line.partition(":")
            if not sep:
                raise ValueError("capture header must use key: value fields")
            fields[key] = value.strip()
    return render_document(lines[0].removeprefix("# "), fields, body)


def html_to_text(body: str | None) -> str:
    if not body:
        return ""
    text = re.sub(r"<(style|script|head)\b.*?</\1>", " ", body, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</(p|div|tr|li|h[1-6]|blockquote)>", "\n", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def normalize_document_headers(markdown: str) -> str:
    """Normalize an existing adapter header without rewriting its body."""
    header, separator, body = markdown.partition("\n---\n")
    lines = header.rstrip().splitlines()
    if separator and header.startswith("# ") and all(not line.strip() or ":" in line for line in lines[1:]):
        return render_header_lines(lines, body.lstrip("\n"))
    return markdown
