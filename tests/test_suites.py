"""agent/suites.py: case registration, discovery, isolated execution, drift."""

from __future__ import annotations

import textwrap

import pytest

from agent import common as lab_common
from agent.results import RunContext
from agent.suites import Case, Suite, discover_suites, run_suite, run_suites, select_suites, suite, suites_in_group


def test_case_registers_in_declaration_order():
    s = suite("demo")

    @s.case("DEMO-01")
    def first(ctx):
        pass

    @s.case("DEMO-02")
    def second(ctx):
        pass

    assert [c.test_id for c in s.cases] == ["DEMO-01", "DEMO-02"]
    # decorator returns the function unchanged -- still directly callable
    assert first.__name__ == "first"


def test_duplicate_case_id_in_same_suite_raises():
    s = suite("demo")

    @s.case("DEMO-01")
    def first(ctx):
        pass

    with pytest.raises(ValueError, match="DEMO-01"):
        @s.case("DEMO-01")
        def second(ctx):
            pass


def test_expected_count_is_len_of_registered_cases_not_a_declared_number():
    s = suite("demo")
    for i in range(5):
        s.case(f"DEMO-{i}")(lambda ctx: None)
    assert len(s.cases) == 5


def test_run_suite_calls_every_case_and_matches_expected_to_actual():
    s = suite("demo")

    @s.case("DEMO-01")
    def c1(ctx):
        ctx.ok("DEMO-01", "fine")

    @s.case("DEMO-02")
    def c2(ctx):
        ctx.ok("DEMO-02", "fine")

    ctx = RunContext("demo", emit_progress=False)
    result = run_suite(s, ctx)
    assert result.expected == 2
    assert result.actual == 2
    assert result.drifted is False
    assert result.setup_ok is True
    assert ctx.passed_count == 2


class _FakeDashboard:
    def __init__(self):
        self.printed: list[str] = []

    def print(self, text, **kwargs):
        self.printed.append(text)

    def record_result(self, **kwargs):
        pass

    def render(self):
        pass

    def maybe_render(self, **kwargs):
        pass


def test_run_suite_suppresses_case_prints_on_success_when_dashboard_active():
    s = suite("demo")

    @s.case("DEMO-01")
    def c1(ctx):
        print("noisy debug line")
        ctx.ok("DEMO-01", "fine")

    dashboard = _FakeDashboard()
    ctx = RunContext("demo", emit_progress=False, dashboard=dashboard)
    run_suite(s, ctx)
    out = "".join(dashboard.printed)
    assert "noisy debug line" not in out
    assert "[PASS] DEMO-01: fine" in out


def test_run_suite_flushes_case_prints_on_explicit_failure_when_dashboard_active():
    s = suite("demo")

    @s.case("DEMO-01")
    def c1(ctx):
        print("useful failure context")
        ctx.fail("DEMO-01", "broke")

    dashboard = _FakeDashboard()
    ctx = RunContext("demo", emit_progress=False, dashboard=dashboard)
    run_suite(s, ctx)
    out = "".join(dashboard.printed)
    assert "useful failure context" in out
    assert "[FAIL] DEMO-01: broke" in out


def test_run_suite_flushes_case_prints_on_unhandled_exception_when_dashboard_active():
    s = suite("demo")

    @s.case("DEMO-01")
    def c1(ctx):
        print("about to blow up")
        raise RuntimeError("boom")

    dashboard = _FakeDashboard()
    ctx = RunContext("demo", emit_progress=False, dashboard=dashboard)
    run_suite(s, ctx)
    out = "".join(dashboard.printed)
    assert "about to blow up" in out
    assert "Unhandled exception in DEMO-01" in out


def test_run_suite_suppresses_setup_prints_on_success_when_dashboard_active():
    s = suite("demo")

    @s.setup
    def _setup():
        print("setup noise")
        return {"x": 1}

    @s.case("DEMO-01")
    def c1(ctx, x):
        ctx.ok("DEMO-01", f"x={x}")

    dashboard = _FakeDashboard()
    ctx = RunContext("demo", emit_progress=False, dashboard=dashboard)
    run_suite(s, ctx)
    out = "".join(dashboard.printed)
    assert "setup noise" not in out
    assert "[PASS] DEMO-01: x=1" in out


