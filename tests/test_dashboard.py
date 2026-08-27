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


def test_mode_plain_ignores_tty_and_term(monkeypatch):
    # "plain" never uses cursor-addressing, so unlike "1" it isn't vetoed by
    # a dumb TERM or a non-tty stdout -- that's the whole point of it.
    monkeypatch.setenv("LAB_LIVE_PROGRESS", "plain")
    monkeypatch.setenv("TERM", "dumb")
    assert LiveDashboard.mode() == "plain"
    assert LiveDashboard.supported() is True


def test_mode_autodetects_inplace_or_off(monkeypatch):
    monkeypatch.delenv("LAB_LIVE_PROGRESS", raising=False)
    monkeypatch.setenv("TERM", "dumb")
    assert LiveDashboard.mode() == "off"


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


def test_plain_render_prints_a_durable_line_with_no_cursor_codes(capfd):
    dashboard = LiveDashboard("Test Lab", 2, plain=True)
    dashboard.start_suite("suite-a", 1, test_total=3)
    dashboard.render()
    dashboard.record_result(name="CASE-01", status="pass", completed=1, failed=False)
    dashboard.render()
    out = capfd.readouterr().out
    assert "\x1b[" not in out  # no cursor-up / clear-line / hide-cursor codes at all
    assert "CASE-01" in out
    assert out.count("\n") == 2  # two renders, each one durable line -- nothing overwritten


def test_plain_clear_is_always_a_noop(capfd):
    dashboard = LiveDashboard("Test Lab", 1, plain=True)
    dashboard.start_suite("suite-a", 1, test_total=1)
    dashboard.render()
    capfd.readouterr()
    dashboard.clear()
    assert capfd.readouterr().out == ""
    assert dashboard.rendered is True  # nothing to un-render in plain mode


def test_clear_before_first_render_is_a_noop(capsys):
    dashboard = LiveDashboard("Test Lab", 1)
    dashboard.clear()  # must not write cursor-control codes with nothing on screen yet
    assert capsys.readouterr().out == ""


def test_maybe_render_skips_when_called_again_too_soon(capfd, monkeypatch):
    dashboard = LiveDashboard("Test Lab", 1)
    dashboard.start_suite("suite-a", 1, test_total=5)
    dashboard.render()
    capfd.readouterr()  # discard the first render's output

    now = [1000.0]
    monkeypatch.setattr("agent.dashboard.time.monotonic", lambda: now[0])
    dashboard._last_render_at = now[0]
    now[0] += 0.01  # well under MIN_RENDER_INTERVAL
    dashboard.maybe_render()
    assert capfd.readouterr().out == ""


def test_maybe_render_renders_once_the_interval_has_passed(capfd, monkeypatch):
    dashboard = LiveDashboard("Test Lab", 1)
    dashboard.start_suite("suite-a", 1, test_total=5)
    dashboard.render()
    capfd.readouterr()

    now = [1000.0]
    monkeypatch.setattr("agent.dashboard.time.monotonic", lambda: now[0])
    dashboard._last_render_at = now[0]
    now[0] += LiveDashboard.MIN_RENDER_INTERVAL + 0.01
    dashboard.maybe_render()
    assert "Test Lab" in capfd.readouterr().out


def test_maybe_render_force_always_renders_even_when_too_soon(capfd, monkeypatch):
    dashboard = LiveDashboard("Test Lab", 1)
    dashboard.start_suite("suite-a", 1, test_total=5)
    dashboard.render()
    capfd.readouterr()

    now = [1000.0]
    monkeypatch.setattr("agent.dashboard.time.monotonic", lambda: now[0])
    dashboard._last_render_at = now[0]
    now[0] += 0.01
    dashboard.maybe_render(force=True)
    assert "Test Lab" in capfd.readouterr().out
