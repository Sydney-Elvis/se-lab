"""`lab report` — render run reports or AI metrics."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from .. import registry
from ..config import Config
from ..reporting.metrics import read_jsonl
from .. import common as lab_common
from .support import read_json, resolve_summary_path


def _configure_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--from-run", required=True)


@registry.command("report run", help="Print a run summary JSON", configure=_configure_run)
def handle_report_run(args: argparse.Namespace, config: Config) -> int:
    summary_path = resolve_summary_path(args.from_run)
    print(summary_path.read_text(encoding="utf-8"))
    return 0


def _int_value(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _normalize_latest_run(
    latest: dict[str, Any],
    summary: dict[str, Any],
    agent_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = summary.get("metadata") if isinstance(summary.get("metadata"), dict) else {}
    suites = summary.get("suites") if isinstance(summary.get("suites"), list) else []
    suite_rows = [suite for suite in suites if isinstance(suite, dict)]
    agent = agent_report or {}

    passed = sum(_int_value(suite.get("passed")) for suite in suite_rows)
    failed = sum(_int_value(suite.get("failed")) for suite in suite_rows)
    skipped = sum(_int_value(suite.get("skipped")) for suite in suite_rows)
    status = str(latest.get("status") or ("fail" if failed else "pass")).upper()
    source_type = agent.get("source_type") or latest.get("source_type")
    source_ref = agent.get("source_ref") or latest.get("source_ref")
    if not source_type and metadata.get("lab_repo_branch"):
        source_type = "branch"
    if not source_ref:
        source_ref = metadata.get("lab_repo_branch")

    return {
        "schema_version": latest.get("schema_version") or 1,
        "run_id": latest.get("run_id") or metadata.get("run_id"),
        "status": status,
        "role": latest.get("role") or metadata.get("lab_role"),
        "host": latest.get("host") or metadata.get("hostname"),
        "source_type": source_type,
        "source_ref": source_ref,
        "product_commit": agent.get("deployment_commit") or latest.get("product_commit"),
        "lab_commit": agent.get("lab_repo_commit") or latest.get("lab_commit") or metadata.get("lab_repo_commit"),
        "started_at_utc": metadata.get("timestamp_utc") or agent.get("started_at_utc") or latest.get("started_at_utc"),
        "completed_at_utc": metadata.get("completed_at_utc") or agent.get("completed_at_utc") or latest.get("completed_at_utc"),
        "duration_seconds": metadata.get("duration_seconds") or agent.get("duration_seconds") or latest.get("duration_seconds"),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "suite_count": len(suite_rows),
        "summary_path": latest.get("summary_path") or agent.get("repo_summary_path"),
        "artifacts_dir": latest.get("artifacts_dir") or agent.get("artifacts_path"),
        "agent_report_json": latest.get("agent_report_json"),
        "agent_report_md": latest.get("agent_report_md"),
        "failure_context_path": latest.get("failure_context_path") or agent.get("failure_context_path"),
        "suites": [
            {
                "suite": suite.get("suite"),
                "status": str(suite.get("status") or "unknown").upper(),
                "passed": _int_value(suite.get("passed")),
                "failed": _int_value(suite.get("failed")),
                "skipped": _int_value(suite.get("skipped")),
                "elapsed_seconds": suite.get("elapsed_seconds"),
            }
            for suite in suite_rows
        ],
    }


def load_latest_run_view() -> tuple[dict[str, Any] | None, str | None]:
    """Load and normalize the latest recorded run. Reused by `lab status`."""
    latest_path = lab_common.latest_artifact_path()
    latest = lab_common.read_latest_artifact_record()
    if latest is None:
        if latest_path.exists():
            return None, f"Latest artifact pointer is malformed: {latest_path}"
        return None, "No recorded lab run."
    if latest.get("kind") not in {"run-summary", "agent-report"}:
        return None, f"Latest artifact record is not a lab run: {latest.get('kind') or 'unknown'}"

    summary_value = latest.get("summary_path")
    if not isinstance(summary_value, str) or not summary_value:
        return None, "Latest artifact record does not reference a run summary."
    summary_path = Path(summary_value)
    try:
        summary = read_json(summary_path)
    except (OSError, json.JSONDecodeError):
        return None, f"Latest run summary is unreadable: {summary_path}"
    if not isinstance(summary, dict):
        return None, f"Latest run summary is malformed: {summary_path}"

    agent_report: dict[str, Any] | None = None
    agent_path_value = latest.get("agent_report_json")
    if isinstance(agent_path_value, str) and agent_path_value:
        try:
            loaded_agent = read_json(Path(agent_path_value))
            if isinstance(loaded_agent, dict):
                agent_report = loaded_agent
        except (OSError, json.JSONDecodeError):
            agent_report = None
    return _normalize_latest_run(latest, summary, agent_report), None


def render_latest_run(view: dict[str, Any], *, include_suites: bool) -> str:
    """Reused by `lab status`."""
    source_type = view.get("source_type") or "unknown"
    source_ref = view.get("source_ref") or "unknown"
    lines = [
        "Latest lab run",
        f"  Result:     {view.get('status') or 'UNKNOWN'}",
        f"  Run:        {view.get('run_id') or 'unknown'}",
        f"  Source:     {source_type} {source_ref}",
        f"  Product:    {view.get('product_commit') or 'unknown'}",
        f"  Lab commit: {view.get('lab_commit') or 'unknown'}",
        f"  Completed:  {view.get('completed_at_utc') or 'unknown'}",
        (
            "  Tests:      "
            f"{view.get('passed', 0)} passed, "
            f"{view.get('failed', 0)} failed, "
            f"{view.get('skipped', 0)} skipped"
        ),
        f"  Duration:   {lab_common.format_duration(view.get('duration_seconds'))}",
        f"  Summary:    {view.get('summary_path') or 'unknown'}",
    ]
    if include_suites:
        lines.extend(["", "Suites", "  Name                         Result   Passed  Failed  Skipped  Duration"])
        for suite in view.get("suites", []):
            lines.append(
                f"  {str(suite.get('suite') or 'unknown'):<28} "
                f"{str(suite.get('status') or 'UNKNOWN'):<8} "
                f"{suite.get('passed', 0):>6} "
                f"{suite.get('failed', 0):>7} "
                f"{suite.get('skipped', 0):>8}  "
                f"{lab_common.format_duration(suite.get('elapsed_seconds'))}"
            )
        paths = [
            ("Artifacts", view.get("artifacts_dir")),
            ("Agent JSON", view.get("agent_report_json")),
            ("Agent Markdown", view.get("agent_report_md")),
            ("Failure context", view.get("failure_context_path")),
        ]
        visible_paths = [(label, value) for label, value in paths if value]
        if visible_paths:
            lines.extend(["", "Paths"])
            lines.extend(f"  {label}: {value}" for label, value in visible_paths)
    return "\n".join(lines)


def _configure_latest(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--json", action="store_true", help="Print a normalized machine-readable latest-run view")
    group.add_argument("--paths", action="store_true", help="Print only the latest run's artifact paths")


@registry.command(
    "report latest",
    help="Render the latest lab run without requiring a run ID",
    configure=_configure_latest,
)
def handle_report_latest(args: argparse.Namespace, config: Config) -> int:
    view, error = load_latest_run_view()
    if view is None:
        print(error or "No recorded lab run.", file=sys.stderr, flush=True)
        return 1
    if args.json:
        print(json.dumps(view, indent=2, sort_keys=True))
        return 0
    if args.paths:
        for key in ("summary_path", "artifacts_dir", "agent_report_json", "agent_report_md", "failure_context_path"):
            if view.get(key):
                print(f"{key}={view[key]}")
        return 0
    print(render_latest_run(view, include_suites=True))
    return 0


def _configure_metrics(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--last", type=int, default=None)
    parser.add_argument("--task", default=None)
    parser.add_argument("--model", default=None)


@registry.command(
    "report ai-metrics",
    help="Summarize recorded AI call metrics",
    configure=_configure_metrics,
)
def handle_report_ai_metrics(args: argparse.Namespace, config: Config) -> int:
    records = read_jsonl(lab_common.metrics_dir() / "ai-calls.jsonl")
    if args.task:
        records = [record for record in records if record.get("task_type") == args.task]
    if args.model:
        records = [record for record in records if record.get("model_alias") == args.model or record.get("model_name") == args.model]
    if args.last:
        records = records[-args.last :]
    summary: dict[str, Any] = {
        "total_calls": len(records),
        "avg_latency_ms": None,
        "by_model": {},
        "by_task": {},
        "by_lane": {},
    }
    latencies = [record["latency_ms"] for record in records if isinstance(record.get("latency_ms"), int)]
    if latencies:
        summary["avg_latency_ms"] = round(sum(latencies) / len(latencies), 1)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[str(record.get("model_alias") or record.get("model_name") or "unknown")].append(record)
    for model, rows in grouped.items():
        model_latencies = [row["latency_ms"] for row in rows if isinstance(row.get("latency_ms"), int)]
        summary["by_model"][model] = {
            "calls": len(rows),
            "avg_latency_ms": round(sum(model_latencies) / len(model_latencies), 1) if model_latencies else None,
            "parse_failures": sum(1 for row in rows if not row.get("parse_success")),
            "successes": sum(1 for row in rows if row.get("success")),
        }
    task_grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    lane_grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        task_grouped[str(record.get("task_type") or "unknown")].append(record)
        lane_grouped[str(record.get("lane") or "unknown")].append(record)
    for task, rows in task_grouped.items():
        summary["by_task"][task] = {
            "calls": len(rows),
            "avg_confidence": round(
                sum(float(row["confidence"]) for row in rows if isinstance(row.get("confidence"), (int, float)))
                / max(1, sum(1 for row in rows if isinstance(row.get("confidence"), (int, float)))),
                3,
            )
            if any(isinstance(row.get("confidence"), (int, float)) for row in rows)
            else None,
        }
    for lane, rows in lane_grouped.items():
        summary["by_lane"][lane] = {"calls": len(rows)}
    print(json.dumps(summary, indent=2))
    return 0
