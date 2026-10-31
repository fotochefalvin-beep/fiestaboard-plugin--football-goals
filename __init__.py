"""Football Goal Notifier plugin for FiestaBoard.

Announces goals in matches of the one team you follow, pushed by sportapi.ai:

* A background WebSocket listener (``sportapi.ScoreListener``) subscribes to
  today's fixtures of the watched team and receives ``score_update`` pushes.
  It starts lazily from ``fetch_data``/``on_config_change`` and stops in
  ``cleanup()``.
* When a fixture's total goes up, the plugin fetches the scorer from
  ``/fixtures/{id}/events`` and fires a board trigger::

      GOAL!
      MECHELEN 1-0 GENK
      S. MICHEZ 22'

  When the scorer is not in ``/events`` yet the trigger fires without line 3
  and ``/events`` is retried after 20 s and 60 s; the same ``trigger_id`` is
  fired again once the scorer shows up.
* The first update of a fixture after each (re)connect is only a baseline;
  score decreases (disallowed goals) are stored silently.
* The template variables (``home_team``, ``away_team``, ``score``,
  ``scorer``, ``minute``) follow the team's match: the next match before
  kick-off, the live score during it (scorer/minute = latest goal), the final
  state for two hours after full time.

Request economy: team lookups at most once per hour per team (normally every
``TEAM_REFRESH_SECONDS``), one ``/fixtures/{id}`` per watched fixture, one
``/events`` call per goal plus at most two retries.

The API key is never logged.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.plugins.base import PluginBase, PluginResult, TriggerResult
from src.triggers import TriggerPriority, get_trigger_service

try:
    from . import sportapi
except ImportError:  # pragma: no cover - imported outside its package, e.g. by
    # pytest collecting this repo's root as a top-level module.
    import sportapi  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

PLUGIN_ID = "football_goals"
TRIGGER_ID_PREFIX = "football_goals_"
DEFAULT_COLS = 22
DEFAULT_ROWS = 6
MAX_VARIABLE_LENGTH = 22
DEFAULT_TRIGGER_DURATION = 60
MIN_TRIGGER_DURATION = 10
VARIABLES = ("home_team", "away_team", "score", "scorer", "minute")

# Team lookups: the spec allows at most one per hour per team; every 3 hours
# (plus once when the day changes) keeps a match day well under 50 requests.
MIN_TEAM_REFRESH_SECONDS = 3600
TEAM_REFRESH_SECONDS = 3 * 3600
# Scorer retries, measured from the moment the goal was seen.
SCORER_RETRY_DELAYS = (20, 60)
# A fixture that kicked off this long ago is still "today's" (late kick-offs
# run past midnight).
LIVE_WINDOW = timedelta(hours=4)
# How long fetch_data keeps showing the last goal, and how long the
# variables keep a finished match's final state.
LAST_GOAL_HOLD_SECONDS = 2 * 3600
FINAL_STATE_HOLD_SECONDS = 2 * 3600
FINISHED_STATUSES = {"FT", "AET", "PENS", "POSTPONED", "CANCELLED", "CANCELED", "ABANDONED"}

try:  # sportapi.ai `date`/`datetime` are Central European time.
    from zoneinfo import ZoneInfo

    SPORTAPI_TZ = ZoneInfo("Europe/Brussels")
except Exception:  # pragma: no cover - no tz database on the box
    SPORTAPI_TZ = timezone(timedelta(hours=1))


# ── text helpers ─────────────────────────────────────────────────────────────


def board_text(value: Any) -> str:
    """Upper-case ASCII for the board: ``Roganović`` -> ``ROGANOVIC``."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"\s+", " ", text).strip()
    return text.upper()


