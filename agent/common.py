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
from typing import Callable, Sequence

from . import registry, runtime

VALID_ROLES = ("srv1", "srv2")

LAB_ENV_FILE = runtime.REPO_ROOT / "lab.env"
DOCKER_CONFIG_DIR = runtime.REPO_ROOT / "docker-config"
REPOS_DIR = runtime.REPO_ROOT / "repos"
FIXTURES_DIR = runtime.REPO_ROOT / "fixtures"
REPO_RESULTS_DIR = runtime.REPO_ROOT / "results"

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


def _role_env_key() -> str:
    _, env_prefix = runtime.require_product_config()
    return f"{env_prefix}_LAB_ROLE"


def _runtime_dir_env_key() -> str:
    _, env_prefix = runtime.require_product_config()
    return f"{env_prefix}_RUNTIME_DIR"


def _repo_url_env_key() -> str:
    _, env_prefix = runtime.require_product_config()
    return f"{env_prefix}_REPO_URL"


def _ghcr_image_env_key() -> str:
    _, env_prefix = runtime.require_product_config()
    return f"{env_prefix}_GHCR_IMAGE"


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
# role / runtime paths
# ---------------------------------------------------------------------------


def detect_role_from_hostname() -> str | None:
    hostname = current_hostname()
    if "int-srv1" in hostname or hostname.endswith("srv1"):
        return "srv1"
    if "int-srv2" in hostname or hostname.endswith("srv2"):
        return "srv2"
    return None


def current_role() -> str:
    role = resolve_setting(_role_env_key(), default=detect_role_from_hostname() or "srv1")
    if role not in VALID_ROLES:
        raise SystemExit(f"Unsupported {_role_env_key()}={role!r}. Expected one of: {', '.join(VALID_ROLES)}.")
    return role


def require_role(expected_role: str) -> None:
    role = current_role()
    if role != expected_role:
        raise SystemExit(
            f"This script is for role '{expected_role}', but the current lab role is '{role}'. "
            f"Update {LAB_ENV_FILE} or rerun setup_vm.sh with --role {expected_role} on the correct host."
        )


def default_runtime_dir_for_role(role: str) -> Path:
    return runtime.REPO_ROOT.parent / f"{runtime.require_product_config()[0]}-{role}"


def runtime_dir() -> Path:
    configured = resolve_setting(_runtime_dir_env_key())
    if configured:
        return Path(configured)
    return default_runtime_dir_for_role(current_role())


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
    candidate = DOCKER_CONFIG_DIR / current_role() / "docker-compose.yaml"
    return candidate if candidate.exists() else None


def role_has_compose_stack() -> bool:
    return compose_template_file() is not None


def project_name() -> str:
    product_name, _ = runtime.require_product_config()
    return f"{product_name}-lab-{current_role()}"


def role_summary() -> str:
    return f"role={current_role()} runtime_dir={runtime_dir()}"


def suggested_commands_for_role(role: str | None = None) -> list[str]:
    active_role = role or current_role()
    commands = ["lab status", "lab build <branch>", "lab pull <tag>"]
    if active_role == "srv1":
        commands.extend(["lab run <branch>", "lab run --tag <tag>", "lab recreate"])
    else:
        commands.extend(["lab checklist <target>", "lab recreate"])
    return commands


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
    print(f"+ {format_command(command)}", flush=True)
    return subprocess.run(command, cwd=str(cwd) if cwd else None, env=env, check=check, text=True)


