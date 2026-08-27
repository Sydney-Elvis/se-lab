"""Test-suite discovery, registration, and execution.

Replaces the hand-maintained SUITE_SCRIPTS/SUITE_TEST_TOTALS/CORE_SUITE_ORDER
dicts a suite author used to have to edit in a completely different file
every time a test was added, removed, or a suite was created. Those drifted
in practice -- the docs table describing them was already out of sync with
over half the real suites before this was written.

The fix is not "count test functions automatically" -- a function that exists
in the file but was never wired into the run sequence would still get
counted, which defeats the point. The fix is to make declaration and
execution the same list: a suite registers its cases with `@suite.case(...)`,
the harness runs exactly that list, and "expected" is `len(cases)` at
collection time -- before anything runs. A case can't be "expected but never
run" or "run but not counted," because being in the list is what makes it
run. Nothing to remember, nothing to keep in sync, nothing that can drift.

Usage in a suite file (tests/test_hdhr.py):

    from agent.suites import suite

    SUITE = suite("hdhr", group="core", order=30)

    @SUITE.setup
    def _setup(m3undle_url):
        # runs once before any case; anything it returns is merged into the
        # context passed to every case (only the arguments each case
        # actually declares by name)
        return {"base": m3undle_url.rstrip("/"), "expected_channels": ...}

    @SUITE.case("HDHR-01")
    def test_hdhr_01(ctx, base):
        ...

    @SUITE.case("HDHR-03")
    def test_hdhr_03(ctx, base, expected_channels):
        ...

A directory of such files is a lab's tests/ -- discover_suites() imports
every test_*.py in it and collects each file's module-level SUITE.
"""

from __future__ import annotations

import contextlib
import importlib.util
import inspect
import io
import itertools
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import common
from .dashboard import LiveDashboard
from .results import RunContext

CaseFunc = Callable[..., None]
SetupFunc = Callable[..., "dict[str, Any] | None"]
TeardownFunc = Callable[..., None]

_MODULE_COUNTER = itertools.count()


@dataclass(slots=True)
class Case:
    test_id: str
    func: CaseFunc


@dataclass(slots=True)
class Suite:
    name: str
    group: str = "core"
    order: int = 100
    cases: list[Case] = field(default_factory=list)
    setup_fn: SetupFunc | None = None
    teardown_fn: TeardownFunc | None = None

    def case(self, test_id: str) -> Callable[[CaseFunc], CaseFunc]:
        """Register a case function under `test_id`. Does not wrap it --
        the function stays directly callable/testable on its own."""

        def register(func: CaseFunc) -> CaseFunc:
            if any(c.test_id == test_id for c in self.cases):
                raise ValueError(f"Suite {self.name!r} already has a case named {test_id!r}")
            self.cases.append(Case(test_id, func))
            return func

        return register

    def setup(self, func: SetupFunc) -> SetupFunc:
        """Register a one-time setup step. Runs before any case; its return
        value (a dict, or None) is merged into every case's available
        context, matched to each case's own parameter names."""
        self.setup_fn = func
        return func

    def teardown(self, func: TeardownFunc) -> TeardownFunc:
        self.teardown_fn = func
        return func


def suite(name: str, *, group: str = "core", order: int = 100) -> Suite:
    return Suite(name=name, group=group, order=order)


@dataclass(slots=True)
class SuiteRunResult:
    suite_name: str
    setup_ok: bool
    setup_error: str | None
    expected: int
    actual: int

    @property
    def drifted(self) -> bool:
        """True when setup succeeded but the recorded result count doesn't
        match the registered case count -- e.g. a case's own code path
        returned without calling ctx.record at all. Meaningless to check
        when setup itself failed: an aborted suite is already reported via
        setup_ok/setup_error, not as a count mismatch."""
        return self.setup_ok and self.expected != self.actual


def _call_with_matching_kwargs(func: Callable[..., Any], *positional: Any, context: dict[str, Any]) -> Any:
    params = inspect.signature(func).parameters
    kwargs = {k: v for k, v in context.items() if k in params}
    return func(*positional, **kwargs)


@contextlib.contextmanager
def _capture(buffer: io.StringIO | None):
    """No-op when buffer is None (no dashboard active -- prints go straight
    to the terminal as always). Otherwise redirects stdout/stderr into
    buffer for the duration of the `with` block."""
    if buffer is None:
        yield
        return
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        yield


