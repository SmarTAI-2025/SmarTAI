"""Reflex frontend_v2 compile smoke test. Run from frontend_v2/ directory."""
import sys

sys.path.insert(0, ".")

import smartai_v2.smartai_v2  # noqa: E402

smartai_v2.smartai_v2.app._compile()
print("OK: reflex compile succeeded")