def run_capture(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    print(f"+ {format_command(command)}", flush=True)
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


def compose_command(*args: str) -> list[str]:
    ensure_layout()
    ensure_runtime_ready_for_compose()
    base = detect_compose_base_command()
    return [*base, "-p", project_name(), "-f", str(runtime_compose_file()), "--env-file", str(runtime_env_file()), *args]


def ensure_runtime_ready_for_compose() -> None:
    template = compose_template_file()
    if template is None:
        raise SystemExit(f"The current role '{current_role()}' does not have a runnable compose stack yet.")
    sync_runtime_compose()


def sync_runtime_compose() -> None:
    ensure_layout()
    template_path = compose_template_file()
    if template_path is None:
        print(f"No compose template is defined for role '{current_role()}'. Runtime directory prepared at {runtime_dir()} only.", flush=True)
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


def compose_up() -> None:
    run(compose_command("down", "--remove-orphans"), check=False)
    run(compose_command("up", "-d", "--remove-orphans"))


def compose_up_only(service: str) -> None:
    """Start just one compose service, e.g. after a fresh DB/state wipe left
    dependent clients unable to pass their health checks yet."""
    run(compose_command("down", "--remove-orphans"), check=False)
    run(compose_command("up", "-d", service))


def compose_down() -> None:
    run(compose_command("down", "--remove-orphans"))


def compose_ps() -> None:
    run(compose_command("ps"))


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
    repo_dir = repo_dir()
    if repo_dir.exists():
        if not is_git_checkout(repo_dir):
            raise SystemExit(f"{repo_dir} exists but is not a git checkout. Move it aside or remove it before deploying a branch.")
        return
    run(["git", "clone", repo_url, str(repo_dir)])


def repo_origin_url() -> str | None:
    repo_dir = repo_dir()
    if not is_git_checkout(repo_dir):
        return None
    result = run_capture(["git", "remote", "get-url", "origin"], cwd=repo_dir, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def print_repo_summary(repo_url: str) -> None:
    repo_dir = repo_dir()
    if repo_origin_url():
        print(f"Using existing checkout at {repo_dir}.", flush=True)
    else:
        print(f"Cloning repository into {repo_dir}.", flush=True)


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
    repo_dir = repo_dir()
    run(["git", "fetch", "--prune", "origin"], cwd=repo_dir)
    git_assert_remote_branch(branch)
    run(["git", "reset", "--hard"], cwd=repo_dir)
    run(["git", "clean", "-ffdx"], cwd=repo_dir)
    run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=repo_dir)
    run(["git", "reset", "--hard", f"origin/{branch}"], cwd=repo_dir)


def git_prepare_tag(tag: str) -> None:
    repo_dir = repo_dir()
    run(["git", "fetch", "--prune", "--tags", "origin"], cwd=repo_dir)
    if not git_ref_exists(f"refs/tags/{tag}"):
        raise SystemExit(f"Tag '{tag}' does not exist after fetching origin.\nCheck the tag name, or build a branch instead.")
    run(["git", "reset", "--hard"], cwd=repo_dir)
    run(["git", "clean", "-ffdx"], cwd=repo_dir)
    # Detached on purpose: a tag is not a branch, and creating a branch of the
    # same name would make every later `git checkout <name>` ambiguous.
    run(["git", "checkout", "--detach", f"refs/tags/{tag}"], cwd=repo_dir)


def git_refresh_current_branch() -> str:
    branch = repo_current_branch()
    if not branch:
        raise SystemExit("The cached checkout is not on a named branch. Use deploy-branch explicitly if you want to switch branches.")
    repo_dir = repo_dir()
    run(["git", "fetch", "--prune", "origin"], cwd=repo_dir)
    git_assert_remote_branch(branch)
    run(["git", "reset", "--hard", f"origin/{branch}"], cwd=repo_dir)
    return branch


def deploy_branch(branch: str, repo_url: str | None = None) -> str:
    resolved_repo_url = resolve_setting(_repo_url_env_key(), explicit=repo_url, required=True)
    image = local_branch_image(branch)
    ensure_repo_checkout(resolved_repo_url)
    print_repo_summary(resolved_repo_url)
    git_prepare_branch(branch)
    commit = repo_head_commit()
    docker_build(image, repo_dir(), source_revision=commit)
    sync_runtime_compose()
    set_deployment_metadata("branch", branch, image=image, source_commit=commit)
    compose_up()
    print(f"Branch '{branch}' is deployed.", flush=True)
    return image


def deploy_source_tag(tag: str, repo_url: str | None = None) -> str:
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
    compose_up()
    print(f"Tag '{tag}' is built from source and deployed.", flush=True)
    return image


def deploy_current_branch(repo_url: str | None = None) -> tuple[str, str]:
    resolved_repo_url = resolve_setting(_repo_url_env_key(), explicit=repo_url, required=True)
    ensure_repo_checkout(resolved_repo_url)
    print_repo_summary(resolved_repo_url)
    branch = git_refresh_current_branch()
    image = local_branch_image(branch)
    commit = repo_head_commit()
    docker_build(image, repo_dir(), source_revision=commit)
    sync_runtime_compose()
    set_deployment_metadata("branch", branch, image=image, source_commit=commit)
    compose_up()
    print(f"Current checkout branch '{branch}' is deployed.", flush=True)
    return branch, image


def deploy_tag(tag: str, image_repo: str | None = None) -> str:
    resolved_image_repo = resolve_setting(_ghcr_image_env_key(), explicit=image_repo, required=True)
    image = ghcr_tag_image(tag, resolved_image_repo)
    docker_pull(image)
    sync_runtime_compose()
    set_deployment_metadata("tag", tag, image=image)
    compose_up()
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
