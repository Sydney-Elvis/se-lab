"""`lab down` — stop the compose stack."""

from __future__ import annotations

import argparse

from .. import registry
from ..config import Config
from .. import common as lab_common


@registry.command("down", help="Stop the compose stack")
def handle_down(args: argparse.Namespace, config: Config) -> int:
    lab_common.compose_down()
    print("Compose stack stopped.", flush=True)
    return 0
