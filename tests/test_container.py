"""agent/container.py: argument validation runs everywhere; docker-touching
behavior only where docker is actually available, so CI without docker still
exercises the logic that doesn't need it."""

from __future__ import annotations

import pytest

from agent import common as lab_common, container, registry
from agent.database.plugin import DatabasePlugin


def test_resolve_image_requires_exactly_one_of_branch_tag_image():
    with pytest.raises(ValueError, match="Exactly one"):
        container.resolve_image()
    with pytest.raises(ValueError, match="Exactly one"):
        container.resolve_image(branch="main", tag="v1")


def test_resolve_image_explicit_passthrough_needs_no_docker():
    assert container.resolve_image(image="explicit:tag") == "explicit:tag"


def test_container_status_missing_container_needs_no_docker(has_docker):
    if not has_docker:
        pytest.skip("docker not available")
    assert container.container_status("no-such-container-xyz-selftest") == "missing"


def test_wait_http_listener_unreachable_returns_false_quickly(has_docker):
    if not has_docker:
        pytest.skip("docker not available")
    ok = container.wait_http_listener("http://127.0.0.1:1/", "no-such-container", timeout=2.0)
    assert ok is False


def test_reset_database_calls_plugin_reset_between_compose_down_and_up(has_docker, monkeypatch):
    if not has_docker:
        pytest.skip("docker not available")

    calls = []

    class FakeDB(DatabasePlugin):
        def reset(self):
            calls.append("reset")

    registry.set_database_plugin(FakeDB())
    monkeypatch.setattr(lab_common, "compose_down", lambda: calls.append("down"))
    monkeypatch.setattr(lab_common, "compose_up_only", lambda service: calls.append(f"up:{service}"))

    container.reset_database("noop")
    assert calls == ["down", "reset", "up:noop"]


def test_restart_stack_with_env_reports_compose_failure(monkeypatch):
    def boom():
        raise RuntimeError("nope")

    monkeypatch.setattr(lab_common, "compose_up", boom)
    ok, msg = container.restart_stack_with_env({"X": "1"}, base_url="http://x", container_name="none")
    assert ok is False
    assert "compose up failed" in msg


def test_restart_stack_with_env_reports_wait_up_timeout(monkeypatch):
    monkeypatch.setattr(lab_common, "compose_up", lambda **kwargs: None)
    monkeypatch.setattr(container, "wait_up", lambda *a, **k: False)
    ok, msg = container.restart_stack_with_env({"X": "1"}, base_url="http://x", container_name="none")
    assert ok is False
    assert "did not become healthy" in msg


def test_restart_stack_with_env_sets_env_and_reports_success(monkeypatch):
    monkeypatch.setattr(lab_common, "compose_up", lambda **kwargs: None)

    wait_up_calls = []

    def fake_wait_up(base_url, container_name, *, health_paths, timeout=120.0):
        wait_up_calls.append((base_url, container_name, tuple(health_paths)))
        return True

    monkeypatch.setattr(container, "wait_up", fake_wait_up)
    ok, msg = container.restart_stack_with_env(
        {"PROBE_VAR": "hello"}, base_url="http://x", container_name="c", health_paths=("/nope",)
    )
    assert lab_common.get_runtime_env_value("PROBE_VAR") == "hello"
    assert wait_up_calls == [("http://x", "c", ("/nope",))]
    assert ok is True
    assert msg == "restarted and healthy"
