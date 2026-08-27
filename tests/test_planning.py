"""agent/planning.py: RunPlan rendering and confirm() gating."""

from __future__ import annotations

import io

import pytest

from agent.planning import RunPlan, RunReport


def test_render_includes_title_rule_and_added_lines():
    plan = RunPlan(label="M3Undle Lab", host="toontown-int-srv1")
    plan.add("Resolved source", "branch main").add("Clean mode", "none")
    text = plan.render()
    assert "M3Undle Lab Run Plan" in text
    assert "=" * len("M3Undle Lab Run Plan") in text
    assert "Host: toontown-int-srv1" in text
    assert "Resolved source: branch main" in text
    assert "Clean mode: none" in text
    # opens and closes with the same rule, header/body in between
    lines = text.splitlines()
    assert lines[1] == lines[-1] == "=" * len("M3Undle Lab Run Plan")


def test_add_returns_self_for_chaining():
    plan = RunPlan(label="Test Lab", host="host")
    result = plan.add("A", "1").add("B", "2")
    assert result is plan
    assert plan.lines == [("A", "1"), ("B", "2")]


def test_confirm_assume_yes_skips_the_prompt(capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_: (_ for _ in ()).throw(AssertionError("should not prompt")))
    plan = RunPlan(label="Test Lab", host="host")
    assert plan.confirm(assume_yes=True) is True
    assert "Test Lab Run Plan" in capsys.readouterr().out


def test_confirm_accepts_y_or_yes(monkeypatch):
    plan = RunPlan(label="Test Lab", host="host")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    assert plan.confirm() is True
    monkeypatch.setattr("builtins.input", lambda *_: "yes")
    assert plan.confirm() is True


def test_confirm_rejects_anything_else(monkeypatch):
    plan = RunPlan(label="Test Lab", host="host")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    for reply in ("n", "no", "", "sure"):
        monkeypatch.setattr("builtins.input", lambda *_, r=reply: r)
        assert plan.confirm() is False


def test_confirm_raises_on_non_interactive_stdin_without_assume_yes(monkeypatch):
    plan = RunPlan(label="Test Lab", host="host")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # isatty() is False
    with pytest.raises(SystemExit, match="--yes"):
        plan.confirm()


def test_confirm_eof_on_input_is_treated_as_no(monkeypatch):
    plan = RunPlan(label="Test Lab", host="host")

    def _raise_eof(*_):
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise_eof)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert plan.confirm() is False


def test_run_report_render_includes_title_rule_and_added_lines():
    report = RunReport(label="M3Undle Lab")
    report.add("Source", "branch main").add("Result", "PASS")
    text = report.render()
    assert "M3Undle Lab Run Summary" in text
    assert "Source: branch main" in text
    assert "Result: PASS" in text
    lines = text.splitlines()
    assert lines[1] == lines[-1] == "=" * len("M3Undle Lab Run Summary")


def test_run_report_aligns_values_into_a_column():
    report = RunReport(label="Test Lab")
    report.add("Started at UTC", "2026-08-27T17:30:07Z")
    report.add("Completed at UTC", "2026-08-27T17:46:53Z")
    lines = report.render().splitlines()
    started, completed = lines[2], lines[3]
    assert started.index("2026") == completed.index("2026")


def test_run_report_add_returns_self_for_chaining():
    report = RunReport(label="Test Lab")
    result = report.add("A", "1").add("B", "2")
    assert result is report
    assert report.lines == [("A", "1"), ("B", "2")]


def test_run_report_print_writes_the_rendered_block(capsys):
    RunReport(label="Test Lab").add("Result", "PASS").print()
    out = capsys.readouterr().out
    assert "Test Lab Run Summary" in out
    assert "Result: PASS" in out
