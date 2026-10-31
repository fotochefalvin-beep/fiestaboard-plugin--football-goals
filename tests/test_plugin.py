"""Goal detection, scorer retries and board text -- one test per I/O matrix row."""

import logging
from datetime import datetime, timedelta

import pytest

from .conftest import (
    BARCA_ALAVES,
    MECHELEN_GENK,
    TEST_KEY,
    FakeResponse,
    events_body,
    goal_event,
    score,
)

EVENTS = f"/fixtures/{MECHELEN_GENK}/events"


def plugin_triggers(service):
    return [t for t in service.list_active_triggers() if t.plugin_id == "soccer_goals"]


def events_calls(session, path=EVENTS):
    return [p for p in session.paths() if p == path]


# ── matrix: goal, scorer known ───────────────────────────────────────────────


def test_goal_with_known_scorer_fires_three_lines(live_plugin, session, trigger_service):
    from src.triggers import TriggerPriority

    plugin = live_plugin()
    plugin.handle_score_update(score(MECHELEN_GENK, 0, 0))  # baseline
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 0))

    [trigger] = plugin_triggers(trigger_service)
    assert trigger.message == "GOAL!\nMECHELEN 1-0 GENK\nS. MICHEZ 22'"
    assert trigger.trigger_id == f"soccer_goals_{MECHELEN_GENK}_1_0"
    assert trigger.priority == TriggerPriority.NOTABLE
    assert trigger.duration_seconds == 60
    assert trigger.data == {"match": "MECHELEN 1-0 GENK", "score": "1-0", "scorer": "S. MICHEZ", "minute": "22'"}
    assert len(events_calls(session)) == 1
    assert all(call["headers"]["X-Api-Key"] == TEST_KEY for call in session.calls)


def test_trigger_duration_setting_is_used(live_plugin, trigger_service):
    plugin = live_plugin({"trigger_duration_seconds": 120})
    plugin.handle_score_update(score(MECHELEN_GENK, 0, 0))
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 0))
    [trigger] = plugin_triggers(trigger_service)
    assert trigger.duration_seconds == 120


# ── matrix: scorer not yet available ─────────────────────────────────────────


def test_scorer_missing_fires_without_line_three_then_refires(live_plugin, session, clock, trigger_service):
    session.routes[EVENTS] = [
        FakeResponse(200, events_body(MECHELEN_GENK, [])),
        FakeResponse(200, events_body(MECHELEN_GENK, [goal_event("S. Michez", "22'")])),
    ]
    fired = []
    plugin = live_plugin()
    original = plugin.fire_trigger
    plugin.fire_trigger = lambda trigger: (fired.append((clock(), trigger.message, trigger.trigger_id)), original(trigger))
    start = clock()
    plugin.handle_score_update(score(MECHELEN_GENK, 0, 0))
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 0))

    assert [(t - start, msg) for t, msg, _ in fired] == [
        (0, "GOAL!\nMECHELEN 1-0 GENK"),
        (20, "GOAL!\nMECHELEN 1-0 GENK\nS. MICHEZ 22'"),
    ]
    assert len({trigger_id for _, _, trigger_id in fired}) == 1
    assert len(events_calls(session)) == 2
    [trigger] = plugin_triggers(trigger_service)
    assert trigger.message.endswith("S. MICHEZ 22'")


def test_scorer_found_on_last_retry(live_plugin, session, clock, trigger_service):
    session.routes[EVENTS] = [
        FakeResponse(200, events_body(MECHELEN_GENK, [])),
        FakeResponse(200, events_body(MECHELEN_GENK, [])),
        FakeResponse(200, events_body(MECHELEN_GENK, [goal_event("S. Michez", "22'")])),
    ]
    plugin = live_plugin()
    start = clock()
    plugin.handle_score_update(score(MECHELEN_GENK, 0, 0))
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 0))
    assert clock() - start == 60
    assert len(events_calls(session)) == 3
    [trigger] = plugin_triggers(trigger_service)
    assert trigger.message == "GOAL!\nMECHELEN 1-0 GENK\nS. MICHEZ 22'"


