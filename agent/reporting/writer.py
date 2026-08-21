from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import common as lab_common


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _stringify_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return json.dumps(value, indent=2, sort_keys=True)


def _append_markdown_block(lines: list[str], value: Any) -> None:
    text = _stringify_value(value)
    if not text:
        return
    if "\n" in text:
        lines.extend(["```json", text, "```"])
    else:
        lines.append(text)


def write_run_report(run_id: str, payload: dict[str, Any]) -> tuple[Path, Path, Path]:
    reports_dir = lab_common.reports_dir()
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / f"{run_id}.json"
    md_path = reports_dir / f"{run_id}.md"
    sha_path = reports_dir / f"{run_id}.sha256"

    json_text = json.dumps(payload, indent=2, sort_keys=True)
    json_path.write_text(json_text + "\n", encoding="utf-8")

    lines = [
        f"# Lab Report `{run_id}`",
        "",
        f"- Started at: {payload.get('started_at_utc') or 'unknown'}",
        f"- Completed at: {payload.get('completed_at_utc')}",
        f"- Duration: {lab_common.format_duration(payload.get('duration_seconds'))}",
        f"- Host: {payload.get('host')}",
        f"- Role: {payload.get('role')}",
        f"- Exit code: {payload.get('exit_code')}",
        f"- Source: {payload.get('source_type')} {payload.get('source_ref')}",
        f"- Lab commit: {payload.get('lab_repo_commit') or 'unknown'}",
    ]
    if payload.get("repo_summary_path"):
        lines.append(f"- Run summary: `{payload['repo_summary_path']}`")
    if payload.get("artifacts_path"):
        lines.append(f"- Artifacts: `{payload['artifacts_path']}`")
    if payload.get("deployment_log_path"):
        lines.append(f"- Deployment log: `{payload['deployment_log_path']}`")
    if payload.get("failure_context_path"):
        lines.append(f"- Failure context: `{payload['failure_context_path']}`")
    if payload.get("manifest_sha256"):
        lines.append(f"- Manifest SHA256: `{payload['manifest_sha256']}`")
    if payload.get("ai"):
        ai = payload["ai"]
        lines.extend(
            [
                "",
                "## AI Summary",
                "",
                f"- Inferred: `{ai.get('inferred', False)}`",
                f"- Confidence: `{ai.get('confidence')}`",
                f"- Lane: `{ai.get('lane')}`",
                f"- Classification: `{ai.get('classification')}`",
                f"- Likely failing service/suite: `{_stringify_value(ai.get('likely_failing_service'))}`",
                f"- Likely error: `{_stringify_value(ai.get('likely_error'))}`",
                "",
            ]
        )
        _append_markdown_block(lines, ai.get("summary"))
        if ai.get("next_steps"):
            lines.extend(["", "Next steps:"])
            _append_markdown_block(lines, ai.get("next_steps"))
    md_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    sha_path.write_text(f"{_sha256_text(json_text)}  {json_path.name}\n", encoding="utf-8")
    lab_common.write_latest_artifact_record(
        {
            "kind": "agent-report",
            "run_id": run_id,
            "status": "fail" if payload.get("exit_code") else "pass",
            "summary_path": payload.get("repo_summary_path"),
            "artifacts_dir": payload.get("artifacts_path"),
            "agent_report_json": str(json_path),
            "agent_report_md": str(md_path),
            "agent_report_sha256": str(sha_path),
            "failure_context_path": payload.get("failure_context_path"),
            "role": payload.get("role"),
            "host": payload.get("host"),
            "image": payload.get("image"),
            "source_type": payload.get("source_type"),
            "source_ref": payload.get("source_ref"),
            "product_commit": payload.get("deployment_commit"),
            "lab_commit": payload.get("lab_repo_commit"),
            "started_at_utc": payload.get("started_at_utc"),
            "completed_at_utc": payload.get("completed_at_utc"),
            "duration_seconds": payload.get("duration_seconds"),
        }
    )
    return json_path, md_path, sha_path
