"""Pre-flight "here's what's about to happen" plan + confirm, and the closing
"here's what happened" run report.

Restores two steps the se-lab migration dropped: the frozen legacy lab's
scripts/run_integration_tests.py printed a "Lab Run Plan" -- host, resolved
source, what action was about to be taken -- and waited for a y/n before
doing anything, so a wrong branch/target argument was caught before it built
or deployed anything; and, after the run, a "Lab Run Summary" -- source,
deployed commit, start/end timestamps, duration, overall result. The
se-lab-based product labs' own `run` commands currently just start and stop
with no plan and no closing report (run_suites()'s own aggregate pass/fail/
skipped/duration table, added alongside these, is a separate, per-suite
thing -- see agent/suites.py).

A product lab's own `handle_run()` builds a RunPlan from whatever it already
knows (source resolution, action, clean mode, suite selection, ...) via
`.add(heading, value)`, then calls `.confirm(assume_yes=args.yes)` before
doing any real work, and a RunReport the same way once the run is done,
then calls `.print()` -- each product lab decides what's worth showing and
in what order; this module only owns the generic header/footer shape and,
for RunPlan, the actual prompt/skip mechanics.

AI preflight/analysis and the agent JSON/markdown/manifest report the
legacy tool's own closing summary also had are deliberately not part of
RunReport -- those already exist as separate commands (eval ai, report
ai-metrics, doctor ai), not something `run` needs to inline.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(slots=True)
class RunPlan:
    label: str  # e.g. "M3Undle Lab"
    host: str
    lines: list[tuple[str, str]] = field(default_factory=list)

    def add(self, heading: str, value: str) -> "RunPlan":
        """Append one heading/value line, in the order it should print.
        Returns self so calls can be chained."""
        self.lines.append((heading, value))
        return self

    def render(self) -> str:
        title = f"{self.label} Run Plan"
        rule = "=" * len(title)
        started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        body = [f"{heading}: {value}" for heading, value in self.lines]
        return "\n".join([title, rule, f"Started at UTC: {started}", f"Host: {self.host}", *body, rule])

    def confirm(self, *, assume_yes: bool = False) -> bool:
        """Print the plan, then return whether to proceed.

        assume_yes=True (e.g. a --yes/-y flag) skips the prompt entirely --
        for CI/automation, where there's nobody to answer it. Without it, a
        non-interactive stdin refuses to proceed rather than silently
        treating EOF as "no" or hanging forever on a read that will never
        get an answer.
        """
        print(self.render(), flush=True)
        if assume_yes:
            return True
        if not sys.stdin.isatty():
            raise SystemExit(
                "Refusing to proceed without a y/n answer on a non-interactive stdin. "
                "Pass --yes to skip this confirmation (e.g. for CI/automation)."
            )
        try:
            answer = input("Continue? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        return answer in {"y", "yes"}


@dataclass(slots=True)
class RunReport:
    """The closing counterpart to RunPlan -- printed once a run is over, not
    confirmed. A product lab supplies every line (source, deployed commit,
    Started/Completed/Duration/Result included) via `.add(heading, value)`,
    in the order it should print; this class only owns the title/rule shape,
    matching RunPlan's own.
    """

    label: str  # e.g. "M3Undle Lab"
    lines: list[tuple[str, str]] = field(default_factory=list)

    def add(self, heading: str, value: str) -> "RunReport":
        self.lines.append((heading, value))
        return self

    def render(self) -> str:
        title = f"{self.label} Run Summary"
        rule = "=" * len(title)
        # Right-pad headings to a common width so values line up in a column
        # (e.g. "Started at UTC:   ..." / "Completed at UTC: ...") -- matches
        # the legacy tool's own closing summary.
        width = max((len(heading) for heading, _ in self.lines), default=0)
        body = [f"{heading}:{' ' * (width - len(heading) + 1)}{value}" for heading, value in self.lines]
        return "\n".join([title, rule, *body, rule])

    def print(self) -> None:
        print(self.render(), flush=True)
