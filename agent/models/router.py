from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..config import Config, get_setting
from ..providers.base import ProviderResult
from ..providers.litellm import LiteLLMProvider
from .responses import ParsedResponse, parse_structured_response


@dataclass(slots=True)
class RoutedResult:
    ok: bool
    alias: str | None
    endpoint: str | None
    model_name: str | None
    provider_type: str | None
    provider_result: ProviderResult | None
    parsed: ParsedResponse | None
    reason: str | None = None
    # None  → coverage check was not requested
    # True  → all required suite names were present in the response
    # False → one or more required suite names were missing
    coverage_passed: bool | None = None
    # True when the model returned parseable JSON but the confidence field was
    # absent or null.  The response is accepted rather than discarded so callers
    # can still surface it; they should attempt cloud verification when possible.
    confidence_unverified: bool = False


ProgressCallback = Callable[[str], None]


def _provider_for(endpoint_type: str):
    """Resolve an endpoint type to a provider.

    LiteLLM is the only supported path: additional backends are configured *in*
    LiteLLM, not here. This raises rather than silently returning a LiteLLM
    provider for some other declared type, which is what made a misconfigured
    endpoint look like it worked.
    """
    if endpoint_type != "litellm":
        raise ValueError(
            f"Unsupported endpoint type {endpoint_type!r}. se-lab routes every model "
            "through LiteLLM; configure other backends inside LiteLLM and point the "
            "endpoint at it."
        )
    return LiteLLMProvider()


def _check_suite_coverage(
    parsed: ParsedResponse,
    provider_result: ProviderResult,
    required: list[str],
) -> list[str]:
    """Return suite names that are missing from the model's response.

    First checks the structured affected_suites list.  Falls back to scanning
    the raw response text so that "all suites failed" prose still passes when
    every name appears at least once.
    """
    if not required:
        return []
    structured = parsed.structured or {}
    covered_structured = {s.lower() for s in (structured.get("affected_suites") or []) if isinstance(s, str)}
    missing = [s for s in required if s.lower() not in covered_structured]
    if not missing:
        return []
    raw_lower = (provider_result.text or "").lower()
    missing = [s for s in missing if s.lower() not in raw_lower]
    return missing