def test_scorer_never_found_keeps_trigger_after_two_retries(live_plugin, session, trigger_service):
    session.routes[EVENTS] = FakeResponse(200, events_body(MECHELEN_GENK, []))
    plugin = live_plugin()
    plugin.handle_score_update(score(MECHELEN_GENK, 0, 0))
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 0))
    assert len(events_calls(session)) == 3  # one call plus at most 2 retries
    [trigger] = plugin_triggers(trigger_service)
    assert trigger.message == "GOAL!\nMECHELEN 1-0 GENK"


def test_older_goal_in_events_does_not_count_as_the_new_one(live_plugin, session, trigger_service):
    """Events listing only the first goal is 'not yet available' for 2-0."""
    session.routes[EVENTS] = FakeResponse(200, events_body(MECHELEN_GENK, [goal_event("S. Michez", "22'")]))
    plugin = live_plugin()
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 0))
    plugin.handle_score_update(score(MECHELEN_GENK, 2, 0))
    [trigger] = plugin_triggers(trigger_service)
    assert trigger.message == "GOAL!\nMECHELEN 2-0 GENK"


def test_newer_goal_stops_old_retries(live_plugin, session, clock, trigger_service):
    """A retry belonging to a superseded goal never re-fires its trigger."""
    plugin = live_plugin()
    pending = []
    plugin._spawn = lambda target, *args: pending.append((target, args))
    plugin.handle_score_update(score(MECHELEN_GENK, 0, 0))
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 0))
    plugin.handle_score_update(score(MECHELEN_GENK, 2, 0))
    session.routes[EVENTS] = FakeResponse(
        200, events_body(MECHELEN_GENK, [goal_event("S. Michez", "22'"), goal_event("B. Other", "40'")])
    )
    target, args = pending[0]
    target(*args)  # the 1-0 announcement is stale: no request, no trigger
    assert plugin_triggers(trigger_service) == []
    assert events_calls(session) == [EVENTS]  # only the stale worker's first lookup
    target, args = pending[1]
    target(*args)
    [trigger] = plugin_triggers(trigger_service)
    assert trigger.message == "GOAL!\nMECHELEN 2-0 GENK\nB. OTHER 40'"


# ── matrix: goal against, goals_shown=selected ──────────────────────────────


def test_goal_against_selected_team_is_silent(live_plugin, session, trigger_service):
    plugin = live_plugin({"goals_shown": "selected"})
    plugin.handle_score_update(score(MECHELEN_GENK, 0, 0))
    plugin.handle_score_update(score(MECHELEN_GENK, 0, 1))  # Genk scores
    assert plugin_triggers(trigger_service) == []
    assert events_calls(session) == []
    assert plugin._baselines[MECHELEN_GENK] == (0, 1)

    session.routes[EVENTS] = FakeResponse(
        200, events_body(MECHELEN_GENK, [goal_event("X. Genk", "10'", "away"), goal_event("S. Michez", "22'")])
    )
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 1))  # Mechelen scores
    [trigger] = plugin_triggers(trigger_service)
    assert trigger.message == "GOAL!\nMECHELEN 1-1 GENK\nS. MICHEZ 22'"


def test_goal_against_is_shown_with_goals_shown_all(live_plugin, session, trigger_service):
    session.routes[EVENTS] = FakeResponse(200, events_body(MECHELEN_GENK, [goal_event("T. Hrosovsky", "12'", "away")]))
    plugin = live_plugin()
    plugin.handle_score_update(score(MECHELEN_GENK, 0, 0))
    plugin.handle_score_update(score(MECHELEN_GENK, 0, 1))
    [trigger] = plugin_triggers(trigger_service)
    assert trigger.message == "GOAL!\nMECHELEN 0-1 GENK\nT. HROSOVSKY 12'"


# ── matrix: unwatched fixture ────────────────────────────────────────────────


def test_unwatched_fixture_is_ignored(live_plugin, session, trigger_service):
    plugin = live_plugin()
    before = len(session.calls)
    plugin.handle_score_update(score(99999, 0, 0))
    plugin.handle_score_update(score(99999, 3, 0))
    assert plugin_triggers(trigger_service) == []
    assert len(session.calls) == before
    assert 99999 not in plugin._baselines


def test_malformed_update_is_ignored(live_plugin, trigger_service):
    plugin = live_plugin()
    plugin.handle_score_update({"type": "score_update", "fixture_id": "x"})
    plugin.handle_score_update({"type": "score_update", "fixture_id": MECHELEN_GENK, "home_score": None})
    assert plugin_triggers(trigger_service) == []


