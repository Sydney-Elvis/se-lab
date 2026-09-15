"""Unit tests for agent.simulators.matrix (MatrixHomeserverFixture, MatrixTestClient).

Docker interaction is mocked the same way tests/test_simulators_base.py mocks
_run_docker -- no real Docker daemon needed. HTTP interaction is mocked at
urllib.request.urlopen, matching family_librarian_lab/m3undle_lab's own
urllib-based client testing style rather than introducing a new dependency.
"""

from __future__ import annotations

import io
import json
import subprocess
import urllib.error

import pytest

from agent.simulators.matrix import (
    MatrixApiError,
    MatrixHomeserverFixture,
    MatrixTestClient,
    parse_bootstrap_registration_token,
)


# ---------------------------------------------------------------------------
# MatrixHomeserverFixture
# ---------------------------------------------------------------------------


def test_defaults_to_docker_backend(monkeypatch):
    monkeypatch.delenv("SE_LAB_MATRIX_BACKEND", raising=False)
    fixture = MatrixHomeserverFixture(port=19200)
    assert fixture.backend == "docker"


def test_rejects_local_backend():
    with pytest.raises(ValueError):
        MatrixHomeserverFixture(port=19200, backend="local")


def test_local_command_not_supported():
    fixture = MatrixHomeserverFixture(port=19200)
    with pytest.raises(NotImplementedError):
        fixture.local_command(engine_dir=None)  # type: ignore[arg-type]


def test_docker_env_uses_fixture_port_server_name_and_token():
    fixture = MatrixHomeserverFixture(port=19201, server_name="lab.example", registration_token="tok-fixed")
    env = fixture.docker_env()
    assert env["CONTINUWUITY_SERVER_NAME"] == "lab.example"
    assert env["CONTINUWUITY_PORT"] == "19201"
    assert env["CONTINUWUITY_ADDRESS"] == "0.0.0.0"
    assert env["CONTINUWUITY_ALLOW_REGISTRATION"] == "true"
    assert env["CONTINUWUITY_REGISTRATION_TOKEN"] == "tok-fixed"