def run_task(
    config: Config,
    *,
    task_name: str,
    prompt: str,
    allow_cloud: bool = True,
    allow_premium: bool = False,
    progress: ProgressCallback | None = None,
    required_suite_names: list[str] | None = None,
) -> RoutedResult:
    task = config.tasks.get(task_name)
    if not task:
        return RoutedResult(False, None, None, None, None, None, None, reason=f"Unknown task {task_name}")

    attempts = 0
    unroutable: list[str] = []
    last_failure: RoutedResult | None = None
    max_attempts = int(task.get("max_attempts", 1))
    for alias in task.get("candidates", []):
        model = config.models.get(alias)
        if not model:
            unroutable.append(f"{alias} (no such alias)")
            continue
        if not (model.get("model_name") or "").strip():
            # Deliberate: se-lab ships empty defaults so an unconfigured lab fails
            # by name instead of inheriting whatever another site had configured.
            unroutable.append(f"{alias} (no model configured; set LAB_MODEL_{alias.upper()} in lab.env)")
            continue
        endpoint_name = model.get("endpoint")
        endpoint = config.endpoints.get(endpoint_name, {})
        if not endpoint.get("enabled", False):
            continue
        if "cloud" in alias and not allow_cloud:
            continue
        if "premium" in alias and not allow_premium:
            continue
        attempts += 1
        if attempts > max_attempts:
            break
        provider = _provider_for(endpoint.get("type", "litellm"))
        api_key = get_setting(endpoint["api_key_env"]) if endpoint.get("api_key_env") else None
        timeout_read = float(endpoint.get("read_timeout_sec", 8))
        if progress:
            progress(
                f"Trying model '{alias}' ({model['model_name']}) via {endpoint_name} "
                f"[attempt {attempts}/{max_attempts}, read timeout {timeout_read:.0f}s]."
            )
        provider_result = provider.chat(
            base_url=endpoint["base_url"],
            model=model["model_name"],
            prompt=prompt,
            timeout_connect=float(endpoint.get("connect_timeout_sec", 2)),
            timeout_read=timeout_read,
            api_key=api_key,
        )
        if not provider_result.ok:
            if progress:
                error = provider_result.error or "provider call failed"
                progress(
                    f"Model '{alias}' failed after {provider_result.latency_ms} ms: {error}"
                )
            last_failure = RoutedResult(
                ok=False,
                alias=alias,
                endpoint=endpoint_name,
                model_name=model["model_name"],
                provider_type=endpoint.get("type", "litellm"),
                provider_result=provider_result,
                parsed=None,
                reason=provider_result.error or "Provider call failed",
            )
            continue
        parsed = parse_structured_response(provider_result.text)
        if parsed.structured is None and progress:
            preview = (provider_result.text or "")[:400].replace("\n", " ")
            progress(f"Model '{alias}' response had no parseable JSON (preview): {preview!r}")
        structured = parsed.structured or {}
        confidence = structured.get("confidence")
        min_conf = float(task.get("min_confidence", 0.0))
        # When confidence is None the model produced JSON but omitted the field.
        # Accept it rather than discarding the analysis — mark as unverified so
        # callers can attempt a cloud verification pass.  A hard reject only
        # happens when the model returned a numeric confidence below threshold.
        confidence_unverified = confidence is None and parsed.structured is not None
        confidence_ok = (
            min_conf == 0.0 and parsed.structured is not None
        ) or (
            isinstance(confidence, (int, float)) and confidence >= min_conf
        ) or confidence_unverified
        if not confidence_ok:
            if progress:
                progress(
                    f"Model '{alias}' returned confidence {confidence} below required {min_conf:.2f}; trying next candidate."
                )
            last_failure = RoutedResult(
                ok=False,
                alias=alias,
                endpoint=endpoint_name,
                model_name=model["model_name"],
                provider_type=endpoint.get("type", "litellm"),
                provider_result=provider_result,
                parsed=parsed,
                reason=f"Confidence {confidence} below minimum {task.get('min_confidence', 0.0)}",
            )
            continue
        missing_suites = _check_suite_coverage(parsed, provider_result, required_suite_names or [])
        if missing_suites:
            if progress:
                progress(
                    f"Model '{alias}' did not cover all failing suites "
                    f"(missing: {', '.join(missing_suites)}); trying next candidate."
                )
            last_failure = RoutedResult(
                ok=False,
                alias=alias,
                endpoint=endpoint_name,
                model_name=model["model_name"],
                provider_type=endpoint.get("type", "litellm"),
                provider_result=provider_result,
                parsed=parsed,
                reason=f"Coverage check failed: missing suites {missing_suites}",
                coverage_passed=False,
            )
            continue
        if progress:
            coverage_note = f", all {len(required_suite_names)} suites covered" if required_suite_names else ""
            if confidence_unverified:
                progress(
                    f"Model '{alias}' returned output without confidence field in {provider_result.latency_ms} ms"
                    f"{coverage_note}; flagged as unverified."
                )
            else:
                progress(
                    f"Model '{alias}' succeeded in {provider_result.latency_ms} ms with confidence {confidence}{coverage_note}."
                )
        return RoutedResult(
            ok=True,
            alias=alias,
            endpoint=endpoint_name,
            model_name=model["model_name"],
            provider_type=endpoint.get("type", "litellm"),
            provider_result=provider_result,
            parsed=parsed,
            coverage_passed=True if required_suite_names else None,
            confidence_unverified=confidence_unverified,
        )
    if last_failure is not None:
        return last_failure
    if unroutable:
        # Nothing was even attempted. Name the aliases so the fix is obvious;
        # a bare "no acceptable result" hides an unconfigured lab as a model failure.
        return RoutedResult(
            False, None, None, None, None, None, None,
            reason=f"No candidate for task '{task_name}' was routable: " + "; ".join(unroutable),
        )
    return RoutedResult(False, None, None, None, None, None, None, reason="No model produced an acceptable result")
