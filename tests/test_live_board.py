"""Live match board, live minute poll and goal header -- one test per matrix row."""

import logging
from datetime import timedelta

from .conftest import (
    BARCA_ALAVES,
    MECHELEN_GENK,
    TEST_KEY,
    FakeResponse,
    events_body,
    fixture_item,
    goal_event,
    live_details,
    score,
    team_body,
)

DETAILS = f"/fixtures/{MECHELEN_GENK}"
EVENTS = f"/fixtures/{MECHELEN_GENK}/events"


def plugin_triggers(service):
    return [t for t in service.list_active_triggers() if t.plugin_id == "football_goals"]


def page(plugin, rows=3):
    return plugin.fetch_data().formatted_lines[:rows]


def polls(session, path=DETAILS):
    return session.paths().count(path)


def tick(plugin, clock, now_holder, seconds=60):
    """Let *seconds* pass, then run one listener refresh."""
    clock.advance(seconds)
    now_holder["now"] += timedelta(seconds=seconds)
    plugin.refresh_fixtures()


# ── matrix: match running, no goal yet ───────────────────────────────────────


def test_running_match_without_goal_is_a_live_page(make_plugin, session, trigger_service):
    session.routes[DETAILS] = FakeResponse(200, live_details(MECHELEN_GENK, 0, 0, minute="12'"))
    plugin = make_plugin()
    plugin.refresh_fixtures()
    result = plugin.fetch_data()
    assert result.formatted_lines[:3] == ["LIVE 12'", "MECHELEN 0-0 GENK", ""]
    assert result.data == {
        "home_team": "MECHELEN", "away_team": "GENK", "home_goals": "0", "away_goals": "0",
        "minute": "12'", "scorer": "", "goal_minute": "",
    }
    assert plugin_triggers(trigger_service) == []
    assert polls(session) == 1  # the live poll also brought the short names


def test_running_match_beats_the_following_match(make_plugin, session):
    """Acceptance: while Mechelen plays, the page never shows the next match."""
    session.routes["/teams/slug/mechelen"] = FakeResponse(200, team_body("mechelen", 149, [
        fixture_item(MECHELEN_GENK, "2026-10-31 16:00:00", "mechelen", "Mechelen", "racing-genk", "Racing Genk"),
        fixture_item(77, "2026-11-04 20:00:00", "mechelen", "Mechelen", "anderlecht", "Anderlecht"),
    ]))
    plugin = make_plugin()
    plugin.refresh_fixtures()  # /fixtures/{id} without score yet
    assert page(plugin, 2) == ["LIVE", "MECHELEN - GENK"]
    session.routes[DETAILS] = FakeResponse(200, live_details(MECHELEN_GENK, 0, 0, minute="31"))
    plugin._live_polled_at -= 60
    plugin.refresh_fixtures()
    assert page(plugin, 2) == ["LIVE 31'", "MECHELEN 0-0 GENK"]


# ── matrix: goal ─────────────────────────────────────────────────────────────


def test_goal_names_the_scoring_team_and_shows_on_the_page(live_plugin, session, trigger_service):
    session.routes[DETAILS] = FakeResponse(200, live_details(MECHELEN_GENK, 0, 0, minute="21'"))
    plugin = live_plugin()
    plugin.handle_score_update(score(MECHELEN_GENK, 0, 0, minute="21'"))
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 0, minute="22'"))
    [trigger] = plugin_triggers(trigger_service)
    assert trigger.message == "GOAL! MECHELEN\nMECHELEN 1-0 GENK\nS. MICHEZ 22'"
    assert trigger.trigger_id == f"football_goals_{MECHELEN_GENK}_1_0"
    assert page(plugin) == ["LIVE 22'", "MECHELEN 1-0 GENK", "S. MICHEZ 22'"]
    data = plugin.fetch_data().data
    assert (data["minute"], data["scorer"], data["goal_minute"]) == ("22'", "S. MICHEZ", "22'")


