"""Backend import + FastAPI app build smoke test. Run from project root."""
import sys
import os

sys.path.insert(0, ".")
os.environ.setdefault("SMARTAI_REQUIRE_AUTH", "false")

from backend.models import Task, TaskStatus, JobProgress  # noqa: F401
from backend.state import get_task_store  # noqa: F401
from backend.main import app

routes = [r.path for r in app.routes]
print(f"OK: FastAPI app built with {len(routes)} routes")
print(f"Sample routes: {routes[:8]}")
