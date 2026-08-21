"""se-lab's CLI entry point.

Deliberately thin: this module owns argument parsing and dispatch only. Every
command's implementation lives in agent/commands/ (se-lab's own built-ins) or
in a product lab's own package (registered the same way — see agent/registry.py).

`status`, `run`, `recreate`, `analyze`, `build`, `pull`, `logs`, and
`checklist` have no se-lab built-in and never will -- each product lab
registers its own, built on the generic helpers in agent/common.py (see
docs/design.md's plugin-interface example). `down` is the one lifecycle verb
se-lab does provide as a built-in (agent/commands/down.py) since "stop the
current compose stack" generalizes; a product lab should not also register a
top-level `down` (registry.command() raises on the duplicate name). For
run/recreate specifically, there's still an unbuilt deploy-target/test-runner
plugin seam -- see .ai_docs/roadmap.md.
"""

from __future__ import annotations

import argparse
import sys

from . import commands, registry  # noqa: F401  (commands import registers se-lab's built-ins)
from .backend.ssh import SSHBackend
from .context import build_context, is_local_server
from .runtime import REPO_ROOT


def _strip_server_args(argv: list[str]) -> list[str]:
    stripped: list[str] = []
    skip_next = False
    for item in argv:
        if skip_next:
            skip_next = False
            continue
        if item == "--server":
            skip_next = True
            continue
        if item.startswith("--server="):
            continue
        stripped.append(item)
    return stripped


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="se-lab: integration test harness runner.")
    parser.add_argument("--server", default=None, help="Run the command on a different host over SSH")
    subparsers = parser.add_subparsers(dest="command", required=True)

    top_level, groups = registry.grouped_commands()
    for cmd in top_level:
        sub = subparsers.add_parser(cmd.name, help=cmd.help)
        if cmd.configure:
            cmd.configure(sub)

    for group_name, subcommands in groups.items():
        group_parser = subparsers.add_parser(group_name, help=f"{group_name} commands")
        group_sub = group_parser.add_subparsers(dest=f"{group_name}_command", required=True)
        for cmd in subcommands:
            _, _, leaf = cmd.name.partition(" ")
            leaf_parser = group_sub.add_parser(leaf, help=cmd.help)
            if cmd.configure:
                cmd.configure(leaf_parser)

    return parser


def _dispatch_name(args: argparse.Namespace) -> str:
    group_dest = f"{args.command}_command"
    sub = getattr(args, group_dest, None)
    return f"{args.command} {sub}" if sub else args.command


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.server and not is_local_server(args.server):
        backend = SSHBackend(args.server, repo_root=REPO_ROOT)
        return backend.proxy_cli(_strip_server_args(argv))

    config = build_context(None).config
    return registry.dispatch(_dispatch_name(args), args, config)


if __name__ == "__main__":
    raise SystemExit(main())
