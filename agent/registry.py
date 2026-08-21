"""Plugin registration front door.

A product lab's own package imports this module and registers its commands,
clients, and analysis/database plugins at import time (before build_parser()
or main() runs). se-lab's own built-in commands register the same way, so
there is exactly one registration mechanism, not a core/plugin split.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .clients.plugin import ClientPlugin
    from .config import Config
    from .analysis.plugin import AnalysisPlugin
    from .database.plugin import DatabasePlugin

CommandHandler = Callable[[argparse.Namespace, "Config"], int]
ParserConfigurer = Callable[[argparse.ArgumentParser], None]


@dataclass(slots=True)
class Command:
    name: str  # top-level ("status") or grouped ("clients status")
    help: str
    handler: CommandHandler
    configure: ParserConfigurer | None = None


_COMMANDS: dict[str, Command] = {}
_CLIENTS: dict[str, type["ClientPlugin"]] = {}
_ANALYSIS_PLUGIN: "AnalysisPlugin | None" = None
_DATABASE_PLUGIN: "DatabasePlugin | None" = None


def command(
    name: str, *, help: str, configure: ParserConfigurer | None = None
) -> Callable[[CommandHandler], CommandHandler]:
    """Register a CLI command. `name` may be grouped, e.g. "clients status"."""

    def register(handler: CommandHandler) -> CommandHandler:
        if name in _COMMANDS:
            existing = _COMMANDS[name].handler
            raise ValueError(
                f"Command {name!r} is already registered "
                f"(by {existing.__module__}.{existing.__qualname__})"
            )
        _COMMANDS[name] = Command(name=name, help=help, handler=handler, configure=configure)
        return handler

    return register


def all_commands() -> list[Command]:
    return sorted(_COMMANDS.values(), key=lambda c: c.name)


def grouped_commands() -> tuple[list[Command], dict[str, list[Command]]]:
    """Split registered commands into top-level and {group: [subcommands]}.

    A command named "clients status" belongs to group "clients" as subcommand
    "status". Used by build_parser() to construct nested subparsers, and by
    `discover` to list available commands without maintaining its own copy.
    """
    top_level: list[Command] = []
    groups: dict[str, list[Command]] = {}
    for cmd in all_commands():
        if " " in cmd.name:
            group, _, _sub = cmd.name.partition(" ")
            groups.setdefault(group, []).append(cmd)
        else:
            top_level.append(cmd)
    return top_level, groups


def dispatch(name: str, args: argparse.Namespace, config: "Config") -> int:
    cmd = _COMMANDS.get(name)
    if cmd is None:
        raise SystemExit(f"Unknown command: {name}")
    return cmd.handler(args, config)


def register_client(cls: type["ClientPlugin"]) -> type["ClientPlugin"]:
    """Class decorator: register a ClientPlugin under its `name` attribute."""
    if not cls.name:
        raise ValueError(f"{cls.__qualname__} must set a non-empty 'name'")
    if cls.name in _CLIENTS:
        raise ValueError(f"Client {cls.name!r} is already registered (by {_CLIENTS[cls.name].__qualname__})")
    _CLIENTS[cls.name] = cls
    return cls


def all_clients() -> dict[str, type["ClientPlugin"]]:
    return dict(_CLIENTS)


def get_client(name: str) -> type["ClientPlugin"]:
    try:
        return _CLIENTS[name]
    except KeyError:
        raise SystemExit(
            f"No client named {name!r} is registered. Known clients: {', '.join(sorted(_CLIENTS)) or '(none)'}"
        ) from None


def set_analysis_plugin(plugin: "AnalysisPlugin") -> None:
    global _ANALYSIS_PLUGIN
    _ANALYSIS_PLUGIN = plugin


def get_analysis_plugin() -> "AnalysisPlugin":
    if _ANALYSIS_PLUGIN is None:
        raise SystemExit(
            "No AnalysisPlugin registered. The product lab must call "
            "registry.set_analysis_plugin(...) at import time before AI analysis commands run."
        )
    return _ANALYSIS_PLUGIN


def set_database_plugin(plugin: "DatabasePlugin") -> None:
    global _DATABASE_PLUGIN
    _DATABASE_PLUGIN = plugin


def get_database_plugin() -> "DatabasePlugin":
    if _DATABASE_PLUGIN is None:
        raise SystemExit(
            "No DatabasePlugin registered, so '--fresh'/'--first-run' database resets are "
            "unavailable. The product lab must call registry.set_database_plugin(...) at "
            "import time to support them."
        )
    return _DATABASE_PLUGIN
