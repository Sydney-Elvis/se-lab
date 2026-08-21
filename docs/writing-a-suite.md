# Writing a test suite

This is the generic mechanism every product lab's `tests/` directory uses. It's documented
once, here, because it's identical across every product lab — se-lab discovers, runs, and
tracks results the same way regardless of what product is under test. Your product lab's own
`tests/README.md` should link here for *how the mechanism works*, and cover only what's
actually specific to your product: fixtures, how to start your simulator/dependencies, example
suites. Don't restate this file locally — it drifts, and now there's exactly one copy to keep
current.

## The problem this replaces

A suite used to declare "how many tests I have" as a number maintained in a *different* file
(`SUITE_TEST_TOTALS`), updated by hand whenever a test was added or removed. It drifted in
practice — nothing forced it to stay in sync, and it didn't.

The fix isn't a smarter way to count test functions automatically — a function that exists in
the file but was never wired into the run sequence would still get counted, which defeats the
point. The fix is to make *declaring* a case and *running* it the same list. A suite registers
its cases with a decorator; the harness runs exactly that list; "expected" is `len(cases)` at
collection time, before anything runs. A case can't be expected-but-never-run or
run-but-not-counted, because being in the list is what makes it run.

## Minimal suite

```python
# tests/test_hdhr.py
from agent.suites import suite

SUITE = suite("hdhr", group="core", order=30)

@SUITE.case("HDHR-01")
def test_hdhr_01(ctx):
    ctx.ok("HDHR-01", "tuner count matches config")
```

That's the whole contract:

- The file lives directly under your lab's `tests/` directory and is named `test_*.py`.
- It defines exactly one module-level `SUITE = suite(name, ...)`.
- `@SUITE.case("SOME-ID")` registers a case function. The function itself is returned unchanged
  by the decorator — it stays directly callable and testable on its own, not wrapped.
- Every case receives a `RunContext` as its first argument (see `agent/results.py`) and reports
  its own outcome via `ctx.ok(...)` / `ctx.fail(...)` / `ctx.skip(...)` / `ctx.record(...)`.

`discover_suites(tests_dir)` (`agent/suites.py`) imports every `test_*.py` under a directory and
collects each file's `SUITE`. A file with no module-level `SUITE` is skipped, not an error —
that's how shared helpers (e.g. `fixtures.py`) coexist in the same directory. Two files
declaring the same suite name is an error, raised at discovery time, not silently resolved.

## Setup, teardown, and shared context

A suite that needs infrastructure up once before any case runs — starting a simulator,
authenticating a client, computing a value every case needs — registers a `setup`:

```python
@SUITE.setup
def _setup(m3undle_url):
    # runs once, before any case. Its return value (a dict, or None) is
    # merged into the context available to every case afterward.
    return {"base": m3undle_url.rstrip("/"), "expected_channels": 12}

@SUITE.case("HDHR-01")
def test_hdhr_01(ctx, base):
    ...

@SUITE.case("HDHR-03")
def test_hdhr_03(ctx, base, expected_channels):
    ...
```

Setup and every case are called with only the keyword arguments they actually declare by name
— matched against whatever the runner was given (`run_suite(SUITE, ctx, m3undle_url=...)`) plus
whatever `setup` returned. A case that doesn't need `expected_channels` simply doesn't declare
that parameter; it isn't forced on every case.

`@SUITE.teardown` registers a cleanup step that always runs after every case, even if some
failed. A teardown exception is reported as a warning, not raised — a broken cleanup step must
never erase the results the suite already recorded.

## Failure isolation

If a case's own code raises, `run_suite` catches it, records it as a failed result under that
case's own test ID, and **keeps running the remaining cases**. One broken case doesn't cost
every case that would have run after it — that's a real improvement over a suite whose cases
were called in a row from a single function, where an uncaught exception partway through
silently lost every test after it.

## What `SuiteRunResult` tells you

```python
result = run_suite(SUITE, ctx, m3undle_url="http://localhost:8080")
```

- `result.expected` — `len(SUITE.cases)`, the number of registered cases.
- `result.actual` — `len(ctx.results)`, what actually got recorded.
- `result.setup_ok` / `result.setup_error` — whether setup completed. If setup failed, no cases
  ran at all; that's reported here, not as a count mismatch — an aborted suite and a suite that
  quietly lost track of a test are different problems and get different signals.
- `result.drifted` — `True` only when setup succeeded *and* `expected != actual`. This is the
  gap registration alone can't close: a case that's correctly registered, gets called, but
  whose own code path returns without calling `ctx.record` at all (a bug in that case, not a
  wiring mistake). Worth surfacing, but it's real code to go fix, not a stale number to bump.

## `group` and `order`

`suite(name, group="core", order=30)` replaces the old `CORE_SUITE_ORDER`/
`STREAM_HARDENING_SUITE_ORDER` lists — curation lives with the suite that's being curated,
not in a separate file someone has to remember to also edit. `discover_suites()` returns suites
sorted by `(order, name)`; `suites_in_group(suites, "core")` filters to one group,
`suites_in_group(suites, "all")` returns everything. Pick whatever group names make sense for
your product lab — se-lab doesn't fix a set of them.

## What this doesn't decide

This module is discovery, registration, and single-suite execution — not the `lab run` command
itself. Deploying a target, choosing which suites to run for a given invocation, the live
progress dashboard, and post-failure AI analysis are a separate layer built on top of this one,
specific to each product lab's deploy/test shape.
