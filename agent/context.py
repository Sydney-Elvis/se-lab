from __future__ import annotations

import socket
from dataclasses import dataclass

from .config import Config, load_config
from .runtime import REPO_ROOT, lab_common


def current_hostname() -> str:
    return socket.gethostname().lower()


def is_local_server(target: str | None) -> bool:
    if not target:
        return True
    host = current_hostname()
    target_lower = target.lower()
    return host == target_lower or host.endswith(target_lower)


@dataclass(slots=True)
class AppContext:
    config: Config
    server: str | None

    @property
    def local(self) -> bool:
        return is_local_server(self.server)

    @property
    def hostname(self) -> str:
        return current_hostname()

    @property
    def role(self) -> str:
        return lab_common.current_role()

    @property
    def repo_root(self):
        return REPO_ROOT


def build_context(server: str | None) -> AppContext:
    return AppContext(config=load_config(), server=server)

