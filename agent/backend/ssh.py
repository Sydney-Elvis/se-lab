from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Sequence

from .base import Backend


class SSHBackend(Backend):
    def __init__(self, host: str, *, repo_root: Path) -> None:
        self.host = host
        self.repo_root = repo_root

    def run(self, command: Sequence[str], *, cwd: Path | None = None, capture: bool = False) -> tuple[int, str]:
        remote_cwd = str(cwd or self.repo_root)
        remote_command = f"cd {shlex.quote(remote_cwd)} && {shlex.join(list(command))}"
        result = subprocess.run(
            ["ssh", self.host, remote_command],
            text=True,
            check=False,
            capture_output=capture,
        )
        output = ""
        if capture:
            output = (result.stdout or "") + (result.stderr or "")
        return result.returncode, output

    def proxy_cli(self, args: Sequence[str]) -> int:
        remote_command = f"cd {shlex.quote(str(self.repo_root))} && python3 scripts/agent.py {shlex.join(list(args))}"
        result = subprocess.run(["ssh", self.host, remote_command], check=False)
        return result.returncode