def run_suite(target: Suite, ctx: RunContext, **base_context: Any) -> SuiteRunResult:
    """Run every case registered on `target`, isolating each from the others.

    A case whose own code raises is recorded as a failed result and the run
    continues to the next case -- one broken case must not silently cost
    every case that would have run after it, which is what an unhandled
    exception inside the old manual "main() calls each test by name in a
    row" pattern did.

    While a dashboard is active, setup/case/teardown calls are run with
    stdout/stderr captured, not printed live -- a suite's own debug prints
    (a scenario name, a simulator's startup log, a per-client status line;
    all real examples that scrolled the dashboard away, confirmed against a
    live run) have no way to coordinate with the dashboard the way
    RunContext's own pass/fail line and common.run()'s subprocess calls do.
    Captured output is discarded on success and flushed to the real
    terminal on failure, so debugging isn't harder than before -- same
    principle already applied to subprocess output in common.run().
    """
    context = dict(base_context)
    setup_ok = True
    setup_error: str | None = None
    dashboard = ctx.dashboard

    def _flush(buffer: io.StringIO) -> None:
        # Only ever called with a buffer that exists because a dashboard is
        # active (see the `buffer = io.StringIO() if dashboard is not None
        # else None` calls below) -- dashboard.print() routes through its own
        # console so this interleaves correctly with a live footer instead of
        # writing the real fd directly.
        content = buffer.getvalue()
        if not content:
            return
        dashboard.print(content, end="")

    if target.setup_fn is not None:
        buffer = io.StringIO() if dashboard is not None else None
        try:
            with _capture(buffer):
                extra = _call_with_matching_kwargs(target.setup_fn, context=context)
            if extra:
                context.update(extra)
        except Exception as exc:  # noqa: BLE001 - setup failing must not crash the run
            setup_ok = False
            setup_error = str(exc)
            if buffer is not None:
                _flush(buffer)
            ctx.fail(f"{target.name}-SETUP", f"Suite setup failed: {exc}")

    if setup_ok:
        for case in target.cases:
            buffer = io.StringIO() if dashboard is not None else None
            before = len(ctx.results)
            try:
                with _capture(buffer):
                    _call_with_matching_kwargs(case.func, ctx, context=context)
            except Exception as exc:  # noqa: BLE001 - one case's bug must not cost the rest
                if buffer is not None:
                    _flush(buffer)
                ctx.fail(case.test_id, f"Unhandled exception in {case.test_id}: {exc}")
            else:
                if buffer is not None and any(r.status == "fail" for r in ctx.results[before:]):
                    _flush(buffer)

        if target.teardown_fn is not None:
            buffer = io.StringIO() if dashboard is not None else None
            try:
                with _capture(buffer):
                    _call_with_matching_kwargs(target.teardown_fn, context=context)
            except Exception as exc:  # noqa: BLE001 - teardown failing must not mask results
                if buffer is not None:
                    _flush(buffer)
                message = f"Warning: {target.name} teardown raised: {exc}"
                if dashboard is not None:
                    dashboard.print(message)
                else:
                    print(message, flush=True, file=sys.__stdout__)

    return SuiteRunResult(
        suite_name=target.name,
        setup_ok=setup_ok,
        setup_error=setup_error,
        expected=len(target.cases),
        actual=len(ctx.results),
    )


@dataclass(slots=True)
class SuitesRunSummary:
    results: list[SuiteRunResult]
    failed: bool


def run_suites(
    suites: list[Suite], *, results_dir: Path, label: str = "Lab", live_progress: bool | None = None, **base_context: Any
) -> SuitesRunSummary:
    """Run each suite in order: print a header, run it, print its summary,
    write its JSON artifact, and track whether anything failed (a case
    failure, a suite setup failure, or result-count drift).

    Extracted 2026-08-23 from what m3undle_lab/commands.py and
    family_librarian_lab/commands.py each hand-rolled as an almost-identical
    loop -- a product's `run` command calls this instead of re-implementing
    it. `**base_context` is whatever each suite's setup/cases need matched by
    parameter name (e.g. `base_url=` for a shared-instance product,
    `scenario_factory=` for one that isolates a fresh environment per case) --
    this function doesn't need to know which.

    `label` names the dashboard's title line (e.g. "M3Undle Lab") -- purely
    cosmetic, se-lab has no way to know a product's display name otherwise.
    `live_progress` overrides LiveDashboard.supported()'s TTY/env autodetect
    when set explicitly (e.g. a test forcing it off); leave it None normally.
    """
    dashboard = None
    if live_progress if live_progress is not None else LiveDashboard.supported():
        dashboard = LiveDashboard(label, len(suites), plain=LiveDashboard.mode() == "plain")
        # So common.run()/run_capture() -- and anything else that shells out
        # during a case (a scenario's own compose up/down, a mid-suite
        # restart_stack_with_env()) -- can stream its output through the same
        # dashboard instead of inheriting the terminal directly.
        common.set_active_dashboard(dashboard)

    def _emit(text: str) -> None:
        # Routed through the dashboard when one is active so it interleaves
        # correctly with the live footer instead of writing the real fd
        # directly; durable either way, so scrollback/log capture always has
        # a permanent trace of which suite was attempted -- not just the live
        # footer's transient state -- if the suite dies before recording a
        # single result (e.g. its scenario's own compose up fails or hangs
        # before any case runs).
        if dashboard is not None:
            dashboard.print(text)
        else:
            print(text, flush=True, file=sys.__stdout__)

    results: list[SuiteRunResult] = []
    failed = False
    try:
        for index, target in enumerate(suites, start=1):
            _emit(f"\n--- Running suite: {target.name} ---")
            if dashboard is not None:
                dashboard.start_suite(target.name, index, test_total=len(target.cases))
                dashboard.render()
            context = RunContext(target.name, dashboard=dashboard)
            result = run_suite(target, context, **base_context)
            context.print_summary()
            context.write_json(results_dir / f"results-{target.name}.json")
            if not result.setup_ok:
                _emit(f"  Setup failed: {result.setup_error}")
            if result.drifted:
                _emit(f"  Result drift: expected {result.expected} registered cases, recorded {result.actual} outcomes.")
            if context.failed_count or result.drifted or not result.setup_ok:
                failed = True
            results.append(result)
    finally:
        if dashboard is not None:
            dashboard.stop()
            common.set_active_dashboard(None)
    return SuitesRunSummary(results=results, failed=failed)


