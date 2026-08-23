"""Plugin registration front door.

A product lab's own package imports this module and registers its commands,
clients, and analysis/database plugins at import time (before build_parser()
or main() runs). se-lab's own built-in commands register the same way, so
there is exactly one registration mechanism, not a core/plugin split.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Sequence

if TYPE_CHECKING:
    from .clients.plugin import ClientPlugin
    from .config import Config
    from .analysis.plugin import AnalysisPlugin
    from .database.plugin import DatabasePlugin
    from .settings.plugin import SettingsPlugin

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
_SETTINGS_PLUGIN: "SettingsPlugin | None" = None
_LAYOUT_HOOK: Callable[[], None] | None = None
_CLIENT_COMPOSE_FILES: tuple[Path, ...] = ()


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


def set_client_compose_files(files: Sequence[Path]) -> None:
    """Extra compose file(s) `clients up/down/reset` layer on top of the base stack --
    typically a network-topology override plus the client services themselves.

    Optional, default empty, same shape as set_layout_hook(): se-lab has no way to
    know a product's client-app compose topology, so a product lab registers it once
    and these generic commands use it without knowing what's inside.
    """
    global _CLIENT_COMPOSE_FILES
    _CLIENT_COMPOSE_FILES = tuple(files)


def client_compose_files() -> tuple[Path, ...]:
    return _CLIENT_COMPOSE_FILES


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


def set_layout_hook(hook: Callable[[], None]) -> None:
    """Register extra bootstrap work to run at the end of common.ensure_layout().

    Optional, default no-op. Backs whatever a product needs before its stack
    can come up that isn't a generic directory (secrets, scenario fixtures,
    per-client state repair) -- se-lab's ensure_layout() runs this without
    knowing what it does, the same way it always ran unconditionally at the
    top of every script in the original single-repo lab.
    """
    global _LAYOUT_HOOK
    _LAYOUT_HOOK = hook


def run_layout_hook() -> None:
    if _LAYOUT_HOOK is not None:
        _LAYOUT_HOOK()


def get_database_plugin() -> "DatabasePlugin":
    if _DATABASE_PLUGIN is None:
        raise SystemExit(
            "No DatabasePlugin registered, so '--fresh'/'--first-run' database resets are "
            "unavailable. The product lab must call registry.set_database_plugin(...) at "
            "import time to support them."
        )
    return _DATABASE_PLUGIN


def set_settings_plugin(plugin: "SettingsPlugin") -> None:
    """Register the product's settings archive implementation."""
    global _SETTINGS_PLUGIN
    _SETTINGS_PLUGIN = plugin


def get_settings_plugin() -> "SettingsPlugin":
    if _SETTINGS_PLUGIN is None:
        raise SystemExit(
            "No SettingsPlugin registered. The product lab must call "
            "registry.set_settings_plugin(...) at import time before 'lab settings' commands run."
        )
    return _SETTINGS_PLUGIN
