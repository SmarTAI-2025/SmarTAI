"""Test-only database reset and deterministic CI sharding helpers."""

from collections import Counter
from pathlib import Path
import shutil

from sqlalchemy.engine import Engine


def assign_modules(nodeids: list[str], shard_count: int) -> dict[str, int]:
    """Keep each module together and balance by collected (parametrized) tests."""
    if shard_count < 1:
        raise ValueError("shard count must be positive")
    weights = Counter(nodeid.split("::", 1)[0] for nodeid in nodeids)
    loads = [0] * shard_count
    assignments = {}
    for module in sorted(weights, key=lambda name: (-weights[name], name)):
        shard = min(range(shard_count), key=lambda index: (loads[index], index))
        assignments[module] = shard + 1
        loads[shard] += weights[module]
    return assignments


def restore_sqlite_database(engine: Engine, template: Path, target: Path) -> None:
    """Restore an empty schema, never sharing mutable rows between tests."""
    engine.dispose()
    # A previous test may have enabled WAL or changed/dropped tables. Replace
    # the whole file and its sidecars, not just rows in known ORM tables.
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{target}{suffix}").unlink(missing_ok=True)
    shutil.copyfile(template, target)
