"""Product-specific glue for AI-assisted failure analysis.

se-lab's own build_failure_context() (context.py) already turns a run summary
into a product-agnostic {failing_suites: [...]} dict — that part measured at
zero product-specific references and stays in se-lab as-is. Everything below
is the text wrapped around that context: it names the product and encodes its
own deploy/harness/product failure taxonomy, so it belongs to the product lab.

classification_rubric() is deliberately its own method rather than inlined
into failure_prompt(): the original M3Undle implementation had two
descriptions of the same rubric (one embedded in the failure-analysis prompt,
a shorter one duplicated in the eval-cases prompt) that had already drifted
from each other. A concrete plugin should write the rubric once here and have
both failure_prompt() and eval_cases() reference it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class AnalysisPlugin(ABC):
    @abstractmethod
    def classification_rubric(self) -> str:
        """The product's deploy/harness/product classification rubric text."""

    @abstractmethod
    def failure_prompt(self, task: str, context: dict[str, Any], *, max_chars: int) -> str:
        """Wrap a build_failure_context() result into a full prompt for `task`."""

    @abstractmethod
    def extract_log_context(self, *, session_id: str | None, max_lines: int) -> dict[str, Any]:
        """Build structured context for a stream/session log analysis request."""

    @abstractmethod
    def eval_cases(self, task: str) -> list[dict[str, Any]]:
        """Lightweight {prompt, expected} cases used by `lab eval ai --task <task>`."""
