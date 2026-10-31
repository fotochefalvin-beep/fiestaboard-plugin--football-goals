"""sportapi.ai I/O for the Football Goal Notifier plugin.

Two pieces, both kept apart from the plugin so they can be tested with fakes:

* :class:`SportApiClient` -- the REST client (``https://sportapi.ai/api``,
  ``X-Api-Key`` header): team lookup by slug (with its upcoming fixtures),
  fixture details (team short names) and fixture events (goal scorers).
* :class:`ScoreListener` -- a background thread that holds the WebSocket
  (``wss://sportapi.ai/ws?key=...``), subscribes to the fixtures the plugin
  asks for and hands every ``score_update`` to a callback. Drops reconnect
  with backoff (5 s doubling to 5 min); :meth:`ScoreListener.stop` closes the
  connection and ends the thread.

The API key is never logged: every message that could carry it goes through
:func:`redact`.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import quote

import requests

try:  # Present in the host image through uvicorn[standard].
    import websockets
except ImportError:  # pragma: no cover - exercised through WEBSOCKETS_AVAILABLE
    websockets = None

logger = logging.getLogger(__name__)

API_BASE = "https://sportapi.ai/api"
WS_URL = "wss://sportapi.ai/ws"
HTTP_TIMEOUT_SECONDS = 8
# Retry-After fallback when a 429 carries none.
DEFAULT_RETRY_AFTER_SECONDS = 300

BACKOFF_MIN_SECONDS = 5.0
BACKOFF_MAX_SECONDS = 300.0
# A connection that stayed up this long resets the backoff.
STABLE_CONNECTION_SECONDS = 60.0
# How often the open connection re-syncs its subscriptions (and lets the
# plugin refresh its fixture list when due).
SYNC_SECONDS = 30.0
# How long the listener idles before asking again when nothing is wanted.
IDLE_SECONDS = 60.0
# Granularity of every wait, so stop() is noticed quickly.
POLL_SECONDS = 0.5
OPEN_TIMEOUT_SECONDS = 15

WEBSOCKETS_AVAILABLE = websockets is not None


def redact(text: Any, secret: str | None) -> str:
    """``str(text)`` with every occurrence of *secret* masked."""
    rendered = str(text)
    if secret:
        rendered = rendered.replace(secret, "***")
    return rendered


# ── REST ─────────────────────────────────────────────────────────────────────


class ApiError(Exception):
    """A sportapi.ai request failed. Messages never contain the key."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class AuthError(ApiError):
    """401/403: the key is missing or rejected."""


class NotFoundError(ApiError):
    """404: e.g. a team slug the API does not know."""


class RateLimitedError(ApiError):
    """429, or a call skipped because an earlier 429 is still in force."""

    def __init__(self, message: str, retry_after: float):
        super().__init__(message, 429)
        self.retry_after = retry_after


def _retry_after_seconds(value: str | None) -> float:
    try:
        seconds = float(value) if value is not None else DEFAULT_RETRY_AFTER_SECONDS
    except (TypeError, ValueError):
        seconds = DEFAULT_RETRY_AFTER_SECONDS
    return max(seconds, 1.0)


