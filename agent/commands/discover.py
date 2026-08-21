"""`lab discover` — durable/runtime paths and available commands for this host."""

from __future__ import annotations

import argparse
import json

from .. import registry
from ..config import Config
from ..context import current_hostname
from ..runtime import REPO_ROOT, lab_common


@registry.command(
    "discover",
    help="Show key durable and runtime paths, and available commands, for the current host",
)
def handle_discover(args: argparse.Namespace, config: Config) -> int:
    metadata = lab_common.get_deployment_metadata()
    top_level, groups = registry.grouped_commands()
    available = [cmd.name for cmd in top_level]
    available.extend(cmd.name for subs in groups.values() for cmd in subs)
    payload = {
        "host": current_hostname(),
        "role": lab_common.current_role(),
        "repo_root": str(REPO_ROOT),
        "runtime_dir": str(lab_common.runtime_dir()),
        "runtime_compose_file": str(lab_common.runtime_compose_file()),
        "runtime_env_file": str(lab_common.runtime_env_file()),
        "repo_dir": str(lab_common.REPO_DIR),
        "deployment_metadata": metadata,
        "available_commands": sorted(available),
    }
    print(json.dumps(payload, indent=2))
    return 0
