from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Sequence


class Backend(ABC):
    @abstractmethod
    def run(self, command: Sequence[str], *, cwd: Path | None = None, capture: bool = False) -> tuple[int, str]:
        raise NotImplementedError

    @abstractmethod
    def proxy_cli(self, args: Sequence[str]) -> int:
        raise NotImplementedError

