"""Who "the user" is — the identity and channel priors the skim prompt is built from.

The skim model scores ``salience`` (how much a document concerns the user's own
work or life). It cannot do that without knowing who the user is: the live probe
of 41 documents with prompt ``skim-v1`` scored 34 at 2 and never used 3, because
"addressed to / written by the user" (3) and "touches the user's projects" (2)
are indistinguishable when "the user" is nobody in particular, and no channel
carried a prior. ``config/skim.toml`` fixes that with an ``[identity]`` table
(name, aliases, role, org, focus areas) and a ``[channels]`` table mapping each
``memories.source_type`` to an ownership prior (``own_activity`` / ``external``).

:func:`load_identity` reads the file into an :class:`Identity`. A missing file
yields neutral defaults (nothing known, no priors — the prompt then omits the
identity block and the request carries no channel tag); a malformed file raises
``ValueError`` so a typo fails at prompt build rather than silently skewing every
record. Not a secret; the file is committed (see its header comment).
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_CONFIG_PATH = os.path.join(_REPO_ROOT, "config", "skim.toml")

#: Channel prior labels returned by :meth:`Identity.channel_prior`.
OWN_ACTIVITY = "own_activity"
EXTERNAL = "external"

_IDENTITY_STR_KEYS = ("name", "role", "org")
_IDENTITY_LIST_KEYS = ("aliases", "focus")
_CHANNEL_LIST_KEYS = (OWN_ACTIVITY, EXTERNAL)


@dataclass(frozen=True)
class Identity:
    """What the skim prompt knows about the user. All fields default to "nothing"."""

    name: str = ""
    aliases: tuple[str, ...] = ()
    role: str = ""
    org: str = ""
    focus: tuple[str, ...] = ()
    own_activity: frozenset[str] = frozenset()
    external: frozenset[str] = frozenset()

    @property
    def is_known(self) -> bool:
        """True when anything about the person is configured (the prompt gets an identity block)."""
        return bool(self.name or self.aliases or self.role or self.org or self.focus)

    def channel_prior(self, source_type: str | None) -> str | None:
        """:data:`OWN_ACTIVITY`, :data:`EXTERNAL`, or ``None`` for an unlisted / missing channel."""
        if not source_type:
            return None
        if source_type in self.own_activity:
            return OWN_ACTIVITY
        if source_type in self.external:
            return EXTERNAL
        return None


def load_identity(path: str | None = None) -> Identity:
    """Read ``config/skim.toml`` (or ``path``) into an :class:`Identity`.

    Missing file: a WARNING and neutral defaults. Malformed file (bad TOML,
    wrong types, unknown keys, a channel listed as both own-activity and
    external): ``ValueError``.
    """
    path = path or DEFAULT_CONFIG_PATH
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        logger.warning("Skim identity config %s not found; the skim prompt carries no identity block "
                       "and requests carry no channel tag", path)
        return Identity()
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"skim config {path}: not valid TOML: {exc}") from None
    return parse_identity(data, source=path)


def parse_identity(data: dict, *, source: str = "skim config") -> Identity:
    """Validate a parsed ``skim.toml`` mapping and build the :class:`Identity`."""
    unknown = set(data) - {"identity", "channels"}
    if unknown:
        raise ValueError(f"{source}: unknown table(s) {sorted(unknown)}; expected [identity] and [channels]")
    identity = _table(data, "identity", source)
    channels = _table(data, "channels", source)

    bad = set(identity) - set(_IDENTITY_STR_KEYS) - set(_IDENTITY_LIST_KEYS)
    if bad:
        raise ValueError(f"{source}: [identity] has unknown key(s) {sorted(bad)}; "
                         f"allowed: {sorted(_IDENTITY_STR_KEYS + _IDENTITY_LIST_KEYS)}")
    bad = set(channels) - set(_CHANNEL_LIST_KEYS)
    if bad:
        raise ValueError(f"{source}: [channels] has unknown key(s) {sorted(bad)}; "
                         f"allowed: {sorted(_CHANNEL_LIST_KEYS)}")

    own = frozenset(_str_list(channels, OWN_ACTIVITY, "channels", source))
    ext = frozenset(_str_list(channels, EXTERNAL, "channels", source))
    both = own & ext
    if both:
        raise ValueError(f"{source}: channel(s) {sorted(both)} listed as both own_activity and external")

    return Identity(
        name=_str(identity, "name", "identity", source),
        aliases=tuple(_str_list(identity, "aliases", "identity", source)),
        role=_str(identity, "role", "identity", source),
        org=_str(identity, "org", "identity", source),
        focus=tuple(_str_list(identity, "focus", "identity", source)),
        own_activity=own,
        external=ext,
    )


def _table(data: dict, key: str, source: str) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"{source}: [{key}] must be a table, got {type(value).__name__}")
    return value


def _str(table: dict, key: str, table_name: str, source: str) -> str:
    value = table.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{source}: [{table_name}] {key} must be a string, got {type(value).__name__}")
    return value.strip()


def _str_list(table: dict, key: str, table_name: str, source: str) -> list[str]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{source}: [{table_name}] {key} must be an array of strings")
    items = [v.strip() for v in value if v.strip()]
    if len(set(items)) != len(items):
        raise ValueError(f"{source}: [{table_name}] {key} has duplicate entries")
    return items
