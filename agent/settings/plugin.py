"""Product-specific settings archive support for lab workflows.

The lab core deliberately owns only the command surface.  A product lab owns
the API calls and archive format because those are tied to its application.
Both interactive lab seeding and automated integration suites call the same
two operations, so the latter can exercise the exact workflow a developer
uses after bringing up a fresh instance.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Literal


SettingsCapability = Literal["unsupported", "settings-only", "full-and-settings"]


class SettingsPlugin(ABC):
    """Export and apply a product's connection/configuration settings.

    Implementations should probe their target application's API in
    :meth:`capability` when support can vary by deployed image or feature
    flag.  Returning ``"unsupported"`` lets the generic commands fail before
    attempting an export or import.
    """

    @abstractmethod
    def export_settings(self, out_path: Path) -> None:
        """Write a settings-scoped archive to ``out_path``."""

    @abstractmethod
    def import_settings(self, archive_path: Path) -> dict:
        """Apply a settings archive and return a summary of what changed."""

    def capability(self) -> SettingsCapability:
        """Return the settings-backup capability available on the target."""
        return "settings-only"

    def default_export_filename(self) -> str:
        """Filename used when ``lab settings export`` has no ``--out`` value.

        Override for a product-specific archive extension, for example
        ``settings-backup.m3undle-backup``.  The name must be a filename, not
        a path, so the core command always keeps default exports under the
        configured artifacts directory.
        """
        return "settings-backup"
