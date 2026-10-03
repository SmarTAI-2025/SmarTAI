"""Local draft acceptance fixture. Never import this from production startup."""
import os
import tempfile
from pathlib import Path

if os.environ.get("SMARTAI_RUNTIME_ENVIRONMENT") != "test":
    raise RuntimeError("Draft QA backend requires the test runtime")
for key in ("SMARTAI_DATABASE_URL", "SMARTAI_DATABASE_URL_LIGHT"):
    url = os.environ.get(key, "")
    if not url.startswith("sqlite:///"):
        raise RuntimeError("Draft QA requires an explicit isolated SQLite database")
    target = Path(url.removeprefix("sqlite:///")).resolve()
    if not any(target.is_relative_to(root.resolve()) for root in (Path(tempfile.gettempdir()), Path("/tmp"))):
        raise RuntimeError("Draft QA database must be in a temporary directory")

from backend.llm import registry

class OfflineProvider:
    supports_vision = False
    is_shared_pool = False

    def __init__(self, config):
        self.config = config
        self.provider_type = config.provider_type
        self.model = config.model
        self.provider_id = config.provider_type + ":" + config.model

    async def ainvoke(self, messages):
        raise RuntimeError("Draft QA disables every external model invocation")

registry.build_provider = OfflineProvider
from backend.main import app  # noqa: E402
