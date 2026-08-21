#!/usr/bin/env python3
"""Product-lab entry point, invoked by the `lab` shim.

This file has to live in the product lab's own tree, not inside se-lab: its
whole job is locating this checkout's own root, and it can only do that
correctly because it lives at a known, fixed position relative to that root
(<product-lab>/scripts/agent.py). se-lab can't discover the product-lab root
by introspecting itself once se-lab is nested as a submodule at
<product-lab>/se-lab/ -- so this script computes it once and hands it over,
before importing anything else from agent/.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "se-lab"))

from agent.runtime import configure

configure(
    repo_root=REPO_ROOT,
    product_name="CHANGE_ME",  # e.g. "m3undle" -- used for image/project/repo-dir naming
    env_prefix="CHANGE_ME",  # e.g. "M3UNDLE" -- env var prefix your product app itself reads
)

# If your product lab registers its own commands/clients/AnalysisPlugin/
# DatabasePlugin (see se-lab's README and docs/design.md), import that
# package here, before `from agent.cli import main` -- registration has to
# happen before build_parser() runs.
#
# import yourproductlab.commands  # noqa: F401

from agent.cli import main

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Cancelled by user.", flush=True)
        raise SystemExit(130)