# ── matrix: first update after connect/reconnect ────────────────────────────


def test_snapshot_after_connect_and_reconnect_is_baseline(live_plugin, session, trigger_service):
    plugin = live_plugin()
    plugin.handle_score_update(score(MECHELEN_GENK, 2, 1))  # snapshot mid-match
    assert plugin_triggers(trigger_service) == []

    plugin.handle_connect()  # reconnect: goals scored meanwhile are not replayed
    plugin.wanted_fixture_ids()
    plugin.handle_score_update(score(MECHELEN_GENK, 3, 1))
    assert plugin_triggers(trigger_service) == []
    assert events_calls(session) == []
    assert plugin._baselines[MECHELEN_GENK] == (3, 1)


def test_scheduled_fixture_gets_zero_zero_baseline(live_plugin, session, now_holder, trigger_service):
    """No snapshot comes for a not-yet-started match, so 0-0 is assumed."""
    session.routes[f"/fixtures/{BARCA_ALAVES}/events"] = FakeResponse(
        200, events_body(BARCA_ALAVES, [goal_event("R. Lewandowski", "3'")])
    )
    plugin = live_plugin()
    assert plugin._baselines[BARCA_ALAVES] == (0, 0)
    now_holder["now"] += timedelta(hours=5)  # 21:30, match running
    plugin.handle_score_update(score(BARCA_ALAVES, 1, 0, minute="3'"))
    [trigger] = plugin_triggers(trigger_service)
    assert trigger.message == "GOAL!\nBARCELONA 1-0 ALAVES\nR. LEWANDOWSKI 3'"


# ── matrix: score decrease ───────────────────────────────────────────────────


def test_score_decrease_updates_baseline_silently(live_plugin, session, trigger_service):
    plugin = live_plugin()
    plugin.handle_score_update(score(MECHELEN_GENK, 2, 1))
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 1))  # disallowed
    assert plugin_triggers(trigger_service) == []
    assert events_calls(session) == []
    assert plugin._baselines[MECHELEN_GENK] == (1, 1)

    session.routes[EVENTS] = FakeResponse(
        200,
        events_body(
            MECHELEN_GENK,
            [goal_event("A", "5'"), goal_event("B", "9'", "away"), goal_event("S. Michez", "80'")],
        ),
    )
    plugin.handle_score_update(score(MECHELEN_GENK, 2, 1))  # real goal, back to 2-1
    [trigger] = plugin_triggers(trigger_service)
    assert trigger.message == "GOAL!\nMECHELEN 2-1 GENK\nS. MICHEZ 80'"


def test_unchanged_score_does_nothing(live_plugin, session, trigger_service):
    plugin = live_plugin()
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 0))
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 0, minute="HT"))
    assert plugin_triggers(trigger_service) == []
    assert events_calls(session) == []


# ── matrix: two goals in one update ──────────────────────────────────────────


def test_two_goals_in_one_update_fire_once_with_latest(live_plugin, session, trigger_service):
    session.routes[EVENTS] = FakeResponse(
        200,
        events_body(MECHELEN_GENK, [goal_event("Late Guy", "90'+4"), goal_event("S. Michez", "22'")]),
    )
    fired = []
    plugin = live_plugin()
    original = plugin.fire_trigger
    plugin.fire_trigger = lambda trigger: (fired.append(trigger), original(trigger))
    plugin.handle_score_update(score(MECHELEN_GENK, 0, 0))
    plugin.handle_score_update(score(MECHELEN_GENK, 2, 0))
    assert len(fired) == 1
    assert fired[0].message == "GOAL!\nMECHELEN 2-0 GENK\nLATE GUY 90'+4"
    assert len(events_calls(session)) == 1


