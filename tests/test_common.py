"""agent/common.py: path resolution, layout, env file handling -- no docker involved."""

from __future__ import annotations

import json
import os
import subprocess

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


def test_runtime_dir_defaults_to_repo_root_parent_product_name(monkeypatch):
    monkeypatch.delenv("SELFTEST_RUNTIME_DIR", raising=False)
    assert lab_common.runtime_dir() == runtime.REPO_ROOT.parent / "selftest"


def test_runtime_dir_honors_explicit_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SELFTEST_RUNTIME_DIR", str(tmp_path))
    assert lab_common.runtime_dir() == tmp_path


def test_repo_dir_and_image_naming_use_product_name():
    assert lab_common.repo_dir().name == "selftest"
    assert lab_common.local_branch_image("main") == "selftest:branch-main"
    assert lab_common.local_tag_image("v1.0.0") == "selftest:tag-v1.0.0"
    assert lab_common.sanitize_branch_name("feature/x y") == "feature-x-y"


def test_version_summary_reports_product_lab_and_se_lab_separately():
    # conftest's scratch REPO_ROOT is a plain tmp dir, not a git checkout;
    # se-lab's own SE_LAB_DIR is this real checkout, so the two lines must
    # differ -- proves version_summary() doesn't just report the same repo twice.
    summary = lab_common.version_summary()
    lines = summary.splitlines()
    assert lines[0] == f"selftest: not a git checkout ({runtime.REPO_ROOT})"
    assert lines[1].startswith("se-lab: ")
    assert " @ " in lines[1]


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


def test_external_url_uses_generic_hosted_link_setting(monkeypatch):
    monkeypatch.setenv("LAB_EXTERNAL_HOST", "toontown-int-srv2")
    assert lab_common.external_url(18378) == "http://toontown-int-srv2:18378"
    assert lab_common.external_url(443, scheme="https", path="login") == "https://toontown-int-srv2:443/login"


def test_print_connection_info_includes_credentials_and_note(monkeypatch, capsys):
    monkeypatch.setenv("LAB_EXTERNAL_HOST", "toontown-int-srv2")
    lab_common.print_connection_info(
        [
            lab_common.ConnectionInfo(
                "Example app", 18080, credentials="user: admin / password: test", note="already configured"
            )
        ]
    )
    assert capsys.readouterr().out == (
        "  Example app: http://toontown-int-srv2:18080  "
        "(user: admin / password: test / already configured)\n"
    )


def test_settings_passphrase_reads_env_prefixed_key(monkeypatch):
    monkeypatch.delenv("SELFTEST_SETTINGS_PASSPHRASE", raising=False)
    with pytest.raises(SystemExit, match="Missing required setting SELFTEST_SETTINGS_PASSPHRASE"):
        lab_common.settings_passphrase()
    assert lab_common.settings_passphrase(required=False) is None
    monkeypatch.setenv("SELFTEST_SETTINGS_PASSPHRASE", "lab-only-value")
    assert lab_common.settings_passphrase() == "lab-only-value"


def _stub_deploy_internals(monkeypatch):
    """Mock every docker/git-touching step deploy_*() calls so these tests check
    only the extra_compose_files plumbing, not real build/checkout behavior --
    that needs real docker, exercised elsewhere (see docs on why mocked compose
    tests aren't a substitute for that)."""
    monkeypatch.setattr(lab_common, "ensure_repo_checkout", lambda repo_url: None)
    monkeypatch.setattr(lab_common, "print_repo_summary", lambda repo_url: None)
    monkeypatch.setattr(lab_common, "git_prepare_branch", lambda branch: None)
    monkeypatch.setattr(lab_common, "git_prepare_tag", lambda tag: None)
    monkeypatch.setattr(lab_common, "git_refresh_current_branch", lambda: "main")
    monkeypatch.setattr(lab_common, "repo_head_commit", lambda: "deadbeef")
    monkeypatch.setattr(lab_common, "docker_build", lambda *a, **kw: None)
    monkeypatch.setattr(lab_common, "docker_pull", lambda *a, **kw: None)
    monkeypatch.setattr(lab_common, "sync_runtime_compose", lambda: None)
    monkeypatch.setattr(lab_common, "set_deployment_metadata", lambda *a, **kw: None)
    calls: list[tuple] = []
    monkeypatch.setattr(lab_common, "compose_up", lambda **kw: calls.append(("compose_up", kw)))
    monkeypatch.setattr(lab_common, "compose_up_service", lambda service, **kw: calls.append(("compose_up_service", service, kw)))
    monkeypatch.setattr(lab_common, "print_published_ports", lambda: None)
    monkeypatch.setenv("SELFTEST_REPO_URL", "https://example.invalid/repo.git")
    monkeypatch.setenv("SELFTEST_GHCR_IMAGE", "ghcr.example.invalid/selftest")
    return calls


