from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .runtime import CONFIG_DIR


def _load_lab_env_values() -> dict[str, str]:
    values: dict[str, str] = {}
    env_path = CONFIG_DIR.parent / "lab.env"
    if not env_path.exists():
        return values
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


_LAB_ENV_VALUES = _load_lab_env_values()


def _setting(name: str, default: str) -> str:
    return os.environ.get(name) or _LAB_ENV_VALUES.get(name) or default


def get_setting(name: str, default: str = "") -> str:
    """Read a value from environment or lab.env (public helper for CLI use)."""
    return _setting(name, default)


_MODEL_ENV_PREFIX = "LAB_MODEL_"


def _model_env_overrides() -> dict[str, str]:
    """Collect LAB_MODEL_<ALIAS> values from lab.env and the environment.

    Environment beats lab.env; both beat anything resolved from a config file.
    """
    overrides: dict[str, str] = {}
    for source in (_LAB_ENV_VALUES, os.environ):
        for key, value in source.items():
            if not key.startswith(_MODEL_ENV_PREFIX) or not value.strip():
                continue
            overrides[key[len(_MODEL_ENV_PREFIX):].lower()] = value.strip()
    return overrides


def _litellm_url() -> str:
    return _setting("LAB_LITELLM_URL", "http://localhost:4000/v1")


