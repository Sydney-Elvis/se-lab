"""`lab clients` — manage third-party client app versions.

Fully generic: every command here operates over the ClientPlugin registry
via image_env_var/default_image/detect_version() (see
agent/clients/plugin.py). A product lab only implements and registers
ClientPlugin subclasses — none of the lifecycle below is per-product code.

The original M3Undle implementation this replaces restricted these commands
to a single hardcoded role ("srv2"). Dropped: which role(s) host client apps
is a per-product decision se-lab has no business assuming a name for: these
commands act on whatever clients `lab_common.active_clients()` reports as
active for the current deployment, on any role.
"""

from __future__ import annotations

import argparse
import time
from datetime import UTC, datetime

from .. import registry
from ..clients.plugin import ClientPlugin
from ..config import Config
from .. import common as lab_common
from .support import confirm_action


def _detect_version_with_retry(plugin: ClientPlugin, *, retries: int = 8, delay: float = 2.0) -> str | None:
    """Retry version detection after a container restart while the service warms up."""
    for attempt in range(retries):
        version = plugin.detect_version()
        if version:
            return version
        if attempt < retries - 1:
            time.sleep(delay)
    return None


def _wait_ready(plugin: ClientPlugin, *, retries: int = 8, delay: float = 2.0) -> bool:
    """Retry plugin.ready() while a just-started container warms up."""
    for attempt in range(retries):
        if plugin.ready():
            return True
        if attempt < retries - 1:
            time.sleep(delay)
    return False


def _require_client_compose_files() -> tuple:
    files = registry.client_compose_files()
    if not files:
        raise SystemExit(
            "No client compose files registered. The product lab must call "
            "registry.set_client_compose_files([...]) at import time before "
            "'clients up/down/reset' can run."
        )
    return files


def _current_image(plugin: ClientPlugin) -> str:
    return lab_common.get_runtime_env_value(plugin.image_env_var) or plugin.default_image


