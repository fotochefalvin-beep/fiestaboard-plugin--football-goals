"""Test fixtures for the Football Goal Notifier plugin.

The tests run against a FiestaBoard core checkout (see README, "Plugin
tests"): ``src.*`` must be importable and this repo linked as
``plugins/football_goals``. Host imports happen inside fixtures (lazily), so
collecting this file does not need the core on the path.

No test touches the network: HTTP goes through :class:`FakeSession`, the
WebSocket through :class:`FakeWebSocket`.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "manifest.json"
TEST_KEY = "sk_live_TESTKEY_never_logged"

MECHELEN_GENK = 15328878
BARCA_ALAVES = 15380455


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    """``requests.Session`` stand-in: routes by path, records every call.

    ``routes[path]`` is a FakeResponse, a list of them (consumed in order,
    the last one repeats) or a callable returning one.
    """

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        path = url.split("/api", 1)[1]
        self.calls.append({"path": path, "headers": headers, "timeout": timeout})
        route = self.routes.get(path)
        if route is None:
            return FakeResponse(404, {"success": False})
        if isinstance(route, list):
            return route.pop(0) if len(route) > 1 else route[0]
        if callable(route):
            return route()
        return route

    def paths(self):
        return [call["path"] for call in self.calls]


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def team_body(slug, team_id, fixtures, results=()):
    return {
        "success": True,
        "team": {"id": team_id, "name": slug.title(), "short_name": slug.title(), "slug": slug},
        "upcoming": list(fixtures),
        "results": list(results),
        "squad": [],
    }


def fixture_item(fid, when, home_slug, home, away_slug, away, status="Scheduled"):
    return {
        "id": fid,
        "date": when[:10],
        "datetime": when,
        "status": status,
        "home_team": home,
        "home_slug": home_slug,
        "away_team": away,
        "away_slug": away_slug,
        "league_name": "Test League",
    }


def date_item(fid, home_id, away_id, status, home_short, away_short, when="2026-10-31T15:00:00Z"):
    """One record of /fixtures/date/{day}: team ids, no slugs."""
    return {
        "id": fid,
        "home_id": home_id,
        "away_id": away_id,
        "status": status,
        "kickoff_utc": when,
        "home_team": home_short,
        "away_team": away_short,
        "home_short": home_short,
        "away_short": away_short,
    }


def fixture_details(fid, home_short, away_short):
    return {"success": True, "fixture": {"id": fid, "home_short": home_short, "away_short": away_short}}


def live_details(fid, home, away, status="LIVE", minute="12'", home_short="Mechelen", away_short="Genk"):
    """/fixtures/{id} of a running match: score, status and match minute."""
    return {
        "success": True,
        "fixture": {
            "id": fid,
            "home_short": home_short,
            "away_short": away_short,
            "home_score": home,
            "away_score": away,
            "status": status,
            "minute": minute,
        },
    }


def goal_event(player, minute, side="home", extra="Goal"):
    return {"event_type": "goal", "team_side": side, "player_name": player, "minute": minute, "extra_info": extra}


def events_body(fid, events):
    return {"success": True, "fixture_id": fid, "data": list(events)}


@pytest.fixture
def manifest():
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(autouse=True)
def trigger_service(monkeypatch, tmp_path):
    """A clean host TriggerService per test, its stores in a temp dir."""
    from src.triggers.service import get_trigger_service, reset_trigger_service

    monkeypatch.setenv("FIESTABOARD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        "src.triggers.service._default_dismissals_file",
        lambda: tmp_path / "trigger_dismissals.json",
    )
    reset_trigger_service()
    yield get_trigger_service()
    reset_trigger_service()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def session():
    """Mechelen (Genk at home today 16:00) and Barcelona (Alaves today 21:00)."""
    return FakeSession(
        {
            "/teams/slug/mechelen": FakeResponse(
                200,
                team_body(
                    "mechelen",
                    149,
                    [fixture_item(MECHELEN_GENK, "2026-10-31 16:00:00", "mechelen", "Mechelen", "racing-genk", "Racing Genk")],
                ),
            ),
            "/teams/slug/barcelona": FakeResponse(
                200,
                team_body(
                    "barcelona",
                    120,
                    [
                        fixture_item(BARCA_ALAVES, "2026-10-31 21:00:00", "barcelona", "Barcelona", "deportivo-alaves", "Alavés"),
                        fixture_item(15688893, "2026-11-03 21:00:00", "barcelona", "Barcelona", "aston-villa", "Aston Villa"),
                    ],
                ),
            ),
            "/fixtures/date/2026-10-31": FakeResponse(
                200, {"success": True, "data": [date_item(1, 16, 12, "LIVE", "Bayern Munich", "Dortmund")]}
            ),
            f"/fixtures/{MECHELEN_GENK}": FakeResponse(200, fixture_details(MECHELEN_GENK, "Mechelen", "Genk")),
            f"/fixtures/{BARCA_ALAVES}": FakeResponse(200, fixture_details(BARCA_ALAVES, "Barcelona", "Alavés")),
            f"/fixtures/{MECHELEN_GENK}/events": FakeResponse(
                200, events_body(MECHELEN_GENK, [goal_event("S. Michez", "22'")])
            ),
        }
    )


@pytest.fixture
def now_holder():
    """Mutable "now" for the plugin (Europe/Brussels): 31 Oct 2026, 16:30.

    Mechelen-Genk (16:00) is live; Barcelona-Alaves (21:00) is later today.
    """
    from plugins.football_goals import SPORTAPI_TZ

    return {"now": datetime(2026, 10, 31, 16, 30, tzinfo=SPORTAPI_TZ)}


@pytest.fixture
def make_plugin(manifest, session, clock, now_holder):
    """Build an enabled plugin with fake clock, fake HTTP and no threads.

    Goal announcements run synchronously; scorer retries advance the fake
    clock instead of sleeping. No listener is started unless a test asks.
    """
    from plugins.football_goals import Plugin, sportapi

    created = []

    def make(config=None, start_listener=False):
        plugin = Plugin(manifest)
        plugin._clock = clock
        plugin._now = lambda: now_holder["now"]
        plugin.client_factory = lambda key, clock=None: sportapi.SportApiClient(key, session=session, clock=clock)
        plugin._spawn = lambda target, *args: target(*args)

        def fake_wait(stop_event, seconds):
            clock.advance(seconds)
            return stop_event.is_set()

        plugin._wait = fake_wait
        if not start_listener:
            plugin._ensure_listener = lambda: None
        plugin.config = dict(
            {"api_key": TEST_KEY, "team": "mechelen", "trigger_duration_seconds": 60},
            **(config or {}),
        )
        plugin.enabled = True
        created.append(plugin)
        return plugin

    yield make
    for plugin in created:
        plugin.cleanup()


@pytest.fixture
def live_plugin(make_plugin):
    """A plugin with fixtures loaded and a connection up (baselines pending)."""

    def make(config=None):
        plugin = make_plugin(config)
        plugin.refresh_fixtures()
        plugin.handle_connect()
        plugin.wanted_fixture_ids()
        return plugin

    return make


def score(fid, home, away, status="LIVE", minute="30'"):
    return {
        "type": "score_update",
        "fixture_id": fid,
        "home_score": home,
        "away_score": away,
        "home_pen": None,
        "away_pen": None,
        "status": status,
        "minute": minute,
    }
