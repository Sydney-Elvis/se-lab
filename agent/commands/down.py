"""`lab down` — stop the current role's compose stack."""

from __future__ import annotations

import argparse

from .. import registry
from ..config import Config
from .. import common as lab_common


@registry.command("down", help="Stop the current role's compose stack")
def handle_down(args: argparse.Namespace, config: Config) -> int:
    role = lab_common.current_role()
    lab_common.compose_down()
    print(f"{role} compose stack stopped.", flush=True)
    return 0
