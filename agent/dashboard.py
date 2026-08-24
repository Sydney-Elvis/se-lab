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

    def __init__(self, label: str, suite_total: int) -> None:
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

    @staticmethod
    def supported() -> bool:
        forced = os.environ.get("LAB_LIVE_PROGRESS") == "1"
        disabled = os.environ.get("LAB_LIVE_PROGRESS") == "0"
        if disabled:
            return False
        return (forced or sys.stdout.isatty()) and os.environ.get("TERM", "") != "dumb"

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

    def render(self) -> None:
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
        # sys.__stdout__, not sys.stdout: run_suite() redirects sys.stdout to
        # a capture buffer for the duration of a suite's setup/case/teardown
        # calls (see agent/suites.py), so a suite's own prints don't scroll
        # the dashboard away -- but the dashboard itself must keep writing to
        # the real terminal regardless, or it would capture and hide itself.
        stream = sys.__stdout__
        if self.rendered:
            stream.write(f"\x1b[{self.LINE_COUNT}A")
        else:
            stream.write("\x1b[?25l")
        for line in lines:
            stream.write("\x1b[2K" + line + "\n")
        stream.flush()
        self.rendered = True

    def clear(self) -> None:
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
