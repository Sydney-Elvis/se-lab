"""agent/dashboard.py: LiveDashboard rendering and the pure progress-bar helpers."""

from __future__ import annotations

from agent.dashboard import LiveDashboard, activity_bar, progress_bar


def test_progress_bar_fills_proportionally():
    assert progress_bar(0, 4) == "[" + "." * 20 + "]"
    assert progress_bar(4, 4) == "[" + "#" * 20 + "]"
    assert progress_bar(2, 4) == "[" + "#" * 10 + "." * 10 + "]"


def test_progress_bar_handles_zero_total():
    assert progress_bar(0, 0) == "[" + "." * 20 + "]"


def test_activity_bar_marker_moves_with_position():
    first = activity_bar(0)
    second = activity_bar(1)
    assert first != second
    assert first.count(">") == 1
    assert second.count(">") == 1


def test_supported_respects_explicit_env_override(monkeypatch):
    monkeypatch.setenv("LAB_LIVE_PROGRESS", "0")
    assert LiveDashboard.supported() is False
    monkeypatch.setenv("LAB_LIVE_PROGRESS", "1")
    monkeypatch.setenv("TERM", "xterm")
    assert LiveDashboard.supported() is True


def test_supported_false_on_dumb_term_even_when_forced(monkeypatch):
    monkeypatch.setenv("LAB_LIVE_PROGRESS", "1")
    monkeypatch.setenv("TERM", "dumb")
    assert LiveDashboard.supported() is False


def test_render_and_clear_do_not_raise(capfd):
    # capfd, not capsys: LiveDashboard writes via sys.__stdout__ deliberately
    # (see the class docstring/comments) so it stays visible even when
    # run_suite() redirects sys.stdout to capture a suite's own prints.
    # capsys only tracks sys.stdout-level writes; capfd tracks the real fd.
    dashboard = LiveDashboard("Test Lab", 3)
    dashboard.start_suite("suite-a", 1, test_total=5)
    dashboard.render()
    dashboard.record_result(name="CASE-01", status="pass", completed=1, failed=False)
    dashboard.render()
    dashboard.record_result(name="CASE-02", status="fail", completed=2, failed=True)
    dashboard.render()
    out = capfd.readouterr().out
    assert "Test Lab" in out
    assert "CASE-02" in out
    assert dashboard.any_failed is True
    dashboard.clear()
    assert dashboard.rendered is False


def test_clear_before_first_render_is_a_noop(capsys):
    dashboard = LiveDashboard("Test Lab", 1)
    dashboard.clear()  # must not write cursor-control codes with nothing on screen yet
    assert capsys.readouterr().out == ""
