"""`lab doctor` — AI endpoint/model configuration and connectivity checks."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from typing import Any

from .. import registry
from ..config import Config, get_setting
from ..context import current_hostname
from ..reporting.metrics import append_ai_call
from ..runtime import lab_common
from .support import run_ad_hoc_model


def _chat_prompt_for_doctor(task: str) -> str:
    return (
        f"Return valid JSON for a {task} health check with keys: status, confidence, summary.\n"
        "Keep it short.\n"
        '{"status":"ok","confidence":0.99,"summary":"healthy"}'
    )


def _meaningful_probe_case() -> dict[str, Any]:
    """A real classification prompt+expected pair, from the product's AnalysisPlugin.

    Backs doctor status's "warm" probe: confirms the model can do real
    structured reasoning, not just echo a trivial prompt. Reuses
    eval_cases("classification") rather than a second hardcoded scenario, so
    there is one place a product lab writes classification examples.
    """
    cases = registry.get_analysis_plugin().eval_cases("classification")
    if not cases:
        raise SystemExit("AnalysisPlugin.eval_cases('classification') returned no cases; doctor status needs at least one.")
    return cases[0]


def _configure_doctor_ai(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task", default="classification")
    parser.add_argument("--model", default=None, help="Model alias, or raw model when combined with --endpoint")
    parser.add_argument("--endpoint", default=None, help="Raw endpoint name for ad-hoc checks")
    parser.add_argument("--config-only", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--allow-premium", action="store_true")


@registry.command(
    "doctor ai",
    help="Validate AI endpoint/model wiring by making a live call",
    configure=_configure_doctor_ai,
)
def handle_doctor_ai(args: argparse.Namespace, config: Config) -> int:
    if args.config_only:
        print(json.dumps({"endpoints": config.endpoints, "models": config.models}, indent=2))
        return 0
    if args.endpoint and not args.model:
        raise SystemExit("When using --endpoint, also provide a raw --model name.")
    if args.all:
        aliases = list(config.models.keys())
    elif args.model and args.model in config.models:
        aliases = [args.model]
    elif args.endpoint:
        aliases = [None]
    else:
        default_alias = next(iter(config.tasks.get(args.task, {}).get("candidates", [])), "fast")
        aliases = [default_alias]
    results: list[dict[str, Any]] = []
    for alias in aliases:
        result = run_ad_hoc_model(
            config,
            alias=alias,
            endpoint_name=args.endpoint,
            model_name=args.model if args.endpoint else None,
            prompt=_chat_prompt_for_doctor(args.task),
        )
        structured = result.parsed.structured if result.parsed else None
        row = {
            "alias": result.alias or alias,
            "endpoint": result.endpoint,
            "model_name": result.model_name,
            "ok": result.ok,
            "latency_ms": result.provider_result.latency_ms if result.provider_result else None,
            "parse_mode": result.parsed.parse_mode if result.parsed else None,
            "structured": structured,
            "raw_text": result.provider_result.text if result.provider_result else None,
            "error": result.provider_result.error if result.provider_result else result.reason,
        }
        results.append(row)
        append_ai_call(
            {
                "run_id": "doctor-ai",
                "timestamp_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "host": current_hostname(),
                "role": lab_common.current_role(),
                "task_type": f"doctor:{args.task}",
                "provider": result.provider_type,
                "endpoint": result.endpoint,
                "model_alias": result.alias,
                "model_name": result.model_name,
                "attempt_number": 1,
                "lane": "doctor",
                "latency_ms": result.provider_result.latency_ms if result.provider_result else None,
                "success": result.ok,
                "parse_success": bool(structured),
                "confidence": structured.get("confidence") if isinstance(structured, dict) else None,
                "prompt_size": len(_chat_prompt_for_doctor(args.task)),
                "response_size": len(result.provider_result.text) if result.provider_result else 0,
            }
        )
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for row in results:
            status = "PASS" if row["ok"] else "FAIL"
            print(f"{status} {row['alias'] or row['model_name']} endpoint={row['endpoint']} latency_ms={row['latency_ms']} parse={row['parse_mode']}", flush=True)
    return 0 if all(row["ok"] for row in results) else 1


@registry.command(
    "doctor config",
    help="Show AI endpoint/model/task status and explain what is disabled or misconfigured",
)
def handle_doctor_config(args: argparse.Namespace, config: Config) -> int:
    NW = 16  # name column width

    def _row(name: str, status: str, detail: str, hint: str = "") -> None:
        print(f"  {name:<{NW}}  {status:<8}  {detail}", flush=True)
        if hint:
            print(f"  {' ' * NW}            {hint}", flush=True)

    warnings: list[str] = []

    print("\nAI Configuration Status", flush=True)
    print("=======================", flush=True)

    print("\nEndpoints", flush=True)
    print("---------", flush=True)
    for ep_name, ep in config.endpoints.items():
        enabled = ep.get("enabled", False)
        url = ep.get("base_url", "?")
        api_key_env = ep.get("api_key_env", "")
        key_required = bool(api_key_env)
        key_set = bool(get_setting(api_key_env)) if key_required else True
        status = "ENABLED" if enabled else "disabled"
        timeout_note = f"  read_timeout={ep.get('read_timeout_sec')}s" if enabled else ""
        hint = ""
        if not enabled:
            parts = []
            if key_required and not key_set:
                parts.append(f"{api_key_env} not set")
            parts.append("set enabled: true in config/agent.yaml")
            hint = f"→ {'; '.join(parts)}"
        elif key_required and not key_set:
            hint = f"→ WARNING: {api_key_env} not set but endpoint is enabled"
        _row(ep_name, status, url + timeout_note, hint)

    print("\nModels", flush=True)
    print("------", flush=True)
    for alias, model in config.models.items():
        ep_name = model.get("endpoint", "?")
        ep = config.endpoints.get(ep_name, {})
        ep_enabled = ep.get("enabled", False)
        model_name = model.get("model_name", "?")
        override_env = f"LAB_MODEL_{alias.upper()}"
        status = "ENABLED" if ep_enabled else "disabled"
        if not ep_enabled:
            hint = f"→ enable {ep_name} to use"
        elif not model_name:
            hint = f"→ not configured — set {override_env} in lab.env"
        else:
            hint = ""
        _row(alias, f"[{status}]", f"{ep_name:<14}  {model_name or '(unset)':<28}  override: {override_env}", hint)
        if ep_enabled and not model_name:
            warnings.append(f"'{alias}' has no model_name set — set {override_env} in lab.env or override in config/agent.yaml.")

    print("\nTasks", flush=True)
    print("-----", flush=True)
    for task_name, task in config.tasks.items():
        candidates = task.get("candidates", [])
        max_a = task.get("max_attempts", 1)
        min_c = task.get("min_confidence", 0.0)
        cand_parts = []
        enabled_count = 0
        for c in candidates:
            m = config.models.get(c, {})
            ep_enabled = config.endpoints.get(m.get("endpoint", ""), {}).get("enabled", False)
            cand_parts.append(f"{c}{'✓' if ep_enabled else '✗'}")
            if ep_enabled:
                enabled_count += 1
        print(
            f"  {task_name:<20}  max_attempts={max_a}  min_confidence={min_c}  "
            f"candidates: {' '.join(cand_parts)}",
            flush=True,
        )
        if max_a < enabled_count:
            warnings.append(
                f"Task '{task_name}': max_attempts={max_a} but {enabled_count} candidates are enabled — "
                f"the last {enabled_count - max_a} will never be tried. "
                f"Raise max_attempts to {enabled_count} in config/agent.yaml."
            )

    print("\nSettings", flush=True)
    print("--------", flush=True)
    ep_litellm = config.endpoints.get("litellm", {})
    actual_timeout = ep_litellm.get("read_timeout_sec", 90)
    if os.environ.get("LAB_LITELLM_READ_TIMEOUT_SEC"):
        timeout_src = "(from LAB_LITELLM_READ_TIMEOUT_SEC env)"
    elif get_setting("LAB_LITELLM_READ_TIMEOUT_SEC"):
        timeout_src = "(from lab.env)"
    else:
        timeout_src = "(default — set LAB_LITELLM_READ_TIMEOUT_SEC in lab.env to change)"
    print(f"  LAB_LITELLM_READ_TIMEOUT_SEC = {actual_timeout}s {timeout_src}", flush=True)
    if actual_timeout < 45:
        warnings.append(
            f"LAB_LITELLM_READ_TIMEOUT_SEC={actual_timeout}s is short for large models.\n"
            "    Set LAB_LITELLM_READ_TIMEOUT_SEC=90 (or higher) in lab.env."
        )
    litellm_url = ep_litellm.get("base_url", "?")
    if os.environ.get("LAB_LITELLM_URL"):
        url_src = "(from LAB_LITELLM_URL env)"
    else:
        url_src = "(from lab.env or default — set LAB_LITELLM_URL in lab.env to change)"
    print(f"  LiteLLM URL                  = {litellm_url} {url_src}", flush=True)
    litellm_key_set = bool(get_setting("LITELLM_KEY"))
    print(f"  LITELLM_KEY                  = {'set' if litellm_key_set else 'NOT SET'}", flush=True)
    if not litellm_key_set:
        warnings.append("LITELLM_KEY is not set — all LiteLLM calls will fail with 401. Set it in lab.env.")
    print("  config override file         = config/agent.yaml (create to override defaults)", flush=True)

    if warnings:
        print("\nWarnings", flush=True)
        print("--------", flush=True)
        for w in warnings:
            print(f"  ! {w}", flush=True)

    print("", flush=True)
    return 0


@registry.command(
    "doctor status",
    help="Show config status then run cold health and warm meaningful probes against all models",
)
def handle_doctor_status(args: argparse.Namespace, config: Config) -> int:
    handle_doctor_config(args, config)

    probe_case = _meaningful_probe_case()

    print("Live Connectivity Tests", flush=True)
    print("-----------------------", flush=True)
    print(
        "  cold = first observed health request; warm = immediate meaningful classification probe",
        flush=True,
    )
    all_pass = True
    for alias in config.models:
        model = config.models[alias]
        ep = config.endpoints.get(model.get("endpoint", ""), {})
        if not ep.get("enabled", False):
            print(f"  {alias:<18}  skipped   (endpoint disabled)", flush=True)
            continue
        cold_result = run_ad_hoc_model(
            config,
            alias=alias,
            endpoint_name=None,
            model_name=None,
            prompt=_chat_prompt_for_doctor("status"),
        )
        cold_pr = cold_result.provider_result
        meaningful_result = None
        meaningful_pr = None
        meaningful_structured = None
        if cold_pr is not None and cold_pr.ok:
            meaningful_result = run_ad_hoc_model(
                config,
                alias=alias,
                endpoint_name=None,
                model_name=None,
                prompt=probe_case["prompt"],
            )
            meaningful_pr = meaningful_result.provider_result
            if meaningful_result.parsed:
                meaningful_structured = meaningful_result.parsed.structured

        cold_latency = f"cold={cold_pr.latency_ms}ms" if cold_pr and cold_pr.latency_ms else "cold=?"
        if meaningful_pr and meaningful_pr.latency_ms:
            warm_latency = f"warm={meaningful_pr.latency_ms}ms"
        elif cold_pr is not None and not cold_pr.ok:
            warm_latency = "warm=skipped"
        else:
            warm_latency = "warm=?"

        classification = None
        if isinstance(meaningful_structured, dict):
            raw_classification = meaningful_structured.get("classification")
            if isinstance(raw_classification, str):
                classification = raw_classification.strip().lower()
        point_ok = classification == probe_case["expected"]
        if meaningful_result is None:
            point = "point=skipped"
        else:
            point = "point=PASS" if point_ok else "point=FAIL"
        latency = f"{cold_latency} {warm_latency} {point}"
        model_name = model.get("model_name", "?")
        cold_connected = cold_pr is not None and cold_pr.ok
        meaningful_connected = meaningful_pr is not None and meaningful_pr.ok
        if cold_result.ok and meaningful_result and meaningful_result.ok and point_ok:
            status = "PASS"
            detail = ""
        elif cold_connected and meaningful_connected:
            status = "WARN"
            if classification:
                detail = f"  expected={probe_case['expected']} got={classification}"
            else:
                reason = meaningful_result.reason if meaningful_result else cold_result.reason
                detail = f"  responded but output not structured ({reason})"
            all_pass = False
        elif cold_connected:
            status = "FAIL"
            if meaningful_pr:
                reason = meaningful_pr.error
            elif meaningful_result:
                reason = meaningful_result.reason
            else:
                reason = "missing warm response"
            detail = f"  warm_error={reason}"
            all_pass = False
        else:
            status = "FAIL"
            detail = f"  error={cold_pr.error if cold_pr else cold_result.reason}"
            all_pass = False
        print(f"  {alias:<18}  {status:<6}  {latency:<40}  model={model_name}{detail}", flush=True)
    print("", flush=True)
    return 0 if all_pass else 1
