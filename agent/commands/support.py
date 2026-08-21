"""Small helpers shared across command modules."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ..config import Config
from ..models.router import RoutedResult, run_task
from ..runtime import lab_common


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_summary_path(from_run: str) -> Path:
    candidate = Path(from_run)
    if candidate.exists():
        return candidate
    direct = lab_common.runtime_run_summary_path(from_run)
    if direct.exists():
        return direct
    matches = [path for path in lab_common.list_run_summary_paths() if path.parent.name.startswith(from_run)]
    if matches:
        return matches[-1]
    raise SystemExit(f"Could not find run summary for {from_run!r}")


def confirm_action(prompt: str, args: argparse.Namespace) -> None:
    if args.yes:
        print("Confirmation: proceeding because --yes was provided.", flush=True)
        return
    if not sys.stdin.isatty():
        print("Confirmation: non-interactive session; proceeding after printed plan.", flush=True)
        return
    response = input(prompt).strip().lower()
    if response not in {"y", "yes"}:
        print("Cancelled.", flush=True)
        raise SystemExit(1)


def run_ad_hoc_model(
    config: Config,
    *,
    alias: str | None,
    endpoint_name: str | None,
    model_name: str | None,
    prompt: str,
) -> RoutedResult:
    """Route one prompt through a specific alias, or an ad-hoc endpoint/model pair.

    Shared by `doctor ai` and `eval ai`, which both need to bypass normal task
    routing to probe one exact model rather than a candidate list.
    """
    if alias:
        config.raw["tasks"]["_ad_hoc"] = {"candidates": [alias], "max_attempts": 1, "min_confidence": 0.0}
        return run_task(config, task_name="_ad_hoc", prompt=prompt, allow_cloud=True, allow_premium=True)
    if not endpoint_name or not model_name:
        raise SystemExit("Specify either a model alias or both an endpoint and a model name.")
    temp_alias = "_ad_hoc_model"
    config.raw["models"][temp_alias] = {"endpoint": endpoint_name, "model_name": model_name}
    config.raw["tasks"]["_ad_hoc"] = {"candidates": [temp_alias], "max_attempts": 1, "min_confidence": 0.0}
    return run_task(config, task_name="_ad_hoc", prompt=prompt, allow_cloud=True, allow_premium=True)
