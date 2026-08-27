"""agent/dashboard.py: LiveDashboard state, lifecycle, and rendering.

Assertions read the dashboard's own injected Console (an io.StringIO-backed
Console, so no real terminal/fd is involved) rather than capsys/capfd -- the
point of moving to Rich is that exact escape sequences are no longer this
module's concern; only the text content and the state machine (start/stop/
render) are. _plain_text() strips any ANSI style codes Rich still emits
(force_terminal=True, for realistic coverage of the colored/live path) so
assertions can match text that spans a style boundary (e.g. "Overall "
and "RUNNING" are two separately-styled runs).
"""

from __future__ import annotations

import io
import re

from rich.console import Console

from agent.dashboard import LiveDashboard

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _plain_text(raw: str) -> str:
    return _ANSI_RE.sub("", raw)


def _dashboard(label="Test Lab", suite_total=3, *, plain=False, width=100) -> tuple[LiveDashboard, io.StringIO]:
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=not plain, width=width, no_color=plain)
    dashboard = LiveDashboard(label, suite_total, plain=plain, console=console)
    return dashboard, buffer


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


def test_suites_completed_reaches_total_only_after_finish_suite_is_called():
    # Regression, confirmed against a real 22-suite run: completed_suites
    # used to be inferred as suite_index - 1 ("suites before the one
    # currently running"), which never reaches suite_total once the *last*
    # suite itself finishes -- there's no next start_suite() call to imply
    # it. finish_suite() is the explicit signal instead.
    dashboard, buffer = _dashboard(suite_total=2)
    dashboard.start_suite("suite-a", 1, test_total=1)
    dashboard.record_result(name="A-01", status="pass", completed=1, failed=False)
    dashboard.render()
    assert "1/2" not in buffer.getvalue()  # nothing finished yet
    dashboard.finish_suite()

    dashboard.start_suite("suite-b", 2, test_total=1)
    dashboard.record_result(name="B-01", status="pass", completed=1, failed=False)
    dashboard.render()
    assert "1/2" in buffer.getvalue()  # suite-a finished, suite-b in progress
    dashboard.finish_suite()
    dashboard.render()
    assert "2/2" in buffer.getvalue()  # both suites now finished


def test_bar_styles_turn_red_once_anything_has_failed():
    dashboard, _ = _dashboard()
    assert dashboard._bar_styles() == ("bar.complete", "bar.finished")
    dashboard.record_result(name="X", status="fail", completed=1, failed=True)
    assert dashboard._bar_styles() == ("red", "red")


def test_render_shows_label_suite_and_test_state():
    dashboard, buffer = _dashboard()
    dashboard.start_suite("suite-a", 1, test_total=5)
    dashboard.render()
    dashboard.record_result(name="CASE-01", status="pass", completed=1, failed=False)
    dashboard.render()
    dashboard.record_result(name="CASE-02", status="fail", completed=2, failed=True)
    dashboard.render()
    out = buffer.getvalue()
    assert "Test Lab" in out
    assert "CASE-02" in out
    assert dashboard.any_failed is True
    dashboard.stop()
    assert dashboard.rendered is False


def test_render_truncates_long_names_instead_of_wrapping():
    dashboard, buffer = _dashboard(width=60)
    long_name = "a-suite-name-so-long-it-would-otherwise-blow-out-the-footer-width"
    dashboard.start_suite(long_name, 1, test_total=1)
    dashboard.render()
    out = buffer.getvalue()
    assert long_name not in out
    assert "…" in out


def test_overall_status_stays_running_mid_suite_not_pass():
    # Regression: the ported logic used to flip to "PASS" as soon as
    # anything at all had happened (the first test of the first suite),
    # well before the run was anywhere near done -- see _overall_status()'s
    # own docstring.
    dashboard, buffer = _dashboard(suite_total=3)
    dashboard.start_suite("suite-a", 1, test_total=5)
    dashboard.record_result(name="CASE-01", status="pass", completed=1, failed=False)
    dashboard.render()
    out = _plain_text(buffer.getvalue())
    assert "Overall RUNNING" in out
    assert "Overall PASS" not in out


