"""Live-updating terminal progress footer for a suite run, built on Rich.

Replaces the earlier hand-rolled ANSI/cursor-addressing implementation
(manual "\\x1b[{n}A" cursor-up + "\\x1b[2K" line-clear sequences). That
approach required every call site that wanted to print anything else --
a suite's own setup/case/teardown output, a subprocess's stdout, the suite
header/summary lines -- to explicitly call dashboard.clear() first and
coordinate line counts by hand, and it was confirmed to fail silently on at
least one real SSH path for reasons that were never root-caused (see the
LAB_LIVE_PROGRESS=plain fallback this module still supports).

Rich's Live display removes that whole class of bug: anything printed
through the *same* Console a Live is bound to is automatically interleaved
correctly (the live region is cleared, the new content is written above it
as ordinary scrollback, and the live region is redrawn below) -- no manual
line-count bookkeeping anywhere. That's why every call site that used to do
`dashboard.clear()` then write raw now just calls `dashboard.print(...)`.

Three modes, selected by LiveDashboard.mode() (see its docstring):
- "off": no dashboard at all.
- "plain": a durable one-line text summary appended on every render(), no
  box, no cursor addressing -- the fallback for a terminal that claims TTY
  support but doesn't actually render in-place redraws correctly.
- "inplace": the live boxed footer (rich.live.Live), suite/test progress
  shown as real Rich progress bars.
"""

from __future__ import annotations

import os
import sys
import time

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.text import Text

from .common import format_duration

_STATUS_STYLES = {
    "PASS": "bold green",
    "PASSED": "bold green",
    "FAIL": "bold red",
    "FAILED": "bold red",
}
_OVERALL_STYLES = {"PASS": "green", "FAIL": "red", "RUNNING": "yellow"}


def _status_style(status: str) -> str:
    return _STATUS_STYLES.get(status, "bold yellow")


def _truncate(text: str, limit: int) -> str:
    """Degrade a long suite/test name cleanly instead of letting it wrap and
    blow out the footer's fixed line count."""
    if limit <= 0 or len(text) <= limit:
        return text
    if limit == 1:
        return text[:1]
    return text[: limit - 1] + "…"


