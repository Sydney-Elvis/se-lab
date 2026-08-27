"""Live-updating terminal progress display for a suite run.

Ported from the frozen legacy lab's scripts/run_integration_tests.py
LiveDashboard -- confirmed fully product-agnostic before porting
(progress_bar()/activity_bar() are pure functions; the class itself only
ever reads suite/test counts and elapsed time, no M3Undle references
anywhere). The legacy lab drove it by polling an external progress-file
because the dashboard and the running suite were separate processes there.
se-lab runs suites in-process, so this version is driven by direct calls
(render() after each recorded result) instead of file-polling -- same
visual result, simpler mechanism for this architecture. No background
ticker thread: elapsed time only advances at each render() call, a
deliberate simplification to avoid interleaving a second thread's writes
with the main thread's own prints (see RunContext's clear()-before-print
coordination, which a ticking thread would race with).
"""

from __future__ import annotations

import os
import sys
import time

from .common import format_duration


def progress_bar(completed: int, total: int, *, width: int = 20) -> str:
    if total <= 0:
        return "[" + "." * width + "]"
    filled = min(width, max(0, round(width * completed / total)))
    return "[" + "#" * filled + "." * (width - filled) + "]"


def activity_bar(position: int, *, width: int = 20) -> str:
    marker = position % width
    cells = ["."] * width
    cells[marker] = ">"
    for offset in range(1, 4):
        cells[(marker - offset) % width] = "="
    return "[" + "".join(cells) + "]"


