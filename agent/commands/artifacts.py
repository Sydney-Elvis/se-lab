"""`lab artifacts` — inspect or prune sandbox run artifacts."""

from __future__ import annotations

import argparse
import json
import shutil
from typing import Any

from .. import registry
from ..config import Config
from .. import common as lab_common
from .support import read_json


@registry.command("artifacts latest", help="Print the top-level latest.json artifact record")
def handle_artifacts_latest(args: argparse.Namespace, config: Config) -> int:
    payload = lab_common.read_latest_artifact_record()
    if payload is None:
        print("No latest artifact record is available.", flush=True)
        return 1
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _configure_list(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--last", type=int, default=None)


@registry.command(
    "artifacts list",
    help="List recorded run summaries in the sandbox artifact tree",
    configure=_configure_list,
)
def handle_artifacts_list(args: argparse.Namespace, config: Config) -> int:
    rows: list[dict[str, Any]] = []
    for summary_path in reversed(lab_common.list_run_summary_paths()):
        data = read_json(summary_path)
        metadata = data.get("metadata", {}) if isinstance(data, dict) else {}
        suites = data.get("suites", []) if isinstance(data, dict) else []
        status = "pass"
        if any(isinstance(suite, dict) and suite.get("status") == "fail" for suite in suites):
            status = "fail"
        rows.append({
            "run_id": metadata.get("run_id") or summary_path.parent.name,
            "branch": metadata.get("lab_repo_branch"),
            "started_at_utc": metadata.get("timestamp_utc"),
            "completed_at_utc": metadata.get("completed_at_utc"),
            "duration_seconds": metadata.get("duration_seconds"),
            "status": status,
            "summary_path": str(summary_path),
            "artifacts_dir": metadata.get("sandbox_artifacts_dir") or str(summary_path.parent),
        })
    if args.last:
        rows = rows[: args.last]
    print(json.dumps(rows, indent=2, sort_keys=True))
    return 0


def _configure_prune(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--keep-runs", type=int, default=10)
    parser.add_argument("--prune-checklists", action="store_true")
    parser.add_argument("--purge-metrics", action="store_true")
    parser.add_argument("--yes", action="store_true")


@registry.command(
    "artifacts prune",
    help="Prune sandbox run artifacts and optional checklists",
    configure=_configure_prune,
)
def handle_artifacts_prune(args: argparse.Namespace, config: Config) -> int:
    if not args.yes:
        raise SystemExit("Re-run with --yes to confirm artifact pruning.")
    summaries = list(reversed(lab_common.list_run_summary_paths()))
    keep = max(0, args.keep_runs)
    kept_run_ids = {path.parent.name for path in summaries[:keep]}
    deleted_runs = 0
    for summary_path in summaries[keep:]:
        run_dir = summary_path.parent
        run_id = run_dir.name
        if run_dir.exists():
            shutil.rmtree(run_dir)
            deleted_runs += 1
        for suffix in (".json", ".md", ".sha256", ".failure-context.json"):
            report_path = lab_common.reports_dir() / f"{run_id}{suffix}"
            if report_path.exists():
                report_path.unlink()
    if args.purge_metrics:
        for metrics_name in ("ai-calls.jsonl", "ai-runs.jsonl"):
            metrics_path = lab_common.metrics_dir() / metrics_name
            if metrics_path.exists():
                metrics_path.unlink()
    checklists_deleted = 0
    if args.prune_checklists:
        checklists_root = lab_common.artifacts_checklists_dir()
        if checklists_root.exists():
            for child in checklists_root.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
                checklists_deleted += 1
    latest_payload = {
        "kind": "artifacts-pruned",
        "deleted_runs": deleted_runs,
        "kept_run_ids": sorted(kept_run_ids, reverse=True),
        "checklists_root": str(lab_common.artifacts_checklists_dir()),
        "prune_checklists": bool(args.prune_checklists),
        "checklists_deleted": checklists_deleted,
        "metrics_purged": bool(args.purge_metrics),
    }
    latest_path = lab_common.write_latest_artifact_record(latest_payload)
    print(json.dumps({**latest_payload, "latest_path": str(latest_path)}, indent=2, sort_keys=True))
    return 0
