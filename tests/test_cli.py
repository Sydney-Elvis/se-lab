"""agent/cli.py: build_parser() assembles from the registry, dispatch works.

Importing agent.cli triggers agent.commands' import, which registers
se-lab's own built-in commands (discover, down, artifacts, doctor, eval,
report, clients) via their @registry.command decorators.
"""

from __future__ import annotations

import pytest

import agent.cli as cli
from agent import registry
from agent.analysis.plugin import AnalysisPlugin
from agent.settings.plugin import SettingsPlugin


def test_cli_loads_and_registers_builtin_commands():
    top, groups = registry.grouped_commands()
    top_names = {c.name for c in top}
    assert {"discover", "down"} <= top_names
    grouped_names = {c.name for subs in groups.values() for c in subs}
    assert {"artifacts list", "doctor config", "report latest", "clients status", "settings export", "settings import"} <= grouped_names


def test_build_parser_accepts_help_without_error():
    parser = cli.build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--help"])
    assert exc_info.value.code == 0


def test_build_parser_rejects_unknown_command():
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["not-a-real-command"])


def test_dispatch_name_for_top_level_and_grouped():
    parser = cli.build_parser()
    args = parser.parse_args(["discover"])
    assert cli._dispatch_name(args) == "discover"
    args = parser.parse_args(["artifacts", "list"])
    assert cli._dispatch_name(args) == "artifacts list"


def test_main_runs_discover_end_to_end(capsys):
    code = cli.main(["discover"])
    assert code == 0
    out = capsys.readouterr().out
    assert '"runtime_dir"' in out
    assert '"available_commands"' in out


def test_main_runs_report_ai_metrics_with_no_data(capsys):
    code = cli.main(["report", "ai-metrics"])
    assert code == 0
    out = capsys.readouterr().out
    assert '"total_calls": 0' in out


def test_main_doctor_ai_needs_analysis_plugin_only_for_status_probe(capsys):
    # doctor config doesn't touch the AnalysisPlugin at all
    code = cli.main(["doctor", "config"])
    assert code == 0


def test_main_doctor_status_fails_loud_without_analysis_plugin():
    with pytest.raises(SystemExit, match="No AnalysisPlugin registered"):
        cli.main(["doctor", "status"])


class _FakeAnalysis(AnalysisPlugin):
    def classification_rubric(self):
        return "rubric"

    def failure_prompt(self, task, context, *, max_chars):
        return "prompt"

    def extract_log_context(self, *, session_id, max_lines):
        return {}

    def eval_cases(self, task):
        return [{"name": "case1", "prompt": "classify this", "expected": "product"}]


def test_main_eval_ai_uses_analysis_plugin_eval_cases(monkeypatch, capsys):
    registry.set_analysis_plugin(_FakeAnalysis())

    from agent.models.router import RoutedResult
    from agent.providers.base import ProviderResult
    from agent.models.responses import ParsedResponse
    import agent.commands.eval as eval_mod

    def fake_run_ad_hoc_model(config, *, alias, endpoint_name, model_name, prompt):
        pr = ProviderResult(ok=True, text="{}", latency_ms=1, status_code=200, error=None)
        parsed = ParsedResponse(structured={"classification": "product", "confidence": 0.9}, parse_mode="json", raw_text="{}")
        return RoutedResult(True, alias or "fast", "litellm", "fake-model", "litellm", pr, parsed, reason=None)

    monkeypatch.setattr(eval_mod, "run_ad_hoc_model", fake_run_ad_hoc_model)
    code = cli.main(["eval", "ai", "--task", "classification", "--candidate", "fast"])
    assert code == 0
    out = capsys.readouterr().out
    assert '"matches": 1' in out


class _FakeSettings(SettingsPlugin):
    def export_settings(self, out_path):
        out_path.write_text("settings archive", encoding="utf-8")

    def import_settings(self, archive_path):
        return {"applied": {"providers": 2}, "archive": archive_path.name}

    def default_export_filename(self):
        return "lab-settings.fake"


def test_main_settings_export_uses_artifacts_dir(monkeypatch, tmp_path, capsys):
    registry.set_settings_plugin(_FakeSettings())
    monkeypatch.setenv("LAB_ARTIFACTS_DIR", str(tmp_path))

    code = cli.main(["settings", "export"])

    assert code == 0
    output_path = tmp_path / "lab-settings.fake"
    assert output_path.read_text(encoding="utf-8") == "settings archive"
    assert str(output_path) in capsys.readouterr().out


def test_main_settings_import_prints_applied_summary(tmp_path, capsys):
    registry.set_settings_plugin(_FakeSettings())
    archive_path = tmp_path / "baseline.fake"
    archive_path.write_text("settings archive", encoding="utf-8")

    code = cli.main(["settings", "import", str(archive_path)])

    assert code == 0
    output = capsys.readouterr().out
    assert '"providers": 2' in output
    assert '"archive": "baseline.fake"' in output


def test_main_settings_stops_when_target_does_not_support_it(tmp_path):
    class UnsupportedSettings(_FakeSettings):
        def capability(self):
            return "unsupported"

    registry.set_settings_plugin(UnsupportedSettings())
    with pytest.raises(SystemExit, match="unavailable on the target instance"):
        cli.main(["settings", "export", "--out", str(tmp_path / "nope.fake")])
