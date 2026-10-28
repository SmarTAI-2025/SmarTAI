"""Keep normal pytest discovery inert; the launcher opts into the probes."""
import os
from pathlib import Path
import sys

if any(name.startswith('SMARTAI_U3_AUDIT_') for name in os.environ):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from safety import require_isolated_run
    require_isolated_run()
else:
    # Directory discovery (including bare pytest at the repository root) must
    # not fail or activate the audit. Explicit test-file requests bypass this
    # ignore list, so every probe module also checks before backend imports.
    collect_ignore_glob = ['test_*.py']