def test_run_suite_flushes_setup_prints_on_setup_failure_when_dashboard_active():
    s = suite("demo")

    @s.setup
    def _setup():
        print("setup context before failure")
        raise RuntimeError("setup broke")

    @s.case("DEMO-01")
    def c1(ctx):
        ctx.ok("DEMO-01", "unreachable")

    dashboard = _FakeDashboard()
    ctx = RunContext("demo", emit_progress=False, dashboard=dashboard)
    run_suite(s, ctx)
    out = "".join(dashboard.printed)
    assert "setup context before failure" in out
    assert "Suite setup failed" in out


def test_run_suite_does_not_capture_prints_when_no_dashboard(capfd):
    s = suite("demo")

    @s.case("DEMO-01")
    def c1(ctx):
        print("visible by default")
        ctx.ok("DEMO-01", "fine")

    ctx = RunContext("demo", emit_progress=False, dashboard=None)
    run_suite(s, ctx)
    out = capfd.readouterr().out
    assert "visible by default" in out


def test_case_that_raises_is_isolated_and_recorded_as_failure():
    s = suite("demo")
    calls = []

    @s.case("DEMO-01")
    def c1(ctx):
        calls.append("c1")
        raise RuntimeError("boom")

    @s.case("DEMO-02")
    def c2(ctx):
        calls.append("c2")
        ctx.ok("DEMO-02", "fine")

    ctx = RunContext("demo", emit_progress=False)
    result = run_suite(s, ctx)
    # both cases ran -- one crashing must not cost the ones after it
    assert calls == ["c1", "c2"]
    assert result.expected == 2
    assert result.actual == 2  # the crash was itself recorded as a result
    assert result.drifted is False
    assert ctx.failed_count == 1
    assert ctx.passed_count == 1
    failed = [r for r in ctx.results if r.status == "fail"][0]
    assert failed.name == "DEMO-01"
    assert "boom" in failed.message


def test_case_that_forgets_to_record_causes_real_drift():
    """The gap static function-counting can't close: a registered case whose
    own code path returns without calling ctx.record at all."""
    s = suite("demo")

    @s.case("DEMO-01")
    def c1(ctx):
        pass  # bug: never calls ctx.ok/fail/skip

    @s.case("DEMO-02")
    def c2(ctx):
        ctx.ok("DEMO-02", "fine")

    ctx = RunContext("demo", emit_progress=False)
    result = run_suite(s, ctx)
    assert result.expected == 2
    assert result.actual == 1
    assert result.drifted is True


def test_setup_failure_stops_cases_and_is_not_reported_as_drift():
    s = suite("demo")
    case_ran = []

    @s.setup
    def _setup():
        raise RuntimeError("simulator did not come up")

    @s.case("DEMO-01")
    def c1(ctx):
        case_ran.append(True)
        ctx.ok("DEMO-01", "fine")

    ctx = RunContext("demo", emit_progress=False)
    result = run_suite(s, ctx)
    assert case_ran == []
    assert result.setup_ok is False
    assert "simulator did not come up" in result.setup_error
    assert result.drifted is False  # aborted suite, not a count mismatch
    assert ctx.failed_count == 1
    assert ctx.results[0].name == "demo-SETUP"


def test_setup_return_value_is_merged_and_matched_by_parameter_name():
    s = suite("demo")

    @s.setup
    def _setup(m3undle_url):
        assert m3undle_url == "http://example"
        return {"base": "http://example/api", "expected_channels": 7}

    @s.case("DEMO-01")
    def c1(ctx, base):
        ctx.ok("DEMO-01", base)

    @s.case("DEMO-02")
    def c2(ctx, base, expected_channels):
        ctx.ok("DEMO-02", f"{base} {expected_channels}")

    ctx = RunContext("demo", emit_progress=False)
    result = run_suite(s, ctx, m3undle_url="http://example")
    assert result.actual == 2
    assert ctx.results[0].message == "http://example/api"
    assert ctx.results[1].message == "http://example/api 7"


