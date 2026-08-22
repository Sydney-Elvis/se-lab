"""`lab settings` — export and apply product settings archives."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .. import common as lab_common
from .. import registry
from ..config import Config
from ..settings.plugin import SettingsPlugin


def _require_supported_plugin() -> SettingsPlugin:
    plugin = registry.get_settings_plugin()
    if plugin.capability() == "unsupported":
        raise SystemExit(
            "Settings backup/import is unavailable on the target instance. "
            "Deploy an image that supports settings archives and try again."
        )
    return plugin


def _default_export_path(plugin: SettingsPlugin) -> Path:
    filename = Path(plugin.default_export_filename())
    if filename.name != str(filename) or filename.name in {"", ".", ".."}:
        raise SystemExit("SettingsPlugin.default_export_filename() must return a filename, not a path.")
    return lab_common.runtime_results_dir() / filename


def _configure_export(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--out",
        type=Path,
        help="Archive output path (defaults to LAB_ARTIFACTS_DIR/settings-backup)",
    )


@registry.command("settings export", help="Export connection and app settings to an archive", configure=_configure_export)
def handle_settings_export(args: argparse.Namespace, config: Config) -> int:
    plugin = _require_supported_plugin()
    out_path = args.out or _default_export_path(plugin)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plugin.export_settings(out_path)
    print(f"Settings archive written to {out_path}", flush=True)
    return 0


def _configure_import(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("path", type=Path, help="Settings archive to apply")


@registry.command(
    "settings import",
    help="Apply connection and app settings from an archive",
    configure=_configure_import,
)
def handle_settings_import(args: argparse.Namespace, config: Config) -> int:
    plugin = _require_supported_plugin()
    archive_path = Path(args.path)
    if not archive_path.is_file():
        raise SystemExit(f"Settings archive does not exist or is not a file: {archive_path}")
    applied = plugin.import_settings(archive_path)
    print(json.dumps(applied, indent=2, sort_keys=True, default=str), flush=True)
    return 0
