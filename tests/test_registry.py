"""agent/registry.py: command registration/dispatch, client/plugin registration."""

from __future__ import annotations

import pytest

from agent import registry
from agent.analysis.plugin import AnalysisPlugin
from agent.clients.plugin import ClientPlugin
from agent.database.plugin import DatabasePlugin


def test_top_level_command_registers_and_dispatches():
    @registry.command("selftest-top-level", help="top level")
    def handler(args, config):
        return 0

    top, groups = registry.grouped_commands()
    assert "selftest-top-level" in {c.name for c in top}
    assert registry.dispatch("selftest-top-level", None, None) == 0


def test_grouped_command_names_split_correctly():
    @registry.command("selftest-group status", help="grouped")
    def h1(args, config):
        return 0

    @registry.command("selftest-group update", help="grouped 2")
    def h2(args, config):
        return 1

    top, groups = registry.grouped_commands()
    assert "selftest-group" in groups
    assert sorted(c.name for c in groups["selftest-group"]) == ["selftest-group status", "selftest-group update"]


def test_duplicate_command_name_raises_naming_the_original():
    @registry.command("status", help="first")
    def handler(args, config):
        return 0

    with pytest.raises(ValueError, match="already registered"):
        @registry.command("status", help="second")
        def handler2(args, config):
            return 1


def test_dispatch_unknown_command_raises():
    with pytest.raises(SystemExit, match="Unknown command"):
        registry.dispatch("nope", None, None)


class _FakeClient(ClientPlugin):
    name = "fakeclient"
    compose_service = "fakeclient"
    image_env_var = "FAKECLIENT_IMAGE"
    default_image = "fake/client:latest"

    def detect_version(self):
        return "1.0.0"


def test_client_registration_and_lookup():
    registry.register_client(_FakeClient)
    assert registry.all_clients() == {"fakeclient": _FakeClient}
    assert registry.get_client("fakeclient") is _FakeClient


def test_unknown_client_raises_naming_known_ones():
    registry.register_client(_FakeClient)
    with pytest.raises(SystemExit, match="fakeclient"):
        registry.get_client("nosuchclient")


def test_client_plugin_ready_defaults_to_detect_version_not_none():
    instance = _FakeClient()
    assert instance.ready() is True
    with pytest.raises(NotImplementedError):
        instance.verify(None)
    assert instance.reset_scenario_data() is None


def test_client_plugin_is_abstract():
    with pytest.raises(TypeError):
        ClientPlugin()


class _FakeAnalysis(AnalysisPlugin):
    def classification_rubric(self):
        return "rubric"

    def failure_prompt(self, task, context, *, max_chars):
        return "prompt"

    def extract_log_context(self, *, session_id, max_lines):
        return {}

    def eval_cases(self, task):
        return []


def test_analysis_plugin_fails_loud_when_unset():
    with pytest.raises(SystemExit, match="No AnalysisPlugin registered"):
        registry.get_analysis_plugin()


def test_analysis_plugin_set_and_get():
    plugin = _FakeAnalysis()
    registry.set_analysis_plugin(plugin)
    assert registry.get_analysis_plugin() is plugin


def test_analysis_plugin_is_abstract():
    with pytest.raises(TypeError):
        AnalysisPlugin()


class _FakeDatabase(DatabasePlugin):
    def __init__(self):
        self.was_reset = False

    def reset(self):
        self.was_reset = True


def test_database_plugin_fails_loud_when_unset():
    with pytest.raises(SystemExit, match="No DatabasePlugin registered"):
        registry.get_database_plugin()


def test_database_plugin_set_and_reset():
    plugin = _FakeDatabase()
    registry.set_database_plugin(plugin)
    registry.get_database_plugin().reset()
    assert plugin.was_reset is True


def test_database_plugin_is_abstract():
    with pytest.raises(TypeError):
        DatabasePlugin()


def test_layout_hook_default_is_a_harmless_noop():
    registry.run_layout_hook()  # must not raise even though nothing is registered


def test_layout_hook_runs_when_registered():
    calls = []
    registry.set_layout_hook(lambda: calls.append("ran"))
    registry.run_layout_hook()
    assert calls == ["ran"]
