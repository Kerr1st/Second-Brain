"""Skim stage — one structured, faithful record per arriving document.

A cheap model (Amazon Nova 2 Lite on Bedrock) reads every parent-level arrival
in a dream-cycle window and emits a :class:`~src.skim.schema.SkimRecord`. It is
data processing, not insight: the record states only what the document says,
and every decision / open question carries a verbatim quote so faithfulness can
be checked mechanically (:mod:`src.skim.faithfulness`). See
docs/PLAN-skim-stage.md.
"""

from src.skim.schema import (  # noqa: F401
    DOC_KINDS,
    MAX_QUOTE_CHARS,
    MAX_SUMMARY_CHARS,
    MAX_TOPICS,
    SKIM_SCHEMA,
    QuotedItem,
    SkimRecord,
    SkimValidationError,
    validate_record,
)
from src.skim.faithfulness import check_record, quote_is_verbatim  # noqa: F401

__all__ = [
    "DOC_KINDS",
    "MAX_QUOTE_CHARS",
    "MAX_SUMMARY_CHARS",
    "MAX_TOPICS",
    "SKIM_SCHEMA",
    "QuotedItem",
    "SkimRecord",
    "SkimValidationError",
    "validate_record",
    "check_record",
    "quote_is_verbatim",
]
