from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence

from .base import Backend


class LocalBackend(Backend):
    def run(self, command: Sequence[str], *, cwd: Path | None = None, capture: bool = False) -> tuple[int, str]:
        result = subprocess.run(
            list(command),
            cwd=str(cwd) if cwd else None,
            text=True,
            check=False,
            capture_output=capture,
        )
        output = ""
        if capture:
            output = (result.stdout or "") + (result.stderr or "")
        return result.returncode, output

    def proxy_cli(self, args: Sequence[str]) -> int:
        raise RuntimeError("Local backend does not proxy CLI invocations")

