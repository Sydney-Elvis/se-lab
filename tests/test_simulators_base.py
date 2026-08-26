"""Unit tests for agent.simulators.base.ExternalSimulator.

No dedicated unit tests existed for m3undle_lab.simulator.SimulatorInstance
before this extraction -- it was only exercised indirectly by Docker/M3Undle
integration suites. These are new coverage for the generic mechanism now
shared here, using a minimal concrete subclass so backend/docker/port-cleanup
behavior can be verified without a real engine checkout or Docker daemon.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent.simulators.base import ExternalSimulator


class _FakeSimulator(ExternalSimulator):
    engine_env_var = "SELFTEST_SIM_ENGINE_DIR"
    backend_env_var = "SELFTEST_SIM_BACKEND"
    image_env_var = "SELFTEST_SIM_IMAGE"
    default_image = "selftest/fake-sim:dev"
    container_name_prefix = "selftest-sim"
    docker_label_prefix = "com.selftest-lab"
    process_marker = "fake_sim.py"

    def local_command(self, engine_dir: Path) -> list[str]:
        return ["fake-sim", "--engine-dir", str(engine_dir), "--port", str(self.port)]

    def docker_run_args(self, image: str) -> list[str]:
        return ["--port", str(self.port)]


def test_invalid_backend_rejected():
    with pytest.raises(ValueError):
        _FakeSimulator(port=19001, backend="carrier-pigeon")


def test_backend_defaults_to_local(monkeypatch):
    monkeypatch.delenv("SELFTEST_SIM_BACKEND", raising=False)
    sim = _FakeSimulator(port=19001)
    assert sim.backend == "local"


def test_backend_honors_env_setting(monkeypatch):
    monkeypatch.setenv("SELFTEST_SIM_BACKEND", "docker")
    sim = _FakeSimulator(port=19001)
    assert sim.backend == "docker"


def test_explicit_backend_wins_over_env(monkeypatch):
    monkeypatch.setenv("SELFTEST_SIM_BACKEND", "docker")
    sim = _FakeSimulator(port=19001, backend="local")
    assert sim.backend == "local"


def test_image_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("SELFTEST_SIM_IMAGE", raising=False)
    sim = _FakeSimulator(port=19001)
    assert sim.image == "selftest/fake-sim:dev"
    assert sim._image_is_default is True


def test_image_env_var_overrides_default(monkeypatch):
    monkeypatch.setenv("SELFTEST_SIM_IMAGE", "pinned/tag:1")
    sim = _FakeSimulator(port=19001)
    assert sim.image == "pinned/tag:1"
    assert sim._image_is_default is False


def test_explicit_image_wins_and_is_not_default(monkeypatch):
    monkeypatch.setenv("SELFTEST_SIM_IMAGE", "pinned/tag:1")
    sim = _FakeSimulator(port=19001, image="explicit/tag:2")
    assert sim.image == "explicit/tag:2"
    assert sim._image_is_default is False


def test_container_name_uses_prefix_and_port():
    sim = _FakeSimulator(port=19042)
    assert sim.container_name == "selftest-sim-19042"


def test_public_host_defaults_from_bind_and_port():
    sim = _FakeSimulator(port=19001, bind="0.0.0.0")
    assert sim.public_host == "http://0.0.0.0:19001"


def test_local_url_falls_back_to_loopback_when_bind_is_wildcard():
    sim = _FakeSimulator(port=19001, bind="0.0.0.0")
    assert sim._local_url == "http://127.0.0.1:19001"


def test_container_path_for_maps_fixtures_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(_FakeSimulator, "FIXTURES_DIR", tmp_path / "fixtures")
    monkeypatch.setattr(_FakeSimulator, "SCENARIOS_DIR", tmp_path / "scenarios")
    fixture_path = tmp_path / "fixtures" / "providers" / "a.json"
    sim = _FakeSimulator(port=19001)
    assert sim.container_path_for(fixture_path) == "/app/fixtures/providers/a.json"


def test_container_path_for_maps_scenarios_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(_FakeSimulator, "FIXTURES_DIR", tmp_path / "fixtures")
    monkeypatch.setattr(_FakeSimulator, "SCENARIOS_DIR", tmp_path / "scenarios")
    scenario_path = tmp_path / "scenarios" / "cooldown-01.yaml"
    sim = _FakeSimulator(port=19001)
    assert sim.container_path_for(scenario_path) == "/app/scenarios/cooldown-01.yaml"


def test_container_path_for_rejects_unrelated_path(tmp_path, monkeypatch):
    monkeypatch.setattr(_FakeSimulator, "FIXTURES_DIR", tmp_path / "fixtures")
    monkeypatch.setattr(_FakeSimulator, "SCENARIOS_DIR", tmp_path / "scenarios")
    sim = _FakeSimulator(port=19001)
    with pytest.raises(RuntimeError):
        sim.container_path_for(tmp_path / "elsewhere" / "a.json")


def test_start_local_invokes_local_command(monkeypatch, tmp_path):
    monkeypatch.setenv("SELFTEST_SIM_ENGINE_DIR", str(tmp_path / "engine"))
    captured: dict[str, object] = {}

    class _FakeProcess:
        def poll(self) -> None:
            return None

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    sim = _FakeSimulator(port=19001)
    sim.start()
    assert captured["cmd"] == ["fake-sim", "--engine-dir", str(tmp_path / "engine"), "--port", "19001"]
    assert sim.is_running() is True


def test_start_local_without_engine_dir_raises(monkeypatch):
    monkeypatch.delenv("SELFTEST_SIM_ENGINE_DIR", raising=False)
    sim = _FakeSimulator(port=19001)
    with pytest.raises(RuntimeError):
        sim.start()


def test_start_docker_builds_run_command(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def _fake_run_docker(*args, timeout=None, check=True):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("agent.simulators.base._run_docker", _fake_run_docker)
    monkeypatch.setattr(_FakeSimulator, "FIXTURES_DIR", tmp_path / "fixtures")
    monkeypatch.setattr(_FakeSimulator, "SCENARIOS_DIR", tmp_path / "scenarios")
    (tmp_path / "fixtures").mkdir()

    sim = _FakeSimulator(port=19001, backend="docker", image="pinned/tag:1")
    sim.start()

    # "rm -f" cleanup, then "run -d ..."
    run_call = next(c for c in calls if c[0] == "run")
    assert "--name" in run_call and "selftest-sim-19001" in run_call
    assert "pinned/tag:1" in run_call
    assert "--port" in run_call and "19001" in run_call
    assert sim._container_started is True
