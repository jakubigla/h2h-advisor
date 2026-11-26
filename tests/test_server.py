import asyncio
import json
from datetime import UTC, date, datetime
from unittest.mock import ANY

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from h2h_advisor import server
from h2h_advisor.analysis.streamers import StreamerOption
from h2h_advisor.models import Category, Player
from h2h_advisor.service import Envelope, SourceAge

TOOLS = {
    "list_leagues",
    "get_league_settings",
    "get_matchup",
    "get_roster",
    "get_free_agents",
    "get_projections",
    "get_schedule",
    "analyze_week",
    "rank_streamers",
    "plan_adds",
    "long_term_upgrades",
    "get_history",
    "daily_brief",
    "status",
}
NOTE_TOOLS = {"add_note", "list_notes", "remove_note"}
NOW = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


def call(name, **arguments):
    return run(server.mcp.call_tool(name, arguments))


def payload(name, **arguments):
    return json.loads(call(name, **arguments).content[0].text)


def player(name, team):
    return Player(f"{name}|{team}", name, team, ("PG",))


def streamer_envelope():
    follow = StreamerOption(
        add=player("Second Add", "BOS"),
        drop=player("Second Drop", "MIA"),
        start_days=[date(2026, 11, 28)],
        usable_days=[date(2026, 11, 28)],
        slot_by_day={date(2026, 11, 28): "Util"},
        raw_games=1,
        light_days=[],
        delta_expected_cats=0.4,
        per_cat_delta={Category.PTS: 1.5},
        adds_left_after=0,
        objective="categories",
        delta_objective=0.4,
    )
    option = StreamerOption(
        add=player("Hot Pickup", "GSW"),
        drop=player("Bench Wing", "MIA"),
        start_days=[date(2026, 11, 26), date(2026, 11, 27)],
        usable_days=[date(2026, 11, 26)],
        slot_by_day={date(2026, 11, 26): "Util", date(2026, 11, 27): "BN"},
        raw_games=2,
        light_days=[date(2026, 11, 27)],
        delta_expected_cats=0.9,
        per_cat_delta={Category.PTS: 4.0, Category.FG_PCT: -0.01},
        adds_left_after=1,
        objective="categories",
        delta_objective=0.9,
        follow_up=follow,
    )
    return Envelope(
        data={"options": [option], "reason": None},
        ages=[SourceAge("yahoo", NOW, False), SourceAge("bbm", None, True)],
        notices=["browser busy in another session; showing cached data"],
    )


class StubAdvisor:
    """Records every call; returns a canned envelope."""

    def __init__(self, envelope=None):
        self.calls = []
        self._envelope = envelope or Envelope({"ok": True}, [], [])

    def __getattr__(self, name):
        def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self._envelope

        return method


@pytest.fixture
def stub(monkeypatch):
    advisor = StubAdvisor()
    monkeypatch.setattr(server, "get_advisor", lambda: advisor)
    return advisor


def test_tool_names_exact():
    assert {t.name for t in run(server.mcp.list_tools())} == TOOLS | NOTE_TOOLS


def test_tools_read_only_annotated():
    tools = [t for t in run(server.mcp.list_tools()) if t.name not in NOTE_TOOLS]
    assert len(tools) == len(TOOLS)
    for tool in tools:
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.destructive_hint is False, tool.name
        assert "read-only" in tool.description.lower(), tool.name
        assert "never changes anything" in tool.description.lower(), tool.name


def test_note_tools_write_only_the_local_notes_file():
    tools = [t for t in run(server.mcp.list_tools()) if t.name in NOTE_TOOLS]
    assert len(tools) == len(NOTE_TOOLS)
    for tool in tools:
        assert tool.annotations.read_only_hint is False, tool.name
        assert tool.annotations.destructive_hint is False, tool.name
        assert tool.annotations.open_world_hint is False, tool.name
        assert "local notes file" in tool.description, tool.name
        assert "never changes anything" in tool.description.lower(), tool.name
    assert "add_note" in server.INSTRUCTIONS