def test_deploy_branch_threads_extra_compose_files_to_compose_up(monkeypatch):
    from pathlib import Path

    calls = _stub_deploy_internals(monkeypatch)
    extra = [Path("/tmp/override.yaml")]
    lab_common.deploy_branch("main", extra_compose_files=extra)
    assert calls == [("compose_up", {"extra_compose_files": extra})]


def test_deploy_source_tag_threads_extra_compose_files_to_compose_up(monkeypatch):
    from pathlib import Path

    calls = _stub_deploy_internals(monkeypatch)
    extra = [Path("/tmp/override.yaml")]
    lab_common.deploy_source_tag("v1.0.0", extra_compose_files=extra)
    assert calls == [("compose_up", {"extra_compose_files": extra})]


def test_deploy_tag_threads_extra_compose_files_to_compose_up(monkeypatch):
    from pathlib import Path

    calls = _stub_deploy_internals(monkeypatch)
    extra = [Path("/tmp/override.yaml")]
    lab_common.deploy_tag("v1.0.0", extra_compose_files=extra)
    assert calls == [("compose_up", {"extra_compose_files": extra})]


def test_deploy_functions_default_extra_compose_files_to_empty(monkeypatch):
    calls = _stub_deploy_internals(monkeypatch)
    lab_common.deploy_branch("main")
    assert calls == [("compose_up", {"extra_compose_files": ()})]


def test_deploy_current_branch_defaults_to_full_stack_compose_up(monkeypatch):
    calls = _stub_deploy_internals(monkeypatch)
    branch, image = lab_common.deploy_current_branch()
    assert branch == "main"
    assert calls == [("compose_up", {"extra_compose_files": ()})]


def test_deploy_current_branch_with_service_restarts_only_that_service(monkeypatch):
    from pathlib import Path

    calls = _stub_deploy_internals(monkeypatch)
    extra = [Path("/tmp/override.yaml")]
    lab_common.deploy_current_branch(service="selftest", extra_compose_files=extra)
    assert calls == [("compose_up_service", "selftest", {"extra_compose_files": extra})]


