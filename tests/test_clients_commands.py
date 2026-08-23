"""agent/commands/clients.py: `up`/`down`/`reset` -- argument validation and call
wiring, without needing real docker (lab_common.compose_up is monkeypatched
throughout; `status`/`update`/`rollback`/`pin` already existed before this file and
are exercised elsewhere)."""

from __future__ import annotations

from pathlib import Path

import pytest

import agent.cli as cli
import agent.commands.clients as clients_cmd
from agent import common as lab_common, registry
from agent.clients.plugin import ClientPlugin

_FAKE_COMPOSE_FILES = [Path("/tmp/fake-compose.yaml")]


class _FakeClient(ClientPlugin):
    name = "fakeclient"
    compose_service = "fakeclient"
    image_env_var = "FAKECLIENT_IMAGE"
    default_image = "fakeclient:latest"

    def detect_version(self):
        return "1.0"

    def ready(self):
        return True


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # _wait_ready()/_detect_version_with_retry() only sleep between retries;
    # nothing here should ever need to actually retry.
    monkeypatch.setattr(clients_cmd.time, "sleep", lambda *_a, **_kw: None)


@pytest.fixture
def fake_client():
    registry.register_client(_FakeClient)
    return _FakeClient


def test_clients_up_fails_loud_with_no_clients_registered():
    with pytest.raises(SystemExit, match="No ClientPlugin registered"):
        cli.main(["clients", "up"])


def test_clients_up_fails_loud_with_no_compose_files_registered(fake_client):
    with pytest.raises(SystemExit, match="set_client_compose_files"):
        cli.main(["clients", "up"])


def test_clients_up_defaults_to_every_registered_client(fake_client, monkeypatch, capsys):
    registry.set_client_compose_files(_FAKE_COMPOSE_FILES)
    calls = {}
    monkeypatch.setattr(lab_common, "set_runtime_env_values", lambda values: calls.setdefault("profiles", values))
    monkeypatch.setattr(lab_common, "compose_up", lambda *, extra_compose_files=(): calls.setdefault("compose_up", list(extra_compose_files)))

    code = cli.main(["clients", "up"])

    assert code == 0
    assert calls["profiles"] == {"COMPOSE_PROFILES": "fakeclient"}
    assert calls["compose_up"] == _FAKE_COMPOSE_FILES
    assert "fakeclient: ready" in capsys.readouterr().out


def test_clients_up_respects_explicit_profile(fake_client, monkeypatch):
    registry.set_client_compose_files(_FAKE_COMPOSE_FILES)
    profiles = {}
    monkeypatch.setattr(lab_common, "set_runtime_env_values", lambda values: profiles.update(values))
    monkeypatch.setattr(lab_common, "compose_up", lambda **_kw: None)

    cli.main(["clients", "up", "--profile", "fakeclient"])

    assert profiles["COMPOSE_PROFILES"] == "fakeclient"


def test_clients_up_rejects_unknown_profile_name(fake_client):
    registry.set_client_compose_files(_FAKE_COMPOSE_FILES)
    with pytest.raises(SystemExit, match="No client named 'nope'"):
        cli.main(["clients", "up", "--profile", "nope"])


def test_clients_down_with_no_active_clients_is_a_noop(monkeypatch, capsys):
    registry.set_client_compose_files(_FAKE_COMPOSE_FILES)
    monkeypatch.setattr(lab_common, "active_clients", lambda: [])
    called = []
    monkeypatch.setattr(lab_common, "compose_up", lambda **_kw: called.append(True))

    code = cli.main(["clients", "down"])

    assert code == 0
    assert not called
    assert "nothing to stop" in capsys.readouterr().out


def test_clients_down_clears_profiles_and_recreates(fake_client, monkeypatch):
    registry.set_client_compose_files(_FAKE_COMPOSE_FILES)
    monkeypatch.setattr(lab_common, "active_clients", lambda: ["fakeclient"])
    profiles = {}
    monkeypatch.setattr(lab_common, "set_runtime_env_values", lambda values: profiles.update(values))
    monkeypatch.setattr(lab_common, "compose_up", lambda **_kw: None)

    cli.main(["clients", "down"])

    assert profiles["COMPOSE_PROFILES"] == ""


def test_clients_reset_requires_a_target(monkeypatch):
    monkeypatch.setattr(lab_common, "active_clients", lambda: [])
    with pytest.raises(SystemExit, match="No client apps active"):
        cli.main(["clients", "reset", "--yes"])


def test_clients_reset_wipes_each_target_before_recreating(fake_client, monkeypatch):
    registry.set_client_compose_files(_FAKE_COMPOSE_FILES)
    wiped = []
    monkeypatch.setattr(_FakeClient, "reset_scenario_data", lambda self: wiped.append(self.name))
    monkeypatch.setattr(lab_common, "set_runtime_env_values", lambda values: None)
    monkeypatch.setattr(lab_common, "compose_up", lambda **_kw: None)

    code = cli.main(["clients", "reset", "--profile", "fakeclient", "--yes"])

    assert code == 0
    assert wiped == ["fakeclient"]
