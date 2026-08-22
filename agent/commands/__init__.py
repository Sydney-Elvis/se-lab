"""se-lab's own built-in commands.

Importing this package registers them with agent.registry (each module's
@registry.command decorators run at import time). A product lab's own
package does the same for its commands; both use the one mechanism in
agent/registry.py.
"""

from __future__ import annotations

from . import artifacts, clients, discover, doctor, down, eval, report, settings

__all__ = ["artifacts", "clients", "discover", "doctor", "down", "eval", "report", "settings"]