DEFAULT_CONFIG: dict[str, Any] = {
    "policy": {
        "premium_enabled": False,
        "max_llm_calls_per_run": 6,
        "max_cloud_escalations_per_run": 1,
        "max_llm_calls_per_stage": 1,
        "allow_parallel_model_racing": False,
        "allow_ai_tool_selection": False,
        "allow_ai_shell_execution": False,
    },
    "thresholds": {
        "log_excerpt_lines": 50,
        "log_excerpt_chars": 12000,
        "analysis_prompt_chars": 12000,
        "cloud_escalation_log_chars": 8000,
        "low_confidence": 0.75,
        "stream_log_lines": 150,
    },
    # All models are accessed through a single LiteLLM endpoint.
    # To add a new provider or model, configure it in LiteLLM — not here.
    "endpoints": {
        "litellm": {
            "type": "litellm",
            "base_url": _litellm_url(),
            "enabled": True,
            "connect_timeout_sec": 2,
            "read_timeout_sec": float(_setting("LAB_LITELLM_READ_TIMEOUT_SEC", "90")),
            "api_key_env": "LITELLM_KEY",
        },
    },
    # Capability tiers referenced by the task configs below. Which model backs a
    # tier is a per-site decision (see the "models" block); the tier meanings are not.
    #
    # Local tier (runs on local infrastructure, no per-call cloud cost):
    #   fast        — smallest and fastest; classification, extraction, routing
    #   light       — mid-small; stream analysis, simple debug, format fallback
    #   standard    — general-purpose workhorse; debugging, summarization
    #   coder       — code specialist; writing, modifying, code debugging
    #   reasoner    — deep reasoning; multi-step problems, unclear root cause
    #   large       — large context; architecture, multi-source, large output
    #   agent       — automation and agentic workflows (local)
    #
    # Cloud tier (per-call cost; only used when local is insufficient):
    #   cloud_fast      — cheapest/fastest cloud; first escalation step
    #   cloud_standard  — standard cloud capability
    #   cloud_heavy     — heavy cloud; complex code, multi-step agents
    #   cloud_agent     — cloud-side agentic workflows
    #   cloud_premium   — highest capability; configure target in LiteLLM
    # Model aliases are capability tiers, not hardware locations. The tier design is
    # the contract se-lab ships; the model_name values are site-specific and MUST stay
    # empty here. A hostname in this file would ship publicly and silently route a
    # downstream lab at someone else's box.
    #
    # Set real model IDs per host in lab.env (gitignored):  LAB_MODEL_<ALIAS>=...
    # Those win over config/agent.yaml, which is tracked and must stay hostname-free.
    # An alias left empty is skipped at routing time with an error naming it.
    "models": {
        "fast":           {"endpoint": "litellm", "model_name": ""},
        "light":          {"endpoint": "litellm", "model_name": ""},
        "standard":       {"endpoint": "litellm", "model_name": ""},
        "coder":          {"endpoint": "litellm", "model_name": ""},
        "reasoner":       {"endpoint": "litellm", "model_name": ""},
        "large":          {"endpoint": "litellm", "model_name": ""},
        "agent":          {"endpoint": "litellm", "model_name": ""},
        "cloud_fast":     {"endpoint": "litellm", "model_name": ""},
        "cloud_standard": {"endpoint": "litellm", "model_name": ""},
        "cloud_heavy":    {"endpoint": "litellm", "model_name": ""},
        "cloud_agent":    {"endpoint": "litellm", "model_name": ""},
        "cloud_premium":  {"endpoint": "litellm", "model_name": ""},
    },
    # Task routing — controls which aliases are tried in order for each task type.
    # To add a new task, either add it here or define it in config/agent.yaml.
    # YAML entries deep-merge with these defaults, so you can override individual
    # fields (e.g. just the candidates list) without duplicating the whole block.
    #
    # Task config fields:
    #   candidates    — aliases tried in order; first acceptable result wins
    #   max_attempts  — stop after this many candidates regardless of outcome
    #   allow_cloud   — whether cloud_* aliases are eligible for this task
    #   min_confidence — minimum confidence score to accept a result (0.0–1.0)
    "tasks": {
        # Fast extraction: classify a failure as deploy/harness/product/unknown
        "classification": {
            "candidates": ["fast", "light", "cloud_fast"],
            "max_attempts": 3,
            "allow_cloud": True,
            "min_confidence": 0.75,
        },
        # Post-failure lab run analysis. Start above the lightweight route so
        # generated reports are useful when humans are already looking at a failure.
        "failure_analysis": {
            "candidates": ["standard", "reasoner", "cloud_fast"],
            "max_attempts": 3,
            "allow_cloud": True,
            "min_confidence": 0.80,
        },
        # Summarize logs, EPG output, or large command output
        "summarization": {
            "candidates": ["standard", "large", "cloud_fast"],
            "max_attempts": 3,
            "allow_cloud": True,
            "min_confidence": 0.70,
        },
        # Identify the root cause of a test failure
        "root_cause": {
            "candidates": ["standard", "reasoner", "cloud_fast"],
            "max_attempts": 3,
            "allow_cloud": True,
            "min_confidence": 0.80,
        },
        # Classify a stream log session (upstream_drop, auth_failure, etc.)
        "stream_analysis": {
            "candidates": ["light", "standard", "cloud_fast"],
            "max_attempts": 3,
            "allow_cloud": True,
            "min_confidence": 0.70,
        },
        # Debug code-level failures; escalates to a code-specialist model
        "code_debug": {
            "candidates": ["standard", "coder", "cloud_heavy"],
            "max_attempts": 3,
            "allow_cloud": True,
            "min_confidence": 0.75,
        },
        # Architecture and system-design analysis
        "architecture": {
            "candidates": ["large", "cloud_standard"],
            "max_attempts": 2,
            "allow_cloud": True,
            "min_confidence": 0.70,
        },
        # Automation and agent workflow tasks
        "agent_workflow": {
            "candidates": ["agent", "cloud_agent", "cloud_heavy"],
            "max_attempts": 3,
            "allow_cloud": True,
            "min_confidence": 0.70,
        },
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_structured_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    if path.suffix.lower() == ".json":
        return json.loads(path.read_text(encoding="utf-8"))

    try:
        import yaml  # type: ignore
    except ImportError:
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


@dataclass(slots=True)
class Config:
    raw: dict[str, Any]

    @property
    def policy(self) -> dict[str, Any]:
        return self.raw["policy"]

    @property
    def thresholds(self) -> dict[str, Any]:
        return self.raw["thresholds"]

    @property
    def endpoints(self) -> dict[str, Any]:
        return self.raw["endpoints"]

    @property
    def models(self) -> dict[str, Any]:
        return self.raw["models"]

    @property
    def tasks(self) -> dict[str, Any]:
        return self.raw["tasks"]


def _apply_model_env_overrides(merged: dict[str, Any]) -> None:
    """Let lab.env/environment win over config files for model IDs.

    Precedence is deliberately env > file. config/agent.yaml is tracked and shared;
    lab.env is gitignored and per-host, so the per-host file has to be the one that
    wins. An alias named only in the environment is accepted and defaults to the
    litellm endpoint, so a site can add a tier without editing tracked config.
    """
    models = merged.setdefault("models", {})
    for alias, model_name in _model_env_overrides().items():
        entry = models.get(alias)
        if isinstance(entry, dict):
            entry["model_name"] = model_name
        else:
            models[alias] = {"endpoint": "litellm", "model_name": model_name}


def load_config() -> Config:
    merged = deepcopy(DEFAULT_CONFIG)
    for filename in ("agent.yaml", "agent.yml", "agent.json", "models.yaml", "models.yml", "models.json"):
        merged = _deep_merge(merged, _load_structured_file(CONFIG_DIR / filename))
    _apply_model_env_overrides(merged)
    return Config(raw=merged)
