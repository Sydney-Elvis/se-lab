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

import importlib.util
import inspect
import itertools
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

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


def run_suite(target: Suite, ctx: RunContext, **base_context: Any) -> SuiteRunResult:
    """Run every case registered on `target`, isolating each from the others.

    A case whose own code raises is recorded as a failed result and the run
    continues to the next case -- one broken case must not silently cost
    every case that would have run after it, which is what an unhandled
    exception inside the old manual "main() calls each test by name in a
    row" pattern did.
    """
    context = dict(base_context)
    setup_ok = True
    setup_error: str | None = None

    if target.setup_fn is not None:
        try:
            extra = _call_with_matching_kwargs(target.setup_fn, context=context)
            if extra:
                context.update(extra)
        except Exception as exc:  # noqa: BLE001 - setup failing must not crash the run
            setup_ok = False
            setup_error = str(exc)
            ctx.fail(f"{target.name}-SETUP", f"Suite setup failed: {exc}")

    if setup_ok:
        for case in target.cases:
            try:
                _call_with_matching_kwargs(case.func, ctx, context=context)
            except Exception as exc:  # noqa: BLE001 - one case's bug must not cost the rest
                ctx.fail(case.test_id, f"Unhandled exception in {case.test_id}: {exc}")

        if target.teardown_fn is not None:
            try:
                _call_with_matching_kwargs(target.teardown_fn, context=context)
            except Exception as exc:  # noqa: BLE001 - teardown failing must not mask results
                print(f"Warning: {target.name} teardown raised: {exc}", flush=True)

    return SuiteRunResult(
        suite_name=target.name,
        setup_ok=setup_ok,
        setup_error=setup_error,
        expected=len(target.cases),
        actual=len(ctx.results),
    )


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