# ── matrix: own goal / penalty ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "extra, line",
    [
        ("Own Goal", "S. MICHEZ 22' (OG)"),
        ("Penalty", "S. MICHEZ 22' (PEN)"),
        ("Goal", "S. MICHEZ 22'"),
        ("Bicycle kick", "S. MICHEZ 22'"),
        ("Open Play", "S. MICHEZ 22'"),
        ("Owner's pick", "S. MICHEZ 22'"),
        ("own_goal", "S. MICHEZ 22' (OG)"),
        (None, "S. MICHEZ 22'"),
    ],
)
def test_own_goal_and_penalty_tags(live_plugin, session, trigger_service, extra, line):
    session.routes[EVENTS] = FakeResponse(200, events_body(MECHELEN_GENK, [goal_event("S. Michez", "22'", extra=extra)]))
    plugin = live_plugin()
    plugin.handle_score_update(score(MECHELEN_GENK, 0, 0))
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 0))
    [trigger] = plugin_triggers(trigger_service)
    assert trigger.message.split("\n")[2] == line


def test_tag_dropped_when_it_does_not_fit(live_plugin, session, trigger_service):
    name = "K. Vanderstappenhovens"  # 22 chars with the minute it no longer fits
    session.routes[EVENTS] = FakeResponse(200, events_body(MECHELEN_GENK, [goal_event(name, "9'", extra="Penalty")]))
    plugin = live_plugin()
    plugin.handle_score_update(score(MECHELEN_GENK, 0, 0))
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 0))
    [trigger] = plugin_triggers(trigger_service)
    third = trigger.message.split("\n")[2]
    assert "(PEN)" not in third
    assert third.endswith(" 9'")
    assert len(third) <= 22


# ── matrix: bad / missing key ────────────────────────────────────────────────


def test_rejected_key_on_events_fires_without_scorer_and_never_logs_key(live_plugin, session, trigger_service, caplog):
    caplog.set_level(logging.DEBUG)
    session.routes[EVENTS] = FakeResponse(401, {"success": False, "error": "API key required."})
    plugin = live_plugin()
    plugin.handle_score_update(score(MECHELEN_GENK, 0, 0))
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 0))
    [trigger] = plugin_triggers(trigger_service)
    assert trigger.message == "GOAL!\nMECHELEN 1-0 GENK"
    assert "rejected" in caplog.text
    assert TEST_KEY not in caplog.text


def test_rejected_key_on_team_lookup_creates_no_fixtures(make_plugin, session, caplog):
    caplog.set_level(logging.DEBUG)
    for path in ("/teams/slug/mechelen", "/teams/slug/barcelona"):
        session.routes[path] = FakeResponse(401, {"success": False})
    plugin = make_plugin()
    plugin.refresh_fixtures()
    assert plugin.wanted_fixture_ids() == []
    assert TEST_KEY not in caplog.text


# ── matrix: quota hit ────────────────────────────────────────────────────────


def test_quota_hit_shows_goal_without_scorer_and_respects_retry_after(live_plugin, session, clock, trigger_service):
    session.routes[EVENTS] = FakeResponse(429, {"success": False, "scope": "day"}, {"Retry-After": "120"})
    plugin = live_plugin()
    plugin.handle_score_update(score(MECHELEN_GENK, 0, 0))
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 0))
    [trigger] = plugin_triggers(trigger_service)
    assert trigger.message == "GOAL!\nMECHELEN 1-0 GENK"
    # Retries at 20 s and 60 s fall inside Retry-After: no further requests.
    assert len(events_calls(session)) == 1


def test_quota_lapses_after_retry_after(live_plugin, session, clock, trigger_service):
    session.routes[EVENTS] = [
        FakeResponse(429, {"success": False}, {"Retry-After": "30"}),
        FakeResponse(200, events_body(MECHELEN_GENK, [goal_event("S. Michez", "22'")])),
    ]
    plugin = live_plugin()
    plugin.handle_score_update(score(MECHELEN_GENK, 0, 0))
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 0))
    assert len(events_calls(session)) == 2  # 20 s retry blocked, 60 s retry sent
    [trigger] = plugin_triggers(trigger_service)
    assert trigger.message.endswith("S. MICHEZ 22'")


# ── board text ───────────────────────────────────────────────────────────────


