"""Small, shared capture outcome contract. Content never enters status records."""
from __future__ import annotations


class CaptureBatch(list):
    """Rendered items plus fetch failures, preserving the existing list API."""
    def __init__(self, iterable=(), *, failed=0):
        super().__init__(iterable)
        self.failed = failed


def exit_code(summary: dict) -> int:
    """Nested channel summaries fail if any attempted capture failed."""
    return int(any(exit_code(v) if isinstance(v, dict) else
                   bool(v) if k in {"failed", "aborted", "unknown_spend"} else False
                   for k, v in summary.items()))