class SportApiClient:
    """Minimal sportapi.ai REST client.

    Honours ``Retry-After``: after a 429 every call raises
    :class:`RateLimitedError` without a request until the wait is over.
    ``request_count`` counts the requests actually sent.
    """

    def __init__(
        self,
        api_key: str,
        session: Any = None,
        clock: Callable[[], float] = time.monotonic,
        base_url: str = API_BASE,
    ):
        self._api_key = api_key
        self._session = session if session is not None else requests.Session()
        self._clock = clock
        self._base_url = base_url.rstrip("/")
        self._blocked_until = 0.0
        self._lock = threading.Lock()
        self.request_count = 0

    def _get(self, path: str) -> dict[str, Any]:
        with self._lock:
            remaining = self._blocked_until - self._clock()
            if remaining > 0:
                raise RateLimitedError(f"rate limited for another {remaining:.0f} s", remaining)
            self.request_count += 1
        try:
            response = self._session.get(
                self._base_url + path,
                headers={"X-Api-Key": self._api_key, "Accept": "application/json"},
                timeout=HTTP_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise ApiError(f"GET {path} failed: {redact(type(exc).__name__, self._api_key)}") from None

        status = response.status_code
        if status == 429:
            wait = _retry_after_seconds(response.headers.get("Retry-After"))
            with self._lock:
                self._blocked_until = max(self._blocked_until, self._clock() + wait)
            raise RateLimitedError(f"GET {path}: quota hit (HTTP 429), retry after {wait:.0f} s", wait)
        if status in (401, 403):
            raise AuthError(f"GET {path}: API key rejected (HTTP {status})", status)
        if status == 404:
            raise NotFoundError(f"GET {path}: not found (HTTP 404)", status)
        if status >= 400:
            raise ApiError(f"GET {path}: HTTP {status}", status)
        try:
            body = response.json()
        except ValueError:
            raise ApiError(f"GET {path}: response is not JSON", status) from None
        if not isinstance(body, dict):
            raise ApiError(f"GET {path}: unexpected response shape", status)
        return body

    def team(self, slug: str) -> dict[str, Any]:
        """``/teams/slug/{slug}``: ``{"team": {...}, "upcoming": [...], "results": [...]}``."""
        return self._get(f"/teams/slug/{slug}")

    def fixtures_on(self, day: str) -> list[dict[str, Any]]:
        """``/fixtures/date/{YYYY-MM-DD}`` -> every fixture that day (with
        ``home_id``/``away_id`` and short names). Live matches show up here
        but not in a team's ``upcoming``/``results``."""
        data = self._get(f"/fixtures/date/{day}").get("data")
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    def fixture(self, fixture_id: int) -> dict[str, Any]:
        """``/fixtures/{id}`` -> the ``fixture`` object (with ``home_short``/``away_short``)."""
        fixture = self._get(f"/fixtures/{int(fixture_id)}").get("fixture")
        if not isinstance(fixture, dict):
            raise ApiError(f"GET /fixtures/{int(fixture_id)}: no fixture in response")
        return fixture

    def events(self, fixture_id: int) -> list[dict[str, Any]]:
        """``/fixtures/{id}/events`` -> the list of events (may be empty)."""
        data = self._get(f"/fixtures/{int(fixture_id)}/events").get("data")
        if not isinstance(data, list):
            return []
        return [event for event in data if isinstance(event, dict)]


# ── WebSocket ────────────────────────────────────────────────────────────────


class ListenerAuthError(Exception):
    """The WebSocket server rejected the key."""


# websockets logs the handshake request line -- which carries ?key= -- at
# DEBUG. Its connections log through this logger, which never goes below INFO.
_ws_logger = logging.getLogger(__name__ + ".ws")
_ws_logger.setLevel(logging.INFO)


def _default_connect(url: str):
    return websockets.connect(url, open_timeout=OPEN_TIMEOUT_SECONDS, max_size=2**20, logger=_ws_logger)


class ScoreListener:
    """Background WebSocket listener for sportapi.ai ``score_update`` pushes.

    Callbacks (all called from the listener's own threads):

    * ``refresh()`` -- run in a worker thread before each (re)connect and
      every ``SYNC_SECONDS`` while connected; the plugin refreshes its
      fixture list here when due. May block on HTTP.
    * ``wanted()`` -> fixture ids to subscribe to. Cheap, no I/O. When it is
      empty the listener disconnects and idles.
    * ``on_connect()`` -- a new connection is up (before any update of it).
    * ``on_update(message)`` -- one decoded ``score_update``. Must be quick.
    """

    def __init__(
        self,
        api_key: str,
        wanted: Callable[[], Iterable[int]],
        on_update: Callable[[dict[str, Any]], None],
        on_connect: Callable[[], None] = lambda: None,
        refresh: Callable[[], None] = lambda: None,
        connect: Callable[[str], Any] | None = None,
        ws_url: str = WS_URL,
        clock: Callable[[], float] = time.monotonic,
        sync_seconds: float = SYNC_SECONDS,
        idle_seconds: float = IDLE_SECONDS,
        backoff_min: float = BACKOFF_MIN_SECONDS,
        backoff_max: float = BACKOFF_MAX_SECONDS,
        poll_seconds: float = POLL_SECONDS,
    ):
        self._api_key = api_key
        self._wanted = wanted
        self._on_update = on_update
        self._on_connect = on_connect
        self._refresh = refresh
        self._connect = connect or _default_connect
        self._url = f"{ws_url}?key={quote(api_key, safe='')}"
        self._clock = clock
        self._sync_seconds = sync_seconds
        self._idle_seconds = idle_seconds
        self._backoff_min = backoff_min
        self._backoff_max = backoff_max
        self._poll = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Runs refresh() so a slow HTTP call never blocks stop().
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self.connections = 0
        #: Backoff delays actually slept, newest last (for tests/diagnostics).
        self.backoff_history: list[float] = []

    # lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="football-goals-rest")
        self._thread = threading.Thread(target=self._run_thread, name="football-goals-ws", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> bool:
        """Close the connection and end the thread. True when it ended in time."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
        return thread is None or not thread.is_alive()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # internals ---------------------------------------------------------------

    def _run_thread(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:  # pragma: no cover - last-resort guard
            logger.error("Score listener crashed: %s", redact(exc, self._api_key))

    async def _sleep(self, seconds: float) -> None:
        deadline = self._clock() + seconds
        while not self._stop.is_set():
            remaining = deadline - self._clock()
            if remaining <= 0:
                return
            await asyncio.sleep(min(self._poll, remaining))

    async def _call_refresh(self) -> None:
        """Run refresh() in the worker thread, giving up early on stop()."""
        try:
            future = self._executor.submit(self._safe_refresh)
        except RuntimeError:  # executor already shut down
            return
        wrapped = asyncio.wrap_future(future)
        while not wrapped.done():
            if self._stop.is_set():
                wrapped.cancel()
                return
            await asyncio.wait({wrapped}, timeout=self._poll)

    def _safe_refresh(self) -> None:
        try:
            self._refresh()
        except Exception as exc:
            logger.error("Fixture refresh failed: %s", redact(exc, self._api_key))

    def _wanted_ids(self) -> set[int]:
        try:
            return {int(fid) for fid in self._wanted()}
        except Exception as exc:
            logger.error("Could not list watched fixtures: %s", redact(exc, self._api_key))
            return set()

    async def _run(self) -> None:
        backoff = self._backoff_min
        while not self._stop.is_set():
            await self._call_refresh()
            if self._stop.is_set():
                break
            if not self._wanted_ids():
                await self._sleep(self._idle_seconds)
                continue

            started = self._clock()
            outcome = "dropped"
            try:
                outcome = await self._session()
            except ListenerAuthError as exc:
                logger.error("sportapi.ai WebSocket rejected the API key: %s", redact(exc, self._api_key))
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception as exc:
                logger.warning(
                    "sportapi.ai WebSocket dropped (%s): %s",
                    type(exc).__name__,
                    redact(exc, self._api_key),
                )
            if self._stop.is_set():
                break
            if outcome == "idle":
                backoff = self._backoff_min
                continue
            if outcome == "connected" and self._clock() - started >= STABLE_CONNECTION_SECONDS:
                backoff = self._backoff_min
            self.backoff_history.append(backoff)
            logger.info("Reconnecting to sportapi.ai in %.0f s", backoff)
            await self._sleep(backoff)
            backoff = min(backoff * 2, self._backoff_max)

    async def _session(self) -> str:
        """One connection. Returns "idle" (nothing to watch), "stopped",
        "connected" (was authenticated, then dropped) or raises."""
        authenticated = False
        async with self._connect(self._url) as ws:
            self.connections += 1
            self._on_connect()
            subscribed: set[int] = set()
            last_sync: float | None = None
            while not self._stop.is_set():
                now = self._clock()
                if last_sync is None or now - last_sync >= self._sync_seconds:
                    if last_sync is not None:
                        await self._call_refresh()
                        if self._stop.is_set():
                            break
                    wanted = self._wanted_ids()
                    if not wanted:
                        return "idle"
                    for fid in sorted(wanted - subscribed):
                        await ws.send(json.dumps({"action": "subscribe", "fixture_id": fid}))
                    for fid in sorted(subscribed - wanted):
                        await ws.send(json.dumps({"action": "unsubscribe", "fixture_id": fid}))
                    subscribed = wanted
                    last_sync = self._clock()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=self._poll)
                except asyncio.TimeoutError:
                    continue
                except Exception as exc:  # connection closed or broken
                    logger.warning(
                        "sportapi.ai WebSocket closed (%s): %s",
                        type(exc).__name__,
                        redact(exc, self._api_key),
                    )
                    return "connected" if authenticated else "dropped"
                message = self._decode(raw)
                if message is None:
                    continue
                kind = message.get("type")
                if kind == "connected":
                    authenticated = True
                elif kind == "error":
                    text = str(message.get("message", ""))
                    if not authenticated or "key" in text.lower():
                        raise ListenerAuthError(text or "connection rejected")
                    logger.warning("sportapi.ai WebSocket error: %s", redact(text, self._api_key))
                elif kind == "score_update":
                    try:
                        self._on_update(message)
                    except Exception as exc:
                        logger.error("Score update handling failed: %s", redact(exc, self._api_key))
            return "stopped" if self._stop.is_set() else ("connected" if authenticated else "dropped")
        # Leaving the `async with` closes the connection.

    @staticmethod
    def _decode(raw: Any) -> dict[str, Any] | None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return message if isinstance(message, dict) else None
