"""Fail closed before a probe imports any write-capable backend modules."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from safety import require_isolated_run


def require_probe_run():
    require_isolated_run()
