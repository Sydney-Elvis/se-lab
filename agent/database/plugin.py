"""Database-reset plugin interface.

Backs `lab recreate --fresh`: wipe just the product's database, leaving
config/data/media directories untouched (that broader wipe, `--first-run`,
already operates on generic runtime directories and needs no product-specific
hook). The original implementation this replaces, _remove_db_files(),
hardcoded m3undle.db/-shm/-wal — must not assume SQLite: family-librarian
uses postgres, where "reset" means something else entirely (drop/recreate a
schema, not delete files).
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class DatabasePlugin(ABC):
    @abstractmethod
    def reset(self) -> None:
        """Reset the product's database to an empty state."""
