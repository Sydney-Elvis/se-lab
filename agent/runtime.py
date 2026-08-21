from __future__ import annotations

from pathlib import Path


def _paths_for(repo_root: Path) -> tuple[Path, Path]:
    return repo_root / "scripts", repo_root / "config"


# Defaults: se-lab's own root. Correct for se-lab's own self-tests and any
# standalone use where there is no separate product lab.
REPO_ROOT: Path = Path(__file__).resolve().parents[1]
SCRIPTS_DIR: Path
CONFIG_DIR: Path
SCRIPTS_DIR, CONFIG_DIR = _paths_for(REPO_ROOT)
PRODUCT_NAME: str | None = None
ENV_PREFIX: str | None = None


def configure(*, repo_root: Path, product_name: str, env_prefix: str) -> None:
    """One-time hand-off from the product lab's entry script to se-lab.

    se-lab is meant to be consumed nested as a submodule (<product-lab>/se-lab/),
    so this module's own __file__ tells you where se-lab sits, not where the
    product lab's config/, scripts/, lab.env live -- those are one level up, at
    the root of whichever repo checked se-lab out, and product_name/env_prefix
    (e.g. "m3undle"/"M3UNDLE") aren't derivable from anywhere in se-lab at all.
    There's no reliable way for se-lab to find any of this by introspecting
    itself; the product lab's own entry script (scripts/agent.py, invoked by
    the `lab` shim) already knows repo_root from its own location, so it hands
    everything here once at startup, before importing anything else from
    agent/. This is a one-time hand-off, not a per-call override -- none of
    these values change during a run.

    Every consumer must read these as module attributes (`runtime.REPO_ROOT`),
    never via `from .runtime import REPO_ROOT` -- that copies today's value
    into the importing module's own namespace and won't see this update.
    """
    global REPO_ROOT, SCRIPTS_DIR, CONFIG_DIR, PRODUCT_NAME, ENV_PREFIX
    REPO_ROOT = repo_root
    SCRIPTS_DIR, CONFIG_DIR = _paths_for(REPO_ROOT)
    PRODUCT_NAME = product_name
    ENV_PREFIX = env_prefix


def require_product_config() -> tuple[str, str]:
    """PRODUCT_NAME/ENV_PREFIX, or a clear error naming the missing configure() call."""
    if PRODUCT_NAME is None or ENV_PREFIX is None:
        raise SystemExit(
            "agent.runtime.configure(product_name=..., env_prefix=...) was never called. "
            "The product lab's entry script must call it before running any lab command."
        )
    return PRODUCT_NAME, ENV_PREFIX
