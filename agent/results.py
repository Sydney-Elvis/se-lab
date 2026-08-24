"""Test result tracking for product-lab test scripts.

Ported near-verbatim from scripts/common/harness/results.py -- confirmed
generic (single M3UNDLE_LAB_PROGRESS_FILE env var reference; renamed to a
plain LAB_PROGRESS_FILE, no product prefix needed, matching
common.py's deployment/last-test bookkeeping keys: se-lab-internal state the
product app itself never reads).

Each test produces a TestResult with status pass/fail/skip. RunContext
collects results for a suite and writes a JSON summary.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .dashboard import LiveDashboard


@dataclass
class TestResult:
    name: str
    status: Literal["pass", "fail", "skip"]
    message: str
    detail: Any = None

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    @property
    def failed(self) -> bool:
        return self.status == "fail"

    @property
    def skipped(self) -> bool:
        return self.status == "skip"


class RunContext:
    def __init__(self, suite: str, *, emit_progress: bool = True, dashboard: "LiveDashboard | None" = None) -> None:
        self.suite = suite
        self.emit_progress = emit_progress
        self.dashboard = dashboard
        self.results: list[TestResult] = []

    def _record(self, result: TestResult) -> TestResult:
        self.results.append(result)
        if self.emit_progress:
            self._write_progress_event(result)
        label = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}[result.status]
        if self.dashboard is not None:
            self.dashboard.clear()  # own the terminal for this one print, same as any other plain output
        # sys.__stdout__, not print()/sys.stdout: run_suite() redirects
        # sys.stdout to a capture buffer for the duration of a case's own
        # execution (so a suite's own debug prints don't scroll the
        # dashboard away), but the case's actual pass/fail result must
        # always be visible live, redirect or not.
        print(f"  [{label}] {result.name}: {result.message}", flush=True, file=sys.__stdout__)
        if self.dashboard is not None:
            self.dashboard.record_result(
                name=result.name, status=result.status, completed=len(self.results), failed=result.failed
            )
            self.dashboard.render()
        return result

    def _write_progress_event(self, result: TestResult) -> None:
        progress_path = os.environ.get("LAB_PROGRESS_FILE")
        if not progress_path:
            return
        event = {
            "suite": self.suite,
            "name": result.name,
            "status": result.status,
            "message": result.message,
            "completed": len(self.results),
            "passed": self.passed_count,
            "failed": self.failed_count,
            "skipped": self.skipped_count,
            "timestamp": time.time(),
        }
        try:
            path = Path(progress_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, separators=(",", ":")) + "\n")
        except OSError:
            pass

    def ok(self, name: str, message: str, detail: Any = None) -> TestResult:
        return self._record(TestResult(name, "pass", message, detail))

    def fail(self, name: str, message: str, detail: Any = None) -> TestResult:
        return self._record(TestResult(name, "fail", message, detail))

    def skip(self, name: str, reason: str) -> TestResult:
        return self._record(TestResult(name, "skip", reason))

    def record(self, name: str, passed: bool | None, message: str, detail: Any = None) -> TestResult:
        """Convenience: True->pass, False->fail, None->skip."""
        if passed is True:
            return self.ok(name, message, detail)
        if passed is False:
            return self.fail(name, message, detail)
        return self.skip(name, message)

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.status == "pass")

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if r.status == "fail")

    @property
    def skipped_count(self) -> int:
        return sum(1 for r in self.results if r.status == "skip")

    def print_summary(self) -> None:
        if self.dashboard is not None:
            self.dashboard.clear()
        total = len(self.results)
        print(f"\n--- {self.suite}: {self.passed_count}/{total} passed, {self.skipped_count} skipped ---", flush=True)
        if self.failed_count:
            print("Failed:", flush=True)
            for r in self.results:
                if r.status == "fail":
                    print(f"  - {r.name}: {r.message}", flush=True)

    def to_dict(self) -> dict:
        result_items = []
        for result in self.results:
            item = {"name": result.name, "status": result.status, "message": result.message}
            if result.detail is not None:
                item["detail"] = result.detail
            result_items.append(item)
        return {
            "suite": self.suite,
            "total": len(self.results),
            "passed": self.passed_count,
            "failed": self.failed_count,
            "skipped": self.skipped_count,
            "results": result_items,
        }

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        print(f"Results written to {path}", flush=True)

    def exit_code(self) -> int:
        """Return 0 if no failures, 1 if any test failed."""
        return 1 if self.failed_count else 0
