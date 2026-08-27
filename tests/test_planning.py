"""agent/planning.py: RunPlan rendering and confirm() gating."""

from __future__ import annotations

import io

import pytest

from agent.planning import RunPlan


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
