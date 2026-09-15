"""Unit tests for agent.simulators.smtp (MailpitFixture, MailpitClient).

Docker interaction is mocked the same way tests/test_simulators_base.py
mocks _run_docker -- no real Docker daemon needed. HTTP interaction is
mocked at urllib.request.urlopen, matching test_simulators_matrix.py's own
style. openssl is not invoked in these tests -- enable_tls is exercised
only by the live smoke test, not here (see se-lab's own commit message /
manual verification for that run's evidence).
"""

from __future__ import annotations

import json
import subprocess

import pytest

from agent.simulators.smtp import (
    DEFAULT_SMTP_AUTH_FILE_CONTENT,
    DEFAULT_SMTP_AUTH_PASSWORD,
    DEFAULT_SMTP_AUTH_USERNAME,
    MailpitClient,
    MailpitFixture,
)


# ---------------------------------------------------------------------------
# MailpitFixture
# ---------------------------------------------------------------------------


def test_defaults_to_docker_backend(monkeypatch):
    monkeypatch.delenv("SE_LAB_SMTP_BACKEND", raising=False)
    fixture = MailpitFixture(port=18300, smtp_port=18301)
    assert fixture.backend == "docker"


def test_rejects_local_backend():
    with pytest.raises(ValueError):
        MailpitFixture(port=18300, smtp_port=18301, backend="local")


def test_local_command_not_supported():
    fixture = MailpitFixture(port=18300, smtp_port=18301)
    with pytest.raises(NotImplementedError):
        fixture.local_command(engine_dir=None)  # type: ignore[arg-type]


def test_fixed_auth_credentials_exposed():
    fixture = MailpitFixture(port=18300, smtp_port=18301)
    assert fixture.auth_username == DEFAULT_SMTP_AUTH_USERNAME
    assert fixture.auth_password == DEFAULT_SMTP_AUTH_PASSWORD


def test_additional_ports_publishes_smtp_port():
    fixture = MailpitFixture(port=18300, smtp_port=18301)
    assert fixture.additional_ports() == [18301]


def test_docker_env_binds_both_ports():
    fixture = MailpitFixture(port=18300, smtp_port=18301)
    env = fixture.docker_env()
    assert env["MP_UI_BIND_ADDR"] == "0.0.0.0:18300"
    assert env["MP_SMTP_BIND_ADDR"] == "0.0.0.0:18301"
    assert "MP_SMTP_TLS_CERT" not in env
    assert "MP_SMTP_AUTH_ALLOW_INSECURE" not in env


def test_docker_env_adds_tls_paths_when_enabled():
    fixture = MailpitFixture(port=18300, smtp_port=18301, enable_tls=True)
    env = fixture.docker_env()
    assert env["MP_SMTP_TLS_CERT"] == "/data/server.pem"
    assert env["MP_SMTP_TLS_KEY"] == "/data/server.key"


def test_docker_env_adds_insecure_auth_flag_when_enabled():
    fixture = MailpitFixture(port=18300, smtp_port=18301, allow_insecure_auth=True)
    env = fixture.docker_env()
    assert env["MP_SMTP_AUTH_ALLOW_INSECURE"] == "true"


def test_docker_volumes_writes_auth_file(tmp_path):
    fixture = MailpitFixture(port=18300, smtp_port=18301, tls_dir=tmp_path)
    volumes = fixture.docker_volumes()

    auth_file_host_path = tmp_path / "smtp-auth-file"
    assert str(auth_file_host_path.resolve()) in volumes
    assert volumes[str(auth_file_host_path.resolve())] == "/data/smtp-auth-file"
    assert auth_file_host_path.read_text(encoding="utf-8") == DEFAULT_SMTP_AUTH_FILE_CONTENT


def test_docker_volumes_skips_tls_files_when_disabled(tmp_path):
    fixture = MailpitFixture(port=18300, smtp_port=18301, tls_dir=tmp_path)
    volumes = fixture.docker_volumes()

    assert not (tmp_path / "server.pem").exists()
    assert fixture.ca_cert_path is None
    assert len(volumes) == 1


def test_start_builds_expected_docker_command(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def _fake_run_docker(*args, timeout=None, check=True):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("agent.simulators.base._run_docker", _fake_run_docker)
    monkeypatch.setattr(MailpitFixture, "FIXTURES_DIR", tmp_path / "fixtures")
    monkeypatch.setattr(MailpitFixture, "SCENARIOS_DIR", tmp_path / "scenarios")
    (tmp_path / "fixtures").mkdir()

    fixture = MailpitFixture(
        port=18300, smtp_port=18301, image="pinned/mailpit:1", tls_dir=tmp_path / "tls"
    )
    fixture.start()

    run_call = next(c for c in calls if c[0] == "run")
    assert "pinned/mailpit:1" in run_call
    port_pairs = [run_call[i + 1] for i, arg in enumerate(run_call) if arg == "-p"]
    assert port_pairs == ["18300:18300", "18301:18301"]
    volume_args = [run_call[i + 1] for i, arg in enumerate(run_call) if arg == "-v"]
    assert any(v.endswith(":/data/smtp-auth-file:ro") for v in volume_args)
    assert fixture._container_started is True


# ---------------------------------------------------------------------------
# MailpitClient
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


def test_ready_true_on_200(monkeypatch):
    monkeypatch.setattr(
        "agent.simulators.smtp.urllib.request.urlopen",
        lambda request, timeout=None: _FakeHttpResponse(200, {"messages": [], "total": 0}),
    )
    client = MailpitClient("http://fixture")
    assert client.ready() is True


def test_messages_raises_on_truncation(monkeypatch):
    monkeypatch.setattr(
        "agent.simulators.smtp.urllib.request.urlopen",
        lambda request, timeout=None: _FakeHttpResponse(200, {"messages": [{"ID": "1"}], "total": 5}),
    )
    client = MailpitClient("http://fixture")
    with pytest.raises(AssertionError):
        client.messages()


def test_find_message_matches_recipient_and_subject(monkeypatch):
    payload = {
        "messages": [
            {"To": [{"Address": "other@example.test"}], "Subject": "unrelated"},
            {"To": [{"Address": "someone@example.test"}], "Subject": "Your book is ready", "Username": "labmailer"},
        ]
    }
    monkeypatch.setattr(
        "agent.simulators.smtp.urllib.request.urlopen",
        lambda request, timeout=None: _FakeHttpResponse(200, payload),
    )
    client = MailpitClient("http://fixture")
    found = client.find_message(to="someone@example.test", subject_contains="ready", timeout_seconds=1.0)

    assert found is not None
    assert found["Username"] == "labmailer"


def test_find_message_times_out_when_absent(monkeypatch):
    monkeypatch.setattr(
        "agent.simulators.smtp.urllib.request.urlopen",
        lambda request, timeout=None: _FakeHttpResponse(200, {"messages": []}),
    )
    client = MailpitClient("http://fixture")
    found = client.find_message(to="nobody@example.test", timeout_seconds=0.05)

    assert found is None