def test_lines_fit_a_narrow_board(live_plugin, session, trigger_service):
    from src.devices import BoardContext

    session.routes[f"/fixtures/{MECHELEN_GENK}"] = FakeResponse(
        200, {"success": True, "fixture": {"id": MECHELEN_GENK, "home_short": "Borussia Mönchengladbach", "away_short": "Wolverhampton Wanderers"}}
    )
    earlier = [goal_event("A. Nobody", f"{minute}'") for minute in range(1, 10)]
    session.routes[EVENTS] = FakeResponse(
        200, events_body(MECHELEN_GENK, earlier + [goal_event("Maximilian Mustermann-Schmidt", "90'+4", extra="Own Goal")])
    )
    plugin = live_plugin()
    with plugin._bound_board(BoardContext("note", rows=3, cols=15)):
        plugin.fetch_data()
    plugin.handle_score_update(score(MECHELEN_GENK, 9, 0))
    plugin.handle_score_update(score(MECHELEN_GENK, 10, 0))
    [trigger] = plugin_triggers(trigger_service)
    lines = trigger.message.split("\n")
    assert lines[0] == "GOAL!"
    assert " 10-0 " in lines[1]
    assert lines[2] == "MAXIMILIA 90'+4"  # name cut, minute kept, (OG) dropped
    assert all(len(line) <= 15 and line == line.upper() for line in lines)
    assert all(len(value) <= 22 for value in trigger.data.values())


def test_accents_are_folded(live_plugin, session, trigger_service):
    session.routes[EVENTS] = FakeResponse(200, events_body(MECHELEN_GENK, [goal_event("M. Roganović", "68'")]))
    plugin = live_plugin()
    plugin.handle_score_update(score(MECHELEN_GENK, 0, 0))
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 0))
    [trigger] = plugin_triggers(trigger_service)
    assert trigger.message.split("\n")[2] == "M. ROGANOVIC 68'"


def test_new_goal_replaces_previous_goal_trigger(live_plugin, session, trigger_service):
    plugin = live_plugin()
    plugin.handle_score_update(score(MECHELEN_GENK, 0, 0))
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 0))
    session.routes[EVENTS] = FakeResponse(
        200, events_body(MECHELEN_GENK, [goal_event("S. Michez", "22'"), goal_event("B. Other", "50'")])
    )
    plugin.handle_score_update(score(MECHELEN_GENK, 2, 0))
    [trigger] = plugin_triggers(trigger_service)
    assert trigger.trigger_id == f"soccer_goals_{MECHELEN_GENK}_2_0"


# ── fetch_data ───────────────────────────────────────────────────────────────


def test_fetch_data_shows_next_match_then_last_goal(live_plugin, clock):
    plugin = live_plugin()
    result = plugin.fetch_data()
    assert result.available
    assert result.formatted_lines[:3] == ["NEXT MATCH", "BARCELONA - ALAVES", "SAT 31 OCT 21:00"]
    assert result.data == {"match": "BARCELONA - ALAVES", "score": "", "scorer": "", "minute": ""}
    assert len(result.formatted_lines) == 6

    plugin.handle_score_update(score(MECHELEN_GENK, 0, 0))
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 0))
    result = plugin.fetch_data()
    assert result.formatted_lines[:3] == ["GOAL!", "MECHELEN 1-0 GENK", "S. MICHEZ 22'"]
    assert result.data["scorer"] == "S. MICHEZ"

    clock.advance(3 * 3600)  # goal is old: back to the next match
    assert plugin.fetch_data().formatted_lines[0] == "NEXT MATCH"


def test_fetch_data_without_matches(make_plugin, session):
    from .conftest import team_body

    session.routes["/teams/slug/mechelen"] = FakeResponse(200, team_body("mechelen", 149, []))
    session.routes["/teams/slug/barcelona"] = FakeResponse(200, team_body("barcelona", 120, []))
    plugin = make_plugin()
    plugin.refresh_fixtures()
    result = plugin.fetch_data()
    assert result.available
    assert result.formatted_lines[0] == "NO MATCH SCHEDULED"


def test_fetch_data_before_first_lookup_says_loading(make_plugin):
    assert make_plugin().fetch_data().formatted_lines[:2] == ["SOCCER GOALS", "LOADING MATCHES"]


def test_websockets_handshake_logger_never_logs_debug():
    import logging as std_logging

    from plugins.soccer_goals import sportapi

    assert sportapi._ws_logger.getEffectiveLevel() >= std_logging.INFO


def test_fetch_data_reports_missing_settings(make_plugin):
    assert not make_plugin({"api_key": ""}).fetch_data().available
    assert not make_plugin({"teams": " , "}).fetch_data().available


