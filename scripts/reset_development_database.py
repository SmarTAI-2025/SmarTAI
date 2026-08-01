#!/usr/bin/env python
"""Reset a local SQLite database and rebuild it with Alembic.

The reset is deliberately limited to SQLite files under the repository's
``data/`` directory. ``--force-explicit-path`` is required for an explicit
SQLite path elsewhere. The command only unlinks the database file; uploaded
storage is never touched.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy.engine import make_url

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_DATA = (REPO_ROOT / "data").resolve()

# Allow running from the repo root without installing the package.
sys.path.insert(0, str(REPO_ROOT))


def _sqlite_path(database_url: str, repo_root: Path = REPO_ROOT) -> Path | None:
    """Return a resolved SQLite path, or ``None`` for a non-SQLite URL."""
    if not database_url.startswith("sqlite"):
        return None
    parsed = make_url(database_url)
    if parsed.database is None or parsed.database == ":memory:":
        return None
    path = Path(parsed.database).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve()


def _database_url(value: str, repo_root: Path = REPO_ROOT) -> str:
    """Accept either a SQLAlchemy URL or a filesystem path."""
    if "://" in value:
        return value
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return f"sqlite:///{path.resolve().as_posix()}"


def _inside_data(path: Path, repo_data: Path = REPO_DATA) -> bool:
    try:
        path.relative_to(repo_data)
    except ValueError:
        return False
    return True


def _upgrade_head(database_url: str) -> None:
    """Run the normalized baseline through Alembic's Python API."""
    from alembic import command
    from alembic.config import Config
    from backend.config import settings

    old_database_url = os.environ.get("SMARTAI_DATABASE_URL")
    old_heavy = os.environ.get("SMARTAI_DATABASE_HEAVY")
    old_settings_heavy = settings.database_heavy
    try:
        os.environ["SMARTAI_DATABASE_URL"] = database_url
        os.environ["SMARTAI_DATABASE_HEAVY"] = "OFF"
        # env.py validates against the process-wide settings singleton.
        settings.database_heavy = False
        config = Config(str(REPO_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(REPO_ROOT / "backend/db/migrations"))
        command.upgrade(config, "head")
    finally:
        settings.database_heavy = old_settings_heavy
        if old_database_url is None:
            os.environ.pop("SMARTAI_DATABASE_URL", None)
        else:
            os.environ["SMARTAI_DATABASE_URL"] = old_database_url
        if old_heavy is None:
            os.environ.pop("SMARTAI_DATABASE_HEAVY", None)
        else:
            os.environ["SMARTAI_DATABASE_HEAVY"] = old_heavy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reset a development SQLite database to the normalized baseline."
    )
    parser.add_argument(
        "--database",
        help="SQLite path or URL (defaults to SMARTAI_DATABASE_URL/settings).",
    )
    parser.add_argument(
        "--force-explicit-path",
        action="store_true",
        help="Allow an explicit SQLite path outside the repository data directory.",
    )
    args = parser.parse_args(argv)

    from backend.config import settings

    database_url = _database_url(args.database or settings.database_url)
    database_path = _sqlite_path(database_url)
    if database_path is None:
        print(
            "Refusing to reset a non-SQLite database. This development command "
            "supports only SQLite files.",
            file=sys.stderr,
        )
        return 2
    if not args.force_explicit_path and not _inside_data(database_path):
        print(
            f"Refusing to reset {database_path}: it is outside {REPO_DATA}. "
            "Use --force-explicit-path only for an intentionally explicit path.",
            file=sys.stderr,
        )
        return 2

    database_path.parent.mkdir(parents=True, exist_ok=True)
    if database_path.exists():
        database_path.unlink()
    print(f"Recreating {database_path} from Alembic ...")
    _upgrade_head(database_url)
    print("Done. Uploaded storage was not modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