def test_goal_without_scorer_leaves_page_line_three_empty(live_plugin, session, trigger_service):
    session.routes[EVENTS] = FakeResponse(200, events_body(MECHELEN_GENK, []))
    plugin = live_plugin()
    plugin.handle_score_update(score(MECHELEN_GENK, 0, 0))
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 0))
    [trigger] = plugin_triggers(trigger_service)
    assert trigger.message == "GOAL! MECHELEN\nMECHELEN 1-0 GENK"
    assert page(plugin) == ["LIVE 30'", "MECHELEN 1-0 GENK", ""]
    assert len(session.paths()) and session.paths().count(EVENTS) == 3  # first try + 20 s / 60 s retries


# ── matrix: goal against ─────────────────────────────────────────────────────


def test_goal_against_names_the_other_team(live_plugin, session, trigger_service):
    session.routes[EVENTS] = FakeResponse(
        200, events_body(MECHELEN_GENK, [goal_event("S. Michez", "22'"), goal_event("T. Hrosovsky", "67'", "away")])
    )
    plugin = live_plugin()
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 0))
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 1, minute="67'"))
    [trigger] = plugin_triggers(trigger_service)
    assert trigger.message.split("\n")[0] == "GOAL! GENK"


def test_both_sides_up_in_one_update_names_no_team(live_plugin, session, trigger_service):
    session.routes[EVENTS] = FakeResponse(
        200, events_body(MECHELEN_GENK, [goal_event("S. Michez", "22'"), goal_event("T. Hrosovsky", "24'", "away")])
    )
    plugin = live_plugin()
    plugin.handle_score_update(score(MECHELEN_GENK, 0, 0))
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 1))
    [trigger] = plugin_triggers(trigger_service)
    assert trigger.message.split("\n")[0] == "GOAL!"


def test_goal_header_is_cut_to_the_board():
    from plugins.football_goals import goal_header

    assert goal_header("Borussia Mönchengladbach", 22) == "GOAL! BORUSSIA MONCHEN"
    assert len(goal_header("Borussia Mönchengladbach", 22)) <= 22
    assert goal_header("Genk", 22) == "GOAL! GENK"
    assert goal_header("", 22) == "GOAL!"
    assert goal_header("Genk", 6) == "GOAL!"
    assert goal_header("Genk", 3) == "GOA"


# ── matrix: half time ────────────────────────────────────────────────────────


def test_half_time_from_the_live_poll(make_plugin, session):
    session.routes[DETAILS] = FakeResponse(200, live_details(MECHELEN_GENK, 1, 1, status="HT", minute=None))
    plugin = make_plugin()
    plugin.refresh_fixtures()
    assert page(plugin, 2) == ["HT", "MECHELEN 1-1 GENK"]
    assert plugin.fetch_data().data["minute"] == "HT"


def test_half_time_from_a_push(live_plugin):
    plugin = live_plugin()
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 1, status="HT", minute="45'+2"))
    assert page(plugin, 2) == ["HT", "MECHELEN 1-1 GENK"]


# ── matrix: full time ────────────────────────────────────────────────────────


