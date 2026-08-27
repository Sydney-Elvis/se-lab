"""Pre-flight "here's what's about to happen" plan + confirm.

Restores a step the se-lab migration dropped: the frozen legacy lab's
scripts/run_integration_tests.py printed a "Lab Run Plan" -- host, resolved
source, what action was about to be taken -- and waited for a y/n before
doing anything, so a wrong branch/target argument was caught before it built
or deployed anything. The se-lab-based product labs' own `run` commands
currently just start.

A product lab's own `handle_run()` builds a RunPlan from whatever it already
knows (source resolution, action, clean mode, suite selection, ...) via
`.add(heading, value)`, then calls `.confirm(assume_yes=args.yes)` before
doing any real work -- each product lab decides what's worth showing and in
what order; this module only owns the generic header/footer and the actual
prompt/skip mechanics.
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