def test_teardown_always_runs_and_its_failure_does_not_mask_results(capfd):
    # capfd, not capsys: the teardown-failure warning writes via
    # sys.__stdout__ unconditionally (same reasoning as RunContext._record()
    # and LiveDashboard -- must stay visible regardless of any active
    # redirect), so capsys won't see it.
    s = suite("demo")

    @s.case("DEMO-01")
    def c1(ctx):
        ctx.ok("DEMO-01", "fine")

    @s.teardown
    def _teardown():
        raise RuntimeError("cleanup failed")

    ctx = RunContext("demo", emit_progress=False)
    result = run_suite(s, ctx)
    assert result.actual == 1
    assert ctx.passed_count == 1
    assert "cleanup failed" in capfd.readouterr().out


def _write_suite_file(directory, filename, suite_name, case_count=2):
    directory.joinpath(filename).write_text(
        textwrap.dedent(f"""
            from agent.suites import suite

            SUITE = suite({suite_name!r})

            """)
        + "\n".join(
            textwrap.dedent(f"""
                @SUITE.case("{suite_name.upper()}-{i:02d}")
                def case_{i}(ctx):
                    ctx.ok("{suite_name.upper()}-{i:02d}", "fine")
                """)
            for i in range(case_count)
        ),
        encoding="utf-8",
    )


def test_discover_suites_finds_test_star_files_and_skips_others(tmp_path):
    _write_suite_file(tmp_path, "test_alpha.py", "alpha", case_count=2)
    _write_suite_file(tmp_path, "test_beta.py", "beta", case_count=3)
    tmp_path.joinpath("fixtures.py").write_text("VALUE = 1\n", encoding="utf-8")
    tmp_path.joinpath("not_a_test_file.py").write_text("SUITE = 'not a Suite instance'\n", encoding="utf-8")

    discovered = discover_suites(tmp_path)

    assert [s.name for s in discovered] == ["alpha", "beta"]
    assert len(discovered[0].cases) == 2
    assert len(discovered[1].cases) == 3


def test_discover_suites_raises_on_duplicate_suite_name(tmp_path):
    _write_suite_file(tmp_path, "test_one.py", "dup")
    _write_suite_file(tmp_path, "test_two.py", "dup")

    with pytest.raises(ValueError, match="dup"):
        discover_suites(tmp_path)


def test_suites_in_group_filters_and_all_bypasses():
    suites = [
        Suite(name="a", group="core"),
        Suite(name="b", group="stream-hardening"),
        Suite(name="c", group="core"),
    ]
    assert [s.name for s in suites_in_group(suites, "core")] == ["a", "c"]
    assert [s.name for s in suites_in_group(suites, "stream-hardening")] == ["b"]
    assert [s.name for s in suites_in_group(suites, "all")] == ["a", "b", "c"]


def _selectable_suites() -> list[Suite]:
    core_a = Suite(name="core-a", group="core", cases=[Case("CA-01", lambda ctx: None), Case("CA-02", lambda ctx: None)])
    core_b = Suite(name="core-b", group="core", cases=[Case("CB-01", lambda ctx: None)])
    hardening = Suite(name="stream", group="stream-hardening", cases=[Case("ST-01", lambda ctx: None)])
    return [core_a, core_b, hardening]


def test_select_suites_defaults_to_all_when_nothing_given():
    suites = _selectable_suites()
    assert [s.name for s in select_suites(suites)] == ["core-a", "core-b", "stream"]


def test_select_suites_by_group():
    suites = _selectable_suites()
    assert [s.name for s in select_suites(suites, group="core")] == ["core-a", "core-b"]


def test_select_suites_by_only_selects_a_single_named_suite():
    suites = _selectable_suites()
    assert [s.name for s in select_suites(suites, only="core-b")] == ["core-b"]


def test_select_suites_by_only_unknown_name_raises_with_available_list():
    with pytest.raises(SystemExit, match="core-a, core-b, stream"):
        select_suites(_selectable_suites(), only="does-not-exist")


