"""The MCP tool descriptions must not promise a vocabulary the boundary rejects.

An agent reads the tool docstring to pick a ``source_type`` (or ``type`` /
``status`` / ``relation_type``); if the docstring lists a label that
``src.validation`` rejects, every call with that label fails with
"source_type must be one of ...". R3's ``memory_create`` docstring still listed
``pdf``, ``course`` and ``notes``, and ``memory_search`` showed ``'cli_chat'``.
"""
import asyncio
import inspect
import re

import pytest

from src import mcp_readonly, mcp_server
from src.validation import FILTERABLE_TYPES, MEMORY_STATUSES, MEMORY_TYPES, RELATION_TYPES, SOURCE_TYPES

LABEL = r"[a-z][a-z0-9_]*"
VOCABULARIES = {
    "source_type": SOURCE_TYPES,
    "type": FILTERABLE_TYPES | MEMORY_TYPES,
    "status": MEMORY_STATUSES,
    "relation_type": RELATION_TYPES,
}


def _tools():
    for module in (mcp_server, mcp_readonly):
        for tool in asyncio.run(module.mcp.list_tools()):
            fn = getattr(module, tool.name, None) or getattr(mcp_server, tool.name)
            yield f"{module.__name__}.{tool.name}", tool.description or "", inspect.signature(fn)


def _mentioned(description, field):
    """Labels a docstring presents as values of ``field``.

    Two shapes are recognised: a pure enumeration line ``field: a, b, c`` (labels
    separated by commas, optionally followed by a parenthesised note) and quoted
    examples (``field='a'`` or ``'a', 'b'``) on a line that names the field.
    """
    found = set()
    word = rf"(?<![a-z_]){field}(?![a-z_])"
    for line in description.splitlines():
        if not re.search(word, line):
            continue
        listing = re.match(rf"\s*-?\s*{word}:\s*({LABEL}(?:\s*,\s*{LABEL})*)\s*(?:\(.*)?$", line)
        if listing:
            found.update(re.split(r"\s*,\s*", listing.group(1)))
        found.update(re.findall(rf"'({LABEL})'", line))
    return found


def test_every_documented_label_is_accepted_by_validation():
    checked = 0
    for name, description, signature in _tools():
        for field, allowed in VOCABULARIES.items():
            if field not in signature.parameters:
                continue
            labels = _mentioned(description, field)
            rejected = sorted(labels - allowed)
            assert not rejected, f"{name} docstring lists {field} values the boundary rejects: {rejected}"
            checked += len(labels)
    assert checked >= 15, "the extractor stopped seeing the documented vocabularies"


def test_memory_create_docstring_enumerates_only_accepted_source_types():
    labels = _mentioned(mcp_server.memory_create.__doc__, "source_type")
    assert labels and labels <= SOURCE_TYPES
    assert not labels & {"pdf", "course", "notes"}


@pytest.mark.parametrize("label", ["pdf", "course", "notes", "cli_chat"])
def test_extractor_would_catch_the_r3_defects(label):
    """Guard the guard: the recogniser sees both shapes the docstrings use."""
    assert label in _mentioned(f"    source_type: youtube, {label}, manual\n", "source_type")
    assert label in _mentioned(f"    - source_type: capture channel, e.g. 'article', '{label}'.\n", "source_type")