def test_full_time_stops_polling_holds_two_hours_then_next_match(make_plugin, session, clock, now_holder):
    session.routes["/teams/slug/mechelen"] = FakeResponse(200, team_body("mechelen", 149, [
        fixture_item(MECHELEN_GENK, "2026-10-31 16:00:00", "mechelen", "Mechelen", "racing-genk", "Racing Genk"),
        fixture_item(77, "2026-11-04 20:00:00", "mechelen", "Mechelen", "anderlecht", "Anderlecht"),
    ]))
    session.routes[DETAILS] = [
        FakeResponse(200, live_details(MECHELEN_GENK, 2, 1, minute="88'")),
        FakeResponse(200, live_details(MECHELEN_GENK, 2, 1, status="FT", minute=None)),
    ]
    plugin = make_plugin()
    plugin.refresh_fixtures()
    assert page(plugin, 1) == ["LIVE 88'"]
    tick(plugin, clock, now_holder)
    assert page(plugin, 2) == ["FT", "MECHELEN 2-1 GENK"]
    assert plugin.fetch_data().data["minute"] == "FT"
    for _ in range(10):
        tick(plugin, clock, now_holder)
    assert polls(session) == 2  # no poll after full time
    assert MECHELEN_GENK not in plugin.wanted_fixture_ids()
    tick(plugin, clock, now_holder, 2 * 3600 - 10 * 60 - 1)
    assert page(plugin, 1) == ["FT"]
    tick(plugin, clock, now_holder, 2)
    assert page(plugin, 2) == ["NEXT MATCH", "MECHELEN - ANDERLECHT"]
    assert polls(session) == 2


def test_live_window_caps_polling_without_full_time(make_plugin, session, clock, now_holder):
    session.routes[DETAILS] = FakeResponse(200, live_details(MECHELEN_GENK, 0, 0, minute="45'"))
    plugin = make_plugin()
    plugin.refresh_fixtures()  # 16:30
    for _ in range(4 * 60):  # until 20:30
        tick(plugin, clock, now_holder)
    # Kick-off 16:00 + LIVE_WINDOW 4 h: polls from 16:30 to 20:00 only.
    assert polls(session) == 211
    assert page(plugin, 1) == ["FT"]


# ── matrix: before kick-off ──────────────────────────────────────────────────


def test_before_kick_off_next_match_and_no_live_poll(make_plugin, session, clock, now_holder):
    plugin = make_plugin({"team": "barcelona"})
    plugin.refresh_fixtures()
    for _ in range(5):
        tick(plugin, clock, now_holder)
    assert polls(session, f"/fixtures/{BARCA_ALAVES}") == 1  # short names only
    result = plugin.fetch_data()
    assert result.formatted_lines[:3] == ["NEXT MATCH", "BARCELONA - ALAVES", "SAT 31 OCT 21:00"]
    assert {k: v for k, v in result.data.items() if k not in ("home_team", "away_team")} == {
        "home_goals": "", "away_goals": "", "minute": "", "scorer": "", "goal_minute": "",
    }


# ── matrix: minute source fails ──────────────────────────────────────────────


def test_failed_poll_keeps_last_minute_and_never_logs_key(make_plugin, session, clock, now_holder, caplog):
    caplog.set_level(logging.DEBUG)
    session.routes[DETAILS] = [
        FakeResponse(200, live_details(MECHELEN_GENK, 0, 0, minute="12'")),
        FakeResponse(500, {}),
        FakeResponse(500, {}),
        FakeResponse(200, live_details(MECHELEN_GENK, 0, 0, minute="15'")),
    ]
    plugin = make_plugin()
    plugin.refresh_fixtures()
    tick(plugin, clock, now_holder, 30)  # throttled: no request
    tick(plugin, clock, now_holder, 30)  # 500
    assert page(plugin, 2) == ["LIVE 12'", "MECHELEN 0-0 GENK"]
    tick(plugin, clock, now_holder)  # 500
    tick(plugin, clock, now_holder)
    assert polls(session) == 4  # one a minute, also while failing
    assert page(plugin, 1) == ["LIVE 15'"]
    assert "Live minute" in caplog.text
    assert TEST_KEY not in caplog.text


def test_rate_limit_pauses_polls_until_retry_after(make_plugin, session, clock, now_holder):
    session.routes[DETAILS] = [
        FakeResponse(200, live_details(MECHELEN_GENK, 0, 0, minute="12'")),
        FakeResponse(429, {"success": False}, {"Retry-After": "300"}),
        FakeResponse(200, live_details(MECHELEN_GENK, 0, 0, minute="19'")),
    ]
    plugin = make_plugin()
    plugin.refresh_fixtures()
    tick(plugin, clock, now_holder)  # 429
    for _ in range(4):  # 2-5 min later: still inside Retry-After
        tick(plugin, clock, now_holder)
    assert polls(session) == 2
    assert page(plugin, 1) == ["LIVE 12'"]
    tick(plugin, clock, now_holder)
    assert polls(session) == 3
    assert page(plugin, 1) == ["LIVE 19'"]