class LiveDashboard:
    # Fast, back-to-back test results (sub-100ms apart -- confirmed against a
    # real run: a suite made entirely of quick, no-wait assertions) each
    # trigger their own render(); over real network latency (SSH in
    # particular) a redraw that fast may never visibly land before the next
    # result's redraw replaces it, so the dashboard appears to vanish for the
    # whole suite rather than blip once. maybe_render() rate-limits to this
    # interval, always forcing through on a failure.
    MIN_RENDER_INTERVAL = 0.15

    def __init__(self, label: str, suite_total: int, *, plain: bool = False, console: Console | None = None) -> None:
        self.label = label
        self.suite_total = suite_total
        self.suite = ""
        self.suite_index = 0
        self.suites_completed = 0
        self.test_total: int | None = None
        self.test_count = 0
        self.latest_name = "none yet"
        self.latest_status = "RUNNING"
        self.any_failed = False
        self.started = time.monotonic()
        # Tracks the case currently in flight, separately from latest_name/
        # latest_status (which only ever reflect the last *completed* case --
        # see record_result()). Without this, "Elapsed" only ever showed
        # whole-run time and "Last test" only ever showed the previous
        # result, so a case genuinely stuck mid-scenario (waiting on a
        # container, a subprocess) looked identical on screen to one still
        # making normal progress -- confirmed confusing for real against a
        # ~30-minute multi-suite run. See start_case()/record_result().
        self.current_case: str | None = None
        self.current_case_started: float | None = None
        self.rendered = False
        self._last_render_at = 0.0
        # See mode()'s docstring: an append-only durable line per render()
        # instead of the boxed Live footer, for a terminal/SSH path that
        # doesn't render the latter at all.
        self.plain = plain
        # sys.__stdout__, not sys.stdout: run_suite() redirects sys.stdout to
        # a capture buffer for the duration of a suite's own setup/case/
        # teardown calls (see agent/suites.py), so a suite's own prints don't
        # scroll the footer away on success -- but the footer itself, and
        # anything explicitly flushed through it, must keep writing to the
        # real terminal regardless.
        self.console = console or Console(file=sys.__stdout__, force_terminal=False if plain else None)
        self._live: Live | None = None if plain else Live(
            console=self.console,
            auto_refresh=False,
            transient=False,
            redirect_stdout=False,
            redirect_stderr=False,
        )
        self._started = False

    @staticmethod
    def mode() -> str:
        """"0" -> off. "plain" -> always the append-only durable fallback,
        regardless of TTY/TERM (it never uses cursor-addressing, so there's
        nothing for those to gate). "1" -> force the live boxed footer, still
        subject to the dumb-terminal veto below. Unset -> autodetect the live
        footer from TTY/TERM.

        The "plain" mode exists because in-place redraw can fail silently on
        a real, otherwise-unremarkable terminal/SSH path -- confirmed for
        real against family-librarian-lab's toontown-int-srv2: the in-place
        block never rendered even once, from two different local SSH clients
        (Windows and macOS), while the identical se-lab commit's identical
        code rendered correctly for the same user against a second host
        (toontown-int-srv1). Kernel, sshd version/config, `stty -a`, Python
        version, locale, shell startup files, and local SSH client config
        were all confirmed byte-identical between the two hosts -- so rather
        than depend on figuring out that remaining, unexplained difference,
        "plain" sidesteps it: plain print() lines are what the durable
        [PASS]/[FAIL] lines already use (see RunContext._record()), and
        those are confirmed to render fine everywhere in-place redraw fails.
        """
        value = os.environ.get("LAB_LIVE_PROGRESS")
        if value == "0":
            return "off"
        if value == "plain":
            return "plain"
        if os.environ.get("TERM", "") == "dumb":
            return "off"
        if value == "1" or sys.stdout.isatty():
            return "inplace"
        return "off"

    @staticmethod
    def supported() -> bool:
        return LiveDashboard.mode() != "off"

    def start(self) -> None:
        """Start the Live footer (idempotent, no-op in plain/off modes).
        Lazily called from render() -- nothing needs to appear on screen
        until there's actual suite/test state to show."""
        if self._live is not None and not self._started:
            self._live.start()
            self._started = True

    def stop(self) -> None:
        """Tear down the Live footer at the end of a run, restoring the
        terminal to a normal scrolling state (cursor visible, no dangling
        live region). Safe to call multiple times or when never started.
        No-op in plain mode: nothing was drawn in-place to un-render."""
        if self.plain:
            return
        if self._live is not None and self._started:
            self._live.stop()
            self._started = False
        self.rendered = False

    def print(self, text: str = "", *, end: str = "\n") -> None:
        """Write text that must interleave correctly with the live footer --
        a suite's own captured output, a subprocess's streamed stdout, suite
        headers/summaries. markup/highlight are disabled because this text
        is arbitrary (docker output routinely contains literal "[...]"
        segments that would otherwise be misparsed as Rich markup), and
        soft_wrap avoids Rich re-wrapping lines that already fit the
        terminal on their own.
        """
        self.console.print(text, end=end, markup=False, highlight=False, soft_wrap=True)

    def start_suite(self, name: str, index: int, *, test_total: int) -> None:
        self.suite = name
        self.suite_index = index
        self.test_total = test_total
        self.test_count = 0
        self.latest_name = "none yet"
        self.latest_status = "RUNNING"
        self.current_case = None
        self.current_case_started = None

    def start_case(self, name: str) -> None:
        """Called right before a case's own function runs -- see run_suite()
        in agent/suites.py. Cleared by record_result() once that same case
        finishes (pass/fail/skip all call it via RunContext), so at most one
        of current_case/latest_name is ever the "active" line in the footer."""
        self.current_case = name
        self.current_case_started = time.monotonic()

    def finish_suite(self) -> None:
        """Mark the current suite as fully done -- called once, after it
        returns, regardless of pass/fail/setup-error (matches what the
        Suites bar has always counted: suites moved past, not suites that
        passed). Without this, the Suites count was inferred as
        `suite_index - 1` ("suites before the one currently running"), which
        is accurate mid-run but never reaches suite_total once the *last*
        suite itself finishes -- there's no next start_suite() call to imply
        it. Confirmed against a real 22-suite run: the final frame read
        "Overall PASS" with the Suites bar stuck at 21/22.
        """
        self.suites_completed += 1

    def record_result(self, *, name: str, status: str, completed: int, failed: bool) -> None:
        self.test_count = completed
        self.latest_name = name
        self.latest_status = status.upper()
        if failed:
            self.any_failed = True
        # This case is no longer "in flight" -- only clear it if it's the
        # same one start_case() marked running (a defensive check, not
        # expected to matter in practice since cases run one at a time).
        if self.current_case == name:
            self.current_case = None
            self.current_case_started = None

    def _overall_status(self) -> str:
        """FAIL as soon as anything has -- otherwise RUNNING until the very
        last suite's very last test has actually completed, only then PASS.
        (Not merely "something started": that would flip to PASS after the
        very first test of the very first suite, well before the run is
        anywhere near done.)
        """
        if self.any_failed:
            return "FAIL"
        suites_done = self.suite_index >= self.suite_total
        tests_done = bool(self.test_total) and self.test_count >= self.test_total
        return "PASS" if suites_done and tests_done else "RUNNING"

    def _bar_styles(self) -> tuple[str, str]:
        """(complete_style, finished_style) for both progress bars -- red,
        once anything has failed, instead of Rich's own default theme
        colors. Sticky like any_failed itself (see _overall_status()), so a
        failure several suites back stays visible at a glance even if you're
        only glancing at the bars, not reading the header text.
        """
        if self.any_failed:
            return "red", "red"
        return "bar.complete", "bar.finished"

    def _current_case_text(self) -> str:
        """'Running: <id> (Xs)' while a case is in flight, else the last
        completed result -- see start_case()/record_result(). The (Xs) is
        this case's own elapsed time, not the whole run's, so it actually
        moves independently and a stall becomes visible at a glance."""
        if self.current_case is not None and self.current_case_started is not None:
            case_elapsed = format_duration(int(time.monotonic() - self.current_case_started))
            return f"Running: {self.current_case} ({case_elapsed})"
        if self.latest_name == "none yet":
            return "Last test: none yet"
        return f"Last test: [{self.latest_status}] {self.latest_name}"

    def _plain_summary(self) -> str:
        completed_suites = self.suites_completed
        overall = self._overall_status()
        elapsed = format_duration(int(time.monotonic() - self.started))
        tests = f"{self.test_count}/{self.test_total}" if self.test_total else str(self.test_count)
        return (
            f"  ===> {self.label} | Overall {overall} | "
            f"Suites {completed_suites}/{self.suite_total} (current: {self.suite or 'starting'}) | "
            f"Tests {tests} | Elapsed {elapsed} | {self._current_case_text()}"
        )

    def _build_renderable(self) -> Panel:
        completed_suites = self.suites_completed
        overall = self._overall_status()
        elapsed = int(time.monotonic() - self.started)

        name_budget = max(10, self.console.size.width - 30)
        current_suite = _truncate(self.suite or "starting", name_budget)
        latest_name = _truncate(self.latest_name, name_budget)

        header = Text.assemble(
            (f"{self.label} | Testing | Overall ", "bold cyan"),
            (overall, f"bold {_OVERALL_STYLES.get(overall, 'yellow')}"),
        )

        bar_style, finished_style = self._bar_styles()
        progress = Progress(
            TextColumn("{task.fields[row_label]:<7}"),
            BarColumn(bar_width=None, complete_style=bar_style, finished_style=finished_style),
            TextColumn("{task.fields[counts]}", justify="right"),
            TextColumn("{task.fields[extra]}"),
            console=self.console,
            expand=True,
        )
        progress.add_task(
            "suites",
            total=max(self.suite_total, 1),
            completed=min(completed_suites, max(self.suite_total, 1)),
            row_label="Suites",
            counts=f"{completed_suites}/{self.suite_total}",
            extra=f"Current: {current_suite}",
        )
        progress.add_task(
            "tests",
            total=self.test_total if self.test_total else None,
            completed=self.test_count,
            row_label="Tests",
            counts=f"{self.test_count}/{self.test_total}" if self.test_total else str(self.test_count),
            extra=f"Elapsed: {format_duration(elapsed)}",
        )

        if self.current_case is not None and self.current_case_started is not None:
            case_elapsed = format_duration(int(time.monotonic() - self.current_case_started))
            current_case_name = _truncate(self.current_case, name_budget)
            last_line = Text.assemble(
                ("Running: ", "bold yellow"),
                f"{current_case_name} ({case_elapsed})",
            )
        elif self.latest_name == "none yet":
            last_line = Text("Last test: none yet")
        else:
            last_line = Text.assemble(
                "Last test: ",
                (f"[{self.latest_status}]", _status_style(self.latest_status)),
                f" {latest_name}",
            )

        return Panel(Group(header, progress, last_line), border_style="cyan", expand=True)

    def render(self) -> None:
        if self.plain:
            # Append-only durable line, no cursor-addressing codes at all --
            # see mode()'s docstring for why this mode exists. soft_wrap:
            # this is one logical line even if it's wider than the terminal;
            # let it run off the edge rather than wrapping into several.
            self.console.print(self._plain_summary(), markup=False, highlight=False, soft_wrap=True)
            self.rendered = True
            self._last_render_at = time.monotonic()
            return

        self.start()
        assert self._live is not None
        self._live.update(self._build_renderable())
        self._live.refresh()
        self.rendered = True
        self._last_render_at = time.monotonic()

    def maybe_render(self, *, force: bool = False) -> None:
        """render(), unless the previous render happened too recently and
        this isn't a moment that must be shown regardless (force=True for a
        failure -- never worth missing). See MIN_RENDER_INTERVAL's own
        comment for why this exists. Internal state (record_result()) is
        always kept current regardless of whether this actually redraws --
        callers should call record_result() unconditionally and only gate
        the render itself through this method.
        """
        if not force and (time.monotonic() - self._last_render_at) < self.MIN_RENDER_INTERVAL:
            return
        self.render()