def _import_module_from_path(path: Path):
    module_name = f"_selab_suite_{path.stem}_{next(_MODULE_COUNTER)}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load suite module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def discover_suites(tests_dir: Path) -> list[Suite]:
    """Import every test_*.py directly under tests_dir and collect its SUITE.

    A file with no module-level SUITE is skipped, not an error -- lets
    tests_dir hold shared helpers (e.g. fixtures.py) alongside suite files
    without every file needing to look like a suite. Raises if two files
    declare the same suite name -- silently running both, or only one, of
    two same-named suites is worse than failing loudly at discovery time.
    """
    discovered: list[Suite] = []
    seen: dict[str, Path] = {}
    for path in sorted(tests_dir.glob("test_*.py")):
        module = _import_module_from_path(path)
        suite_obj = getattr(module, "SUITE", None)
        if not isinstance(suite_obj, Suite):
            continue
        if suite_obj.name in seen:
            raise ValueError(
                f"Suite name {suite_obj.name!r} is declared in both {seen[suite_obj.name]} and {path}"
            )
        seen[suite_obj.name] = path
        discovered.append(suite_obj)
    return sorted(discovered, key=lambda s: (s.order, s.name))


def suites_in_group(suites: list[Suite], group: str) -> list[Suite]:
    if group == "all":
        return list(suites)
    return [s for s in suites if s.group == group]


def select_suites(
    suites: list[Suite], *, only: str | None = None, group: str | None = None, case: str | None = None
) -> list[Suite]:
    """Filter suites for a `run` command: by suite name (`only`), or by
    group (default "all"), then optionally narrow further to one case id
    within whatever suites remain.

    Extracted 2026-08-24 from what m3undle_lab and family_librarian_lab each
    hand-rolled independently as their own `_select_suites()` -- the same
    underlying feature (m3undle's `--only`/`--test-group` selected by suite/
    group; family-librarian's `--group`/`--case` selected by group/case) had
    drifted into different flag names and, for `--case`, existed on only one
    product lab. One shared implementation means a selector means the same
    thing on every product lab, and a product lab can't drift from another
    by editing its own copy.

    `only` and `group` are mutually exclusive at the CLI level (a product
    lab's own argparse should enforce that, e.g. via
    add_mutually_exclusive_group()) -- this function doesn't re-check it,
    since by the time it's called only one should ever be set.
    """
    if only:
        selected = [s for s in suites if s.name == only]
        if not selected:
            available = ", ".join(s.name for s in suites) or "(none)"
            raise SystemExit(f"Unknown suite {only!r}. Available: {available}")
    else:
        target_group = group or "all"
        selected = suites_in_group(suites, target_group)
        if not selected:
            available = ", ".join(sorted({s.group for s in suites})) or "(none)"
            raise SystemExit(f"Unknown or empty suite group {target_group!r}. Available groups: all, {available}")

    if not case:
        return selected
    narrowed: list[Suite] = []
    for candidate in selected:
        matching = [c for c in candidate.cases if c.test_id == case]
        if matching:
            narrowed.append(
                Suite(
                    name=candidate.name,
                    group=candidate.group,
                    order=candidate.order,
                    cases=matching,
                    setup_fn=candidate.setup_fn,
                    teardown_fn=candidate.teardown_fn,
                )
            )
    return narrowed