def test_select_suites_by_group_unknown_raises_with_available_list():
    with pytest.raises(SystemExit, match="core, stream-hardening"):
        select_suites(_selectable_suites(), group="does-not-exist")


def test_select_suites_narrows_to_one_case_within_the_group():
    suites = select_suites(_selectable_suites(), group="core", case="CA-02")
    assert [s.name for s in suites] == ["core-a"]
    assert [c.test_id for c in suites[0].cases] == ["CA-02"]


def test_select_suites_case_narrowing_drops_suites_with_no_matching_case():
    suites = select_suites(_selectable_suites(), case="CB-01")
    assert [s.name for s in suites] == ["core-b"]


def test_discovery_order_is_by_declared_order_then_name(tmp_path):
    tmp_path.joinpath("test_z.py").write_text(
        "from agent.suites import suite\nSUITE = suite('z-suite', order=10)\n", encoding="utf-8"
    )
    tmp_path.joinpath("test_a.py").write_text(
        "from agent.suites import suite\nSUITE = suite('a-suite', order=10)\n", encoding="utf-8"
    )
    tmp_path.joinpath("test_early.py").write_text(
        "from agent.suites import suite\nSUITE = suite('early-suite', order=1)\n", encoding="utf-8"
    )
    discovered = discover_suites(tmp_path)
    assert [s.name for s in discovered] == ["early-suite", "a-suite", "z-suite"]


def test_run_suites_writes_one_result_json_per_suite_and_reports_no_failure(tmp_path):
    passing = suite("passing")

    @passing.case("P-01")
    def p1(ctx):
        ctx.ok("P-01", "fine")

    also_passing = suite("also-passing")

    @also_passing.case("Q-01")
    def q1(ctx):
        ctx.ok("Q-01", "fine")

    summary = run_suites([passing, also_passing], results_dir=tmp_path)

    assert summary.failed is False
    assert [r.suite_name for r in summary.results] == ["passing", "also-passing"]
    assert (tmp_path / "results-passing.json").exists()
    assert (tmp_path / "results-also-passing.json").exists()


def test_run_suites_with_live_progress_forced_on_still_produces_correct_results(tmp_path, capfd):
    # capfd, not capsys: dashboard/result output goes via sys.__stdout__
    # deliberately (see agent/dashboard.py, agent/results.py), which capsys
    # can't see.
    passing = suite("passing")

    @passing.case("P-01")
    def p1(ctx):
        ctx.ok("P-01", "fine")

    broken = suite("broken")

    @broken.case("B-01")
    def b1(ctx):
        ctx.fail("B-01", "nope")

    summary = run_suites([passing, broken], results_dir=tmp_path, label="Test Lab", live_progress=True)

    assert summary.failed is True
    assert [r.suite_name for r in summary.results] == ["passing", "broken"]
    out = capfd.readouterr().out
    assert "Test Lab" in out
    assert "[PASS] P-01: fine" in out
    assert "[FAIL] B-01: nope" in out


def test_run_suites_prints_durable_suite_header_even_with_dashboard_active(tmp_path, capfd):
    # Regression test: previously the "--- Running suite ---" header only
    # printed when there was no dashboard -- with one active, the suite name
    # only ever existed inside the transient in-place block. A suite whose
    # own setup hangs or dies before any case records a result (e.g. a
    # scenario's compose up) left scrollback with zero trace of which suite
    # was even attempted. See agent/suites.py's run_suites().
    target = suite("base-security")

    @target.case("SEC-01")
    def case(ctx):
        ctx.ok("SEC-01", "fine")

    run_suites([target], results_dir=tmp_path, label="Test Lab", live_progress=True)

    out = capfd.readouterr().out
    assert "--- Running suite: base-security ---" in out


