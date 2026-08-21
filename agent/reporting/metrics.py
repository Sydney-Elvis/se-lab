from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import common as lab_common


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def append_ai_call(record: dict[str, Any]) -> None:
    append_jsonl(lab_common.metrics_dir() / "ai-calls.jsonl", record)


def append_ai_run(record: dict[str, Any]) -> None:
    append_jsonl(lab_common.metrics_dir() / "ai-runs.jsonl", record)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows

