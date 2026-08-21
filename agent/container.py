"""Container lifecycle helpers for integration test scripts.

Ported from m3undle-lab's scripts/common/harness/container.py (294 lines) --
"already generic" per the original roadmap plan, which undersold it the same
way the plan undersold common.py and cli.py before their own audits. Most of
what it did duplicates agent/common.py (compose up/down, ensure_layout,
sync_runtime_compose, image building/pulling) -- that overlap isn't ported
again here. What's left, generalized:

  - resolve_image(): branch/tag/explicit dispatch, now reading
    runtime.PRODUCT_NAME/ENV_PREFIX instead of hardcoded M3UNDLE_*.
  - container_status/container_started_at/wait_for_restart/wait_up/
    wait_http_listener: all took the container name ("m3undle") as a hidden
    assumption; now an explicit parameter. wait_up/wait_http_listener also
    hardcoded M3Undle's specific health-check paths (/health/live, /health);
    now a caller-supplied list, since se-lab has no way to know a product's
    health endpoint shape.
  - reset_database(): replaced reset_srv1_database()'s hardcoded
    srv1-only + SQLite-filename check with registry.get_database_plugin()
    -- the same DatabasePlugin every product lab already has to register for
    `lab recreate --fresh`, not a second product-specific hook.
  - get_docker_gateway(): already fully generic, ported as-is.

start()/stop() aren't ported: they were a thin combination of
common.set_current_image() + common.compose_up()/compose_down(), now
directly available with the same extra_compose_files support (added to
common.compose_up() for exactly this "test-mode override file" use case) --
a second wrapper here would just be one more name for the same call.
"""

from __future__ import annotations

import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Sequence

from . import common as lab_common, registry, runtime


def resolve_image(*, branch: str | None = None, tag: str | None = None, image: str | None = None) -> str:
    """Resolve a Docker image from exactly one of: branch, tag, or image string.

    branch -> clones/updates the repo, builds a local image
    tag    -> pulls the GHCR-tagged image (requires <ENV_PREFIX>_GHCR_IMAGE)
    image  -> uses the string directly, no build or pull
    """
    provided = [x for x in (branch, tag, image) if x]
    if len(provided) != 1:
        raise ValueError("Exactly one of branch, tag, or image must be provided")

    _, env_prefix = runtime.require_product_config()

    if branch:
        repo_url = lab_common.resolve_setting(f"{env_prefix}_REPO_URL", required=True)
        lab_common.ensure_repo_checkout(repo_url)
        lab_common.git_prepare_branch(branch)
        img = lab_common.local_branch_image(branch)
        lab_common.docker_build(img, lab_common.repo_dir())
        return img

    if tag:
        base = lab_common.resolve_setting(f"{env_prefix}_GHCR_IMAGE", required=True)
        img = lab_common.ghcr_tag_image(tag, base)
        lab_common.docker_pull(img)
        return img

    return image  # explicit image string


def reset_database(service: str) -> None:
    """Stop the stack, reset the database via the registered DatabasePlugin,
    then bring just `service` back up.

    Dependent services stay down until a profile is reconfigured, matching
    the state a fresh reset leaves things in.
    """
    lab_common.compose_down()
    registry.get_database_plugin().reset()
    lab_common.compose_up_only(service)


def get_docker_gateway(network_name: str | None = None) -> str | None:
    """Gateway IP of the lab's Docker bridge network, or None if not found.

    Containers can reach the host (and services bound to 0.0.0.0 on it) via
    this IP.
    """
    net = network_name or f"{lab_common.project_name()}_default"
    try:
        result = subprocess.run(
            ["docker", "network", "inspect", net, "--format", "{{range .IPAM.Config}}{{.Gateway}}{{end}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() or None
    except Exception:
        return None


def container_status(container_name: str) -> str:
    """Docker status string for `container_name`, or 'missing'/'unknown'."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Status}}", container_name],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() or "missing"
    except Exception:
        return "unknown"


def container_started_at(container_name: str) -> str | None:
    """Docker's start timestamp for `container_name`."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.StartedAt}}", container_name],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() or None
    except Exception:
        return None


def wait_for_restart(container_name: str, previous_started_at: str | None, *, timeout: float = 120.0) -> bool:
    """Wait until Docker reports a new start timestamp for `container_name`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = container_started_at(container_name)
        if current and previous_started_at and current != previous_started_at:
            return True
        time.sleep(1.0)
    return False


def wait_up(base_url: str, container_name: str, *, health_paths: Sequence[str] = ("/health",), timeout: float = 120.0) -> bool:
    """Poll `health_paths` until one responds 200 or timeout elapses.

    Fails fast if the container exits unexpectedly. Prints a status line
    every 10 seconds so progress is visible. Tries each path in order on
    every poll, so a product with a fallback health path (e.g. a newer
    sub-endpoint plus an older one) can pass all of them.
    """
    base = base_url.rstrip("/")
    urls = [f"{base}{path}" for path in health_paths]
    deadline = time.monotonic() + timeout
    last_error = ""
    next_status_print = time.monotonic() + 10.0

    while time.monotonic() < deadline:
        for probe in urls:
            try:
                with urllib.request.urlopen(probe, timeout=3.0) as resp:
                    if resp.status == 200:
                        return True
                    last_error = f"HTTP {resp.status} from {probe}"
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    last_error = f"HTTP {exc.code} from {probe}"
            except Exception as exc:
                last_error = str(exc)

        status = container_status(container_name)
        if status in ("exited", "dead", "missing"):
            print(f"\n  Container exited prematurely (status={status}). Run: docker logs {container_name}", flush=True)
            return False

        if time.monotonic() >= next_status_print:
            elapsed = timeout - (deadline - time.monotonic())
            print(
                f"  Still waiting for {container_name} ({lab_common.format_duration(elapsed)} elapsed,"
                f" container={status}, last_error={last_error!r})...",
                flush=True,
            )
            next_status_print = time.monotonic() + 10.0

        time.sleep(2.0)

    print(f"  Timed out after {timeout:.0f}s. Last error: {last_error}", flush=True)
    return False


def wait_http_listener(base_url: str, container_name: str, *, timeout: float = 60.0) -> bool:
    """Wait for the web listener even when the app isn't ready to serve real requests yet."""
    url = f"{base_url.rstrip('/')}/"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3.0) as resp:
                if 100 <= resp.status < 500:
                    return True
        except urllib.error.HTTPError as exc:
            if 100 <= exc.code < 500:
                return True
        except Exception:
            pass
        if container_status(container_name) in ("exited", "dead", "missing"):
            return False
        time.sleep(1.0)
    return False