def test_start_builds_expected_docker_command(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def _fake_run_docker(*args, timeout=None, check=True):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("agent.simulators.base._run_docker", _fake_run_docker)
    monkeypatch.setattr(MatrixHomeserverFixture, "FIXTURES_DIR", tmp_path / "fixtures")
    monkeypatch.setattr(MatrixHomeserverFixture, "SCENARIOS_DIR", tmp_path / "scenarios")
    (tmp_path / "fixtures").mkdir()

    fixture = MatrixHomeserverFixture(port=19202, image="pinned/continuwuity:1")
    fixture.start()

    run_call = next(c for c in calls if c[0] == "run")
    assert "pinned/continuwuity:1" in run_call
    env_pairs = [run_call[i + 1] for i, arg in enumerate(run_call) if arg == "-e"]
    assert "CONTINUWUITY_PORT=19202" in env_pairs
    assert "CONTINUWUITY_ALLOW_REGISTRATION=true" in env_pairs
    assert fixture._container_started is True


def test_read_bootstrap_registration_token_parses_ansi_colored_log(monkeypatch):
    fixture = MatrixHomeserverFixture(port=19203)
    colored_log = (
        "\x1b[1mregistration token \x1b[1;32mAbCdEf123456\x1b[0m . Pick your own username\n"
    )

    def _fake_run(args, text=True, capture_output=True, check=False):
        assert args == ["docker", "logs", fixture.container_name]
        return subprocess.CompletedProcess(args, 0, stdout=colored_log, stderr="")

    monkeypatch.setattr("agent.simulators.matrix.subprocess.run", _fake_run)

    token = fixture.read_bootstrap_registration_token(timeout=1.0)

    assert token == "AbCdEf123456"


def test_read_bootstrap_registration_token_times_out(monkeypatch):
    fixture = MatrixHomeserverFixture(port=19204)

    def _fake_run(args, text=True, capture_output=True, check=False):
        return subprocess.CompletedProcess(args, 0, stdout="nothing here yet", stderr="")

    monkeypatch.setattr("agent.simulators.matrix.subprocess.run", _fake_run)

    with pytest.raises(RuntimeError):
        fixture.read_bootstrap_registration_token(timeout=0.1)


def test_parse_bootstrap_registration_token_pure_function():
    colored_log = "\x1b[1mregistration token \x1b[1;32mZyXwVu987654\x1b[0m . Pick your own username\n"
    assert parse_bootstrap_registration_token(colored_log) == "ZyXwVu987654"


def test_parse_bootstrap_registration_token_returns_none_when_absent():
    assert parse_bootstrap_registration_token("nothing relevant here") is None


# ---------------------------------------------------------------------------
# MatrixTestClient
# ---------------------------------------------------------------------------


class _FakeHttpResponse:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _http_error(status: int, payload: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://fixture", code=status, msg="error",
        hdrs=None, fp=io.BytesIO(json.dumps(payload).encode("utf-8")),
    )


def test_register_user_completes_uia_dummy_flow(monkeypatch):
    responses = [
        _http_error(401, {"session": "sess-1", "flows": [{"stages": ["m.login.dummy"]}]}),
        _FakeHttpResponse(200, {"user_id": "@bot:fixture.test", "access_token": "tok-123"}),
    ]

    def _fake_urlopen(request, timeout=None):
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("agent.simulators.matrix.urllib.request.urlopen", _fake_urlopen)

    client = MatrixTestClient("http://fixture", "fixture.test")
    user_id, token = client.register_user("bot", "password")

    assert user_id == "@bot:fixture.test"
    assert token == "tok-123"


def test_register_user_completes_uia_registration_token_flow(monkeypatch):
    captured_bodies = []
    responses = [
        _http_error(401, {"session": "sess-2", "flows": [{"stages": ["m.login.registration_token"]}]}),
        _FakeHttpResponse(200, {"user_id": "@human:fixture.test", "access_token": "tok-456"}),
    ]

    def _fake_urlopen(request, timeout=None):
        captured_bodies.append(json.loads(request.data) if request.data else None)
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("agent.simulators.matrix.urllib.request.urlopen", _fake_urlopen)

    client = MatrixTestClient("http://fixture", "fixture.test")
    user_id, token = client.register_user("human", "password", registration_token="fixed-token")

    assert user_id == "@human:fixture.test"
    assert token == "tok-456"
    assert captured_bodies[1]["auth"] == {
        "type": "m.login.registration_token", "token": "fixed-token", "session": "sess-2",
    }


def test_register_user_without_uia_returns_immediately(monkeypatch):
    monkeypatch.setattr(
        "agent.simulators.matrix.urllib.request.urlopen",
        lambda request, timeout=None: _FakeHttpResponse(
            200, {"user_id": "@bot:fixture.test", "access_token": "tok-999"}
        ),
    )

    client = MatrixTestClient("http://fixture", "fixture.test")
    user_id, token = client.register_user("bot", "password")

    assert user_id == "@bot:fixture.test"
    assert token == "tok-999"


def test_login_stores_credentials(monkeypatch):
    monkeypatch.setattr(
        "agent.simulators.matrix.urllib.request.urlopen",
        lambda request, timeout=None: _FakeHttpResponse(
            200, {"user_id": "@human:fixture.test", "access_token": "tok-abc"}
        ),
    )

    client = MatrixTestClient("http://fixture", "fixture.test")
    user_id, token = client.login("human", "password")

    assert client.user_id == "@human:fixture.test" == user_id
    assert client.access_token == "tok-abc" == token


def test_generic_error_raises_matrix_api_error(monkeypatch):
    monkeypatch.setattr(
        "agent.simulators.matrix.urllib.request.urlopen",
        lambda request, timeout=None: (_ for _ in ()).throw(
            _http_error(403, {"errcode": "M_FORBIDDEN", "error": "nope"})
        ),
    )

    client = MatrixTestClient("http://fixture", "fixture.test")
    with pytest.raises(MatrixApiError) as excinfo:
        client.whoami()
    assert excinfo.value.status == 403
    assert excinfo.value.errcode == "M_FORBIDDEN"


def test_join_room_resets_sync_cursor(monkeypatch):
    monkeypatch.setattr(
        "agent.simulators.matrix.urllib.request.urlopen",
        lambda request, timeout=None: _FakeHttpResponse(200, {}),
    )

    client = MatrixTestClient("http://fixture", "fixture.test")
    client.access_token = "tok"
    client._next_batch = "stale-token-from-before-the-join"

    client.join_room("!roomid:fixture.test")

    # A since-bounded sync from before the join isn't guaranteed to include
    # messages sent while this user was only invited (invite state is
    # stripped, not the timeline) -- the next sync() must be a fresh
    # initial sync, not continue from the stale cursor.
    assert client._next_batch is None


def test_send_message_quotes_room_id(monkeypatch):
    captured = {}

    def _fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        return _FakeHttpResponse(200, {"event_id": "$evt1"})

    monkeypatch.setattr("agent.simulators.matrix.urllib.request.urlopen", _fake_urlopen)

    client = MatrixTestClient("http://fixture", "fixture.test")
    client.access_token = "tok"
    event_id = client.send_message("!roomid:fixture.test", "hello")

    assert event_id == "$evt1"
    # Real Matrix room ids always contain "!" and ":", which must be
    # percent-encoded as a URL path segment (unlike "/", which quote()'s
    # default safe='/'' leaves alone -- room ids never actually contain one).
    assert "%21roomid%3Afixture.test" in captured["url"]


def test_wait_for_message_matches_predicate(monkeypatch):
    sync_bodies = [
        {"next_batch": "b1", "rooms": {"join": {}}},
        {
            "next_batch": "b2",
            "rooms": {
                "join": {
                    "!room:fixture.test": {
                        "timeline": {
                            "events": [
                                {"type": "m.room.message", "content": {"msgtype": "m.text", "body": "hi"}},
                                {"type": "m.room.message", "content": {"msgtype": "m.text", "body": "yes"}},
                            ]
                        }
                    }
                }
            },
        },
    ]

    client = MatrixTestClient("http://fixture", "fixture.test")
    monkeypatch.setattr(client, "sync", lambda since=None, timeout_ms=0: sync_bodies.pop(0))

    result = client.wait_for_message(predicate=lambda room_id, text: text == "yes", timeout=5.0)

    assert result == ("!room:fixture.test", "yes")


def test_wait_for_message_times_out(monkeypatch):
    client = MatrixTestClient("http://fixture", "fixture.test")
    monkeypatch.setattr(client, "sync", lambda since=None, timeout_ms=0: {"next_batch": "b1", "rooms": {}})

    result = client.wait_for_message(timeout=0.05)

    assert result is None
