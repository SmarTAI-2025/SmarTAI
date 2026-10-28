"""Prevent accidentally running write-capable probes outside the audit launcher."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from safety import require_isolated_run

require_isolated_run()