def test_git_repo_primitives_against_a_real_repo(tmp_path):
    """The mocked deploy_*() tests above stub out every git-touching helper,
    which is exactly why a self-shadowing `repo_dir = repo_dir()` bug in
    print_repo_summary()/repo_origin_url()/git_prepare_branch()/
    git_prepare_tag()/git_refresh_current_branch() went unnoticed until it
    crashed for real on srv1 (see docs on why mocked compose/git tests aren't
    a substitute for exercising the real thing). This drives those functions
    against an actual local git repo instead."""
    import shutil
    import subprocess

    origin = tmp_path / "origin.git"
    work = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "Test"], check=True)
    (work / "README.md").write_text("main\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-q", "-m", "main commit"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "HEAD:main"], check=True)
    subprocess.run(["git", "-C", str(work), "tag", "v1.0.0"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "v1.0.0"], check=True)
    subprocess.run(["git", "-C", str(work), "checkout", "-q", "-b", "feature"], check=True)
    (work / "README.md").write_text("feature\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "commit", "-q", "-am", "feature commit"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "HEAD:feature"], check=True)

    target = lab_common.repo_dir()
    shutil.rmtree(target, ignore_errors=True)
    try:
        origin_url = str(origin)
        lab_common.ensure_repo_checkout(origin_url)
        lab_common.print_repo_summary(origin_url)  # must not raise (regression: self-shadowed repo_dir)
        assert lab_common.repo_origin_url() == origin_url

        lab_common.git_prepare_branch("main")
        assert lab_common.repo_current_branch() == "main"

        lab_common.git_prepare_tag("v1.0.0")
        assert lab_common.repo_current_branch() is None  # detached HEAD, not on a branch

        lab_common.git_prepare_branch("feature")
        assert lab_common.repo_current_branch() == "feature"
        assert lab_common.git_refresh_current_branch() == "feature"

        kind, ref = lab_common.resolve_build_target("v1.0.0", origin_url)
        assert (kind, ref) == ("tag", "v1.0.0")
        kind, ref = lab_common.resolve_build_target("main", origin_url)
        assert (kind, ref) == ("branch", "main")
    finally:
        shutil.rmtree(target, ignore_errors=True)


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


def test_port_occupied_detects_a_real_bound_listener():
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        # 0.0.0.0, matching what a host-network container binds and what
        # _port_occupied() itself probes -- a listener on just 127.0.0.1
        # doesn't reliably conflict with a 0.0.0.0 probe on every OS.
        listener.bind(("0.0.0.0", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert lab_common._port_occupied(port) is True
    # Freed once the listening socket above is closed (SO_REUSEADDR means an
    # immediate rebind attempt shouldn't be confused by TIME_WAIT either).
    assert lab_common._port_occupied(port) is False


def test_ensure_required_host_ports_available_noop_when_none_registered(monkeypatch):
    called = []
    monkeypatch.setattr(lab_common, "_port_occupied", lambda port: called.append(port) or True)
    lab_common.ensure_required_host_ports_available()
    assert called == []  # never even checked -- nothing registered to check


def test_ensure_required_host_ports_available_returns_when_ports_free(monkeypatch):
    registry.set_required_host_ports((5004, 8080))
    monkeypatch.setattr(lab_common, "_port_occupied", lambda port: False)

    def _fail(*args, **kwargs):
        raise AssertionError("should not need to look for a conflicting container")

    monkeypatch.setattr(lab_common, "_conflicting_host_network_containers", _fail)
    lab_common.ensure_required_host_ports_available(timeout_seconds=0)


def test_ensure_required_host_ports_available_returns_when_only_same_project_container_holds_it(monkeypatch):
    registry.set_required_host_ports((8080,))
    monkeypatch.setattr(lab_common, "_port_occupied", lambda port: True)
    monkeypatch.setattr(lab_common, "_conflicting_host_network_containers", lambda exclude_project: [])
    # No exception, no prompt -- compose_up()'s own down-then-up handles this case.
    lab_common.ensure_required_host_ports_available(timeout_seconds=0)


def test_ensure_required_host_ports_available_raises_clearly_when_non_interactive(monkeypatch):
    registry.set_required_host_ports((8080,))
    monkeypatch.setattr(lab_common, "_port_occupied", lambda port: True)
    monkeypatch.setattr(lab_common, "_conflicting_host_network_containers", lambda exclude_project: ["legacy-m3undle"])
    monkeypatch.setattr(lab_common.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit, match="legacy-m3undle"):
        lab_common.ensure_required_host_ports_available(timeout_seconds=0)


def test_ensure_required_host_ports_available_stops_containers_when_confirmed(monkeypatch):
    registry.set_required_host_ports((8080,))
    monkeypatch.setattr(lab_common, "_port_occupied", lambda port: True)
    monkeypatch.setattr(lab_common, "_conflicting_host_network_containers", lambda exclude_project: ["legacy-m3undle"])
    monkeypatch.setattr(lab_common.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    stopped = []
    monkeypatch.setattr(lab_common, "run", lambda cmd, **kwargs: stopped.append(cmd))
    lab_common.ensure_required_host_ports_available(timeout_seconds=0)
    assert stopped == [["docker", "stop", "legacy-m3undle"]]


def test_ensure_required_host_ports_available_aborts_when_declined(monkeypatch):
    registry.set_required_host_ports((8080,))
    monkeypatch.setattr(lab_common, "_port_occupied", lambda port: True)
    monkeypatch.setattr(lab_common, "_conflicting_host_network_containers", lambda exclude_project: ["legacy-m3undle"])
    monkeypatch.setattr(lab_common.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    with pytest.raises(SystemExit, match="Aborted"):
        lab_common.ensure_required_host_ports_available(timeout_seconds=0)


def test_run_lock_allows_reacquisition_once_released(monkeypatch, tmp_path):
    monkeypatch.setenv("SELFTEST_RUNTIME_DIR", str(tmp_path))
    with lab_common.run_lock():
        assert (tmp_path / "run.lock").exists()
    assert not (tmp_path / "run.lock").exists()
    with lab_common.run_lock():
        assert (tmp_path / "run.lock").exists()


def test_run_lock_releases_even_when_the_guarded_body_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("SELFTEST_RUNTIME_DIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="boom"):
        with lab_common.run_lock():
            raise RuntimeError("boom")
    assert not (tmp_path / "run.lock").exists()


def test_run_lock_blocks_when_the_holder_pid_is_still_alive(monkeypatch, tmp_path):
    monkeypatch.setenv("SELFTEST_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "run.lock").write_text("424242", encoding="utf-8")
    monkeypatch.setattr(lab_common, "_pid_alive", lambda pid: True)
    with pytest.raises(SystemExit, match="already in progress"):
        with lab_common.run_lock(label="lab run"):
            raise AssertionError("must not enter the guarded body")
    # Held, not reclaimed -- the file (and its PID) must be left exactly as
    # another live invocation of run_lock() itself left it.
    assert (tmp_path / "run.lock").read_text(encoding="utf-8") == "424242"


def test_run_lock_reclaims_a_stale_lock_from_a_dead_pid(monkeypatch, tmp_path):
    monkeypatch.setenv("SELFTEST_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "run.lock").write_text("424242", encoding="utf-8")
    monkeypatch.setattr(lab_common, "_pid_alive", lambda pid: False)
    entered = False
    with lab_common.run_lock():
        entered = True
        assert (tmp_path / "run.lock").read_text(encoding="utf-8") == str(os.getpid())
    assert entered
    assert not (tmp_path / "run.lock").exists()


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str):
        self.returncode = returncode
        self.stdout = stdout


def test_parse_compose_ps_json_handles_array_shape():
    # A lab that runs `docker compose -p <its own ad-hoc project> ps --format
    # json` itself (rather than through compose_command()'s fixed
    # project_name()) still needs this exact parsing -- this is the function
    # such a caller should reach for directly instead of hand-rolling it.
    payload = json.dumps([
        {"Service": "web", "State": "running"},
        {"Service": "db", "State": "running"},
    ])
    entries = lab_common.parse_compose_ps_json(payload)
    assert [e["Service"] for e in entries] == ["web", "db"]


def test_parse_compose_ps_json_handles_json_lines_shape():
    line1 = json.dumps({"Service": "web", "State": "running"})
    line2 = json.dumps({"Service": "db", "State": "exited"})
    entries = lab_common.parse_compose_ps_json(f"{line1}\n{line2}\n")
    assert [e["Service"] for e in entries] == ["web", "db"]


def test_parse_compose_ps_json_empty_input():
    assert lab_common.parse_compose_ps_json("") == []
    assert lab_common.parse_compose_ps_json("   \n  ") == []


def test_published_ports_parses_json_array_from_compose_ps(monkeypatch):
    monkeypatch.setattr(lab_common, "compose_command", lambda *a, **kw: ["docker", "compose", "ps", "--format", "json"])
    payload = json.dumps([
        {"Service": "web", "Publishers": [{"PublishedPort": 8080, "TargetPort": 80, "Protocol": "tcp"}]},
        {"Service": "db", "Publishers": []},
    ])
    monkeypatch.setattr(lab_common, "run_capture", lambda cmd, **kw: _FakeCompletedProcess(0, payload))
    assert lab_common.published_ports() == [("web", "8080->80/tcp")]


def test_published_ports_parses_json_lines_from_older_compose(monkeypatch):
    monkeypatch.setattr(lab_common, "compose_command", lambda *a, **kw: ["docker-compose", "ps", "--format", "json"])
    line1 = json.dumps({"Service": "web", "Publishers": [{"PublishedPort": 5432, "TargetPort": 5432, "Protocol": "tcp"}]})
    line2 = json.dumps({"Service": "worker", "Publishers": None})
    monkeypatch.setattr(lab_common, "run_capture", lambda cmd, **kw: _FakeCompletedProcess(0, f"{line1}\n{line2}\n"))
    assert lab_common.published_ports() == [("web", "5432->5432/tcp")]


def test_published_ports_empty_when_compose_ps_fails(monkeypatch):
    monkeypatch.setattr(lab_common, "compose_command", lambda *a, **kw: ["docker", "compose", "ps"])
    monkeypatch.setattr(lab_common, "run_capture", lambda cmd, **kw: _FakeCompletedProcess(1, ""))
    assert lab_common.published_ports() == []


def test_print_published_ports_reports_none_when_empty(monkeypatch, capsys):
    monkeypatch.setattr(lab_common, "published_ports", lambda: [])
    lab_common.print_published_ports()
    assert "No host ports published" in capsys.readouterr().out


def test_print_published_ports_lists_service_and_mapping(monkeypatch, capsys):
    monkeypatch.setattr(lab_common, "published_ports", lambda: [("web", "8080->80/tcp")])
    lab_common.print_published_ports()
    out = capsys.readouterr().out
    assert "web: 8080->80/tcp" in out


class _FakeDashboard:
    """Duck-typed stand-in for LiveDashboard -- just needs .print()."""

    def __init__(self):
        self.printed: list[str] = []

    def print(self, text, **kwargs):
        self.printed.append(text)


def test_active_dashboard_is_none_when_none_registered():
    lab_common.set_active_dashboard(None)
    assert lab_common.active_dashboard() is None


def test_set_active_dashboard_registers_the_object():
    dashboard = _FakeDashboard()
    lab_common.set_active_dashboard(dashboard)
    try:
        assert lab_common.active_dashboard() is dashboard
    finally:
        lab_common.set_active_dashboard(None)


def test_run_streams_subprocess_output_live_through_the_dashboard():
    # Unlike the old capture-and-suppress-until-failure behavior, subprocess
    # output must now show live regardless of exit status -- see
    # agent/common.py's stream_subprocess_to_dashboard().
    dashboard = _FakeDashboard()
    lab_common.set_active_dashboard(dashboard)
    try:
        # $((6*7)) so the produced output (42) never appears in the command
        # argv itself -- only in what the subprocess actually prints.
        lab_common.run(["sh", "-c", "echo $((6*7))"])
        assert any("42" in line for line in dashboard.printed)
    finally:
        lab_common.set_active_dashboard(None)


def test_run_still_inherits_the_terminal_when_no_dashboard_is_active(capfd):
    # capfd (file-descriptor level), not capsys: subprocess.run() without
    # capture_output hands the child the real fd directly, bypassing
    # Python-level sys.stdout entirely -- capsys can't see it, which is
    # itself confirmation this path truly inherits the terminal rather than
    # going through Python's own stdout.
    lab_common.set_active_dashboard(None)
    lab_common.run(["sh", "-c", "echo $((6*7))"])
    out = capfd.readouterr().out
    assert "42" in out


def test_run_surfaces_streamed_output_on_failure_with_a_dashboard_active():
    dashboard = _FakeDashboard()
    lab_common.set_active_dashboard(dashboard)
    try:
        with pytest.raises(subprocess.CalledProcessError):
            # $((6*7)) so the surfaced output (42) is unambiguously from the
            # subprocess's own stdout, not the "+ command" trace line.
            lab_common.run(["sh", "-c", "echo $((6*7)); exit 1"])
        assert any("42" in line for line in dashboard.printed)
    finally:
        lab_common.set_active_dashboard(None)


def test_run_no_check_returns_failed_result_without_raising_when_dashboard_active():
    lab_common.set_active_dashboard(_FakeDashboard())
    try:
        result = lab_common.run(["sh", "-c", "exit 3"], check=False)
        assert result.returncode == 3
    finally:
        lab_common.set_active_dashboard(None)


def test_run_capture_prints_its_trace_line_through_the_dashboard_when_active():
    dashboard = _FakeDashboard()
    lab_common.set_active_dashboard(dashboard)
    try:
        lab_common.run_capture(["echo", "hi"])
        assert any("echo hi" in line for line in dashboard.printed)
    finally:
        lab_common.set_active_dashboard(None)