def test_server_instructions_state_the_rules():
    text = server.INSTRUCTIONS.lower()
    for phrase in ("read-only", "daily_brief", "fetched_at", "never claim"):
        assert phrase in text


def test_rank_streamers_returns_json_envelope(monkeypatch):
    advisor = StubAdvisor(streamer_envelope())
    monkeypatch.setattr(server, "get_advisor", lambda: advisor)

    result = payload("rank_streamers", league="Hoops", max_results=3)

    assert advisor.calls == [
        (
            "rank_streamers",
            (),
            {
                "league": "Hoops",
                "horizon": "today",
                "max_results": 3,
                "refresh": False,
            },
        )
    ]
    assert result["notices"] == ["browser busy in another session; showing cached data"]
    assert result["ages"] == [
        {"source": "yahoo", "fetched_at": NOW.isoformat(), "stale": False},
        {"source": "bbm", "fetched_at": None, "stale": True},
    ]
    option = result["data"]["options"][0]
    assert option["add"]["name"] == "Hot Pickup"
    assert option["start_days"] == ["2026-11-26", "2026-11-27"]
    assert option["slot_by_day"] == {"2026-11-26": "Util", "2026-11-27": "BN"}
    assert option["per_cat_delta"] == {"PTS": 4.0, "FG_PCT": -0.01}
    assert option["follow_up"]["add"]["name"] == "Second Add"
    assert option["follow_up"]["follow_up"] is None


def test_every_tool_maps_parameters_to_the_advisor(stub):
    day = date(2026, 11, 26)
    cases = [
        ("list_leagues", {"refresh": True}, "list_leagues", {"refresh": True}),
        (
            "get_league_settings",
            {"league": "A"},
            "league_settings",
            {"league": "A", "refresh": False},
        ),
        (
            "get_matchup",
            {"league": "A", "week": 4},
            "matchup",
            {"league": "A", "week": 4, "refresh": False},
        ),
        (
            "get_roster",
            {"league": "A", "team": "3"},
            "roster",
            {"league": "A", "team": "3", "refresh": False},
        ),
        (
            "get_free_agents",
            {"league": "A", "position": "C"},
            "free_agents",
            {"league": "A", "limit": 50, "position": "C", "refresh": False},
        ),
        (
            "get_projections",
            {"scope": "day", "date": "2026-11-26"},
            "projections",
            {"scope": "day", "day": day, "refresh": False},
        ),
        (
            "get_projections",
            {"scope": "ros"},
            "projections",
            {"scope": "ros", "day": None, "refresh": False},
        ),
        (
            "get_schedule",
            {"start": "2026-11-26", "end": "2026-11-29"},
            "schedule",
            {"start": day, "end": date(2026, 11, 29)},
        ),
        (
            "analyze_week",
            {"league": "A"},
            "analyze_week",
            {"league": "A", "refresh": False},
        ),
        (
            "long_term_upgrades",
            {"league": "A"},
            "long_term_upgrades",
            {"league": "A", "max_results": 5, "refresh": False},
        ),
        ("get_history", {"league": "A"}, "history", {"league": "A"}),
        (
            "daily_brief",
            {},
            "daily_brief",
            {"leagues": "all", "horizon": "today", "refresh": False, "progress": ANY},
        ),
        (
            "daily_brief",
            {"leagues": ["A", "B"], "horizon": "rest_of_week"},
            "daily_brief",
            {
                "leagues": ["A", "B"],
                "horizon": "rest_of_week",
                "refresh": False,
                "progress": ANY,
            },
        ),
        ("status", {}, "status", {}),
        (
            "add_note",
            {"kind": "keep", "player": "Backup Big", "league": "Hoops", "until": "2026-12-10"},
            "add_note",
            {"kind": "keep", "player": "Backup Big", "league": "Hoops", "reason": "",
             "until": date(2026, 12, 10)},
        ),
        (
            "add_note",
            {"kind": "drop_first", "player": "Bench Wing"},
            "add_note",
            {"kind": "drop_first", "player": "Bench Wing", "league": "all", "reason": "", "until": None},
        ),
        ("list_notes", {}, "list_notes", {"league": None}),
        ("remove_note", {"note_id": 3}, "remove_note", {"note_id": 3}),
    ]
    for tool, arguments, method, expected in cases:
        stub.calls.clear()
        assert payload(tool, **arguments) == {
            "data": {"ok": True},
            "ages": [],
            "notices": [],
        }
        assert stub.calls == [(method, (), expected)], tool


