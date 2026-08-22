"""se-lab's own self-tests.

configure() has to run before any other agent.* module is imported (see
agent/runtime.py) -- for a real product lab that's naturally true, since
scripts/agent.py calls it before `from agent.cli import main`. Here, pytest
imports conftest.py before collecting any test module, so calling configure()
in pytest_configure() (not a fixture -- fixtures run per-test, too late)
keeps the same ordering guarantee.

All self-tests share one fixture "product lab" root for the whole session:
a scratch temp directory standing in for what would normally be a real
product lab's checkout, with its own docker-config/docker-compose.yaml
so compose-dependent functions have a template to find. <ENV_PREFIX>_RUNTIME_DIR
is pointed inside that same temp directory (not the sibling default) so
everything this session touches stays under one directory, removed at the end.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PRODUCT_NAME = "selftest"
ENV_PREFIX = "SELFTEST"

_scratch_root: Path | None = None


def pytest_configure(config: pytest.Config) -> None:
    global _scratch_root
    _scratch_root = Path(tempfile.mkdtemp(prefix="se-lab-selftest-"))

    docker_config = _scratch_root / "docker-config"
    docker_config.mkdir(parents=True)
    (docker_config / "docker-compose.yaml").write_text(
        "services:\n  noop:\n    image: alpine:latest\n    command: [\"true\"]\n",
        encoding="utf-8",
    )

    os.environ[f"{ENV_PREFIX}_RUNTIME_DIR"] = str(_scratch_root / "runtime")

    from agent.runtime import configure

    configure(repo_root=_scratch_root, product_name=PRODUCT_NAME, env_prefix=ENV_PREFIX)


def pytest_unconfigure(config: pytest.Config) -> None:
    if _scratch_root is not None and _scratch_root.exists():
        shutil.rmtree(_scratch_root, ignore_errors=True)


@pytest.fixture
def scratch_root() -> Path:
    assert _scratch_root is not None
    return _scratch_root


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Snapshot agent.registry's mutable state before each test, restore after.

    Not a clear-to-empty: se-lab's own built-in commands (discover, down,
    artifacts, ...) register themselves exactly once, at agent.commands'
    first import -- whichever test happens to run first. A blanket clear
    would permanently wipe them for every test after that one. Snapshotting
    means each test starts from whatever was there before it ran, and a
    test's own fakes never leak into the next test either way.
    """
    from agent import registry

    commands_before = dict(registry._COMMANDS)
    clients_before = dict(registry._CLIENTS)
    analysis_before = registry._ANALYSIS_PLUGIN
    database_before = registry._DATABASE_PLUGIN
    settings_before = registry._SETTINGS_PLUGIN
    layout_before = registry._LAYOUT_HOOK

    yield

    registry._COMMANDS.clear()
    registry._COMMANDS.update(commands_before)
    registry._CLIENTS.clear()
    registry._CLIENTS.update(clients_before)
    registry._ANALYSIS_PLUGIN = analysis_before
    registry._DATABASE_PLUGIN = database_before
    registry._SETTINGS_PLUGIN = settings_before
    registry._LAYOUT_HOOK = layout_before


@pytest.fixture
def has_docker() -> bool:
    return shutil.which("docker") is not None