def test_poll_is_throttled_to_once_a_minute(make_plugin, session, clock, now_holder):
    session.routes[DETAILS] = FakeResponse(200, live_details(MECHELEN_GENK, 0, 0, minute="40'"))
    plugin = make_plugin()
    plugin.refresh_fixtures()
    for _ in range(20):  # listener syncs every 30 s for 10 minutes
        tick(plugin, clock, now_holder, 30)
    assert polls(session) == 11


# ── REST score: shown, never a trigger ───────────────────────────────────────


def test_rest_score_updates_the_page_but_fires_nothing(live_plugin, session, clock, now_holder, trigger_service):
    plugin = live_plugin()
    plugin.handle_score_update(score(MECHELEN_GENK, 0, 0))  # baseline
    session.routes[DETAILS] = FakeResponse(200, live_details(MECHELEN_GENK, 1, 0, minute="23'"))
    tick(plugin, clock, now_holder, 200)
    assert plugin_triggers(trigger_service) == []
    assert session.paths().count(EVENTS) == 0
    assert page(plugin) == ["LIVE 23'", "MECHELEN 1-0 GENK", ""]
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 0, minute="23'"))  # the push still announces it
    [trigger] = plugin_triggers(trigger_service)
    assert trigger.message == "GOAL! MECHELEN\nMECHELEN 1-0 GENK\nS. MICHEZ 22'"
    assert page(plugin)[2] == "S. MICHEZ 22'"


def test_recent_push_beats_a_lagging_rest_score(live_plugin, session, clock, now_holder):
    plugin = live_plugin()
    plugin.handle_score_update(score(MECHELEN_GENK, 0, 0))
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 0, minute="22'"))
    session.routes[DETAILS] = FakeResponse(200, live_details(MECHELEN_GENK, 0, 0, minute="23'"))
    tick(plugin, clock, now_holder)
    assert page(plugin) == ["LIVE 23'", "MECHELEN 1-0 GENK", "S. MICHEZ 22'"]
    tick(plugin, clock, now_holder, 180)  # REST still says 0-0 three minutes on: shown
    assert page(plugin, 2) == ["LIVE 23'", "MECHELEN 0-0 GENK"]


def test_rest_response_without_score_keeps_the_pushed_score(live_plugin, session, clock, now_holder):
    plugin = live_plugin()
    plugin.handle_score_update(score(MECHELEN_GENK, 2, 0))
    session.routes[DETAILS] = FakeResponse(200, live_details(MECHELEN_GENK, None, "x", minute=55))
    tick(plugin, clock, now_holder, 300)
    assert page(plugin, 2) == ["LIVE 55'", "MECHELEN 2-0 GENK"]


# ── minute formatting ────────────────────────────────────────────────────────


def test_format_minute():
    from plugins.football_goals import as_int, format_minute

    assert format_minute(67) == "67'"
    assert format_minute("67") == "67'"
    assert format_minute("67'") == "67'"
    assert format_minute("90+3") == "90'+3"
    assert format_minute("90'+3") == "90'+3"
    assert format_minute(" 45' + 2' ") == "45'+2"
    assert format_minute("Pen") == "PEN"
    assert format_minute(None) == ""
    assert format_minute(True) == ""
    assert as_int(True) is None and as_int("3") == 3 and as_int(None) is None


def test_other_final_status_is_shown_as_is(live_plugin):
    plugin = live_plugin()
    plugin.handle_score_update(score(MECHELEN_GENK, 0, 0, status="ABANDONED"))
    assert page(plugin, 1) == ["ABANDONED"]