def team_slug(name: str) -> str:
    """A team as typed in settings -> a sportapi.ai slug (``FC Barcelona`` -> ``fc-barcelona``)."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def split_teams(value: Any) -> List[str]:
    """Comma-separated text (the ``team`` setting, or the legacy ``teams``)
    -> unique slugs, in order."""
    if not isinstance(value, str):
        return []
    slugs = [team_slug(part) for part in value.split(",") if part.strip()]
    return list(dict.fromkeys(slug for slug in slugs if slug))


def fit_match_line(home: str, away: str, home_score: int, away_score: int, cols: int) -> str:
    """``MECHELEN 1-0 GENK``, shortening the longer name until it fits *cols*."""
    home, away = board_text(home) or "HOME", board_text(away) or "AWAY"
    score = f"{home_score}-{away_score}"
    budget = cols - len(score) - 2
    if budget < 2:
        return score[:cols]
    while len(home) + len(away) > budget:
        if len(home) >= len(away):
            home = home[:-1].rstrip()
        else:
            away = away[:-1].rstrip()
    return f"{home} {score} {away}"


def fit_scorer_line(player: str, minute: str, tag: str, cols: int) -> str:
    """``S. MICHEZ 22'`` plus `` (OG)``/`` (PEN)`` when it fits, never wider than *cols*."""
    player, minute = board_text(player), board_text(minute)
    base = f"{player} {minute}".strip()
    if tag and len(base) + 1 + len(tag) <= cols:
        return f"{base} {tag}"
    if len(base) <= cols:
        return base
    room = cols - len(minute) - 1
    if room < 1:
        return base[:cols]
    return f"{player[:room].rstrip()} {minute}"


def goal_tag(extra_info: Any) -> str:
    """``(OG)`` for an own goal, ``(PEN)`` for a penalty, else ``""``."""
    info = str(extra_info or "").lower()
    if re.search(r"\bown\b|\bown[ _-]?goal\b|\bog\b", info):
        return "(OG)"
    if re.search(r"\bpen(alty)?\b", info):
        return "(PEN)"
    return ""


def is_goal_event(event: Dict[str, Any]) -> bool:
    kind = str(event.get("event_type") or "").lower()
    if "goal" not in kind:
        return False
    return not any(word in kind for word in ("miss", "disallow", "cancel", "saved"))


def minute_key(minute: Any) -> Tuple[int, int]:
    """``90'+4`` -> ``(90, 4)`` for ordering; unknown -> ``(-1, 0)``."""
    numbers = [int(n) for n in re.findall(r"\d+", str(minute or ""))]
    if not numbers:
        return (-1, 0)
    return (numbers[0], numbers[1] if len(numbers) > 1 else 0)


def latest_goal(events: List[Dict[str, Any]], total_goals: int) -> Optional[Dict[str, Any]]:
    """The newest goal, once ``/events`` lists at least *total_goals* goals."""
    goals = [event for event in events if is_goal_event(event)]
    if not goals or len(goals) < total_goals:
        return None
    ordered = sorted(enumerate(goals), key=lambda pair: (minute_key(pair[1].get("minute")), pair[0]))
    return ordered[-1][1]


