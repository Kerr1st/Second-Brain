"""Small defensive adapters for structured model responses."""
import json
from src.prompts.revision import REVISION_SCHEMA

def _as_int(value) -> int | None:
    try:
        return int(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None

def _unwrap_nested_candidate(out: dict) -> dict:
    """A revision sometimes puts the whole candidate JSON into the ``content`` string
    (observed 2026-09-18: an accepted memory was stored as a JSON blob). If ``content``
    parses as a dict that itself has a ``content`` key, use the inner object's fields."""
    content = out.get("content")
    if isinstance(content, str) and content.lstrip().startswith("{"):
        try:
            inner = json.loads(content)
        except ValueError:
            return out
        if isinstance(inner, dict) and isinstance(inner.get("content"), str):
            merged = dict(out)
            merged.update({k: v for k, v in inner.items() if k in REVISION_SCHEMA["properties"]})
            return merged
    return out

def _as_str_list(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    return []