def test_schedule_data_keys_are_iso_dates(monkeypatch):
    envelope = Envelope({date(2026, 11, 26): ["BOS", "LAL"]}, [], [])
    monkeypatch.setattr(server, "get_advisor", lambda: StubAdvisor(envelope))
    result = payload("get_schedule", start="2026-11-26", end="2026-11-26")
    assert result["data"] == {"2026-11-26": ["BOS", "LAL"]}


def test_projections_rejects_week_scope(stub):
    with pytest.raises(ToolError, match="scope"):
        call("get_projections", scope="week")
    assert stub.calls == []


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_projections", {"scope": "day", "date": "tomorrow"}),
        ("get_schedule", {"start": "2026-13-01", "end": "2026-11-29"}),
        ("get_schedule", {"start": "2026-11-26", "end": "nope"}),
    ],
)
def test_invalid_date_is_a_tool_error(stub, tool, arguments):
    with pytest.raises(ToolError, match="ISO"):
        call(tool, **arguments)
    assert stub.calls == []


def test_advisor_value_errors_become_tool_errors(monkeypatch):
    class Failing:
        def rank_streamers(self, **_):
            raise ValueError("'xyz' matches no league; leagues: 1: A")

    monkeypatch.setattr(server, "get_advisor", lambda: Failing())
    with pytest.raises(ToolError, match="matches no league"):
        call("rank_streamers", league="xyz")


def test_serve_does_not_open_browser(home, monkeypatch):
    opened = []

    class Spy:
        def __init__(self, *args, **kwargs):
            opened.append(args)

    monkeypatch.setattr(server, "BrowserSession", Spy)
    monkeypatch.setattr(server, "_advisor", None)

    run(server.mcp.list_tools())
    advisor = server.get_advisor()
    assert server.get_advisor() is advisor  # built once, lazily
    assert opened == []


def test_advisor_session_factory_opens_a_headless_session(home, monkeypatch):
    seen = {}

    class Fake:
        def __init__(self, cache, limiter, rules, headless=False, **kwargs):
            seen.update(headless=headless, rules=rules)

    monkeypatch.setattr(server, "BrowserSession", Fake)
    monkeypatch.setattr(server, "_advisor", None)
    advisor = server.get_advisor()
    assert isinstance(advisor._session_factory(), Fake)
    assert seen["headless"] is True
    assert seen["rules"].yahoo.action_patterns


def test_run_serves_over_stdio(monkeypatch):
    seen = []
    monkeypatch.setattr(server.mcp, "run", lambda *a, **k: seen.append((a, k)))
    server.run()
    assert seen == [(("stdio",), {})]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_free_agents", {"league": "A", "limit": 0}),
        ("rank_streamers", {"league": "A", "max_results": 0}),
        ("long_term_upgrades", {"league": "A", "max_results": -1}),
    ],
)
def test_counts_below_one_are_tool_errors(stub, tool, arguments):
    with pytest.raises(ToolError, match="at least 1"):
        call(tool, **arguments)
    assert stub.calls == []


def test_schedule_end_before_start_is_a_tool_error(stub):
    with pytest.raises(ToolError, match="before start"):
        call("get_schedule", start="2026-11-29", end="2026-11-26")
    assert stub.calls == []


def test_tool_descriptions_document_parameters():
    tools = {t.name: t for t in run(server.mcp.list_tools())}
    assert "'opponent'" in tools["get_roster"].description
    assert "ignored" in tools["get_projections"].description


def test_instructions_explain_the_notices():
    text = server.INSTRUCTIONS
    for phrase in (
        "browser busy",
        "needs login",
        "/h2h-advisor:setup",
        "uv run --directory <path to the h2h-advisor plugin> h2h-advisor login",
        "bbm_league_id",
    ):
        assert phrase in text, phrase


