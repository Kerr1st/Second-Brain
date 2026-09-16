"""Hermetic tests for src.capture.slack (no DB, no MCP)."""
import asyncio
from datetime import date

from src.capture import slack
from src.capture.mcp_client import McpToolError

DM = "\ue000"


def _wrapped(text, author="U1", ts="1788453260.081609"):
    return f'<slack-user-content author="{author}" ts="{ts}">\n{text.replace(" ", DM)}\n</slack-user-content>'


def _m(text, user_id="U1", real="Ada Lovelace", ts="2026-09-15T16:34:20.081Z", thread=None):
    return {"text": _wrapped(text, user_id), "user": {"id": user_id, "name": "ada", "realName": real},
            "timestamp": ts, "threadTs": None, "isThreadParent": bool(thread), "replyCount": len(thread or []),
            "thread": thread or []}


class TestCleanText:
    def test_removes_wrapper_tags_and_datamarks(self):
        assert slack.clean_text(_wrapped("hello there world")) == "hello there world"

    def test_resolves_mentions_links_and_channel_refs(self):
        raw = _wrapped("hi <@U2> see <https://x.y/z|the doc> in <#C9|general>")
        out = slack.clean_text(raw, users={"U2": "Grace Hopper"})
        assert out == "hi @Grace Hopper see the doc (https://x.y/z) in #general"

    def test_empty(self):
        assert slack.clean_text(None) == "" and slack.clean_text("") == ""


class TestRenderDay:
    def test_renders_messages_and_thread_replies_in_time_order(self):
        reply = _m("reply text", user_id="U2", real="Grace Hopper", ts="2026-09-15T17:00:00.000Z")
        later = _m("second", ts="2026-09-15T18:00:00.000Z")
        first = _m("first <@U2>", ts="2026-09-15T16:00:00.000Z", thread=[reply])
        conv = {"id": "C1", "name": "eng", "kind": "channel", "purpose": "Engineering chat", "topic": ""}
        url, md = slack.render_day(conv, date(2026, 9, 15), [later, first])
        assert url == "slack://C1/2026-09-15"
        head, _, body = md.partition("\n---\n")
        assert head.startswith("# Slack: #eng — 2026-09-15\n")
        assert "Source: slack://C1/2026-09-15" in head and "Type: slack-conversation" in head
        assert "Purpose: Engineering chat" in head and "Messages: 2" in head
        lines = body.strip().splitlines()
        assert lines[0].endswith("Ada Lovelace**: first @Grace Hopper")
        assert lines[1].lstrip().startswith("↳") and lines[1].endswith("Grace Hopper: reply text")
        assert lines[2].endswith("Ada Lovelace**: second")
        assert DM not in md and "slack-user-content" not in md

    def test_dm_label_and_empty_day(self):
        conv = {"id": "D1", "name": "grace", "kind": "dm"}
        assert slack.render_day(conv, date(2026, 9, 15), []) is None
        _, md = slack.render_day(conv, date(2026, 9, 15), [_m("yo")])
        assert md.startswith("# Slack: DM with grace — 2026-09-15")

    def test_truncation(self):
        msgs = [_m("x" * 5000, ts=f"2026-09-15T10:{i:02d}:00.000Z") for i in range(20)]
        _, md = slack.render_day({"id": "C1", "name": "big", "kind": "channel"}, date(2026, 9, 15), msgs)
        assert md.endswith("[truncated]") and len(md) < slack.MAX_DOC_CHARS + 1000


class TestWindows:
    def test_completed_days_excludes_today_oldest_first(self):
        assert slack.completed_days(2, today=date(2026, 9, 16)) == [date(2026, 9, 14), date(2026, 9, 15)]
        assert slack.completed_days(1, today=date(2026, 9, 16)) == [date(2026, 9, 15)]

    def test_day_bounds_are_24h_utc_strings(self):
        since, until = slack.day_bounds_utc(date(2026, 9, 15))
        assert since.endswith("Z") and until.endswith("Z")
        assert slack._parse_ts(until) - slack._parse_ts(since) == __import__("datetime").timedelta(days=1)