class LiveDashboard:
    LINE_COUNT = 4
    RESET = "\x1b[0m"
    CYAN = "\x1b[1;36m"
    GREEN = "\x1b[1;32m"
    RED = "\x1b[1;31m"
    YELLOW = "\x1b[1;33m"
    # Fast, back-to-back test results (sub-100ms apart -- confirmed against a
    # real run: a suite made entirely of quick, no-wait assertions) each
    # trigger their own clear()-before-print; over real network latency (SSH
    # in particular) the redraw that follows may never visibly land before
    # the next result's clear() wipes it again, so the dashboard appears to
    # vanish for the whole suite rather than blip once. maybe_render() rate-
    # limits to this interval, always forcing through on a failure.
    MIN_RENDER_INTERVAL = 0.15

    def __init__(self, label: str, suite_total: int, *, plain: bool = False) -> None:
        self.label = label
        self.suite_total = suite_total
        self.suite = ""
        self.suite_index = 0
        self.test_total: int | None = None
        self.test_count = 0
        self.latest_name = "none yet"
        self.latest_status = "RUNNING"
        self.any_failed = False
        self.started = time.monotonic()
        self.rendered = False
        self._last_render_at = 0.0
        # See mode()'s docstring: an append-only durable line per render()
        # instead of in-place cursor-addressed redraw, for a terminal/SSH
        # path that doesn't render the latter at all.
        self.plain = plain

    @staticmethod
    def mode() -> str:
        """"0" -> off. "plain" -> always the append-only durable fallback,
        regardless of TTY/TERM (it never uses cursor-addressing, so there's
        nothing for those to gate). "1" -> force in-place, still subject to
        the dumb-terminal veto below. Unset -> autodetect in-place from
        TTY/TERM.

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

    @staticmethod
    def _fit(value: str) -> str:
        try:
            columns = os.get_terminal_size(sys.__stdout__.fileno()).columns
        except (OSError, ValueError, AttributeError):
            columns = 100
        return value[: max(20, columns - 12)]

    @classmethod
    def _status_color(cls, status: str) -> str:
        if status in {"PASS", "PASSED"}:
            return cls.GREEN
        if status in {"FAIL", "FAILED"}:
            return cls.RED
        return cls.YELLOW

    @classmethod
    def _line(cls, text: str, *, status: str | None = None) -> str:
        text = cls._fit(text)
        prefix = f"{cls.CYAN}===>{cls.RESET} "
        if status is None:
            return prefix + f"{cls.CYAN}{text}{cls.RESET}"
        marker = status.upper()
        colored_marker = f"{cls._status_color(marker)}{marker}{cls.RESET}"
        return prefix + f"{cls.CYAN}{text.replace(marker, colored_marker, 1)}{cls.RESET}"

    def start_suite(self, name: str, index: int, *, test_total: int) -> None:
        self.suite = name
        self.suite_index = index
        self.test_total = test_total
        self.test_count = 0
        self.latest_name = "none yet"
        self.latest_status = "RUNNING"

    def record_result(self, *, name: str, status: str, completed: int, failed: bool) -> None:
        self.test_count = completed
        self.latest_name = name
        self.latest_status = status.upper()
        if failed:
            self.any_failed = True

    def _plain_summary(self) -> str:
        completed_suites = self.suite_index - 1
        in_progress = bool(completed_suites or self.test_count)
        overall = "FAIL" if self.any_failed else ("RUNNING" if not in_progress else "PASS")
        elapsed = format_duration(int(time.monotonic() - self.started))
        tests = f"{self.test_count}/{self.test_total}" if self.test_total else str(self.test_count)
        last = f"[{self.latest_status}] {self.latest_name}" if self.latest_name != "none yet" else "none yet"
        return (
            f"  ===> {self.label} | Overall {overall} | "
            f"Suites {completed_suites}/{self.suite_total} (current: {self.suite or 'starting'}) | "
            f"Tests {tests} | Elapsed {elapsed} | Last: {last}"
        )

    def render(self) -> None:
        # sys.__stdout__, not sys.stdout: run_suite() redirects sys.stdout to
        # a capture buffer for the duration of a suite's setup/case/teardown
        # calls (see agent/suites.py), so a suite's own prints don't scroll
        # the dashboard away -- but the dashboard itself must keep writing to
        # the real terminal regardless, or it would capture and hide itself.
        stream = sys.__stdout__
        if self.plain:
            # Append-only durable line, no cursor-addressing codes at all --
            # see mode()'s docstring for why this mode exists.
            stream.write(self._plain_summary() + "\n")
            stream.flush()
            self.rendered = True
            self._last_render_at = time.monotonic()
            return

        completed_suites = self.suite_index - 1
        in_progress = bool(completed_suites or self.test_count)
        overall = "FAIL" if self.any_failed else ("RUNNING" if not in_progress else "PASS")
        elapsed = int(time.monotonic() - self.started)
        lines = [
            self._line(f"{self.label} | Testing | Overall {overall}", status=overall),
            self._line(
                f"Suites {progress_bar(completed_suites, self.suite_total)} "
                f"{completed_suites}/{self.suite_total} complete | Current: {self.suite or 'starting'}"
            ),
            self._line(
                f"Tests {progress_bar(self.test_count, self.test_total)} "
                f"{self.test_count}/{self.test_total} complete | Elapsed {format_duration(elapsed)}"
                if self.test_total
                else f"Tests {activity_bar(self.test_count + elapsed)} {self.test_count} complete "
                f"| Elapsed {format_duration(elapsed)}"
            ),
            self._line(
                f"Last test: [{self.latest_status}] {self.latest_name}"
                if self.latest_name != "none yet"
                else f"Last test: {self.latest_name}",
                status=self.latest_status if self.latest_name != "none yet" else None,
            ),
        ]
        if self.rendered:
            stream.write(f"\x1b[{self.LINE_COUNT}A")
        else:
            stream.write("\x1b[?25l")
        for line in lines:
            stream.write("\x1b[2K" + line + "\n")
        stream.flush()
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

    def clear(self) -> None:
        if self.plain:
            return  # append-only: nothing was drawn in-place to erase
        if not self.rendered:
            return
        stream = sys.__stdout__
        stream.write(f"\x1b[{self.LINE_COUNT}A")
        for _ in range(self.LINE_COUNT):
            stream.write("\x1b[2K\n")
        stream.write(f"\x1b[{self.LINE_COUNT}A")
        stream.write("\x1b[?25h")
        stream.flush()
        self.rendered = False
