"""agent/results.py: RunContext/TestResult pass/fail/skip tracking and output."""

from __future__ import annotations

import json

from agent.results import RunContext


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