def parse_kickoff(value: Any) -> Optional[datetime]:
    """sportapi.ai ``datetime`` (``2026-10-31 21:00:00``, CET/CEST) -> aware datetime."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SPORTAPI_TZ)
    return parsed


@dataclass
class Fixture:
    """A watched fixture."""

    id: int
    kickoff: Optional[datetime]
    home_slug: str
    away_slug: str
    home_name: str
    away_name: str
    status: str = ""
    home_short: str = ""
    away_short: str = ""
    details_loaded: bool = False

    @property
    def home_label(self) -> str:
        return self.home_short or self.home_name

    @property
    def away_label(self) -> str:
        return self.away_short or self.away_name


# ── plugin ───────────────────────────────────────────────────────────────────


class FootballGoalsPlugin(PluginBase):
    """Pushes goals of watched teams' matches to the board."""

    #: Factory for the WebSocket listener (tests swap in a fake connect).
    listener_factory: Callable[..., Any] = sportapi.ScoreListener
    #: Factory for the REST client (tests swap in a fake session).
    client_factory: Callable[..., Any] = sportapi.SportApiClient

    def __init__(self, manifest: Dict[str, Any]):
        super().__init__(manifest)
        self._lock = threading.RLock()
        self._clock: Callable[[], float] = time.monotonic
        self._now: Callable[[], datetime] = lambda: datetime.now(SPORTAPI_TZ)
        self._listener: Any = None
        self._listener_key: Optional[Tuple[str, Tuple[str, ...]]] = None
        self._client: Any = None
        self._client_key: Optional[str] = None
        # Set when the listener generation ends: scorer retries stop waiting.
        self._stop_event = threading.Event()
        self._reset_state()
        self._websockets_warned = False
        self._board_cols = DEFAULT_COLS
        self._board_rows = DEFAULT_ROWS
        self._legacy_warned = False
        # The last fired goal: its variables and the board lines 2-3.
        self._last_goal: Optional[Dict[str, str]] = None
        self._last_goal_match_line = ""
        self._last_goal_at = 0.0

    def _reset_state(self) -> None:
        self._fixtures: Dict[int, Fixture] = {}
        self._upcoming: Dict[int, Fixture] = {}
        self._team_fixtures: Dict[str, List[Fixture]] = {}
        self._team_refreshed_at: Dict[str, float] = {}
        self._team_refreshed_day: Dict[str, date] = {}
        self._unresolved: set = set()
        # Slug as typed -> the API's canonical slug for that team.
        self._canonical: Dict[str, str] = {}
        # Slug as typed -> sportapi.ai team id (to spot live matches by date).
        self._team_ids: Dict[str, int] = {}
        # Live fixtures found through /fixtures/date (missing from team lists).
        self._live_fixtures: Dict[int, Fixture] = {}
        self._live_checked = False
        # After a failed live check, no new try before this (clock) time.
        self._live_retry_at = 0.0
        self._baselines: Dict[int, Tuple[int, int]] = {}
        self._seen: set = set()
        # Plugin-wide: any newer goal (in any fixture) supersedes older ones.
        self._goal_seq = 0
        # The team's current match as seen through score pushes (see
        # ``_match_variables``): fixture, live score, latest goal, finish time.
        self._match: Optional[Dict[str, Any]] = None

    @property
    def plugin_id(self) -> str:
        return PLUGIN_ID

    # settings ----------------------------------------------------------------

    def _api_key(self, config: Optional[Dict[str, Any]] = None) -> str:
        value = (config if config is not None else self.config).get("api_key")
        return value.strip() if isinstance(value, str) else ""

    def _team_value(self, config: Optional[Dict[str, Any]] = None) -> str:
        """The ``team`` setting as typed; else the first slug of a legacy
        ``teams`` setting (0.1.x configs), logged once."""
        config = config if config is not None else self.config
        value = config.get("team")
        if isinstance(value, str) and value.strip():
            return value
        legacy = split_teams(config.get("teams"))
        if legacy:
            if not self._legacy_warned:
                logger.warning("Old 'teams' setting found: following only '%s'; set 'team' instead", legacy[0])
                self._legacy_warned = True
            return legacy[0]
        return ""

    def _teams(self, config: Optional[Dict[str, Any]] = None) -> List[str]:
        """The one watched team as a list (internals stay list-shaped);
        empty when unset or when several teams were typed."""
        value = self._team_value(config)
        if "," in value:
            return []
        return split_teams(value)[:1]

    def _trigger_duration(self) -> int:
        value = self.config.get("trigger_duration_seconds", DEFAULT_TRIGGER_DURATION)
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            return DEFAULT_TRIGGER_DURATION
        return max(seconds, MIN_TRIGGER_DURATION)

    def validate_config(self, config: Dict[str, Any]) -> List[str]:
        """Settings errors. Makes no network call (the host holds its registry
        lock here); teams the API could not resolve so far are reported."""
        errors: List[str] = []
        if not self._api_key(config):
            errors.append("sportapi.ai API key is required")
        value = self._team_value(config)
        teams = self._teams(config)
        if "," in value:
            errors.append("Enter one team, e.g. 'mechelen' (no commas)")
        elif not teams:
            errors.append("Enter a team, e.g. 'mechelen'")
        duration = config.get("trigger_duration_seconds", DEFAULT_TRIGGER_DURATION)
        try:
            if int(duration) < MIN_TRIGGER_DURATION:
                errors.append(f"trigger_duration_seconds must be at least {MIN_TRIGGER_DURATION}")
        except (TypeError, ValueError):
            errors.append("trigger_duration_seconds must be a whole number")
        with self._lock:
            unresolved = sorted(slug for slug in teams if slug in self._unresolved)
        for slug in unresolved:
            errors.append(f"sportapi.ai does not know the team '{slug}' (use the team slug, e.g. 'barcelona')")
        return errors

    # lifecycle ---------------------------------------------------------------

    def on_config_change(self, old_config: Dict[str, Any], new_config: Dict[str, Any]) -> None:
        """Restart the listener for the new settings (stop the old one first).

        With the same API key, what is known about teams that stay configured
        is kept, so saving settings never re-spends team lookups.
        """
        self._stop_listener()
        with self._lock:
            if self._teams(old_config) != self._teams(new_config):
                # Another team: the old team's match and goal no longer apply.
                self._match = None
                self._last_goal = None
            if self._api_key(old_config) != self._api_key(new_config):
                self._reset_state()
                self._client = None
                self._client_key = None
            else:
                keep = set(self._teams(new_config))
                for mapping in (self._team_fixtures, self._team_refreshed_at, self._team_refreshed_day,
                                self._canonical, self._team_ids):
                    for slug in [slug for slug in mapping if slug not in keep]:
                        del mapping[slug]
                self._unresolved &= keep
                # Rebuilt from the kept team lists; running matches come back
                # through the live check.
                self._fixtures, self._upcoming, self._live_fixtures = {}, {}, {}
                self._live_checked = False
        if self.enabled:
            self._ensure_listener()

    def cleanup(self) -> None:
        """Close the WebSocket and stop the listener thread."""
        self._stop_listener()

    def _stop_listener(self) -> None:
        with self._lock:
            listener, self._listener = self._listener, None
            self._listener_key = None
            self._stop_event.set()
            self._stop_event = threading.Event()
        if listener is not None:
            if not listener.stop(timeout=10.0):
                logger.warning("Score listener did not stop within 10 s")

    def _get_client(self) -> Any:
        key = self._api_key()
        with self._lock:
            if self._client is None or self._client_key != key:
                self._client = self.client_factory(key, clock=self._clock)
                self._client_key = key
            return self._client

    def _ensure_listener(self) -> None:
        """Start the listener when settings allow and none runs for them."""
        if not sportapi.WEBSOCKETS_AVAILABLE:
            if not self._websockets_warned:
                logger.error("Football Goals needs the 'websockets' package, which is not installed")
                self._websockets_warned = True
            return
        key, teams = self._api_key(), self._teams()
        if not key or not teams:
            return
        wanted_key = (key, tuple(teams))
        with self._lock:
            if (self._listener is not None and self._listener_key == wanted_key
                    and not self._listener.stopped and self._listener.is_alive()):
                return
        if self._listener is not None:
            self._stop_listener()
        with self._lock:
            if self._listener is not None:
                return
            listener = self.listener_factory(
                key,
                wanted=self.wanted_fixture_ids,
                on_update=self.handle_score_update,
                on_connect=self.handle_connect,
                refresh=self.refresh_fixtures,
            )
            self._listener = listener
            self._listener_key = wanted_key
        listener.start()
        logger.info("Football Goals listener started for '%s'", teams[0])

    # fixtures (REST) ---------------------------------------------------------

    def _team_due(self, slug: str, now: float, today: date) -> bool:
        last = self._team_refreshed_at.get(slug)
        if last is None:
            return True
        age = now - last
        if age < MIN_TEAM_REFRESH_SECONDS:
            return False
        return age >= TEAM_REFRESH_SECONDS or self._team_refreshed_day.get(slug) != today

    def refresh_fixtures(self) -> None:
        """Refresh the watched teams' fixtures when due (called by the listener)."""
        teams = self._teams()
        if not teams or not self._api_key():
            return
        client = self._get_client()
        now, today = self._clock(), self._now().date()
        for slug in teams:
            with self._lock:
                if not self._team_due(slug, now, today):
                    continue
                # Count the attempt, so errors never cause a request storm.
                self._team_refreshed_at[slug] = now
                self._team_refreshed_day[slug] = today
            try:
                body = client.team(slug)
            except sportapi.NotFoundError:
                with self._lock:
                    self._unresolved.add(slug)
                logger.warning("sportapi.ai does not know the team '%s'", slug)
                continue
            except sportapi.ApiError as exc:
                logger.warning("Fixture lookup for '%s' failed: %s", slug, exc)
                with self._lock:  # due again after MIN_TEAM_REFRESH_SECONDS, not 3 h
                    self._team_refreshed_at[slug] = now - (TEAM_REFRESH_SECONDS - MIN_TEAM_REFRESH_SECONDS)
                continue
            with self._lock:
                self._unresolved.discard(slug)
                team = body.get("team") if isinstance(body.get("team"), dict) else {}
                self._canonical[slug] = str(team.get("slug") or slug)
                if isinstance(team.get("id"), int):
                    self._team_ids[slug] = team["id"]
                self._team_fixtures[slug] = self._parse_team_fixtures(body)
        self._check_live(client, today)
        self._rebuild_fixtures()
        self._load_details(client)

    def _check_live(self, client: Any, today: date) -> None:
        """Once per listener start: find watched matches already running.

        A running match is missing from a team's ``upcoming``/``results``, so
        after a (re)start mid-match only ``/fixtures/date/{today}`` has it.
        One request per start; later refreshes keep it via retention.
        """
        with self._lock:
            if self._live_checked or not self._team_ids or self._clock() < self._live_retry_at:
                return
            self._live_checked = True
            slug_by_id = {team_id: self._canonical.get(slug, slug) for slug, team_id in self._team_ids.items()}
        try:
            items = client.fixtures_on(today.isoformat())
        except sportapi.ApiError as exc:
            logger.warning("Live fixture check failed: %s", exc)
            with self._lock:  # retry, but at most once per MIN_TEAM_REFRESH_SECONDS
                self._live_checked = False
                self._live_retry_at = self._clock() + MIN_TEAM_REFRESH_SECONDS
            return
        found: Dict[int, Fixture] = {}
        for item in items:
            home_id, away_id = item.get("home_id"), item.get("away_id")
            if home_id not in slug_by_id and away_id not in slug_by_id:
                continue
            status = str(item.get("status") or "")
            if status.upper() in FINISHED_STATUSES or status.lower() == "scheduled":
                continue
            try:
                fixture_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            found[fixture_id] = Fixture(
                id=fixture_id,
                kickoff=parse_kickoff(item.get("kickoff_utc")) or parse_kickoff(item.get("datetime")),
                home_slug=slug_by_id.get(home_id, ""),
                away_slug=slug_by_id.get(away_id, ""),
                home_name=str(item.get("home_team") or ""),
                away_name=str(item.get("away_team") or ""),
                status=status,
                home_short=str(item.get("home_short") or ""),
                away_short=str(item.get("away_short") or ""),
                details_loaded=True,
            )
        with self._lock:
            self._live_fixtures = found
        if found:
            logger.info("Watching %d match(es) already in progress", len(found))

    @staticmethod
    def _parse_team_fixtures(body: Dict[str, Any]) -> List[Fixture]:
        fixtures: List[Fixture] = []
        for key in ("upcoming", "results"):
            for item in body.get(key) or []:
                if not isinstance(item, dict):
                    continue
                try:
                    fixture_id = int(item.get("id"))
                except (TypeError, ValueError):
                    continue
                kickoff = parse_kickoff(item.get("kickoff_utc")) or parse_kickoff(item.get("datetime"))
                if kickoff is None and isinstance(item.get("date"), str):
                    kickoff = parse_kickoff(item["date"] + " 00:00:00")
                fixtures.append(
                    Fixture(
                        id=fixture_id,
                        kickoff=kickoff,
                        home_slug=str(item.get("home_slug") or ""),
                        away_slug=str(item.get("away_slug") or ""),
                        home_name=str(item.get("home_team") or ""),
                        away_name=str(item.get("away_team") or ""),
                        status=str(item.get("status") or ""),
                    )
                )
        return fixtures

    def _rebuild_fixtures(self) -> None:
        """Today's (and still running) fixtures to watch, plus the upcoming ones."""
        now = self._now()
        today = now.astimezone(SPORTAPI_TZ).date()
        with self._lock:
            watched: Dict[int, Fixture] = {}
            upcoming: Dict[int, Fixture] = {}
            # Fixtures already watched stay candidates: a match drops out of
            # its team's lists while it is being played.
            candidates: Dict[int, Fixture] = dict(self._live_fixtures)
            candidates.update(self._fixtures)
            for slug in self._teams():  # only teams still configured
                for fixture in self._team_fixtures.get(slug, []):
                    candidates[fixture.id] = fixture
            for fixture in candidates.values():
                known = self._fixtures.get(fixture.id) or self._upcoming.get(fixture.id)
                if known is not None and known.details_loaded:
                    fixture.home_short, fixture.away_short = known.home_short, known.away_short
                    fixture.details_loaded = True
                if fixture.kickoff is None or fixture.status.upper() in FINISHED_STATUSES:
                    continue
                local_day = fixture.kickoff.astimezone(SPORTAPI_TZ).date()
                if local_day == today or now - LIVE_WINDOW <= fixture.kickoff <= now:
                    watched[fixture.id] = fixture
                if fixture.kickoff > now:
                    upcoming[fixture.id] = fixture
            self._fixtures = watched
            self._upcoming = upcoming
            # The team list may report the current match finished before (or
            # without) a push saying so.
            match = self._match
            if match is not None and match["finished_at"] is None:
                listed = candidates.get(match["fixture"].id)
                if listed is not None and listed.status.upper() in FINISHED_STATUSES:
                    match["finished_at"] = self._clock()

    def _load_details(self, client: Any) -> None:
        """One ``/fixtures/{id}`` per watched fixture, for the short team names."""
        with self._lock:
            pending = [f for f in self._fixtures.values() if not f.details_loaded]
        for fixture in pending:
            try:
                details = client.fixture(fixture.id)
            except sportapi.RateLimitedError as exc:
                logger.warning("Fixture details for %d skipped: %s", fixture.id, exc)
                return
            except sportapi.ApiError as exc:
                logger.warning("Fixture details for %d failed: %s", fixture.id, exc)
                with self._lock:
                    fixture.details_loaded = True  # use the long names, don't retry
                continue
            with self._lock:
                fixture.home_short = str(details.get("home_short") or "")
                fixture.away_short = str(details.get("away_short") or "")
                fixture.details_loaded = True

    def wanted_fixture_ids(self) -> List[int]:
        """Fixture ids the listener subscribes to.

        A fixture that has not kicked off yet gets a 0-0 baseline here, so a
        goal in the first push after kick-off is not taken for a snapshot
        (sportapi.ai sends no snapshot for a scheduled fixture).
        """
        now = self._now()
        with self._lock:
            for fixture in self._fixtures.values():
                if fixture.id not in self._seen and fixture.kickoff is not None and fixture.kickoff > now:
                    self._baselines[fixture.id] = (0, 0)
                    self._seen.add(fixture.id)
            return sorted(self._fixtures)

    # goal detection ----------------------------------------------------------

    def handle_connect(self) -> None:
        """New connection: the next update of every fixture is a baseline."""
        with self._lock:
            self._seen.clear()

    def handle_score_update(self, message: Dict[str, Any]) -> None:
        """React to one ``score_update`` push (called on the listener thread)."""
        try:
            fixture_id = int(message.get("fixture_id"))
            home = int(message.get("home_score"))
            away = int(message.get("away_score"))
        except (TypeError, ValueError):
            return
        with self._lock:
            fixture = self._fixtures.get(fixture_id)
            if fixture is None:
                return  # not a watched fixture
            if message.get("status"):
                fixture.status = str(message["status"])
            previous = self._baselines.get(fixture_id)
            first = fixture_id not in self._seen
            self._seen.add(fixture_id)
            self._baselines[fixture_id] = (home, away)
            match = self._track_match(fixture, home, away)
            if first or previous is None:
                return  # snapshot after (re)connect: baseline only
            if home + away <= previous[0] + previous[1]:
                return  # unchanged or a disallowed goal: baseline only
            # Every goal in the team's match is announced, also goals against.
            self._goal_seq += 1
            seq = self._goal_seq
            match["goal_seq"] = seq
            stop_event = self._stop_event
        self._spawn(self._announce_goal, fixture, home, away, seq, stop_event)

    def _track_match(self, fixture: Fixture, home: int, away: int) -> Dict[str, Any]:
        """Follow the live score of the team's match (lock held).

        Every push counts, a decrease too. When the score changes, the latest
        goal's scorer/minute are cleared until ``/events`` names the goal that
        led to the new score.
        """
        match = self._match
        if match is None or match["fixture"].id != fixture.id:
            match = {"fixture": fixture, "score": None, "scorer": "", "minute": "",
                     "goal_seq": None, "finished_at": None}
            self._match = match
        match["fixture"] = fixture
        if match["score"] != (home, away):
            match["score"] = (home, away)
            match["scorer"] = match["minute"] = ""
            match["goal_seq"] = None
        if fixture.status.upper() in FINISHED_STATUSES:
            if match["finished_at"] is None:
                match["finished_at"] = self._clock()
        else:
            match["finished_at"] = None
        return match

    def _spawn(self, target: Callable[..., None], *args: Any) -> None:
        """Run a goal announcement off the listener thread (HTTP + retries)."""
        threading.Thread(target=target, args=args, name="football-goals-goal", daemon=True).start()

    def _wait(self, stop_event: threading.Event, seconds: float) -> bool:
        """Wait for a scorer retry; True when the listener stopped meanwhile."""
        return stop_event.wait(seconds)

    def _fetch_scorer(self, fixture_id: int, total: int) -> Optional[Dict[str, Any]]:
        try:
            events = self._get_client().events(fixture_id)
        except sportapi.ApiError as exc:
            logger.warning("Goal events for %d unavailable: %s", fixture_id, exc)
            return None
        return latest_goal(events, total)

    def _announce_goal(
        self, fixture: Fixture, home: int, away: int, seq: int, stop_event: threading.Event
    ) -> None:
        """Fire the goal; fetch the scorer, retrying after 20 s and 60 s."""
        started = self._clock()
        trigger_id = f"{TRIGGER_ID_PREFIX}{fixture.id}_{home}_{away}"
        goal = self._fetch_scorer(fixture.id, home + away)
        if not self._current(fixture.id, seq, stop_event):
            return
        self._fire(fixture, home, away, goal, trigger_id, seq)
        for delay in SCORER_RETRY_DELAYS:
            if goal is not None:
                return
            if self._wait(stop_event, max(0.0, started + delay - self._clock())):
                return
            if not self._current(fixture.id, seq, stop_event):
                return
            goal = self._fetch_scorer(fixture.id, home + away)
            if goal is not None and self._current(fixture.id, seq, stop_event):
                self._fire(fixture, home, away, goal, trigger_id, seq)

    def _current(self, fixture_id: int, seq: int, stop_event: threading.Event) -> bool:
        with self._lock:
            return not stop_event.is_set() and self._goal_seq == seq

    def _goal_lines(
        self, fixture: Fixture, home: int, away: int, goal: Optional[Dict[str, Any]]
    ) -> Tuple[List[str], Dict[str, str]]:
        cols = self._board_cols
        match = fit_match_line(fixture.home_label, fixture.away_label, home, away, cols)
        lines = ["GOAL!", match]
        scorer = minute = ""
        if goal is not None:
            scorer = board_text(goal.get("player_name"))
            minute = board_text(goal.get("minute"))
            line = fit_scorer_line(scorer, minute, goal_tag(goal.get("extra_info")), cols)
            if line:
                lines.append(line)
        data = self._variables(fixture, f"{home}-{away}", scorer, minute)
        return lines, data

    @staticmethod
    def _variables(fixture: Optional[Fixture], score: str = "", scorer: str = "", minute: str = "") -> Dict[str, str]:
        """The five template variables, upper-case ASCII, each <= 22 chars."""
        values = {
            "home_team": board_text(fixture.home_label) if fixture is not None else "",
            "away_team": board_text(fixture.away_label) if fixture is not None else "",
            "score": score,
            "scorer": board_text(scorer),
            "minute": board_text(minute),
        }
        return {name: values[name][:MAX_VARIABLE_LENGTH] for name in VARIABLES}

    def _fire(
        self,
        fixture: Fixture,
        home: int,
        away: int,
        goal: Optional[Dict[str, Any]],
        trigger_id: str,
        seq: Optional[int] = None,
    ) -> None:
        lines, data = self._goal_lines(fixture, home, away, goal)
        trigger = TriggerResult(
            triggered=True,
            trigger_id=trigger_id,
            message="\n".join(lines),
            priority=TriggerPriority.NOTABLE,
            duration_seconds=self._trigger_duration(),
            data=data,
        )
        with self._lock:
            service = get_trigger_service()
            for active in service.list_active_triggers():
                if active.plugin_id == self.plugin_id and active.trigger_id != trigger_id:
                    service.dismiss_trigger(active.trigger_id)  # replaced, not suppressed
            self.fire_trigger(trigger)
            self._last_goal = data
            self._last_goal_match_line = lines[1]
            self._last_goal_at = self._clock()
            match = self._match
            if (goal is not None and match is not None and seq is not None
                    and match["fixture"].id == fixture.id and match["goal_seq"] == seq):
                match["scorer"], match["minute"] = data["scorer"], data["minute"]
        logger.info(
            "Goal trigger %s fired (%s)", trigger_id, "with scorer" if goal is not None else "scorer pending"
        )

    # board -------------------------------------------------------------------

    def _remember_board(self) -> None:
        board = self.board
        if board is not None:
            self._board_cols = int(getattr(board, "cols", DEFAULT_COLS) or DEFAULT_COLS)
            self._board_rows = int(getattr(board, "rows", DEFAULT_ROWS) or DEFAULT_ROWS)

    def _pad(self, lines: List[str]) -> List[str]:
        cols, rows = self._board_cols, self._board_rows
        lines = [line[:cols] for line in lines][:rows]
        return lines + [""] * (rows - len(lines))

    def _next_match(self) -> Optional[Fixture]:
        now = self._now()
        with self._lock:
            candidates = [f for f in self._upcoming.values() if f.kickoff is not None and f.kickoff > now]
        return min(candidates, key=lambda f: f.kickoff) if candidates else None

    def _match_variables(self) -> Dict[str, str]:
        """The variables for the team's match.

        During a match (from its first score push): live teams and score,
        scorer/minute of the latest goal. After full time the final state is
        kept for ``FINAL_STATE_HOLD_SECONDS``. A watched match that kicked off
        but sent no push yet shows its teams only. Otherwise the next match's
        teams, or all empty.
        """
        now = self._now()
        with self._lock:
            match = self._match
            if match is not None:
                fixture = self._fixtures.get(match["fixture"].id, match["fixture"])
                if (match["finished_at"] is None and fixture.kickoff is not None
                        and now - fixture.kickoff > LIVE_WINDOW):
                    match["finished_at"] = self._clock()  # no full-time push seen
                finished_at = match["finished_at"]
                if finished_at is None or self._clock() - finished_at < FINAL_STATE_HOLD_SECONDS:
                    home, away = match["score"]
                    return self._variables(fixture, f"{home}-{away}", match["scorer"], match["minute"])
                self._match = None
            running = [
                f for f in self._fixtures.values()
                if f.kickoff is not None and f.kickoff <= now and f.status.upper() not in FINISHED_STATUSES
            ]
        if running:
            return self._variables(max(running, key=lambda f: f.kickoff))
        return self._variables(self._next_match())

    def fetch_data(self) -> PluginResult:
        """Board: the last goal (for two hours) or else the next match.
        Variables: the team's match (see ``_match_variables``)."""
        self._remember_board()
        if not sportapi.WEBSOCKETS_AVAILABLE:
            if not self._websockets_warned:
                logger.error("Football Goals needs the 'websockets' package, which is not installed")
                self._websockets_warned = True
            return PluginResult(
                available=True,
                data=self._variables(None),
                formatted_lines=self._pad(["FOOTBALL GOALS", "WEBSOCKETS PACKAGE", "NOT INSTALLED"]),
            )
        if not self._api_key():
            return PluginResult(available=False, error="sportapi.ai API key is not set")
        if not self._teams():
            if "," in self._team_value():
                return PluginResult(available=False, error="Enter one team (no commas)")
            return PluginResult(available=False, error="No team configured")
        self._ensure_listener()
        data = self._match_variables()

        with self._lock:
            goal = self._last_goal
            match_line = self._last_goal_match_line
            fresh = goal is not None and self._clock() - self._last_goal_at < LAST_GOAL_HOLD_SECONDS
        if fresh:
            lines = ["GOAL!", match_line]
            if goal["scorer"]:
                lines.append(fit_scorer_line(goal["scorer"], goal["minute"], "", self._board_cols))
            return PluginResult(available=True, data=data, formatted_lines=self._pad(lines))

        fixture = self._next_match()
        with self._lock:
            loaded = bool(self._team_refreshed_at)
        if fixture is None and not loaded:
            return PluginResult(available=True, data=data, formatted_lines=self._pad(["FOOTBALL GOALS", "LOADING MATCHES"]))
        if fixture is None:
            return PluginResult(available=True, data=data, formatted_lines=self._pad(["NO MATCH SCHEDULED"]))
        home, away = board_text(fixture.home_label), board_text(fixture.away_label)
        match = f"{home} - {away}"
        if len(match) > self._board_cols:
            match = fit_match_line(home, away, 0, 0, self._board_cols).replace(" 0-0 ", " - ")
        kickoff = fixture.kickoff.astimezone(SPORTAPI_TZ)
        when = board_text(kickoff.strftime("%a %d %b %H:%M"))
        return PluginResult(available=True, data=data, formatted_lines=self._pad(["NEXT MATCH", match, when]))


Plugin = FootballGoalsPlugin
