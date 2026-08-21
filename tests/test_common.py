"""agent/common.py: path resolution, layout, env file handling -- no docker involved."""

from __future__ import annotations

import pytest

from agent import common as lab_common, registry, runtime


@pytest.fixture(autouse=True)
def _clean_runtime_env_file():
    """Several tests below read/write the shared scratch runtime .env file --
    without resetting it, an earlier test's writes (e.g. deployment metadata)
    leak into a later test that expects a clean slate."""
    lab_common.ensure_layout()
    env_file = lab_common.runtime_env_file()
    original = env_file.read_text(encoding="utf-8") if env_file.exists() else None
    yield
    if original is None:
        env_file.unlink(missing_ok=True)
    else:
        env_file.write_text(original, encoding="utf-8")


def test_product_config_is_set_by_conftest():
    assert runtime.require_product_config() == ("selftest", "SELFTEST")


def test_current_role_defaults_and_validates(monkeypatch):
    monkeypatch.delenv("SELFTEST_LAB_ROLE", raising=False)
    assert lab_common.current_role() in ("srv1", "srv2")
    monkeypatch.setenv("SELFTEST_LAB_ROLE", "srv2")
    assert lab_common.current_role() == "srv2"
    monkeypatch.setenv("SELFTEST_LAB_ROLE", "not-a-role")
    with pytest.raises(SystemExit, match="Unsupported"):
        lab_common.current_role()


def test_repo_dir_and_image_naming_use_product_name():
    assert lab_common.repo_dir().name == "selftest"
    assert lab_common.local_branch_image("main") == "selftest:branch-main"
    assert lab_common.local_tag_image("v1.0.0") == "selftest:tag-v1.0.0"
    assert lab_common.sanitize_branch_name("feature/x y") == "feature-x-y"


def test_ensure_layout_creates_directories_and_runs_the_hook():
    calls = []
    registry.set_layout_hook(lambda: calls.append("hook ran"))
    lab_common.ensure_layout()
    assert lab_common.runtime_dir().exists()
    assert lab_common.runtime_config_dir().exists()
    assert lab_common.runtime_data_dir().exists()
    assert lab_common.artifacts_runs_dir().exists()
    assert calls == ["hook ran"]


def test_ensure_layout_writes_placeholder_image_once():
    lab_common.ensure_layout()
    env_file = lab_common.runtime_env_file()
    first = env_file.read_text(encoding="utf-8")
    assert "SELFTEST_IMAGE=selftest:unset" in first
    lab_common.ensure_layout()
    assert env_file.read_text(encoding="utf-8") == first


def test_runtime_env_values_roundtrip():
    lab_common.set_runtime_env_values({"FOO": "bar", "BAZ": "qux"})
    assert lab_common.get_runtime_env_value("FOO") == "bar"
    assert lab_common.get_runtime_env_value("BAZ") == "qux"
    lab_common.set_runtime_env_var("FOO", "updated")
    assert lab_common.get_runtime_env_value("FOO") == "updated"
    lab_common.unset_runtime_env_var("FOO")
    assert lab_common.get_runtime_env_value("FOO") is None
    assert lab_common.get_runtime_env_value("BAZ") == "qux"


def test_deployment_metadata_roundtrip():
    lab_common.set_deployment_metadata("branch", "main", image="selftest:branch-main", source_commit="abc123")
    metadata = lab_common.get_deployment_metadata()
    assert metadata["source_type"] == "branch"
    assert metadata["source_ref"] == "main"
    assert metadata["source_commit"] == "abc123"
    assert metadata["image"] == "selftest:branch-main"


def test_last_test_metadata_roundtrip():
    lab_common.set_last_test_metadata("branch", "main", status="pass", run_id="run-1")
    metadata = lab_common.get_last_test_metadata()
    assert metadata["status"] == "pass"
    assert metadata["run_id"] == "run-1"
    assert metadata["target_ref"] == "main"


def test_placeholder_image_reads_back_as_no_image():
    lab_common.ensure_layout()
    assert lab_common.get_current_image() is None
    lab_common.set_current_image("selftest:branch-main")
    assert lab_common.get_current_image() == "selftest:branch-main"


def test_resolve_setting_precedence(monkeypatch, scratch_root):
    monkeypatch.delenv("PROBE_SETTING", raising=False)
    assert lab_common.resolve_setting("PROBE_SETTING") is None
    assert lab_common.resolve_setting("PROBE_SETTING", default="fallback") == "fallback"
    with pytest.raises(SystemExit, match="Missing required setting"):
        lab_common.resolve_setting("PROBE_SETTING", required=True)
    monkeypatch.setenv("PROBE_SETTING", "from-env")
    assert lab_common.resolve_setting("PROBE_SETTING") == "from-env"
    assert lab_common.resolve_setting("PROBE_SETTING", explicit="from-arg") == "from-arg"


def test_client_version_history_roundtrip():
    assert lab_common.read_client_version_history() == {}
    lab_common.push_client_version_record("fakeclient", {"version": "1.0.0"})
    lab_common.push_client_version_record("fakeclient", {"version": "1.0.1"})
    history = lab_common.read_client_version_history()
    assert [r["version"] for r in history["fakeclient"]] == ["1.0.1", "1.0.0"]


def test_active_clients_reads_compose_profiles():
    from agent.clients.plugin import ClientPlugin

    class FakeClient(ClientPlugin):
        name = "fc"
        compose_service = "fc"
        image_env_var = "FC_IMAGE"
        default_image = "fc:latest"

        def detect_version(self):
            return "1.0"

    registry.register_client(FakeClient)
    lab_common.set_runtime_env_values({"COMPOSE_PROFILES": "fc,notregistered"})
    assert lab_common.active_clients() == ["fc"]