def test_run_suites_final_render_shows_all_suites_complete(tmp_path, capfd):
    # Regression, confirmed against a real 22-suite run: the final frame used
    # to read "Overall PASS" with the Suites bar stuck at (total - 1) --
    # there was no explicit signal that the *last* suite had itself finished,
    # only that it had started. See LiveDashboard.finish_suite().
    first = suite("first")

    @first.case("A-01")
    def a1(ctx):
        ctx.ok("A-01", "fine")

    second = suite("second")

    @second.case("B-01")
    def b1(ctx):
        ctx.ok("B-01", "fine")

    run_suites([first, second], results_dir=tmp_path, label="Test Lab", live_progress=True)

    out = capfd.readouterr().out
    assert "2/2" in out


def test_run_suites_final_render_reflects_true_completion_despite_rate_limiting(tmp_path, capfd, monkeypatch):
    # Regression, confirmed against a real run: LiveDashboard.maybe_render()
    # rate-limits mid-run redraws (MIN_RENDER_INTERVAL), which for a suite of
    # fast, back-to-back passing cases can leave the box showing a stale
    # snapshot from several results ago as its very last visible state before
    # teardown. run_suites()'s finally block now forces one last unthrottled
    # render first. Freezing time.monotonic makes every maybe_render() after
    # the first look "too soon", so the only way "COMPLETED-04" (the last
    # case) can appear is via that forced final render.
    now = [1000.0]
    monkeypatch.setattr("agent.dashboard.time.monotonic", lambda: now[0])

    s = suite("fast")

    def _make_case(test_id):
        def case(ctx):
            ctx.ok(test_id, "fine")

        return case

    for i in range(5):
        test_id = f"COMPLETED-{i:02d}"
        s.case(test_id)(_make_case(test_id))

    run_suites([s], results_dir=tmp_path, label="Test Lab", live_progress=True)

    out = capfd.readouterr().out
    assert "5/5" in out
    assert "Overall PASS" in out


def test_run_suites_registers_and_clears_the_active_dashboard_with_live_progress(tmp_path):
    passing = suite("passing")

    @passing.case("P-01")
    def p1(ctx):
        # While this case runs, common.run()'s subprocess path must see the
        # dashboard registered -- confirms run_suites() wires it up before
        # any case executes, not just around the loop's own prints.
        assert lab_common._ACTIVE_DASHBOARD is not None

    run_suites([passing], results_dir=tmp_path, label="Test Lab", live_progress=True)
    assert lab_common._ACTIVE_DASHBOARD is None


def test_run_suites_clears_the_active_dashboard_even_when_something_unexpected_raises(tmp_path, monkeypatch):
    # run_suite() itself never raises (case/setup failures are caught and
    # recorded) -- this simulates the unexpected case the try/finally guards
    # against, e.g. a bug elsewhere in the loop body.
    import agent.suites as suites_module

    def _boom(target, ctx, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(suites_module, "run_suite", _boom)
    with pytest.raises(RuntimeError, match="boom"):
        run_suites([suite("s")], results_dir=tmp_path, label="Test Lab", live_progress=True)
    assert lab_common._ACTIVE_DASHBOARD is None


def test_run_suites_reports_failed_when_any_case_fails(tmp_path):
    broken = suite("broken")

    @broken.case("B-01")
    def b1(ctx):
        ctx.fail("B-01", "nope")

    summary = run_suites([broken], results_dir=tmp_path)

    assert summary.failed is True
    assert summary.results[0].suite_name == "broken"


def test_run_suites_reports_failed_on_setup_failure_without_drift(tmp_path):
    broken_setup = suite("broken-setup")

    @broken_setup.setup
    def _setup():
        raise RuntimeError("boom")

    @broken_setup.case("S-01")
    def s1(ctx):
        ctx.ok("S-01", "unreachable")

    summary = run_suites([broken_setup], results_dir=tmp_path)

    assert summary.failed is True
    assert summary.results[0].setup_ok is False
    assert summary.results[0].drifted is False


def test_run_suites_passes_base_context_through_to_cases(tmp_path):
    s = suite("needs-context")

    @s.case("C-01")
    def c1(ctx, widget):
        ctx.record("C-01", widget == "provided", f"widget={widget!r}")

    summary = run_suites([s], results_dir=tmp_path, widget="provided")

    assert summary.failed is False
