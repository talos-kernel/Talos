"""The Telegram long poll heals short network blips itself — and only those.

Measured on a deployed instance over nine days: 150 read timeouts, 37 connection
resets, 73 DNS failures and 127 gateway errors, almost all of them single events that
the very next poll survived. Each one opened a new TCP and TLS connection, did its own
DNS lookup, and was written to the event log as a channel failure.

These tests pin the contract that replaced that: one kept-alive connection, a bounded
retry for failures that cannot have changed anything, and an unchanged loud failure for
everything that lasts or that a retry cannot fix.
"""
from __future__ import annotations

import json
import socket

import pytest
import requests

from talos import telegram
from talos.telegram import TelegramClient

TOKEN = "private-token"
UPDATE = {"update_id": 7, "message": {"from": {"id": 7}, "chat": {"id": 42}, "text": "hello"}}


def _ok(result: list) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response._content = json.dumps({"ok": True, "result": result}).encode()
    return response


def _http(status: int, url: str, body: dict | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.url = url
    response._content = json.dumps(body or {"ok": False}).encode()
    return response


class _Wire:
    """A scripted transport with a fake clock; counts connections, not just calls."""

    def __init__(self, script: list, *, timeout_costs_s: float = 60.0) -> None:
        self.script = list(script)
        self.now = 1000.0
        self.sleeps: list[float] = []
        self.offsets: list[int] = []
        self.timeouts: list[object] = []
        self.sessions = 0
        self.closed = 0
        self._timeout_costs_s = timeout_costs_s

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def new_session(self) -> "_Wire":
        self.sessions += 1
        return self

    def close(self) -> None:
        self.closed += 1

    def get(self, url: str, **kwargs: object):
        self.offsets.append(kwargs["params"]["offset"])  # type: ignore[index]
        self.timeouts.append(kwargs.get("timeout"))
        step = self.script.pop(0)
        if step == "read-timeout":
            self.now += self._timeout_costs_s
            raise requests.ReadTimeout(f"Read timed out at {url}")
        if step == "dns":
            raise requests.ConnectionError(f"Failed to resolve at {url}")
        if step == "reset":
            raise requests.ConnectionError(f"Connection reset by peer at {url}")
        if isinstance(step, int):
            return _http(step, url)
        if isinstance(step, tuple):
            status, body = step
            return _http(status, url, body)
        return _ok(step)


def _client(wire: _Wire, monkeypatch) -> TelegramClient:
    monkeypatch.setattr(telegram, "_new_poll_session", wire.new_session)
    return TelegramClient(TOKEN, 50, clock=wire.clock, sleep=wire.sleep)


@pytest.mark.parametrize("failure", ["read-timeout", "dns", "reset", 502, 500])
def test_a_single_transient_failure_heals_inside_the_poll(monkeypatch, failure) -> None:
    wire = _Wire([failure, [UPDATE]])
    updates = _client(wire, monkeypatch).get_updates(5)

    assert [u.text for u in updates] == ["hello"]
    # Same offset: a retried read can neither skip nor repeat an update.
    assert wire.offsets == [5, 5]
    # The failed connection is not reused; its replacement resolves the name again.
    assert wire.sessions == 2 and wire.closed == 1
    assert wire.sleeps == [telegram._POLL_RETRY_DELAYS_S[0]]


def test_healthy_polls_share_one_connection(monkeypatch) -> None:
    wire = _Wire([[], [], [UPDATE]])
    client = _client(wire, monkeypatch)
    for _ in range(3):
        client.get_updates(0)

    assert wire.sessions == 1 and wire.closed == 0 and wire.sleeps == []
    # Connecting gets its own short deadline; only the read waits out the long poll.
    assert wire.timeouts == [(telegram._POLL_CONNECT_TIMEOUT_S, 60)] * 3


def test_a_lasting_outage_still_fails_loudly_and_within_the_window(monkeypatch) -> None:
    wire = _Wire(["dns"] * 20)
    client = _client(wire, monkeypatch)

    with pytest.raises(requests.ConnectionError) as caught:
        client.get_updates(0)

    assert sum(wire.sleeps) <= telegram._POLL_HEAL_WINDOW_S
    assert len(wire.offsets) <= len(telegram._POLL_RETRY_DELAYS_S) + 1
    assert set(wire.offsets) == {0}
    assert TOKEN not in str(caught.value) and TOKEN not in repr(caught.value)


def test_a_second_read_timeout_is_an_outage_not_a_blip(monkeypatch) -> None:
    # Each read timeout already cost a full long poll. Retrying those for as long as
    # fast failures would hide two minutes of silence behind a healthy-looking log.
    wire = _Wire(["read-timeout"] * 5)
    client = _client(wire, monkeypatch)

    with pytest.raises(requests.ReadTimeout):
        client.get_updates(0)

    assert len(wire.offsets) == 2


@pytest.mark.parametrize("status", [401, 403, 404, 409])
def test_a_rejection_is_never_retried(monkeypatch, status) -> None:
    # 409 is a second poller, 401 a revoked token. Waiting changes neither, and both
    # are exactly what the operator has to see at once.
    wire = _Wire([status, [UPDATE]])
    client = _client(wire, monkeypatch)

    with pytest.raises(requests.HTTPError) as caught:
        client.get_updates(0)

    assert wire.offsets == [0] and wire.sleeps == []
    assert caught.value.response is None
    assert TOKEN not in str(caught.value)


def test_rate_limiting_waits_exactly_as_long_as_telegram_asked(monkeypatch) -> None:
    body = {"ok": False, "description": "Too Many Requests: retry after 5",
            "parameters": {"retry_after": 5}}
    wire = _Wire([(429, body), [UPDATE]])

    assert len(_client(wire, monkeypatch).get_updates(0)) == 1
    assert wire.sleeps == [5.0]


def test_a_long_rate_limit_is_reported_instead_of_slept_through(monkeypatch) -> None:
    body = {"ok": False, "parameters": {"retry_after": 600}}
    wire = _Wire([(429, body), [UPDATE]])

    with pytest.raises(requests.HTTPError):
        _client(wire, monkeypatch).get_updates(0)

    assert wire.sleeps == []


@pytest.mark.parametrize("retry_after", [True, -3, 0, "5", None, 2.5])
def test_a_malformed_retry_after_falls_back_to_the_own_schedule(monkeypatch, retry_after) -> None:
    body = {"ok": False, "parameters": {"retry_after": retry_after}}
    wire = _Wire([(429, body), [UPDATE]])

    _client(wire, monkeypatch).get_updates(0)

    assert wire.sleeps == [telegram._POLL_RETRY_DELAYS_S[0]]


def test_the_poll_connection_keeps_itself_alive() -> None:
    # A long poll is fifty silent seconds. Without probes a dropped NAT entry is only
    # noticed when the read deadline passes; with them the kernel reports it first.
    session = telegram._new_poll_session()
    try:
        options = session.get_adapter("https://api.telegram.org/").poolmanager \
            .connection_pool_kw["socket_options"]
    finally:
        session.close()

    assert (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1) in options
    assert (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1) in options
