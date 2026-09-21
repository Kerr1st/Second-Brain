"""Tests for src/skim/identity.py — the [identity] / [channels] loader behind the skim prompt.

Missing file → neutral defaults (nothing known, no priors); the committed
config/skim.toml parses and names the user; a malformed file fails loudly.
No model calls, no corpus content.
"""

from __future__ import annotations

import logging

import pytest

from src.skim.identity import (
    DEFAULT_CONFIG_PATH,
    EXTERNAL,
    OWN_ACTIVITY,
    Identity,
    load_identity,
    parse_identity,
)

FULL = """
[identity]
name = "Ada Example"
aliases = ["adax", " ada.e "]
role = "Staff Engineer"
org = "Example Corp"
focus = ["Widgets", "the Orrery project"]

[channels]
own_activity = ["slack", "outlook_email"]
external = ["article"]
"""


def _write(tmp_path, text, name="skim.toml"):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


class TestDefaults:
    def test_missing_file_gives_neutral_defaults_and_warns(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="src.skim.identity"):
            ident = load_identity(str(tmp_path / "nope.toml"))
        assert ident == Identity()
        assert not ident.is_known
        assert ident.channel_prior("slack") is None and ident.channel_prior("article") is None
        assert "not found" in caplog.text and "nope.toml" in caplog.text

    def test_neutral_identity_is_empty_everywhere(self):
        ident = Identity()
        assert (ident.name, ident.aliases, ident.role, ident.org, ident.focus) == ("", (), "", "", ())
        assert ident.own_activity == frozenset() and ident.external == frozenset()

    def test_identity_is_immutable(self):
        with pytest.raises(AttributeError):
            Identity().name = "x"  # type: ignore[misc]


class TestCommittedConfig:
    def test_default_path_is_config_skim_toml(self):
        assert DEFAULT_CONFIG_PATH.endswith("/config/skim.toml")

    def test_committed_file_names_the_user(self):
        ident = load_identity()  # the real config/skim.toml
        assert ident.is_known
        assert ident.name == "Kerr First"
        assert ident.aliases == ("kerrfir",)
        assert ident.role == "Senior Customer Success Specialist"
        assert ident.org == "Amazon Web Services"
        assert ident.focus == ("Kiro", "customer success", "Second Brain (personal knowledge system)")

    def test_committed_file_classifies_every_capture_channel(self):
        ident = load_identity()
        assert ident.own_activity == {
            "outlook_email", "outlook_calendar", "slack", "distilled_chat", "kiro_cli_chat", "kiro_ide_chat",
            "quick_desktop_chat", "quick_desktop_decision", "quick_desktop_doc", "weekly_digest",
        }
        assert ident.external == {"article", "youtube", "browser_history", "quick_desktop_feed"}
        assert not (ident.own_activity & ident.external)


class TestFile:
    def test_values_are_read_and_stripped(self, tmp_path):
        ident = load_identity(_write(tmp_path, FULL))
        assert ident.name == "Ada Example" and ident.aliases == ("adax", "ada.e")
        assert ident.role == "Staff Engineer" and ident.org == "Example Corp"
        assert ident.focus == ("Widgets", "the Orrery project")
        assert ident.own_activity == {"slack", "outlook_email"} and ident.external == {"article"}

    def test_channel_prior(self, tmp_path):
        ident = load_identity(_write(tmp_path, FULL))
        assert ident.channel_prior("slack") == OWN_ACTIVITY == "own_activity"
        assert ident.channel_prior("article") == EXTERNAL == "external"
        assert ident.channel_prior("youtube") is None  # not listed
        assert ident.channel_prior(None) is None and ident.channel_prior("") is None

    def test_identity_only_file_has_no_priors(self, tmp_path):
        ident = load_identity(_write(tmp_path, '[identity]\nname = "Ada"\n'))
        assert ident.is_known and ident.name == "Ada"
        assert ident.channel_prior("slack") is None

    def test_channels_only_file_is_not_a_known_person(self, tmp_path):
        ident = load_identity(_write(tmp_path, '[channels]\nown_activity = ["slack"]\n'))
        assert not ident.is_known
        assert ident.channel_prior("slack") == OWN_ACTIVITY

    def test_empty_file_equals_defaults(self, tmp_path):
        assert load_identity(_write(tmp_path, "")) == Identity()

    def test_blank_entries_are_dropped(self, tmp_path):
        ident = load_identity(_write(tmp_path, '[identity]\nname = "  "\nfocus = ["", "  ", "Kiro"]\n'))
        assert ident.name == "" and ident.focus == ("Kiro",)


class TestMalformed:
    @pytest.mark.parametrize("text, hint", [
        ("[identity\nname = 'x'", "not valid TOML"),
        ('identity = "Ada"', "[identity] must be a table"),
        ('channels = ["slack"]', "[channels] must be a table"),
        ("[identity]\nname = 3", "name must be a string"),
        ('[identity]\naliases = "kerrfir"', "aliases must be an array of strings"),
        ("[identity]\nfocus = [1, 2]", "focus must be an array of strings"),
        ('[identity]\nalias = ["x"]', "unknown key(s) ['alias']"),
        ('[channels]\nown = ["slack"]', "unknown key(s) ['own']"),
        ('[channels]\nown_activity = "slack"', "own_activity must be an array of strings"),
        ('[channels]\nown_activity = ["slack"]\nexternal = ["slack", "article"]', "both own_activity and external"),
        ('[channels]\nexternal = ["article", "article"]', "duplicate entries"),
        ('[people]\nname = "x"', "unknown table(s) ['people']"),
    ])
    def test_malformed_file_raises_value_error_naming_the_problem(self, tmp_path, text, hint):
        path = _write(tmp_path, text)
        with pytest.raises(ValueError, match=r".*") as exc_info:
            load_identity(path)
        assert hint in str(exc_info.value) and path in str(exc_info.value)

    def test_parse_identity_accepts_a_mapping_directly(self):
        ident = parse_identity({"identity": {"name": "Ada"}, "channels": {"external": ["article"]}})
        assert ident.name == "Ada" and ident.external == {"article"}
        with pytest.raises(ValueError, match="skim config: \\[identity\\] name must be a string"):
            parse_identity({"identity": {"name": ["Ada"]}})
