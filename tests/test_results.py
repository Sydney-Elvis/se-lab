"""agent/results.py: RunContext/TestResult pass/fail/skip tracking and output."""

from __future__ import annotations

import io
import json

from rich.console import Console

from agent.dashboard import LiveDashboard
from agent.results import RunContext


def _dashboard(label="Test Lab", suite_total=1) -> tuple[LiveDashboard, io.StringIO]:
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=True, width=100)
    return LiveDashboard(label, suite_total, console=console), buffer


def test_run_context_counts_and_exit_code():
    ctx = RunContext("demo-suite", emit_progress=False)
    ctx.ok("t1", "worked")
    ctx.fail("t2", "broke", detail={"x": 1})
    ctx.skip("t3", "not applicable")
    assert ctx.passed_count == 1
    assert ctx.failed_count == 1
    assert ctx.skipped_count == 1
    assert ctx.exit_code() == 1


def test_run_context_all_passing_has_zero_exit_code():
    ctx = RunContext("demo-suite", emit_progress=False)
    ctx.ok("t1", "worked")
    assert ctx.exit_code() == 0


def test_record_convenience_maps_bool_and_none():
    ctx = RunContext("demo-suite", emit_progress=False)
    assert ctx.record("a", True, "m").status == "pass"
    assert ctx.record("b", False, "m").status == "fail"
    assert ctx.record("c", None, "m").status == "skip"


def test_to_dict_and_write_json(tmp_path):
    ctx = RunContext("demo-suite", emit_progress=False)
    ctx.ok("t1", "worked")
    ctx.fail("t2", "broke")
    d = ctx.to_dict()
    assert d == {
        "suite": "demo-suite",
        "total": 2,
        "passed": 1,
        "failed": 1,
        "skipped": 0,
        "results": [
            {"name": "t1", "status": "pass", "message": "worked"},
            {"name": "t2", "status": "fail", "message": "broke"},
        ],
    }
    out_path = tmp_path / "results.json"
    ctx.write_json(out_path)
    assert json.loads(out_path.read_text(encoding="utf-8")) == d


def test_progress_file_gets_one_event_per_result(tmp_path, monkeypatch):
    progress_path = tmp_path / "progress.jsonl"
    monkeypatch.setenv("LAB_PROGRESS_FILE", str(progress_path))
    ctx = RunContext("demo-suite")
    ctx.ok("t1", "worked")
    ctx.fail("t2", "broke")
    lines = progress_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first_event = json.loads(lines[0])
    assert first_event["name"] == "t1"
    assert first_event["status"] == "pass"
    assert first_event["completed"] == 1


def test_no_progress_file_set_means_no_file_written(tmp_path, monkeypatch):
    monkeypatch.delenv("LAB_PROGRESS_FILE", raising=False)
    ctx = RunContext("demo-suite")
    ctx.ok("t1", "worked")
    assert list(tmp_path.iterdir()) == []


def test_record_updates_dashboard_and_prints_through_it_without_raising():
    dashboard, buffer = _dashboard()
    dashboard.start_suite("demo-suite", 1, test_total=2)
    ctx = RunContext("demo-suite", emit_progress=False, dashboard=dashboard)
    ctx.ok("t1", "worked")
    assert dashboard.test_count == 1
    assert dashboard.latest_name == "t1"
    assert dashboard.any_failed is False
    ctx.fail("t2", "broke")
    assert dashboard.test_count == 2
    assert dashboard.any_failed is True
    out = buffer.getvalue()
    assert "[PASS] t1: worked" in out
    assert "[FAIL] t2: broke" in out


def test_record_rate_limits_dashboard_redraws_but_never_the_pass_fail_line(monkeypatch):
    # Two results back-to-back, both passing, faster than
    # LiveDashboard.MIN_RENDER_INTERVAL apart: the [PASS] lines must always
    # show (RunContext's own print isn't rate-limited, only the dashboard's
    # own redraw is), but only the first result's redraw should land.
    dashboard, buffer = _dashboard()
    dashboard.start_suite("demo-suite", 1, test_total=2)
    ctx = RunContext("demo-suite", emit_progress=False, dashboard=dashboard)

    now = [1000.0]
    monkeypatch.setattr("agent.dashboard.time.monotonic", lambda: now[0])
    ctx.ok("t1", "worked")  # dashboard._last_render_at starts at 0.0 -- always renders
    now[0] += 0.01  # well under MIN_RENDER_INTERVAL
    ctx.ok("t2", "worked again")

    out = buffer.getvalue()
    assert out.count("[PASS] t1: worked") == 1
    assert out.count("[PASS] t2: worked again") == 1
    assert dashboard.test_count == 2  # state is always current...
    assert "Last test: [PASS] t2" not in out  # ...even though t2's own redraw was rate-limited


def test_print_summary_prints_through_the_dashboard_without_stopping_it():
    # Unlike the old manual-ANSI implementation, the live footer no longer
    # needs to be torn down and redrawn around every suite's summary --
    # printing through the dashboard's own console (see LiveDashboard.print())
    # interleaves correctly while the footer keeps running.
    dashboard, buffer = _dashboard()
    dashboard.start_suite("demo-suite", 1, test_total=1)
    ctx = RunContext("demo-suite", emit_progress=False, dashboard=dashboard)
    ctx.ok("t1", "worked")
    assert dashboard.rendered is True
    ctx.print_summary()
    assert dashboard.rendered is True
    assert "demo-suite: 1/1 passed" in buffer.getvalue()
