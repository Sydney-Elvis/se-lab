"""Client app plugin interface.

Modeled on M3Undle's original _CLIENTS dict (image/port config for nextpvr,
jellyfin, emby) plus its update/rollback/pin lifecycle — the audit found that
shape reusable, only the implementation around it was srv2-role-bound.

The generic parts stay in se-lab as commands operating over every registered
ClientPlugin, not per-product code: `clients status/update/rollback/pin` only
need image_env_var/default_image (to pull and pin an image) and
detect_version() (to know what's running and record history) — none of that
requires product-specific logic once a plugin supplies those three things.
Only detect_version() (how to ask *this* client what version it's running —
an HTTP probe, a version file, whatever) is inherently per-client.

`ready()`/`verify()` are a separate concern from the version-management
lifecycle above: they support automated test flows (bring a client up, assert
an expected outcome occurred). A client with no verify() implementation is
manual-only (e.g. Jellyfin for M3Undle) — se-lab falls back to checklist
generation for those instead of failing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class VerifyResult:
    passed: bool
    detail: str


class ClientPlugin(ABC):
    name: str
    compose_service: str
    image_env_var: str
    default_image: str

    @abstractmethod
    def detect_version(self) -> str | None:
        """Best-effort detection of the version currently running, or None."""

    def ready(self) -> bool:
        """Default: healthy once detect_version() returns something.

        Override when "ready" and "has a detectable version" diverge.
        """
        return self.detect_version() is not None

    def verify(self, context: Any) -> VerifyResult:
        """Assert the expected test outcome occurred for this client.

        Not implemented by default — se-lab treats an unimplemented verify()
        as "manual-only" and falls back to checklist generation.
        """
        raise NotImplementedError(f"{self.name} has no automated verify(); manual-only client.")
