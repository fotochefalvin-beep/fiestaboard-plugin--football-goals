"""The WebSocket listener and the REST client, with fake transports."""

import asyncio
import json
import logging
import threading
import time
from functools import partial

import pytest

from .conftest import MECHELEN_GENK, TEST_KEY, FakeClock, FakeResponse, FakeSession, score


class Closed(Exception):
    """Stands in for websockets.ConnectionClosed."""


class FakeWebSocket:
    """Scripted server: yields `script` items, then waits (or closes)."""

    def __init__(self, script, close_at_end=False):
        self.script = list(script)
        self.close_at_end = close_at_end
        self.sent = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def recv(self):
        while True:
            if self.script:
                item = self.script.pop(0)
                if item == "CLOSE":
                    raise Closed("connection closed")
                return item if isinstance(item, (str, bytes)) else json.dumps(item)
            if self.close_at_end:
                raise Closed("connection closed")
            await asyncio.sleep(0.01)


class FakeServer:
    """Hands out one FakeWebSocket per connect, from a list of scripts."""

    def __init__(self, scripts, close_at_end=False):
        self.scripts = list(scripts)
        self.close_at_end = close_at_end
        self.sockets = []
        self.urls = []

    def connect(self, url):
        self.urls.append(url)
        if not self.scripts:
            raise OSError("connection refused")
        script = self.scripts.pop(0)
        if isinstance(script, Exception):
            raise script
        ws = FakeWebSocket(script, close_at_end=self.close_at_end)
        self.sockets.append(ws)
        return ws


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def make_listener(server, wanted=(MECHELEN_GENK,), **kwargs):
    from plugins.soccer_goals import sportapi

    updates, connects = [], []
    defaults = dict(
        wanted=lambda: list(wanted),
        on_update=updates.append,
        on_connect=lambda: connects.append(True),
        connect=server.connect,
        sync_seconds=0.05,
        idle_seconds=0.05,
        backoff_min=0.01,
        backoff_max=0.04,
        poll_seconds=0.01,
    )
    defaults.update(kwargs)
    listener = sportapi.ScoreListener(TEST_KEY, **defaults)
    return listener, updates, connects


CONNECTED = {"type": "connected", "message": "Authenticated."}


def test_spec_backoff_constants():
    from plugins.soccer_goals import sportapi

    assert sportapi.BACKOFF_MIN_SECONDS == 5
    assert sportapi.BACKOFF_MAX_SECONDS == 300


def test_subscribes_and_delivers_score_updates():
    server = FakeServer([[CONNECTED, {"type": "subscribed", "fixture_id": MECHELEN_GENK}, score(MECHELEN_GENK, 1, 0), "not json", "[1]", b'{"type":"pong"}']])
    listener, updates, connects = make_listener(server)
    listener.start()
    try:
        assert wait_until(lambda: updates)
    finally:
        assert listener.stop(timeout=5)
    assert updates[0]["home_score"] == 1
    assert connects == [True]
    assert server.sockets[0].sent[0] == {"action": "subscribe", "fixture_id": MECHELEN_GENK}
    assert server.sockets[0].closed
    assert server.urls[0].endswith("?key=" + TEST_KEY)


def test_subscriptions_follow_wanted_fixtures():
    wanted = {1, 2}
    server = FakeServer([[CONNECTED]])
    listener, _, _ = make_listener(server, wanted=(), )
    listener._wanted = lambda: set(wanted)
    listener.start()
    try:
        assert wait_until(lambda: server.sockets and len(server.sockets[0].sent) >= 2)
        wanted.discard(1)
        wanted.add(3)
        assert wait_until(lambda: len(server.sockets[0].sent) >= 4)
    finally:
        listener.stop(timeout=5)
    sent = server.sockets[0].sent
    assert {"action": "unsubscribe", "fixture_id": 1} in sent
    assert {"action": "subscribe", "fixture_id": 3} in sent


def test_no_connection_while_nothing_is_watched():
    refreshes = []
    server = FakeServer([[CONNECTED]])
    listener, _, _ = make_listener(server, wanted=(), refresh=lambda: refreshes.append(1))
    listener.start()
    try:
        assert wait_until(lambda: len(refreshes) >= 3)
    finally:
        listener.stop(timeout=5)
    assert server.urls == []


