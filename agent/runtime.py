from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
COMMON_DIR = SCRIPTS_DIR / "common"
CONFIG_DIR = REPO_ROOT / "config"

if str(COMMON_DIR) not in sys.path:
    sys.path.insert(0, str(COMMON_DIR))

import common as lab_common  # noqa: E402

REPORTS_DIR = lab_common.runtime_artifacts_root_dir() / "agent"
METRICS_DIR = REPORTS_DIR / "metrics"

