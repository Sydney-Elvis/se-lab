"""Generic lab infrastructure: paths, git, docker/compose, results tracking.

Ported from m3undle-lab's scripts/common/common.py (1289 lines, 82 functions),
which mixed generic mechanism with real M3Undle-specific logic throughout.
Audited function-by-function before porting (see .ai_docs/roadmap.md) into
three tiers:

  (A) Fully generic -- ported here unchanged.
  (B) Generic mechanism, product-specific naming only -- ported here,
      parametrized by runtime.PRODUCT_NAME/runtime.ENV_PREFIX (see
      agent/runtime.py's configure()) instead of hardcoded "m3undle"/
      "M3UNDLE_*". Deployment/last-test bookkeeping keys use a plain LAB_
      prefix instead: they're se-lab-internal state the product app itself
      never reads, so they don't need product namespacing at all.
  (C) Real product-specific logic -- NOT ported. Stays entirely in the
      product lab's own code: secret/credential bootstrapping
      (ensure_encryption_key), anything that manipulates a specific client
      app's own state (normalize_srv2_nextpvr_hdhr_state's NextPVR SQL
      surgery, sync_srv2_scenarios, sync_srv2_ffmpeg_wrapper), and hardcoded
      network/image defaults for specific client apps (SRV2_RUNTIME_ENV_DEFAULTS).
      ensure_layout() calls registry.run_layout_hook() at the end so a product
      lab can still run this kind of work unconditionally at the top of every
      command, the same guarantee the original ensure_layout() gave -- se-lab
      just doesn't know what the hook does.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from . import registry, runtime

LAB_ENV_FILE = runtime.REPO_ROOT / "lab.env"
DOCKER_CONFIG_DIR = runtime.REPO_ROOT / "docker-config"
REPOS_DIR = runtime.REPO_ROOT / "repos"
FIXTURES_DIR = runtime.REPO_ROOT / "fixtures"
REPO_RESULTS_DIR = runtime.REPO_ROOT / "results"

# se-lab's own checkout, independent of runtime.REPO_ROOT (the product lab's
# root, set via configure()) -- this file's own location always tells you
# where the se-lab package itself lives, matching runtime.py's own default
# REPO_ROOT computation.
SE_LAB_DIR = Path(__file__).resolve().parent.parent

RESULTS_DIR_ENV = "LAB_RESULTS_DIR"
ARTIFACTS_DIR_ENV = "LAB_ARTIFACTS_DIR"

DEPLOYMENT_METADATA_KEYS = (
    "LAB_DEPLOY_SOURCE_TYPE",
    "LAB_DEPLOY_SOURCE_REF",
    "LAB_DEPLOY_SOURCE_COMMIT",
    "LAB_DEPLOY_UPDATED_AT_UTC",
    "LAB_DEPLOY_UPDATED_HOST",
)
LAST_TEST_METADATA_KEYS = (
    "LAB_LAST_TEST_TARGET_TYPE",
    "LAB_LAST_TEST_TARGET_REF",
    "LAB_LAST_TEST_STATUS",
    "LAB_LAST_TEST_AT_UTC",
    "LAB_LAST_TEST_HOST",
    "LAB_LAST_TEST_RUN_ID",
    "LAB_LAST_TEST_SUMMARY",
    "LAB_LAST_TEST_ARTIFACTS",
)


def repo_dir() -> Path:
    product_name, _ = runtime.require_product_config()
    return REPOS_DIR / product_name


def _placeholder_image() -> str:
    product_name, _ = runtime.require_product_config()
    return f"{product_name}:unset"


def _image_env_key() -> str:
    _, env_prefix = runtime.require_product_config()
    return f"{env_prefix}_IMAGE"


def _runtime_dir_env_key() -> str:
    _, env_prefix = runtime.require_product_config()
    return f"{env_prefix}_RUNTIME_DIR"


def _repo_url_env_key() -> str:
    _, env_prefix = runtime.require_product_config()
    return f"{env_prefix}_REPO_URL"


def _ghcr_image_env_key() -> str:
    _, env_prefix = runtime.require_product_config()
    return f"{env_prefix}_GHCR_IMAGE"


def _settings_passphrase_env_key() -> str:
    _, env_prefix = runtime.require_product_config()
    return f"{env_prefix}_SETTINGS_PASSPHRASE"


# ---------------------------------------------------------------------------
# formatting / progress
# ---------------------------------------------------------------------------


def format_duration(seconds: float | int | None) -> str:
    """Format elapsed time for people while keeping raw seconds in artifacts."""
    if seconds is None:
        return "unknown"
    value = max(0.0, float(seconds))
    if value < 10:
        return f"{value:.1f} seconds"
    total_seconds = int(round(value))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} hour" + ("" if hours == 1 else "s"))
    if minutes:
        parts.append(f"{minutes} minute" + ("" if minutes == 1 else "s"))
    if secs or not parts:
        parts.append(f"{secs} second" + ("" if secs == 1 else "s"))
    return " ".join(parts)


_COMMAND_PROGRESS_CALLBACK: Callable[[Sequence[str], str], None] | None = None
_COMMAND_PROGRESS_LOG: Path | None = None
_ACTIVE_DASHBOARD: Any | None = None  # duck-typed: anything with a .print(text)


def set_command_progress(
    callback: Callable[[Sequence[str], str], None] | None,
    *,
    log_path: Path | None = None,
) -> None:
    global _COMMAND_PROGRESS_CALLBACK, _COMMAND_PROGRESS_LOG
    _COMMAND_PROGRESS_CALLBACK = callback
    _COMMAND_PROGRESS_LOG = log_path
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8")


def set_active_dashboard(dashboard: Any | None) -> None:
    """Register the currently-running LiveDashboard (or None), so run()/
    run_capture() -- and any other code about to print while a dashboard
    might be active -- can route through it instead of writing to the
    terminal directly.

    Duck-typed (just needs .print()), not agent.dashboard.LiveDashboard
    directly: dashboard.py already imports from this module for
    format_duration(), so importing LiveDashboard back here would cycle.
    """
    global _ACTIVE_DASHBOARD
    _ACTIVE_DASHBOARD = dashboard


def active_dashboard() -> Any | None:
    """The registered dashboard (or None) -- for a product lab's own
    subprocess call that bypasses run()/run_capture() (e.g. one that needs
    its own project-name/profile plumbing) to decide whether to route its
    own output through the dashboard the same way run() does.
    """
    return _ACTIVE_DASHBOARD


def _dashboard_subprocess_env(env: dict[str, str] | None) -> dict[str, str]:
    """Env for a subprocess whose output will be streamed line-by-line
    through the dashboard's Rich console while a live footer is active.

    Piping stdout (rather than inheriting the terminal) already makes most
    CLIs auto-detect a non-tty and degrade to plain, sequential output, but
    Docker Compose/BuildKit's own animated multi-line progress renderers
    aren't fully consistent about that across versions -- forcing them
    explicitly avoids their own cursor-addressing competing with the live
    footer's, which piping alone doesn't guarantee.
    """
    merged = dict(env) if env is not None else dict(os.environ)
    merged.setdefault("COMPOSE_ANSI_MODE", "never")
    merged.setdefault("BUILDKIT_PROGRESS", "plain")
    return merged


def stream_subprocess_to_dashboard(
    dashboard: Any,
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run `command`, printing its combined stdout/stderr through
    `dashboard.print()` line-by-line as it arrives, so it scrolls above the
    live footer in real time instead of being captured and suppressed. Used
    by run() whenever a dashboard is active, and directly by a product lab's
    own subprocess wrapper that bypasses run() (see family_librarian_lab's
    commands.py `_compose()`).
    """
    dashboard.print(f"+ {format_command(command)}")
    process = subprocess.Popen(
        command,
        cwd=str(cwd) if cwd else None,
        env=_dashboard_subprocess_env(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        dashboard.print(line.rstrip("\n"))
    return_code = process.wait()
    result = subprocess.CompletedProcess(command, return_code)
    if check and return_code != 0:
        raise subprocess.CalledProcessError(return_code, command)
    return result


# ---------------------------------------------------------------------------
# env files / settings
# ---------------------------------------------------------------------------


def load_lab_env() -> dict[str, str]:
    return load_env_file(LAB_ENV_FILE)


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def current_hostname() -> str:
    return socket.gethostname().lower()


def resolve_setting(
    name: str,
    *,
    explicit: str | None = None,
    default: str | None = None,
    required: bool = False,
) -> str | None:
    if explicit:
        return explicit
    env_value = os.environ.get(name)
    if env_value:
        return env_value
    file_values = load_lab_env()
    if file_values.get(name):
        return file_values[name]
    if default is not None:
        return default
    if required:
        raise SystemExit(
            f"Missing required setting {name}. Set it in the shell environment, "
            f"add it to {LAB_ENV_FILE}, or pass the matching CLI flag."
        )
    return None


def settings_passphrase(*, required: bool = True) -> str | None:
    """The pinned passphrase settings archives are encrypted/authenticated under.

    Read via the standard env/lab.env precedence under
    ``<ENV_PREFIX>_SETTINGS_PASSPHRASE``. A ``SettingsPlugin`` implementation
    calls this instead of inventing its own env var name -- every product lab
    then shares one naming convention, and ``export_settings``/
    ``import_settings`` stay free of a passphrase parameter on the ABC itself
    (see .ai_docs/settings-backup-restore-plan.md's encryption section: this
    is lab-automation infrastructure, not product security material, so it
    belongs here the same way the other ENV_PREFIX-parametrized helpers above
    do -- se-lab still never generates or manages the value, only names where
    to find it).
    """
    return resolve_setting(_settings_passphrase_env_key(), required=required)


def write_env_file_values(path: Path, updates: dict[str, str]) -> None:
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    new_lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue
        key, _ = line.split("=", 1)
        key = key.strip()
        if key in updates:
            new_lines.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            new_lines.append(line)
    for key, value in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={value}")
    path.write_text("\n".join(new_lines).rstrip() + "\n", encoding="utf-8")


def set_runtime_env_values(updates: dict[str, str]) -> None:
    ensure_layout()
    write_env_file_values(runtime_env_file(), updates)


def get_runtime_env_value(name: str) -> str | None:
    return load_env_file(runtime_env_file()).get(name)


def set_runtime_env_var(name: str, value: str) -> None:
    env_path = runtime_env_file()
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    new_lines = [f"{name}={value}" if line.startswith(f"{name}=") else line for line in lines]
    if not any(line.startswith(f"{name}=") for line in lines):
        new_lines.append(f"{name}={value}")
    env_path.write_text("\n".join(new_lines).rstrip() + "\n", encoding="utf-8")
    print(f"Updated {env_path} with {name}.", flush=True)


def unset_runtime_env_var(name: str) -> None:
    env_path = runtime_env_file()
    if not env_path.exists():
        return
    lines = env_path.read_text(encoding="utf-8").splitlines()
    new_lines = [line for line in lines if not line.startswith(f"{name}=")]
    env_path.write_text("\n".join(new_lines).rstrip() + ("\n" if new_lines else ""), encoding="utf-8")
    print(f"Removed {name} from {env_path}.", flush=True)


# ---------------------------------------------------------------------------
# runtime paths
# ---------------------------------------------------------------------------


def default_runtime_dir() -> Path:
    return runtime.REPO_ROOT.parent / runtime.require_product_config()[0]


def runtime_dir() -> Path:
    configured = resolve_setting(_runtime_dir_env_key())
    if configured:
        return Path(configured)
    return default_runtime_dir()


def runtime_compose_file() -> Path:
    return runtime_dir() / "docker-compose.yaml"


def runtime_env_file() -> Path:
    return runtime_dir() / ".env"


def runtime_config_dir() -> Path:
    return runtime_dir() / ".config"


def runtime_data_dir() -> Path:
    return runtime_dir() / "data"


def runtime_artifacts_root_dir() -> Path:
    return runtime_dir() / "artifacts"


def artifacts_runs_dir() -> Path:
    return runtime_artifacts_root_dir() / "runs"


def artifacts_checklists_dir() -> Path:
    return runtime_artifacts_root_dir() / "checklists"


def latest_artifact_path() -> Path:
    return runtime_artifacts_root_dir() / "latest.json"


def reports_dir() -> Path:
    """Where se-lab's own agent/ writes reports: run reports + AI metrics."""
    return runtime_artifacts_root_dir() / "agent"


def metrics_dir() -> Path:
    return reports_dir() / "metrics"


def runtime_results_dir() -> Path:
    configured = os.environ.get(ARTIFACTS_DIR_ENV)
    if configured:
        return Path(configured)
    return runtime_artifacts_root_dir() / "adhoc"


def runtime_run_artifacts_dir(run_id: str) -> Path:
    return artifacts_runs_dir() / run_id


def runtime_run_summary_path(run_id: str) -> Path:
    return runtime_run_artifacts_dir(run_id) / "summary.json"


def repo_run_report_path(run_id: str) -> Path:
    return runtime_run_summary_path(run_id)


def results_run_id(prefix: str, *, timestamp: str | None = None) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", prefix.lower()).strip("-") or "run"
    stamp = timestamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{slug}-{stamp}"


def create_results_run(prefix: str, *, timestamp: str | None = None) -> tuple[str, Path, Path]:
    run_id = results_run_id(prefix, timestamp=timestamp)
    summary_path = repo_run_report_path(run_id)
    artifacts_dir = runtime_run_artifacts_dir(run_id)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    return run_id, summary_path, artifacts_dir


def list_run_summary_paths(prefix: str | None = None) -> list[Path]:
    runs_dir = artifacts_runs_dir()
    if not runs_dir.exists():
        return []
    candidates = [path for path in runs_dir.glob("*/summary.json") if path.is_file()]
    if prefix:
        candidates = [path for path in candidates if path.parent.name.startswith(prefix)]
    return sorted(candidates, key=lambda path: path.stat().st_mtime)


def write_latest_artifact_record(record: dict[str, object]) -> Path:
    path = latest_artifact_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **record,
    }
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary_path.replace(path)
    return path


def read_latest_artifact_record() -> dict[str, object] | None:
    path = latest_artifact_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# layout
# ---------------------------------------------------------------------------


def ensure_layout() -> None:
    """Create the directories/files every lab needs, then run the product's own hook.

    Deliberately generic: no role-conditional product directories (a product's
    own data-directory shape), no secrets, no fixture syncing. A product lab
    that needs those registers registry.set_layout_hook(...), run unconditionally
    here -- the same guarantee the original single-repo ensure_layout() gave.
    """
    for directory in (DOCKER_CONFIG_DIR, REPOS_DIR, FIXTURES_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    runtime_dir().mkdir(parents=True, exist_ok=True)
    runtime_artifacts_root_dir().mkdir(parents=True, exist_ok=True)
    artifacts_runs_dir().mkdir(parents=True, exist_ok=True)
    artifacts_checklists_dir().mkdir(parents=True, exist_ok=True)
    runtime_results_dir().mkdir(parents=True, exist_ok=True)
    runtime_config_dir().mkdir(parents=True, exist_ok=True)
    runtime_data_dir().mkdir(parents=True, exist_ok=True)
    if not runtime_env_file().exists():
        runtime_env_file().write_text(f"{_image_env_key()}={_placeholder_image()}\n", encoding="utf-8")
    registry.run_layout_hook()


def compose_template_file() -> Path | None:
    candidate = DOCKER_CONFIG_DIR / "docker-compose.yaml"
    return candidate if candidate.exists() else None


def has_compose_stack() -> bool:
    return compose_template_file() is not None


def project_name() -> str:
    product_name, _ = runtime.require_product_config()
    return f"{product_name}-lab"


def runtime_summary() -> str:
    return f"runtime_dir={runtime_dir()}"


def suggested_commands() -> list[str]:
    return [
        "lab status",
        "lab build <branch>",
        "lab pull <tag>",
        "lab run <branch>",
        "lab run --tag <tag>",
        "lab recreate",
        "lab checklist <target>",
    ]


# ---------------------------------------------------------------------------
# subprocess / compose
# ---------------------------------------------------------------------------


def format_command(command: Sequence[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    if _COMMAND_PROGRESS_CALLBACK is not None:
        _COMMAND_PROGRESS_CALLBACK(command, "Starting")
        process = subprocess.Popen(
            command,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        last_render = 0.0
        log_stream = _COMMAND_PROGRESS_LOG.open("a", encoding="utf-8") if _COMMAND_PROGRESS_LOG else None
        try:
            if log_stream:
                log_stream.write(f"$ {format_command(command)}\n")
            for line in process.stdout:
                if log_stream:
                    log_stream.write(line)
                    log_stream.flush()
                now = time.monotonic()
                if now - last_render >= 0.1:
                    detail = line.strip()
                    if detail:
                        _COMMAND_PROGRESS_CALLBACK(command, detail)
                        last_render = now
        finally:
            if log_stream:
                log_stream.close()
        return_code = process.wait()
        _COMMAND_PROGRESS_CALLBACK(command, "Completed" if return_code == 0 else f"Failed with exit {return_code}")
        result = subprocess.CompletedProcess(command, return_code)
        if check and return_code != 0:
            raise subprocess.CalledProcessError(return_code, command)
        return result
    if _ACTIVE_DASHBOARD is not None:
        # Stream instead of inheriting the terminal: a subprocess's own
        # multi-line output (docker compose's progress, in particular) still
        # needs to appear live, but it can't be handed the real terminal
        # directly while a live footer owns the bottom of the screen -- see
        # stream_subprocess_to_dashboard()'s own docstring.
        return stream_subprocess_to_dashboard(_ACTIVE_DASHBOARD, command, cwd=cwd, env=env, check=check)
    print(f"+ {format_command(command)}", flush=True)
    return subprocess.run(command, cwd=str(cwd) if cwd else None, env=env, check=check, text=True)


def run_capture(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run `command` fully captured and silent -- for callers that want the
    output programmatically, not shown live. Its own trace line still routes
    through the dashboard when one is active, same as run(), so it doesn't
    corrupt the live footer."""
    message = f"+ {format_command(command)}"
    if _ACTIVE_DASHBOARD is not None:
        _ACTIVE_DASHBOARD.print(message)
    else:
        print(message, flush=True)
    return subprocess.run(command, cwd=str(cwd) if cwd else None, env=env, check=check, text=True, capture_output=True)


def _docker_compose_plugin_available() -> bool:
    result = subprocess.run(
        ["docker", "compose", "version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, check=False
    )
    return result.returncode == 0


def detect_compose_base_command() -> list[str]:
    if _docker_compose_plugin_available():
        return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    raise SystemExit("Neither 'docker compose' nor 'docker-compose' is available on this server.")


def compose_command(*args: str, extra_compose_files: Sequence[Path] = ()) -> list[str]:
    ensure_layout()
    ensure_runtime_ready_for_compose()
    base = detect_compose_base_command()
    cmd = [*base, "-p", project_name(), "-f", str(runtime_compose_file())]
    for extra in extra_compose_files:
        cmd += ["-f", str(extra)]
    cmd += ["--env-file", str(runtime_env_file()), *args]
    return cmd


def ensure_runtime_ready_for_compose() -> None:
    template = compose_template_file()
    if template is None:
        raise SystemExit("No runnable compose stack yet -- missing docker-config/docker-compose.yaml.")
    sync_runtime_compose()


def sync_runtime_compose() -> None:
    ensure_layout()
    template_path = compose_template_file()
    if template_path is None:
        print(f"No compose template defined yet. Runtime directory prepared at {runtime_dir()} only.", flush=True)
        return
    if not template_path.exists():
        raise SystemExit(f"Missing compose template: {template_path}")
    template = template_path.read_text(encoding="utf-8")
    runtime_file = runtime_compose_file()
    if runtime_file.exists():
        current = runtime_file.read_text(encoding="utf-8")
        if current == template:
            return
        backup_runtime_file(runtime_file)
    shutil.copy2(template_path, runtime_file)
    print(f"Installed runtime compose file at {runtime_file}.", flush=True)


def backup_runtime_file(path: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = path.with_name(f"{path.name}.{stamp}.bak")
    shutil.copy2(path, backup_path)
    return backup_path


def _port_occupied(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        # Compose can leave recently closed connections in TIME_WAIT; only a
        # live listener should count as occupied, not ordinary teardown residue.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("0.0.0.0", port))
        except OSError:
            return True
    return False


def _conflicting_host_network_containers(exclude_project: str) -> list[str]:
    """Names of running host-network containers from a DIFFERENT compose
    project than the one about to be (re)deployed. Host-network containers
    publish no `ports:`, so Docker itself can't report a bind conflict for
    them -- this is the only way to find out who's actually holding a port.
    A container from the SAME project isn't a real conflict: compose_up()'s
    own down-then-up cycle is about to replace it anyway.
    """
    result = run_capture(["docker", "ps", "--format", "{{.Names}}"], check=False)
    names = [line for line in result.stdout.splitlines() if line.strip()]
    conflicting = []
    for name in names:
        inspect = run_capture(
            [
                "docker", "inspect", "--format",
                '{{.HostConfig.NetworkMode}}\t{{index .Config.Labels "com.docker.compose.project"}}',
                name,
            ],
            check=False,
        )
        mode, _, project = inspect.stdout.strip().partition("\t")
        if mode == "host" and project and project != exclude_project:
            conflicting.append(name)
    return conflicting


def ensure_required_host_ports_available(*, timeout_seconds: float = 10.0) -> None:
    """Preflight compose_up()/compose_up_only() against registry.required_host_ports().

    Ask before stopping anything, rather than either crashing mid-deploy with
    a raw traceback (Docker can't detect a host-network port conflict itself
    -- the container just fails to bind) or leaving a stale container from a
    different lab/project silently answering health probes while this deploy
    fails. Both have caused real confusion on shared hosts before.
    """
    ports = registry.required_host_ports()
    if not ports:
        return

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        occupied = [p for p in ports if _port_occupied(p)]
        if not occupied:
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)

    conflicting = _conflicting_host_network_containers(project_name())
    if not conflicting:
        # Either this project's own soon-to-be-recreated container, or
        # something outside Docker/Compose entirely -- nothing safe to stop
        # on this project's behalf; let the deploy proceed and surface
        # whatever Docker itself reports.
        return

    formatted_ports = ", ".join(str(p) for p in occupied)
    names = ", ".join(conflicting)
    if not sys.stdin.isatty():
        raise SystemExit(
            f"Required host TCP port(s) {formatted_ports} are already in use by host-networked "
            f"container(s) {names} from a different lab/project, and this session is "
            "non-interactive. Stop them manually (`docker stop <name>`), then rerun."
        )
    response = input(
        f"Port(s) {formatted_ports} are already in use by host-networked container(s) {names} "
        "from a different lab/project. Stop them and continue? [y/N] "
    ).strip().lower()
    if response not in {"y", "yes"}:
        raise SystemExit("Aborted: required ports are still in use by another lab/project.")
    for name in conflicting:
        run(["docker", "stop", name])


# ---------------------------------------------------------------------------
# single-instance guard
# ---------------------------------------------------------------------------


def _run_lock_path() -> Path:
    return runtime_dir() / "run.lock"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@contextlib.contextmanager
def run_lock(*, label: str = "lab run") -> Iterator[None]:
    """Refuse to start a second lifecycle command (run/up/recreate) against
    this same runtime dir while one is already in progress.

    Without this, an overlapping or crashed/interrupted-without-cleanup
    invocation collides with the next one on whatever host port or compose
    project it still holds -- Docker reports a raw port-bind failure with no
    indication another lab invocation is the actual cause (confirmed for
    real against family-librarian-lab: an interrupted `run` left a container
    holding its fixed host port, and the next `run` died deep inside a
    suite's own compose up with no hint why). A lock whose owning PID is no
    longer alive is reclaimed automatically -- a killed or crashed process
    never gets a chance to clean its own lock file up.

    Best-effort, like the PID-based cleanup in agent/simulators/base.py, not
    a formally airtight distributed lock -- reclaiming a lock whose writer
    is caught between creating and writing the file is a theoretical race
    not worth engineering around for a human-driven CLI.
    """
    ensure_layout()
    path = _run_lock_path()
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder_text = path.read_text(encoding="utf-8").strip() if path.exists() else ""
            holder = int(holder_text) if holder_text.isdigit() else None
            if holder is not None and _pid_alive(holder):
                raise SystemExit(
                    f"Another {label} is already in progress (PID {holder}, lock file: {path}). "
                    "Wait for it to finish, or remove the lock file yourself if you're sure it's stale."
                )
            path.unlink(missing_ok=True)
            continue
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
            break
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def compose_up(*, extra_compose_files: Sequence[Path] = ()) -> None:
    ensure_required_host_ports_available()
    run(compose_command("down", "--remove-orphans", extra_compose_files=extra_compose_files), check=False)
    run(compose_command("up", "-d", "--remove-orphans", extra_compose_files=extra_compose_files))


def compose_up_only(service: str, *, extra_compose_files: Sequence[Path] = ()) -> None:
    """Start just one compose service, e.g. after a fresh DB/state wipe left
    dependent clients unable to pass their health checks yet."""
    ensure_required_host_ports_available()
    run(compose_command("down", "--remove-orphans", extra_compose_files=extra_compose_files), check=False)
    run(compose_command("up", "-d", service, extra_compose_files=extra_compose_files))


def compose_down() -> None:
    run(compose_command("down", "--remove-orphans"))


def compose_ps() -> None:
    run(compose_command("ps"))


def parse_compose_ps_json(output: str) -> list[dict[str, Any]]:
    """Parse the stdout of `compose ps --format json`. Compose versions
    disagree on shape -- a JSON array on newer releases, one JSON object per
    line on older ones -- so try both rather than pin to one.

    Deliberately a pure function over text, not tied to compose_command()'s
    fixed single-project-per-lab assumption: a lab that manages its own
    per-scenario/per-run Compose project names (running `docker compose
    -p <ad-hoc-name> ps --format json` itself, outside compose_command())
    still needs this exact same parsing -- getting the shape wrong is not a
    hypothetical, see the family-librarian-lab audit finding it was missing
    entirely. Call this directly on your own subprocess's stdout rather than
    re-deriving it; don't re-derive compose_command()'s own project_name()
    assumption just to reach this parsing logic.
    """
    text = output.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        pass
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _compose_ps_entries() -> list[dict[str, Any]]:
    """This lab's own `compose ps --format json`, via compose_command()'s
    fixed project_name(). See parse_compose_ps_json() for the shape-handling
    itself, which is what a lab using its own ad-hoc project name should call
    directly instead of this function."""
    result = run_capture(compose_command("ps", "--format", "json"), check=False)
    if result.returncode != 0:
        return []
    return parse_compose_ps_json(result.stdout)


def published_ports() -> list[tuple[str, str]]:
    """(service, "published->target/protocol") for each host port this
    project's containers currently publish, per `compose ps`."""
    pairs: list[tuple[str, str]] = []
    for entry in _compose_ps_entries():
        service = entry.get("Service") or entry.get("Name") or "?"
        for publisher in entry.get("Publishers") or []:
            published = publisher.get("PublishedPort")
            if not published:
                continue
            target = publisher.get("TargetPort", "?")
            protocol = publisher.get("Protocol", "tcp")
            pairs.append((service, f"{published}->{target}/{protocol}"))
    return pairs


def print_published_ports() -> None:
    pairs = published_ports()
    if not pairs:
        print("No host ports published by this stack.", flush=True)
        return
    print("Published ports:", flush=True)
    for service, mapping in pairs:
        print(f"  {service}: {mapping}", flush=True)


def compose_logs(tail: int, service: str) -> None:
    run(compose_command("logs", "--tail", str(tail), service))


def docker_pull(image: str) -> None:
    run(["docker", "pull", image])


def docker_build(image: str, context_dir: Path, source_revision: str | None = None) -> None:
    cmd = ["docker", "build", "--tag", image]
    if source_revision:
        cmd += ["--build-arg", f"SOURCE_REVISION={source_revision}"]
    cmd.append(".")
    run(cmd, cwd=context_dir)


# ---------------------------------------------------------------------------
# image naming / current image / deployment + last-test metadata
# ---------------------------------------------------------------------------


def sanitize_branch_name(branch: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", branch).strip("-")
    return cleaned or "branch"


def local_branch_image(branch: str) -> str:
    product_name, _ = runtime.require_product_config()
    return f"{product_name}:branch-{sanitize_branch_name(branch)}"


def local_tag_image(tag: str) -> str:
    product_name, _ = runtime.require_product_config()
    return f"{product_name}:tag-{sanitize_branch_name(tag)}"


def ghcr_tag_image(tag: str, ghcr_image: str) -> str:
    return f"{ghcr_image}:{tag}"


def set_current_image(image: str) -> None:
    set_runtime_env_values({_image_env_key(): image})
    print(f"Updated {runtime_env_file()} with the selected image.", flush=True)


def get_current_image() -> str | None:
    image = get_runtime_env_value(_image_env_key())
    if image == _placeholder_image():
        return None
    return image


def set_deployment_metadata(source_type: str, source_ref: str, *, image: str, source_commit: str | None = None) -> None:
    updates = {
        _image_env_key(): image,
        DEPLOYMENT_METADATA_KEYS[0]: source_type,
        DEPLOYMENT_METADATA_KEYS[1]: source_ref,
        DEPLOYMENT_METADATA_KEYS[2]: source_commit or "",
        DEPLOYMENT_METADATA_KEYS[3]: datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        DEPLOYMENT_METADATA_KEYS[4]: current_hostname(),
    }
    set_runtime_env_values(updates)


def get_deployment_metadata() -> dict[str, str | None]:
    env = load_env_file(runtime_env_file())
    image = env.get(_image_env_key())
    if image == _placeholder_image():
        image = None
    source_type = env.get(DEPLOYMENT_METADATA_KEYS[0]) or None
    source_ref = env.get(DEPLOYMENT_METADATA_KEYS[1]) or None
    source_commit = env.get(DEPLOYMENT_METADATA_KEYS[2]) or None
    source_inferred = False

    if image and not source_type:
        branch = repo_current_branch()
        if branch and image == local_branch_image(branch):
            source_type = "branch"
            source_ref = branch
            source_commit = source_commit or repo_head_commit()
            source_inferred = True
        else:
            ghcr_image = resolve_setting(_ghcr_image_env_key(), required=False)
            ghcr_prefix = f"{ghcr_image}:" if ghcr_image else None
            if ghcr_prefix and image.startswith(ghcr_prefix):
                source_type = "tag"
                source_ref = image.removeprefix(ghcr_prefix)
                source_inferred = True

    return {
        "image": image,
        "source_type": source_type,
        "source_ref": source_ref,
        "source_commit": source_commit,
        "updated_at_utc": env.get(DEPLOYMENT_METADATA_KEYS[3]) or None,
        "updated_host": env.get(DEPLOYMENT_METADATA_KEYS[4]) or None,
        "source_inferred": "true" if source_inferred else None,
    }


def set_last_test_metadata(
    target_type: str, target_ref: str, *, status: str, run_id: str | None = None,
    summary_path: str | None = None, artifacts_path: str | None = None,
) -> None:
    values = (target_type, target_ref, status, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
              current_hostname(), run_id or "", summary_path or "", artifacts_path or "")
    set_runtime_env_values(dict(zip(LAST_TEST_METADATA_KEYS, values)))


def get_last_test_metadata() -> dict[str, str | None]:
    env = load_env_file(runtime_env_file())
    keys = ("target_type", "target_ref", "status", "at_utc", "host", "run_id", "summary_path", "artifacts_path")
    return {label: env.get(key) or None for label, key in zip(keys, LAST_TEST_METADATA_KEYS)}


# ---------------------------------------------------------------------------
# git / deploy
# ---------------------------------------------------------------------------


def is_git_checkout(path: Path) -> bool:
    return (path / ".git").exists()


def ensure_repo_checkout(repo_url: str) -> None:
    ensure_layout()
    target = repo_dir()
    if target.exists():
        if not is_git_checkout(target):
            raise SystemExit(f"{target} exists but is not a git checkout. Move it aside or remove it before deploying a branch.")
        return
    run(["git", "clone", repo_url, str(target)])


def repo_origin_url() -> str | None:
    target = repo_dir()
    if not is_git_checkout(target):
        return None
    result = run_capture(["git", "remote", "get-url", "origin"], cwd=target, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def print_repo_summary(repo_url: str) -> None:
    target = repo_dir()
    if repo_origin_url():
        print(f"Using existing checkout at {target}.", flush=True)
    else:
        print(f"Cloning repository into {target}.", flush=True)


def git_branch(path: Path) -> str | None:
    if not is_git_checkout(path):
        return None
    result = run_capture(["git", "branch", "--show-current"], cwd=path, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def git_commit(path: Path, *, short: bool = False) -> str | None:
    if not is_git_checkout(path):
        return None
    command = ["git", "rev-parse"]
    if short:
        command.append("--short")
    command.append("HEAD")
    result = run_capture(command, cwd=path, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def repo_current_branch() -> str | None:
    return git_branch(repo_dir())


def repo_head_commit(short: bool = False) -> str | None:
    return git_commit(repo_dir(), short=short)


def _repo_version_line(label: str, path: Path) -> str:
    if not is_git_checkout(path):
        return f"{label}: not a git checkout ({path})"
    branch = git_branch(path) or "detached"
    commit = git_commit(path, short=True) or "unknown"
    return f"{label}: {branch} @ {commit}"


def version_summary() -> str:
    """One line per git checkout that makes up "the lab": the product lab
    itself and se-lab, the framework nested inside it. Deliberately not
    repo_dir() (repos/<product>) -- that's the product *application's* source
    checkout, which is what `status` reports, not what version of the lab
    tooling is running.
    """
    label = runtime.PRODUCT_NAME or "se-lab"
    lines = [_repo_version_line(label, runtime.REPO_ROOT)]
    if runtime.REPO_ROOT != SE_LAB_DIR:
        lines.append(_repo_version_line("se-lab", SE_LAB_DIR))
    return "\n".join(lines)


def git_ref_exists(ref: str) -> bool:
    result = run_capture(["git", "rev-parse", "--verify", "--quiet", ref], cwd=repo_dir(), check=False)
    return result.returncode == 0


def git_assert_remote_branch(branch: str) -> None:
    """Fail with an actionable message when origin/<branch> cannot resolve.

    Callers reset and clean the working tree before checking the target out, so
    an unresolvable ref otherwise surfaced as a CalledProcessError traceback
    after the tree had already been wiped. Tags are the common case: they never
    exist as origin/<name>, so a tag target could never have worked here.
    Call this after fetching origin and before touching the working tree.
    """
    if git_ref_exists(f"refs/remotes/origin/{branch}"):
        return
    if git_ref_exists(f"refs/tags/{branch}"):
        raise SystemExit(
            f"'{branch}' is a tag, not a branch, so origin/{branch} does not exist.\n"
            f"Build the tag from source with './lab build {branch}', "
            f"or deploy the pre-built image with './lab pull {branch}'."
        )
    raise SystemExit(
        f"origin/{branch} does not exist after fetching origin.\n"
        f"The branch may be misspelled, renamed, or deleted upstream after a merge.\n"
        f"Build the current local checkout with './lab build --local', "
        f"or pass a branch that exists on origin."
    )


def _classify_build_target(target: str) -> tuple[str, str] | None:
    if git_ref_exists(f"refs/tags/{target}"):
        return "tag", target
    if git_ref_exists(f"refs/remotes/origin/{target}"):
        return "branch", target
    return None


def resolve_build_target(target: str, repo_url: str | None = None) -> tuple[str, str]:
    """Classify a build target as a tag or a branch, tags winning ties.

    Tag is checked before branch so a release tag builds from source with the
    same command as a branch. A name that is both resolves as the tag; repo
    naming convention keeps that collision from arising.

    Local refs are consulted first and origin is fetched only when the target
    matches neither, so the common case stays offline-cheap while a
    newly-pushed tag or branch still resolves on the second pass.
    """
    resolved_repo_url = resolve_setting(_repo_url_env_key(), explicit=repo_url, required=True)
    ensure_repo_checkout(resolved_repo_url)
    resolved = _classify_build_target(target)
    if resolved:
        return resolved
    run(["git", "fetch", "--prune", "--tags", "origin"], cwd=repo_dir())
    resolved = _classify_build_target(target)
    if resolved:
        return resolved
    raise SystemExit(f"'{target}' is neither a tag nor a branch on origin after fetching.\nCheck the name, or build the current checkout with './lab build --local'.")


def git_prepare_branch(branch: str) -> None:
    target = repo_dir()
    run(["git", "fetch", "--prune", "origin"], cwd=target)
    git_assert_remote_branch(branch)
    run(["git", "reset", "--hard"], cwd=target)
    run(["git", "clean", "-ffdx"], cwd=target)
    run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=target)
    run(["git", "reset", "--hard", f"origin/{branch}"], cwd=target)


def git_prepare_tag(tag: str) -> None:
    target = repo_dir()
    run(["git", "fetch", "--prune", "--tags", "origin"], cwd=target)
    if not git_ref_exists(f"refs/tags/{tag}"):
        raise SystemExit(f"Tag '{tag}' does not exist after fetching origin.\nCheck the tag name, or build a branch instead.")
    run(["git", "reset", "--hard"], cwd=target)
    run(["git", "clean", "-ffdx"], cwd=target)
    # Detached on purpose: a tag is not a branch, and creating a branch of the
    # same name would make every later `git checkout <name>` ambiguous.
    run(["git", "checkout", "--detach", f"refs/tags/{tag}"], cwd=target)


def git_refresh_current_branch() -> str:
    branch = repo_current_branch()
    if not branch:
        raise SystemExit("The cached checkout is not on a named branch. Use deploy-branch explicitly if you want to switch branches.")
    target = repo_dir()
    run(["git", "fetch", "--prune", "origin"], cwd=target)
    git_assert_remote_branch(branch)
    run(["git", "reset", "--hard", f"origin/{branch}"], cwd=target)
    return branch


def deploy_branch(branch: str, repo_url: str | None = None, *, extra_compose_files: Sequence[Path] = ()) -> str:
    resolved_repo_url = resolve_setting(_repo_url_env_key(), explicit=repo_url, required=True)
    image = local_branch_image(branch)
    ensure_repo_checkout(resolved_repo_url)
    print_repo_summary(resolved_repo_url)
    git_prepare_branch(branch)
    commit = repo_head_commit()
    docker_build(image, repo_dir(), source_revision=commit)
    sync_runtime_compose()
    set_deployment_metadata("branch", branch, image=image, source_commit=commit)
    compose_up(extra_compose_files=extra_compose_files)
    print_published_ports()
    print(f"Branch '{branch}' is deployed.", flush=True)
    return image


def deploy_source_tag(tag: str, repo_url: str | None = None, *, extra_compose_files: Sequence[Path] = ()) -> str:
    """Build a release tag from source, as opposed to deploy_tag's GHCR pull.

    Recorded as source_type "source-tag" so a later metadata-driven deploy
    rebuilds from source instead of pulling the GHCR image for the same name.
    """
    resolved_repo_url = resolve_setting(_repo_url_env_key(), explicit=repo_url, required=True)
    image = local_tag_image(tag)
    ensure_repo_checkout(resolved_repo_url)
    print_repo_summary(resolved_repo_url)
    git_prepare_tag(tag)
    commit = repo_head_commit()
    docker_build(image, repo_dir(), source_revision=commit)
    sync_runtime_compose()
    set_deployment_metadata("source-tag", tag, image=image, source_commit=commit)
    compose_up(extra_compose_files=extra_compose_files)
    print_published_ports()
    print(f"Tag '{tag}' is built from source and deployed.", flush=True)
    return image


def deploy_current_branch(repo_url: str | None = None, *, extra_compose_files: Sequence[Path] = ()) -> tuple[str, str]:
    resolved_repo_url = resolve_setting(_repo_url_env_key(), explicit=repo_url, required=True)
    ensure_repo_checkout(resolved_repo_url)
    print_repo_summary(resolved_repo_url)
    branch = git_refresh_current_branch()
    image = local_branch_image(branch)
    commit = repo_head_commit()
    docker_build(image, repo_dir(), source_revision=commit)
    sync_runtime_compose()
    set_deployment_metadata("branch", branch, image=image, source_commit=commit)
    compose_up(extra_compose_files=extra_compose_files)
    print_published_ports()
    print(f"Current checkout branch '{branch}' is deployed.", flush=True)
    return branch, image


def deploy_tag(tag: str, image_repo: str | None = None, *, extra_compose_files: Sequence[Path] = ()) -> str:
    resolved_image_repo = resolve_setting(_ghcr_image_env_key(), explicit=image_repo, required=True)
    image = ghcr_tag_image(tag, resolved_image_repo)
    docker_pull(image)
    sync_runtime_compose()
    set_deployment_metadata("tag", tag, image=image)
    compose_up(extra_compose_files=extra_compose_files)
    print_published_ports()
    print(f"Tag '{tag}' is deployed.", flush=True)
    return image


# ---------------------------------------------------------------------------
# client app version tracking
# ---------------------------------------------------------------------------
#
# agent/commands/clients.py's status/update/rollback/pin call these. They
# don't come from a "tier" of the original common.py: the original cli.py
# called all six of these on `lab_common`, but none of the six were ever
# defined anywhere in that repo -- confirmed by exhaustive grep, not an
# oversight in the audit. clients update/rollback/pin/status would have
# crashed with AttributeError if ever actually run there. Inert in practice
# (clients isn't part of the automated suites, and srv2 no longer hosts any
# client apps to manage), so left as-is on the frozen lab per the standing
# triage rule; implemented properly here instead, from what their call sites
# in cli.py clearly intended.


def set_lab_env_values(updates: dict[str, str]) -> None:
    """Persist to lab.env, not just the live runtime .env -- survives a layout reset."""
    write_env_file_values(LAB_ENV_FILE, updates)


def active_clients() -> list[str]:
    """Registered ClientPlugin names present in COMPOSE_PROFILES for this deployment."""
    profiles = {p.strip() for p in (get_runtime_env_value("COMPOSE_PROFILES") or "").split(",") if p.strip()}
    return [name for name in registry.all_clients() if name in profiles]


def get_image_repo_digest(image: str) -> str | None:
    """The pulled image's repo digest (e.g. 'name@sha256:...'), or None if not found locally."""
    result = run_capture(["docker", "inspect", "--format", "{{index .RepoDigests 0}}", image], check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def compose_up_service(service: str) -> None:
    """Restart just one compose service, without touching any others already running."""
    run(compose_command("up", "-d", "--no-deps", service))


def _client_version_history_path() -> Path:
    return runtime_dir() / "client-versions.json"


def read_client_version_history() -> dict[str, list[dict]]:
    path = _client_version_history_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def push_client_version_record(client: str, record: dict) -> None:
    """Prepend a version snapshot to `client`'s history (newest first)."""
    history = read_client_version_history()
    entries = history.setdefault(client, [])
    entries.insert(0, record)
    path = _client_version_history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def exit_with_message(message: str, exit_code: int = 1) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(exit_code)