def test_disconnects_when_watch_list_empties():
    wanted = {MECHELEN_GENK}
    server = FakeServer([[CONNECTED]])
    listener, _, _ = make_listener(server)
    listener._wanted = lambda: set(wanted)
    listener.start()
    try:
        assert wait_until(lambda: server.sockets)
        wanted.clear()
        assert wait_until(lambda: server.sockets[0].closed)
    finally:
        listener.stop(timeout=5)
    assert listener.backoff_history == []


def test_drops_reconnect_with_doubling_backoff():
    server = FakeServer([["CLOSE"], ["CLOSE"], OSError("refused"), ["CLOSE"], ["CLOSE"], [CONNECTED]])
    listener, _, connects = make_listener(server)
    listener.start()
    try:
        assert wait_until(lambda: len(server.urls) >= 6)
    finally:
        listener.stop(timeout=5)
    assert listener.backoff_history[:5] == [0.01, 0.02, 0.04, 0.04, 0.04]  # doubling, capped


def test_stable_connection_resets_backoff(monkeypatch):
    from plugins.soccer_goals import sportapi

    monkeypatch.setattr(sportapi, "STABLE_CONNECTION_SECONDS", 0.0)
    server = FakeServer([[CONNECTED, "CLOSE"], [CONNECTED, "CLOSE"], [CONNECTED, "CLOSE"], [CONNECTED]])
    listener, _, _ = make_listener(server)
    listener.start()
    try:
        assert wait_until(lambda: len(server.urls) >= 4)
    finally:
        listener.stop(timeout=5)
    assert listener.backoff_history[:3] == [0.01, 0.01, 0.01]


def test_rejected_key_backs_off_and_never_logs_key(caplog):
    caplog.set_level(logging.DEBUG)
    rejection = {"type": "error", "message": "Invalid or missing API key. Connect with ?key=" + TEST_KEY}
    server = FakeServer([[rejection], [rejection], [rejection]])
    listener, updates, _ = make_listener(server)
    listener.start()
    try:
        assert wait_until(lambda: len(server.urls) >= 3)
    finally:
        listener.stop(timeout=5)
    assert updates == []
    assert "rejected the API key" in caplog.text
    assert TEST_KEY not in caplog.text
    assert listener.backoff_history[:2] == [0.01, 0.02]


def test_non_auth_error_after_connect_is_logged_only(caplog):
    server = FakeServer([[CONNECTED, {"type": "error", "message": "bad fixture"}, score(MECHELEN_GENK, 0, 0)]])
    listener, updates, _ = make_listener(server)
    listener.start()
    try:
        assert wait_until(lambda: updates)
    finally:
        listener.stop(timeout=5)
    assert "bad fixture" in caplog.text
    assert len(server.urls) == 1


def test_callback_errors_do_not_kill_the_listener(caplog):
    def boom(_message):
        raise RuntimeError("handler broke")

    def bad_wanted():
        raise RuntimeError("wanted broke")

    server = FakeServer([[CONNECTED, score(MECHELEN_GENK, 0, 0)]])
    listener, _, _ = make_listener(server, on_update=boom, refresh=lambda: (_ for _ in ()).throw(RuntimeError("refresh broke")))
    listener.start()
    try:
        assert wait_until(lambda: "handler broke" in caplog.text)
        assert listener.is_alive()
    finally:
        listener.stop(timeout=5)
    assert "refresh broke" in caplog.text
    listener2, _, _ = make_listener(FakeServer([]), wanted=())
    listener2._wanted = bad_wanted
    assert listener2._wanted_ids() == set()
    assert "wanted broke" in caplog.text


def test_stop_is_prompt_even_during_slow_refresh():
    release = threading.Event()
    server = FakeServer([[CONNECTED]])
    listener, _, _ = make_listener(server, refresh=lambda: release.wait(30))
    listener.start()
    time.sleep(0.1)
    started = time.monotonic()
    assert listener.stop(timeout=10)
    assert time.monotonic() - started < 2
    assert not listener.is_alive()
    release.set()
    listener.start()  # a stopped listener is not restarted
    assert not listener.is_alive()


def test_sleep_and_refresh_after_executor_shutdown():
    from plugins.soccer_goals import sportapi

    listener, _, _ = make_listener(FakeServer([]))
    listener._executor = __import__("concurrent.futures").futures.ThreadPoolExecutor(1)
    listener._executor.shutdown()
    asyncio.run(listener._call_refresh())  # no crash
    assert sportapi.ScoreListener._decode(b'{"a": 1}') == {"a": 1}


# ── plugin lifecycle with the real listener ─────────────────────────────────


