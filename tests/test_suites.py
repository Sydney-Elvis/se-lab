"""agent/suites.py: case registration, discovery, isolated execution, drift."""

from __future__ import annotations

import textwrap

import pytest

from agent.results import RunContext
from agent.suites import Suite, discover_suites, run_suite, run_suites, suite, suites_in_group


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


def test_teardown_always_runs_and_its_failure_does_not_mask_results(capsys):
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
    assert "cleanup failed" in capsys.readouterr().out


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


def test_run_suites_with_live_progress_forced_on_still_produces_correct_results(tmp_path, capsys):
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
    out = capsys.readouterr().out
    assert "Test Lab" in out
    assert "[PASS] P-01: fine" in out
    assert "[FAIL] B-01: nope" in out


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
