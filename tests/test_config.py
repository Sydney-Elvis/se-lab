"""agent/config.py: precedence, empty-alias behavior, fail-loud on unreadable YAML."""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from agent.config import load_config
from agent.runtime import CONFIG_DIR


@pytest.fixture(autouse=True)
def _clean_config_dir():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("agent.yaml", "agent.yml", "agent.json", "models.yaml", "models.yml", "models.json"):
        (CONFIG_DIR / name).unlink(missing_ok=True)
    yield
    for name in ("agent.yaml", "agent.yml", "agent.json", "models.yaml", "models.yml", "models.json"):
        (CONFIG_DIR / name).unlink(missing_ok=True)


def test_model_aliases_default_to_empty_not_a_hostname():
    config = load_config()
    for alias, model in config.models.items():
        assert model["model_name"] == "", f"{alias} should default empty, got {model['model_name']!r}"


def test_env_beats_tracked_yaml_for_model_ids(monkeypatch):
    (CONFIG_DIR / "agent.yaml").write_text("models:\n  standard:\n    model_name: from-tracked-yaml\n", encoding="utf-8")
    monkeypatch.setenv("LAB_MODEL_STANDARD", "from-env")
    config = load_config()
    assert config.models["standard"]["model_name"] == "from-env"


def test_yaml_only_alias_still_resolves_without_env_override():
    (CONFIG_DIR / "agent.yaml").write_text("models:\n  reasoner:\n    model_name: yaml-only-value\n", encoding="utf-8")
    config = load_config()
    assert config.models["reasoner"]["model_name"] == "yaml-only-value"


def test_env_only_alias_not_in_any_config_file_is_still_created(monkeypatch):
    monkeypatch.setenv("LAB_MODEL_NEWTIER", "an-alias-yaml-never-declared")
    config = load_config()
    assert config.models["newtier"]["model_name"] == "an-alias-yaml-never-declared"
    assert config.models["newtier"]["endpoint"] == "litellm"


def test_present_but_unparseable_yaml_raises_instead_of_degrading_silently(monkeypatch):
    (CONFIG_DIR / "agent.yaml").write_text("thresholds: {analysis_prompt_chars: 99999}\n", encoding="utf-8")

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("No module named yaml")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(RuntimeError, match="PyYAML is not installed"):
        load_config()


def test_missing_config_files_are_fine():
    config = load_config()
    assert config.thresholds["analysis_prompt_chars"] == 12000