def test_overall_status_is_pass_only_once_the_last_suites_last_test_completes():
    dashboard, buffer = _dashboard(suite_total=1)
    dashboard.start_suite("suite-a", 1, test_total=1)
    dashboard.record_result(name="CASE-01", status="pass", completed=1, failed=False)
    dashboard.render()
    assert "Overall PASS" in _plain_text(buffer.getvalue())


def test_indeterminate_test_progress_when_total_is_zero():
    # A suite with zero registered cases (or before start_suite has run) has
    # no meaningful test total -- must not raise dividing/formatting against
    # a total of zero.
    dashboard, buffer = _dashboard()
    dashboard.start_suite("empty-suite", 1, test_total=0)
    dashboard.render()
    assert "Tests" in buffer.getvalue()


def test_plain_render_prints_a_durable_line_with_no_ansi_codes():
    dashboard, buffer = _dashboard(plain=True)
    dashboard.start_suite("suite-a", 1, test_total=3)
    dashboard.render()
    dashboard.record_result(name="CASE-01", status="pass", completed=1, failed=False)
    dashboard.render()
    out = buffer.getvalue()
    assert "\x1b[" not in out
    assert "CASE-01" in out
    assert out.count("\n") == 2  # two renders, each one durable line -- nothing overwritten


def test_plain_stop_is_always_a_noop():
    dashboard, buffer = _dashboard(plain=True)
    dashboard.start_suite("suite-a", 1, test_total=1)
    dashboard.render()
    buffer.seek(0)
    buffer.truncate()
    dashboard.stop()
    assert buffer.getvalue() == ""
    assert dashboard.rendered is True  # nothing to un-render in plain mode


def test_stop_before_first_render_is_a_noop():
    dashboard, buffer = _dashboard()
    dashboard.stop()  # must not raise with nothing ever started
    assert dashboard.rendered is False


def test_print_interleaves_through_the_same_console_while_live():
    dashboard, buffer = _dashboard()
    dashboard.start_suite("suite-a", 1, test_total=1)
    dashboard.render()
    dashboard.print("+ docker compose up")
    out = buffer.getvalue()
    assert "+ docker compose up" in out


def test_print_does_not_interpret_markup_in_arbitrary_text():
    dashboard, buffer = _dashboard()
    dashboard.print("[+] Running 3/3 containers")
    assert "[+] Running 3/3 containers" in buffer.getvalue()


def test_maybe_render_skips_when_called_again_too_soon(monkeypatch):
    dashboard, buffer = _dashboard()
    dashboard.start_suite("suite-a", 1, test_total=5)
    dashboard.render()
    buffer.seek(0)
    buffer.truncate()

    now = [1000.0]
    monkeypatch.setattr("agent.dashboard.time.monotonic", lambda: now[0])
    dashboard._last_render_at = now[0]
    now[0] += 0.01  # well under MIN_RENDER_INTERVAL
    dashboard.maybe_render()
    assert buffer.getvalue() == ""


def test_maybe_render_renders_once_the_interval_has_passed(monkeypatch):
    dashboard, buffer = _dashboard()
    dashboard.start_suite("suite-a", 1, test_total=5)
    dashboard.render()
    buffer.seek(0)
    buffer.truncate()

    now = [1000.0]
    monkeypatch.setattr("agent.dashboard.time.monotonic", lambda: now[0])
    dashboard._last_render_at = now[0]
    now[0] += LiveDashboard.MIN_RENDER_INTERVAL + 0.01
    dashboard.maybe_render()
    assert "Test Lab" in buffer.getvalue()


def test_maybe_render_force_always_renders_even_when_too_soon(monkeypatch):
    dashboard, buffer = _dashboard()
    dashboard.start_suite("suite-a", 1, test_total=5)
    dashboard.render()
    buffer.seek(0)
    buffer.truncate()

    now = [1000.0]
    monkeypatch.setattr("agent.dashboard.time.monotonic", lambda: now[0])
    dashboard._last_render_at = now[0]
    now[0] += 0.01
    dashboard.maybe_render(force=True)
    assert "Test Lab" in buffer.getvalue()
