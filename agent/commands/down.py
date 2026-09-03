"""`lab down` — stop every Compose project for this product lab."""

from __future__ import annotations

import argparse

from .. import registry
from ..config import Config
from .. import common as lab_common

HELP = (
    "Stop every Compose project for this product lab: the main stack plus any "
    "scenario-run or leftover project Docker still knows about, matched by name "
    "(no docker-config/docker-compose.yaml required). Named volumes are kept by "
    "default -- pass --clean-volumes to also wipe them."
)


def _configure_down(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--clean-volumes",
        action="store_true",
        help="Also remove named volumes (e.g. database data) for every project torn down",
    )


@registry.command("down", help=HELP, configure=_configure_down)
def handle_down(args: argparse.Namespace, config: Config) -> int:
    names = lab_common.compose_down_all(remove_volumes=args.clean_volumes)
    if not names:
        print(f"No Compose projects found for {lab_common.project_name()!r}.", flush=True)
        return 0
    print(f"Stopped {len(names)} Compose project(s): {', '.join(names)}", flush=True)
    if not args.clean_volumes:
        print("Volumes kept -- rerun with --clean-volumes to also remove them.", flush=True)
    return 0