def test_config_change_and_cleanup_leave_no_listener_threads(make_plugin, session):
    from plugins.soccer_goals import sportapi

    servers = []

    def factory(*args, **kwargs):
        server = FakeServer([[CONNECTED]] * 5)
        servers.append(server)
        return sportapi.ScoreListener(
            *args, connect=server.connect, sync_seconds=0.05, idle_seconds=0.05, poll_seconds=0.01, **kwargs
        )

    def ws_threads():
        return [t for t in threading.enumerate() if t.name == "soccer-goals-ws" and t.is_alive()]

    baseline = len(ws_threads())
    plugin = make_plugin(start_listener=True)
    plugin.listener_factory = factory
    plugin.fetch_data()
    first = plugin._listener
    assert wait_until(lambda: servers and servers[0].sockets)
    plugin.fetch_data()  # same settings: no second listener
    assert plugin._listener is first
    assert len(ws_threads()) == baseline + 1

    plugin.config = dict(plugin.config, teams="mechelen")
    assert not first.is_alive()
    assert servers[0].sockets[0].closed
    assert wait_until(lambda: len(ws_threads()) == baseline + 1)

    second = plugin._listener
    plugin.cleanup()
    assert not second.is_alive()
    assert plugin._listener is None
    assert len(ws_threads()) == baseline


def test_disabling_the_plugin_stops_the_listener(make_plugin):
    from plugins.soccer_goals import sportapi

    server = FakeServer([[CONNECTED]])
    plugin = make_plugin(start_listener=True)
    plugin.listener_factory = partial(sportapi.ScoreListener, connect=server.connect, poll_seconds=0.01)
    plugin.fetch_data()
    listener = plugin._listener
    assert wait_until(lambda: server.sockets)
    plugin.enabled = False
    assert not listener.is_alive()


# ── REST client ──────────────────────────────────────────────────────────────


def test_client_errors_and_rate_limit():
    from plugins.soccer_goals import sportapi

    clock = FakeClock()
    session = FakeSession(
        {
            "/teams/slug/a": FakeResponse(500, {}),
            "/teams/slug/b": FakeResponse(200, ValueError("not json")),
            "/teams/slug/c": FakeResponse(200, ["list"]),
            "/fixtures/1": FakeResponse(200, {"success": True}),
            "/fixtures/1/events": FakeResponse(200, {"success": True, "data": None}),
            "/fixtures/2/events": FakeResponse(200, {"success": True, "data": [{"event_type": "goal"}, "x"]}),
            "/fixtures/3": FakeResponse(429, {}, {"Retry-After": "nonsense"}),
        }
    )
    client = sportapi.SportApiClient(TEST_KEY, session=session, clock=clock)
    for slug in ("a", "b", "c"):
        with pytest.raises(sportapi.ApiError):
            client.team(slug)
    with pytest.raises(sportapi.ApiError):
        client.fixture(1)
    assert client.events(1) == []
    assert client.events(2) == [{"event_type": "goal"}]
    with pytest.raises(sportapi.RateLimitedError) as hit:
        client.fixture(3)
    assert hit.value.retry_after == sportapi.DEFAULT_RETRY_AFTER_SECONDS
    sent = client.request_count
    with pytest.raises(sportapi.RateLimitedError):
        client.events(2)  # blocked locally, no request
    assert client.request_count == sent
    clock.advance(sportapi.DEFAULT_RETRY_AFTER_SECONDS + 1)
    assert client.events(2) == [{"event_type": "goal"}]


def test_client_network_error_never_shows_key():
    import requests

    from plugins.soccer_goals import sportapi

    class Broken:
        def get(self, url, headers=None, timeout=None):
            raise requests.ConnectionError(f"failed for {url} with {headers}")

    client = sportapi.SportApiClient(TEST_KEY, session=Broken())
    with pytest.raises(sportapi.ApiError) as err:
        client.team("mechelen")
    assert TEST_KEY not in str(err.value)
    assert sportapi.redact("x " + TEST_KEY, TEST_KEY) == "x ***"
    assert sportapi.redact("x", "") == "x"


def test_default_client_uses_requests_session():
    import requests

    from plugins.soccer_goals import sportapi

    assert isinstance(sportapi.SportApiClient(TEST_KEY)._session, requests.Session)


def test_key_is_url_encoded_in_ws_url():
    from plugins.soccer_goals import sportapi

    listener = sportapi.ScoreListener("a+b/c&d=e", wanted=lambda: [], on_update=lambda m: None)
    assert listener._url.endswith("?key=a%2Bb%2Fc%26d%3De")