def test_missing_websockets_is_shown_not_raised(make_plugin, monkeypatch, caplog):
    from plugins.soccer_goals import sportapi

    monkeypatch.setattr(sportapi, "WEBSOCKETS_AVAILABLE", False)
    plugin = make_plugin(start_listener=True)
    result = plugin.fetch_data()
    assert result.available
    assert "WEBSOCKETS PACKAGE" in result.formatted_lines
    assert "websockets" in caplog.text
    assert plugin._listener is None


# ── validate_config / teams ──────────────────────────────────────────────────


def test_validate_config(make_plugin):
    plugin = make_plugin()
    assert plugin.validate_config(dict(plugin.config)) == []
    errors = plugin.validate_config({"api_key": " ", "teams": "", "goals_shown": "mine", "trigger_duration_seconds": 3})
    assert len(errors) == 4
    assert plugin.validate_config(dict(plugin.config, trigger_duration_seconds="x")) == [
        "trigger_duration_seconds must be a whole number"
    ]


def test_unknown_team_is_reported_not_raised(make_plugin, session, caplog):
    plugin = make_plugin({"teams": "mechelen, kv nowhere"})
    plugin.refresh_fixtures()
    assert "kv-nowhere" in caplog.text
    errors = plugin.validate_config(dict(plugin.config))
    assert errors == ["sportapi.ai does not know the team 'kv-nowhere' (use the team slug, e.g. 'barcelona')"]
    assert plugin.wanted_fixture_ids() == [MECHELEN_GENK]


def test_team_names_become_slugs():
    from plugins.soccer_goals import split_teams

    assert split_teams("Mechelen, FC Barcelona ,  Racing Genk, mechelen, Standard Liège") == [
        "mechelen",
        "fc-barcelona",
        "racing-genk",
        "standard-liege",
    ]
    assert split_teams(None) == []


def test_goals_shown_and_duration_fallbacks(make_plugin):
    plugin = make_plugin({"goals_shown": "weird", "trigger_duration_seconds": "abc"})
    assert plugin._goals_shown() == "all"
    assert plugin._trigger_duration() == 60
    assert make_plugin({"trigger_duration_seconds": 2})._trigger_duration() == 10


# ── fixtures: which matches are watched ──────────────────────────────────────


def test_only_todays_unfinished_fixtures_are_watched(make_plugin, session, now_holder):
    from .conftest import fixture_item, team_body

    session.routes["/teams/slug/mechelen"] = FakeResponse(
        200,
        team_body(
            "mechelen",
            149,
            [
                fixture_item(1, "2026-10-31 20:00:00", "mechelen", "Mechelen", "a", "A"),
                fixture_item(2, "2026-11-01 15:00:00", "mechelen", "Mechelen", "b", "B"),
            ],
            results=[
                fixture_item(3, "2026-10-31 14:00:00", "mechelen", "Mechelen", "c", "C", status="FT"),
                fixture_item(4, "2026-10-31 15:30:00", "mechelen", "Mechelen", "d", "D", status="LIVE"),
                fixture_item(5, "2026-10-30 23:30:00", "mechelen", "Mechelen", "e", "E", status="LIVE"),
                {"id": "bad"},
                "junk",
            ],
        ),
    )
    plugin = make_plugin({"teams": "mechelen"})
    now_holder["now"] = datetime(2026, 10, 31, 1, 0, tzinfo=now_holder["now"].tzinfo)
    plugin.refresh_fixtures()
    assert plugin.wanted_fixture_ids() == [1, 4, 5]  # 5 kicked off 1.5 h ago, past midnight


def test_watched_fixtures_roll_over_at_midnight(make_plugin, session, now_holder, clock):
    plugin = make_plugin()
    plugin.refresh_fixtures()
    assert plugin.wanted_fixture_ids() == [MECHELEN_GENK, BARCA_ALAVES]
    now_holder["now"] = datetime(2026, 11, 3, 12, 0, tzinfo=now_holder["now"].tzinfo)
    clock.advance(3600)
    plugin.refresh_fixtures()
    assert plugin.wanted_fixture_ids() == [15688893]