def _fake(channels, dms, messages_by_conv, log=None):
    async def call(tool, params):
        if log is not None:
            log.append((tool, params))
        if tool == "list_my_channels":
            return {"sections": [{"channels": channels}], "totalChannels": len(channels)}
        if tool == "list_dms":
            return {"dms": dms}
        if tool == "get_messages":
            spec = messages_by_conv.get(params["channel"])
            if spec == "error":
                raise McpToolError("get_messages: rate limited")
            return {"channel": {"id": params["channel"], "purpose": "p"}, "messages": spec or [], "hasMore": False}
        raise AssertionError(tool)
    return call


class TestCollect:
    def test_lists_channels_then_dms_bounded_and_deduped(self):
        channels = [{"id": f"C{i}", "name": f"c{i}", "topic": ""} for i in range(3)] + [{"id": "C0", "name": "dup"}]
        dms = [{"channelId": "D1", "name": "grace", "isGroup": False, "lastActivity": "2026-09-15T20:00:00Z"}]
        convs = asyncio.run(slack.list_conversations(_fake(channels, dms, {}), max_conversations=3))
        assert [c["id"] for c in convs] == ["C0", "C1", "C2"]
        convs = asyncio.run(slack.list_conversations(_fake(channels, dms, {}), max_conversations=10))
        assert [c["id"] for c in convs] == ["C0", "C1", "C2", "D1"] and convs[-1]["kind"] == "dm"

    def test_dm_present_in_sidebar_listing_is_classified_as_dm(self):
        # Live behaviour: list_my_channels includes DMs as generic entries; the DM listing must win.
        channels = [{"id": "C1", "name": "eng"}, {"id": "D1", "name": "grace", "topic": ""}]
        dms = [{"channelId": "D1", "name": "grace", "isGroup": False, "lastActivity": "2026-09-15T20:00:00Z"}]
        convs = asyncio.run(slack.list_conversations(_fake(channels, dms, {})))
        assert [(c["id"], c["kind"]) for c in convs] == [("C1", "channel"), ("D1", "dm")]
        assert convs[1]["last_activity"] == "2026-09-15T20:00:00Z"

    def test_collect_skips_known_days_empty_days_stale_dms_and_errors(self):
        days = [date(2026, 9, 14), date(2026, 9, 15)]
        since14, _ = slack.day_bounds_utc(days[0])
        in14 = slack._parse_ts(since14).isoformat().replace("+00:00", "Z")
        channels = [{"id": "C1", "name": "eng"}, {"id": "C2", "name": "quiet"}, {"id": "C3", "name": "broken"}]
        dms = [{"channelId": "D_old", "name": "old", "isGroup": False, "lastActivity": "2026-01-01T00:00:00Z"},
               {"channelId": "D_new", "name": "new", "isGroup": True, "lastActivity": "2026-09-15T12:00:00Z"}]
        msgs = {"C1": [_m("hello", ts=in14)], "C2": [], "C3": "error", "D_new": [_m("dm text", ts=in14)]}
        already = {"slack://C1/2026-09-15"}
        log = []
        items = asyncio.run(slack.collect(_fake(channels, dms, msgs, log), days, already))
        urls = sorted(u for u, _ in items)
        # C1: the 14th only (15th known); C2 empty; C3 errors are logged and skipped;
        # D_old skipped without a fetch; D_new captured. Messages are window-filtered per day,
        # so D_new's message (dated the 14th) lands only on the 14th.
        assert urls == ["slack://C1/2026-09-14", "slack://D_new/2026-09-14"]
        fetched = {p["channel"] for t, p in log if t == "get_messages"}
        assert "D_old" not in fetched and fetched == {"C1", "C2", "C3", "D_new"}
        assert "Group DM new" in dict(items)["slack://D_new/2026-09-14"]

    def test_fetch_day_filters_messages_outside_window(self):
        day = date(2026, 9, 15)
        since, until = slack.day_bounds_utc(day)
        inside = slack._parse_ts(since).isoformat().replace("+00:00", "Z")
        outside = "2026-09-13T00:00:00Z"
        msgs = {"C1": [_m("in", ts=inside), _m("out", ts=outside)]}
        _, got = asyncio.run(slack.fetch_day(_fake([], [], msgs), {"id": "C1"}, day))
        assert [slack.clean_text(m["text"]) for m in got] == ["in"]