def _snapshot_client(plugin: ClientPlugin) -> dict:
    image = _current_image(plugin)
    return {
        "client": plugin.name,
        "image": image,
        "digest": lab_common.get_image_repo_digest(image),
        "version": plugin.detect_version(),
        "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _pin_client_image(plugin: ClientPlugin, image: str) -> None:
    lab_common.set_runtime_env_values({plugin.image_env_var: image})
    lab_common.set_lab_env_values({plugin.image_env_var: image})


@registry.command("clients status", help="Show current version and rollback history for each active client")
def handle_clients_status(args: argparse.Namespace, config: Config) -> int:
    active = lab_common.active_clients()
    history = lab_common.read_client_version_history()

    if not active:
        print("No client apps active in COMPOSE_PROFILES.", flush=True)
        return 0

    print(flush=True)
    for name in active:
        plugin = registry.get_client(name)()
        image = _current_image(plugin)
        version = plugin.detect_version() or "unknown"
        digest = lab_common.get_image_repo_digest(image) or "no remote digest"
        entries = history.get(name, [])
        prev = entries[1] if len(entries) > 1 else None
        print(f"  {name}")
        print(f"    image:    {image}")
        print(f"    version:  {version}")
        print(f"    digest:   {digest}")
        if prev:
            prev_ver = prev.get("version") or "unknown"
            prev_img = prev.get("image", "")
            prev_digest = prev.get("digest", "")
            print(f"    previous: {prev_ver}  ({prev_img}@{prev_digest.split('@')[-1] if '@' in (prev_digest or '') else prev_digest})")
        else:
            print("    previous: none recorded — run 'lab clients update' to start tracking")
        print(flush=True)
    return 0


def _configure_update(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("client", nargs="?", default=None, help="Specific client to update (omit for all active)")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")


@registry.command(
    "clients update",
    help="Pull latest image for all (or one) active client(s) and restart",
    configure=_configure_update,
)
def handle_clients_update(args: argparse.Namespace, config: Config) -> int:
    if args.client:
        registry.get_client(args.client)  # validates the name
        targets = [args.client]
    else:
        targets = lab_common.active_clients()
    if not targets:
        raise SystemExit("No client apps active in COMPOSE_PROFILES.")

    print("\nClients Update Plan")
    print(f"  Targets: {', '.join(targets)}")
    print("  Action:  pull latest image for each, restart only affected containers")
    print(flush=True)
    confirm_action("Continue? [y/N] ", args)

    changed: list[str] = []
    for name in targets:
        plugin = registry.get_client(name)()
        image = plugin.default_image

        print(f"\n[{name}] Snapshotting current state ...", flush=True)
        before = _snapshot_client(plugin)
        before_digest = before.get("digest")
        before_version = before.get("version") or "unknown"

        print(f"[{name}] Pulling {image} ...", flush=True)
        lab_common.docker_pull(image)

        after_digest = lab_common.get_image_repo_digest(image)

        if before_digest and after_digest and before_digest == after_digest:
            print(f"[{name}] No change (already at latest).", flush=True)
            continue

        # Record the before snapshot so rollback is possible, then restart
        lab_common.push_client_version_record(name, before)

        print(f"[{name}] New image detected — restarting container ...", flush=True)
        _pin_client_image(plugin, image)
        lab_common.compose_up_service(plugin.compose_service)

        after_version = _detect_version_with_retry(plugin) or "unknown"
        after_record = {
            "client": name,
            "image": image,
            "digest": after_digest,
            "version": after_version,
            "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        lab_common.push_client_version_record(name, after_record)

        print(f"[{name}] Updated: {before_version} → {after_version}", flush=True)
        changed.append(name)

    print(flush=True)
    if changed:
        print(f"Updated: {', '.join(changed)}", flush=True)
    else:
        print("All clients already at latest — no changes made.", flush=True)
    return 0


def _configure_rollback(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("client", help="Client to roll back")
    parser.add_argument("--clean", action="store_true", help="Wipe and reseed scenario data before starting")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")


@registry.command(
    "clients rollback",
    help="Revert a client to its previously recorded version",
    configure=_configure_rollback,
)
def handle_clients_rollback(args: argparse.Namespace, config: Config) -> int:
    plugin = registry.get_client(args.client)()
    history = lab_common.read_client_version_history()
    entries = history.get(args.client, [])

    if len(entries) < 2:
        raise SystemExit(
            f"No previous version recorded for {args.client}. "
            "Run 'lab clients update' first to establish version history."
        )

    current = entries[0]
    previous = entries[1]
    rollback_image = previous.get("digest") or previous.get("image") or plugin.default_image
    rollback_version = previous.get("version") or "unknown"
    current_version = current.get("version") or "unknown"

    print("\nClients Rollback Plan")
    print(f"  Client:   {args.client}")
    print(f"  Current:  {current_version}  ({current.get('image', '')})")
    print(f"  Rollback: {rollback_version}  ({rollback_image})")
    if args.clean:
        print("  Clean:    yes — scenario data will be wiped and reseeded")
    print(flush=True)
    confirm_action("Continue with rollback? [y/N] ", args)

    if args.clean:
        print(f"Wiping and reseeding {args.client} scenario data ...", flush=True)
        plugin.reset_scenario_data()

    print(f"Pulling {rollback_image} ...", flush=True)
    lab_common.docker_pull(rollback_image)

    print(f"Restarting {args.client} ...", flush=True)
    _pin_client_image(plugin, rollback_image)
    lab_common.compose_up_service(plugin.compose_service)

    rollback_record = {
        "client": args.client,
        "image": rollback_image,
        "digest": previous.get("digest"),
        "version": rollback_version,
        "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    lab_common.push_client_version_record(args.client, rollback_record)

    print(f"\n{args.client} rolled back to {rollback_version}.", flush=True)
    return 0


def _configure_pin(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("client", help="Client to pin")
    parser.add_argument("image", help="Full image spec, e.g. repo/image@sha256:... or repo/image:tag")
    parser.add_argument("--clean", action="store_true", help="Wipe and reseed scenario data before starting")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")


@registry.command(
    "clients pin",
    help="Pin a client to a specific image and restart",
    configure=_configure_pin,
)
def handle_clients_pin(args: argparse.Namespace, config: Config) -> int:
    plugin = registry.get_client(args.client)()
    image = args.image
    current_version = plugin.detect_version() or "unknown"
    current_image = _current_image(plugin)

    print("\nClients Pin Plan")
    print(f"  Client:       {args.client}")
    print(f"  Current:      {current_image} ({current_version})")
    print(f"  Pin to:       {image}")
    if args.clean:
        print("  Clean:        yes — scenario data will be wiped and reseeded")
    print(flush=True)
    confirm_action("Continue? [y/N] ", args)

    before = _snapshot_client(plugin)
    lab_common.push_client_version_record(args.client, before)

    if args.clean:
        print(f"Wiping and reseeding {args.client} scenario data ...", flush=True)
        plugin.reset_scenario_data()

    print(f"Pulling {image} ...", flush=True)
    lab_common.docker_pull(image)

    print(f"Restarting {args.client} ...", flush=True)
    _pin_client_image(plugin, image)
    lab_common.compose_up_service(plugin.compose_service)

    after_version = _detect_version_with_retry(plugin) or "unknown"
    after_record = {
        "client": args.client,
        "image": image,
        "digest": lab_common.get_image_repo_digest(image),
        "version": after_version,
        "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    lab_common.push_client_version_record(args.client, after_record)

    print(f"\n{args.client} pinned to {image} ({after_version}).", flush=True)
    return 0


def _configure_up(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profile",
        nargs="+",
        metavar="CLIENT",
        default=None,
        help="Client(s) to bring up (default: every registered client)",
    )


@registry.command(
    "clients up",
    help="Bring up the product stack plus the selected client app(s)",
    configure=_configure_up,
)
def handle_clients_up(args: argparse.Namespace, config: Config) -> int:
    known = sorted(registry.all_clients())
    if not known:
        raise SystemExit("No ClientPlugin registered for this product lab.")

    targets = args.profile or known
    for name in targets:
        registry.get_client(name)  # validates

    extra_files = _require_client_compose_files()
    print(f"Bringing up stack with client profiles: {', '.join(targets)}", flush=True)
    lab_common.set_runtime_env_values({"COMPOSE_PROFILES": ",".join(targets)})
    lab_common.compose_up(extra_compose_files=extra_files)

    print(flush=True)
    for name in targets:
        plugin = registry.get_client(name)()
        if _wait_ready(plugin):
            print(f"  {name}: ready", flush=True)
        else:
            print(f"  {name}: not ready yet — check 'lab clients status' or the container logs", flush=True)
    return 0


@registry.command("clients down", help="Stop and remove the client app(s), leaving the product stack running")
def handle_clients_down(args: argparse.Namespace, config: Config) -> int:
    extra_files = _require_client_compose_files()
    active = lab_common.active_clients()
    if not active:
        print("No client apps active — nothing to stop.", flush=True)
        return 0

    print(f"Stopping client profiles: {', '.join(active)}", flush=True)
    lab_common.set_runtime_env_values({"COMPOSE_PROFILES": ""})
    lab_common.compose_up(extra_compose_files=extra_files)
    print("Client apps stopped and removed; product stack still running.", flush=True)
    return 0


def _configure_reset(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profile",
        nargs="+",
        metavar="CLIENT",
        default=None,
        help="Client(s) to reset (default: currently active)",
    )
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")


@registry.command(
    "clients reset",
    help="Wipe client app state and bring the client(s) back up clean",
    configure=_configure_reset,
)
def handle_clients_reset(args: argparse.Namespace, config: Config) -> int:
    targets = args.profile or lab_common.active_clients()
    if not targets:
        raise SystemExit("No client apps active or specified to reset.")
    for name in targets:
        registry.get_client(name)  # validates

    extra_files = _require_client_compose_files()

    print("\nClients Reset Plan")
    print(f"  Targets: {', '.join(targets)}")
    print("  Action:  wipe scenario data, recreate containers clean")
    print(flush=True)
    confirm_action("Continue? [y/N] ", args)

    for name in targets:
        plugin = registry.get_client(name)()
        print(f"[{name}] Wiping and reseeding scenario data ...", flush=True)
        plugin.reset_scenario_data()

    print(f"Recreating stack with client profiles: {', '.join(targets)}", flush=True)
    lab_common.set_runtime_env_values({"COMPOSE_PROFILES": ",".join(targets)})
    lab_common.compose_up(extra_compose_files=extra_files)

    print(flush=True)
    for name in targets:
        plugin = registry.get_client(name)()
        if _wait_ready(plugin):
            print(f"  {name}: ready", flush=True)
        else:
            print(f"  {name}: not ready yet — check 'lab clients status' or the container logs", flush=True)
    return 0