def test_fixture_details_failure_falls_back_to_long_names(live_plugin, session, trigger_service):
    del session.routes[f"/fixtures/{MECHELEN_GENK}"]
    session.routes[EVENTS] = FakeResponse(200, events_body(MECHELEN_GENK, [goal_event("S. Michez", "22'")]))
    plugin = live_plugin()
    plugin.handle_score_update(score(MECHELEN_GENK, 0, 0))
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 0))
    [trigger] = plugin_triggers(trigger_service)
    assert trigger.message.split("\n")[1] == "MECHELEN 1-0 RACING GE"  # long name, cut to 22


# ── request economy ──────────────────────────────────────────────────────────


def test_whole_match_day_stays_under_fifty_requests(make_plugin, session, clock, now_holder):
    """24 h of listener syncs every 30 s, two matches, eight goals."""
    from plugins.soccer_goals import SPORTAPI_TZ

    session.routes[EVENTS] = FakeResponse(200, events_body(MECHELEN_GENK, []))  # worst case: never found
    session.routes[f"/fixtures/{BARCA_ALAVES}/events"] = FakeResponse(200, events_body(BARCA_ALAVES, []))
    now_holder["now"] = datetime(2026, 10, 31, 0, 0, 30, tzinfo=SPORTAPI_TZ)
    plugin = make_plugin()
    plugin.handle_connect()
    goals = {
        datetime(2026, 10, 31, 16, 20): (MECHELEN_GENK, [(0, 0), (1, 0), (1, 1), (2, 1), (3, 1)]),
        datetime(2026, 10, 31, 21, 20): (BARCA_ALAVES, [(0, 0), (1, 0), (2, 0), (2, 1), (3, 1)]),
    }
    for step in range(24 * 120):
        plugin.refresh_fixtures()
        plugin.wanted_fixture_ids()
        local = now_holder["now"].replace(tzinfo=None, second=0, microsecond=0)
        if local in goals and now_holder["now"].second == 30:
            fid, scores = goals[local]
            for home, away in scores:
                plugin.handle_score_update(score(fid, home, away))
        clock.advance(30)
        now_holder["now"] += timedelta(seconds=30)

    paths = session.paths()
    team_calls = [p for p in paths if p.startswith("/teams/")]
    assert len([p for p in paths if p.endswith("/events")]) == 8 * 3
    assert len(team_calls) <= 2 * 9
    assert len(paths) < 50, paths


# ── running matches, retention, settings changes ────────────────────────────


def test_restart_mid_match_finds_the_running_match(make_plugin, session, trigger_service):
    """A running match is missing from the team lists; /fixtures/date has it."""
    from .conftest import date_item, team_body

    session.routes["/teams/slug/mechelen"] = FakeResponse(200, team_body("mechelen", 149, []))
    session.routes["/fixtures/date/2026-10-31"] = FakeResponse(
        200,
        {
            "success": True,
            "data": [
                date_item(MECHELEN_GENK, 149, 148, "LIVE", "Mechelen", "Genk"),
                date_item(2, 149, 7, "FT", "Mechelen", "Old"),
                date_item(3, 5, 6, "LIVE", "Other", "Match"),
                date_item("x", 149, 6, "LIVE", "Bad", "Id"),
            ],
        },
    )
    plugin = make_plugin({"teams": "mechelen"})
    plugin.refresh_fixtures()
    plugin.refresh_fixtures()  # checked once per start only
    assert plugin.wanted_fixture_ids() == [MECHELEN_GENK]
    assert session.paths().count("/fixtures/date/2026-10-31") == 1
    assert f"/fixtures/{MECHELEN_GENK}" not in session.paths()  # short names came along

    plugin.handle_connect()
    plugin.handle_score_update(score(MECHELEN_GENK, 0, 0))
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 0))
    [trigger] = plugin_triggers(trigger_service)
    assert trigger.message == "GOAL!\nMECHELEN 1-0 GENK\nS. MICHEZ 22'"


def test_live_check_failure_is_logged(make_plugin, session, caplog):
    session.routes["/fixtures/date/2026-10-31"] = FakeResponse(500, {})
    plugin = make_plugin()
    plugin.refresh_fixtures()
    assert "Live fixture check failed" in caplog.text
    assert plugin.wanted_fixture_ids() == [MECHELEN_GENK, BARCA_ALAVES]


