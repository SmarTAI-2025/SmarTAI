"""Print reflex version. Used as a smoke test from the project root."""
import reflex
from importlib.metadata import version

print(f"reflex.__version__: {reflex.__version__}")
try:
    print(f"importlib.metadata.version('reflex'): {version('reflex')}")
except Exception as e:
    print(f"importlib.metadata error: {e}")
