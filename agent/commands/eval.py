"""`lab eval` — controlled model comparison against a product's lightweight eval cases."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from typing import Any

from .. import registry
from ..config import Config
from ..context import current_hostname
from ..reporting.metrics import append_ai_call
from ..runtime import lab_common
from .support import run_ad_hoc_model


def _evaluate_model(
    config: Config, *, alias: str | None, endpoint_name: str | None, model_name: str | None, task: str
) -> dict[str, Any]:
    cases = registry.get_analysis_plugin().eval_cases(task)
    outcomes: list[dict[str, Any]] = []
    for case in cases:
        started_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = run_ad_hoc_model(config, alias=alias, endpoint_name=endpoint_name, model_name=model_name, prompt=case["prompt"])
        structured = result.parsed.structured if result.parsed and result.parsed.structured else {}
        predicted = structured.get("classification")
        provider_result = result.provider_result
        append_ai_call(
            {
                "run_id": "eval-ai",
                "timestamp_utc": started_at,
                "host": current_hostname(),
                "role": lab_common.current_role(),
                "task_type": f"eval:{task}",
                "provider": result.provider_type,
                "endpoint": result.endpoint,
                "model_alias": result.alias,
                "model_name": result.model_name,
                "attempt_number": 1,
                "lane": "eval",
                "latency_ms": provider_result.latency_ms if provider_result else None,
                "success": result.ok,
                "parse_success": bool(result.parsed and result.parsed.structured),
                "confidence": structured.get("confidence"),
                "prompt_size": len(case["prompt"]),
                "response_size": len(provider_result.text) if provider_result else 0,
                "expected": case["expected"],
                "predicted": predicted,
                "match": predicted == case["expected"],
                "error": provider_result.error if provider_result else result.reason,
            }
        )
        outcomes.append(
            {
                "case": case["name"],
                "expected": case["expected"],
                "predicted": predicted,
                "match": predicted == case["expected"],
                "latency_ms": provider_result.latency_ms if provider_result else None,
                "parse_success": bool(result.parsed and result.parsed.structured),
                "confidence": structured.get("confidence"),
                "alias": result.alias,
                "model_name": result.model_name,
                "ok": result.ok,
                "error": provider_result.error if provider_result else result.reason,
            }
        )
    matches = sum(1 for outcome in outcomes if outcome["match"])
    latencies = [outcome["latency_ms"] for outcome in outcomes if isinstance(outcome["latency_ms"], int)]
    return {
        "task": task,
        "cases": outcomes,
        "score": {
            "matches": matches,
            "total": len(outcomes),
            "parse_successes": sum(1 for outcome in outcomes if outcome["parse_success"]),
            "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
        },
    }


def _configure_eval_ai(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task", default="classification", choices=["classification", "root-cause", "stream-analysis"])
    parser.add_argument("--model", default=None, help="Model alias, or raw model when combined with --endpoint")
    parser.add_argument("--endpoint", default=None, help="Raw endpoint name for ad-hoc checks")
    parser.add_argument("--candidate", default=None, help="Candidate model alias")
    parser.add_argument("--baseline", default=None, help="Baseline model alias")


@registry.command(
    "eval ai",
    help="Evaluate one candidate model against a product's lightweight eval cases",
    configure=_configure_eval_ai,
)
def handle_eval_ai(args: argparse.Namespace, config: Config) -> int:
    if args.endpoint and not args.model:
        raise SystemExit("When using --endpoint, also provide a raw --model name.")
    baseline = (
        _evaluate_model(config, alias=args.baseline, endpoint_name=None, model_name=None, task=args.task)
        if args.baseline
        else None
    )
    task_key = {"classification": "classification", "root-cause": "root_cause", "stream-analysis": "stream_analysis"}.get(
        args.task, "classification"
    )
    candidate_alias = None if args.endpoint else args.candidate or args.model or next(
        iter(config.tasks.get(task_key, {}).get("candidates", [])),
        "fast",
    )
    candidate = _evaluate_model(
        config,
        alias=candidate_alias,
        endpoint_name=args.endpoint,
        model_name=args.model if args.endpoint else None,
        task=args.task,
    )
    payload = {"candidate": candidate, "baseline": baseline}
    print(json.dumps(payload, indent=2))
    return 0