def test_running_match_stays_watched_when_it_leaves_the_team_list(make_plugin, session, clock):
    from .conftest import team_body

    plugin = make_plugin()
    plugin.refresh_fixtures()
    session.routes["/teams/slug/mechelen"] = FakeResponse(200, team_body("mechelen", 149, []))
    clock.advance(4 * 3600)
    plugin.refresh_fixtures()
    assert session.paths().count("/teams/slug/mechelen") == 2
    assert MECHELEN_GENK in plugin.wanted_fixture_ids()

    plugin.handle_connect()
    plugin.handle_score_update(score(MECHELEN_GENK, 2, 1, status="FT"))
    plugin.refresh_fixtures()
    assert MECHELEN_GENK not in plugin.wanted_fixture_ids()


def test_settings_change_keeps_team_lookups_with_same_key(make_plugin, session):
    plugin = make_plugin()
    plugin.refresh_fixtures()
    team_calls = len([p for p in session.paths() if p.startswith("/teams/")])
    plugin.config = dict(plugin.config, teams="mechelen", goals_shown="selected")
    plugin.refresh_fixtures()
    assert len([p for p in session.paths() if p.startswith("/teams/")]) == team_calls
    assert plugin.wanted_fixture_ids() == [MECHELEN_GENK]

    plugin.config = dict(plugin.config, api_key="sk_live_OTHER")
    plugin.refresh_fixtures()
    assert session.calls[-1]["headers"]["X-Api-Key"] == "sk_live_OTHER"
    assert len([p for p in session.paths() if p.startswith("/teams/")]) == team_calls + 1


# ── review fixes ─────────────────────────────────────────────────────────────


def test_failed_team_lookup_is_retried_after_one_hour(make_plugin, session, clock):
    session.routes["/teams/slug/mechelen"] = [
        FakeResponse(500, {}),
        session.routes["/teams/slug/mechelen"],
    ]
    plugin = make_plugin({"teams": "mechelen"})
    plugin.refresh_fixtures()
    assert plugin.wanted_fixture_ids() == []
    clock.advance(1800)
    plugin.refresh_fixtures()
    assert session.paths().count("/teams/slug/mechelen") == 1
    clock.advance(1800)
    plugin.refresh_fixtures()
    assert session.paths().count("/teams/slug/mechelen") == 2
    assert plugin.wanted_fixture_ids() == [MECHELEN_GENK]


def test_failed_live_check_is_retried_at_most_hourly(make_plugin, session, clock):
    path = "/fixtures/date/2026-10-31"
    session.routes[path] = [FakeResponse(500, {}), session.routes[path]]
    plugin = make_plugin()
    plugin.refresh_fixtures()
    clock.advance(30)
    plugin.refresh_fixtures()
    assert session.paths().count(path) == 1
    clock.advance(3600)
    plugin.refresh_fixtures()
    plugin.refresh_fixtures()
    assert session.paths().count(path) == 2


def test_newer_goal_in_other_match_stops_older_retries(live_plugin, session, now_holder, trigger_service):
    session.routes[f"/fixtures/{BARCA_ALAVES}/events"] = FakeResponse(
        200, events_body(BARCA_ALAVES, [goal_event("R. Lewandowski", "3'")])
    )
    plugin = live_plugin()
    pending = []
    plugin._spawn = lambda target, *args: pending.append((target, args))
    plugin.handle_score_update(score(MECHELEN_GENK, 0, 0))
    plugin.handle_score_update(score(MECHELEN_GENK, 1, 0))
    now_holder["now"] += timedelta(hours=5)
    plugin.handle_score_update(score(BARCA_ALAVES, 1, 0))
    pending[1][0](*pending[1][1])  # Barcelona goal fires
    before = len(session.calls)
    pending[0][0](*pending[0][1])  # stale Mechelen announcement: nothing
    [trigger] = plugin_triggers(trigger_service)
    assert trigger.trigger_id == f"soccer_goals_{BARCA_ALAVES}_1_0"
    assert len(session.calls) == before + 1  # only its first lookup


def test_dead_listener_is_replaced(make_plugin):
    class DeadListener:
        stopped = False

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def is_alive(self):
            return False

        def stop(self, timeout=10.0):
            return True

    plugin = make_plugin(start_listener=True)
    plugin.listener_factory = DeadListener
    plugin.fetch_data()
    first = plugin._listener
    plugin.fetch_data()
    assert plugin._listener is not first
