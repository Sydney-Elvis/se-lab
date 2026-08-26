"""agent/status.py: BaseStatus -- the shared `lab status` report every product
lab subclasses, without needing real docker (compose_ps/published_ports are
monkeypatched)."""

from __future__ import annotations

import pytest

from agent import common as lab_common, registry
from agent.clients.plugin import ClientPlugin
from agent.status import BaseStatus


@pytest.fixture(autouse=True)
def _clean_runtime_env_file():
    """deployment/last-test metadata below write to the shared scratch runtime
    .env file -- without resetting it, one test's writes leak into the next
    (see tests/test_common.py's identical fixture)."""
    lab_common.ensure_layout()
    env_file = lab_common.runtime_env_file()
    original = env_file.read_text(encoding="utf-8") if env_file.exists() else None
    yield
    if original is None:
        env_file.unlink(missing_ok=True)
    else:
        env_file.write_text(original, encoding="utf-8")


class _FakeClient(ClientPlugin):
    name = "fakeclient"
    compose_service = "fakeclient-svc"
    image_env_var = "FAKECLIENT_IMAGE"
    default_image = "fakeclient:latest"

    def detect_version(self):
        return "1.2.3"


@pytest.fixture
def fake_client():
    registry.register_client(_FakeClient)
    return _FakeClient


def test_deployment_lines_reports_runtime_and_unset_image():
    lines = BaseStatus().deployment_lines()
    assert lines[0] == f"Runtime: {lab_common.runtime_summary()}"
    assert "Current configured image: not set" in lines


def test_deployment_lines_includes_deployment_metadata_when_present():
    lab_common.set_deployment_metadata("branch", "main", image="selftest:branch-main", source_commit="deadbeef")
    lines = BaseStatus().deployment_lines()
    assert "Current configured image: selftest:branch-main" in lines
    assert "Deployment source: branch main" in lines
    assert "Deployment commit: deadbeef" in lines


def test_deployment_lines_includes_last_test_result():
    lab_common.set_last_test_metadata("branch", "main", status="pass")
    lines = BaseStatus().deployment_lines()
    assert any(line.startswith("Last test result: pass") for line in lines)


def test_print_compose_state_skips_without_a_compose_stack(monkeypatch, capsys):
    monkeypatch.setattr(lab_common, "has_compose_stack", lambda: False)
    BaseStatus().print_compose_state()
    assert "No compose stack defined yet." in capsys.readouterr().out


def test_print_compose_state_calls_compose_ps_and_published_ports(monkeypatch):
    monkeypatch.setattr(lab_common, "has_compose_stack", lambda: True)
    calls = []
    monkeypatch.setattr(lab_common, "compose_ps", lambda: calls.append("ps"))
    monkeypatch.setattr(lab_common, "print_published_ports", lambda: calls.append("ports"))
    BaseStatus().print_compose_state()
    assert calls == ["ps", "ports"]


def test_client_lines_empty_when_no_clients_active(monkeypatch):
    monkeypatch.setattr(lab_common, "active_clients", lambda: [])
    assert BaseStatus().client_lines() == []


def test_client_lines_reports_version_ready_and_ports(fake_client, monkeypatch):
    monkeypatch.setattr(lab_common, "active_clients", lambda: ["fakeclient"])
    monkeypatch.setattr(
        lab_common, "published_ports", lambda: [("fakeclient-svc", "8096->8096/tcp"), ("postgres", "5432->5432/tcp")]
    )
    lines = BaseStatus().client_lines()
    assert lines[0] == "Clients:"
    assert lines[1] == "  fakeclient: ready, version=1.2.3, ports=8096->8096/tcp"


def test_client_lines_reports_no_published_ports_when_none_match(fake_client, monkeypatch):
    monkeypatch.setattr(lab_common, "active_clients", lambda: ["fakeclient"])
    monkeypatch.setattr(lab_common, "published_ports", lambda: [])
    lines = BaseStatus().client_lines()
    assert lines[1] == "  fakeclient: ready, version=1.2.3, ports=no published ports"


def test_run_orchestrates_sections_in_order_and_returns_extras_exit_code(monkeypatch, capsys):
    order = []
    monkeypatch.setattr(BaseStatus, "deployment_lines", lambda self: order.append("deployment") or ["dep-line"])
    monkeypatch.setattr(BaseStatus, "print_compose_state", lambda self: order.append("compose"))
    monkeypatch.setattr(BaseStatus, "client_lines", lambda self: order.append("clients") or ["client-line"])

    class _Extended(BaseStatus):
        def extra(self) -> int:
            order.append("extra")
            print("product-specific section", flush=True)
            return 7

    exit_code = _Extended().run()

    assert order == ["deployment", "compose", "clients", "extra"]
    assert exit_code == 7
    out = capsys.readouterr().out
    assert "dep-line" in out
    assert "client-line" in out
    assert "product-specific section" in out