def test_non_finite_floats_serialize_as_null(monkeypatch):
    envelope = Envelope({"drop_ros_value": float("-inf"), "gain": 1.0}, [], [])
    monkeypatch.setattr(server, "get_advisor", lambda: StubAdvisor(envelope))
    assert payload("status")["data"] == {"drop_ros_value": None, "gain": 1.0}


# -- real envelopes from the Advisor (fake sources from test_service) -------------


@pytest.fixture
def real_advisor(tmp_path, monkeypatch):
    """Serve a real Advisor over test_service's fake Yahoo/BBM world."""
    from test_service import Clock, Factory, FakeBbm, FakeYahoo, World

    from h2h_advisor.cache import Cache
    from h2h_advisor.config import Config
    from h2h_advisor.service import Advisor

    def build(world=None):
        world = world or World()
        advisor = Advisor(
            Config(),
            Cache(tmp_path / "cache.db"),
            Factory(world),
            Clock(),
            yahoo_source=lambda session, index, today: FakeYahoo(session, world),
            bbm_source=lambda session, index: FakeBbm(session, world),
        )
        monkeypatch.setattr(server, "get_advisor", lambda: advisor)
        return world

    return build


def test_daily_brief_envelope_serializes(real_advisor):
    real_advisor()
    result = payload("daily_brief", horizon="rest_of_week")
    sites = {a["source"].split(":")[0] for a in result["ages"]}
    assert sites >= {"yahoo", "bbm"}
    assert all(set(a) == {"source", "fetched_at", "stale"} for a in result["ages"])
    assert all(isinstance(n, str) for n in result["notices"])
    leagues = result["data"]["leagues"]
    assert set(leagues) == {"51347", "57243"}
    brief = leagues["51347"]
    assert brief["name"] == "Hoops League"
    analysis = brief["analysis"]
    assert analysis["objective"] == "categories"
    assert isinstance(analysis["matchup_win_prob"], float)
    cats = analysis["analysis"]["categories"]
    assert {c["category"] for c in cats} >= {"FG_PCT", "TO"}
    assert all(isinstance(d, str) for d in analysis["analysis"]["days_left"])
    assert set(analysis["build"]) == {"strong", "punt", "weak", "source"}
    option = brief["streamers"][0]
    assert all(isinstance(k, str) for k in option["slot_by_day"])
    assert isinstance(option["adds_left_after"], int)
    assert isinstance(brief["facts"], list)
    json.dumps(result, allow_nan=False)  # strict JSON: no Infinity / NaN


def test_analyze_week_envelope_serializes_with_one_win_targets(real_advisor):
    from test_service import A, World, settings

    world = World()
    world.settings[A] = settings(A, "Hoops League", scoring_type="one_win")
    real_advisor(world)
    result = payload("analyze_week", league="Hoops")
    data = result["data"]
    assert data["objective"] == "one_win"
    assert len(data["targets"]) == 5
    assert set(data["targets"]) <= {c.value for c in Category}
    assert isinstance(data["matchup_win_prob"], float)
    assert isinstance(data["at_risk_strong"], list)
    assert data["analysis"]["adds_left"] >= 0
    json.dumps(result, allow_nan=False)


class RecordingContext:
    def __init__(self):
        self.reports = []

    async def report_progress(self, progress, total=None, message=None):
        self.reports.append((progress, total, message))


def test_daily_brief_progress_is_sent_from_the_tool_thread():
    ctx = RecordingContext()
    report = server.league_progress(ctx)

    async def tool_thread():
        await anyio.to_thread.run_sync(lambda: [report(1, 2, "A"), report(2, 2, "B")])

    anyio.run(tool_thread)
    assert ctx.reports == [(1, 2, "league A done"), (2, 2, "league B done")]


def test_daily_brief_progress_is_best_effort():
    report = server.league_progress(RecordingContext())
    report(1, 2, "A")  # not in a tool worker thread: silently skipped
    server.league_progress(None)(1, 2, "A")
