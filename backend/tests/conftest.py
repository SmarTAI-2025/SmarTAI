"""Keep the test suite on an isolated SQLite database and storage directory."""

import os
import tempfile
from pathlib import Path

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="smartai-tests-"))
os.environ.setdefault("SMARTAI_DATABASE_URL", f"sqlite:///{(_TEST_ROOT / 'test.db').as_posix()}")
os.environ.setdefault("SMARTAI_DATABASE_AUTO_CREATE", "true")
os.environ.setdefault("SMARTAI_SEED_TEST_USERS", "false")
os.environ.setdefault("SMARTAI_STORAGE_ROOT", str(_TEST_ROOT / "uploads"))
os.environ.setdefault("SMARTAI_PROVIDER_ENCRYPTION_KEY", "test-suite-provider-master-key")

import pytest  # noqa: E402

from backend.db.base import Base  # noqa: E402
from backend.db.session import configure_database  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_database():
    database_url = os.environ["SMARTAI_DATABASE_URL"]
    postgres_url = os.environ.get("SMARTAI_TEST_POSTGRES_URL")
    if postgres_url and database_url == postgres_url:
        # The focused PostgreSQL suite owns its schema lifecycle through its
        # ``pg_database`` fixture.  Do not apply the SQLite file reset before
        # that fixture has a chance to run.
        yield
        return

    engine = configure_database(database_url)
    # ``create_all`` alone retains rows from the previous test, which makes
    # fixed-id security regressions order-dependent. This suite always uses the
    # disposable SQLite file declared above; recreating that file avoids the
    # normalized schema's intentional cyclic foreign keys during ``drop_all``.
    engine.dispose()
    prefix = "sqlite:///"
    if not database_url.startswith(prefix) or database_url.endswith(":memory:"):
        raise RuntimeError("backend tests require their disposable SQLite database")
    Path(database_url[len(prefix):]).unlink(missing_ok=True)
    Base.metadata.create_all(engine)
    yield
