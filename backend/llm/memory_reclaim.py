"""Return unused pages before a low-memory, idle scheduler reduces concurrency."""
import gc
import sys
from functools import lru_cache


@lru_cache(maxsize=1)
def _malloc_trim():
    if not sys.platform.startswith("linux"):
        return None
    import ctypes
    try:
        trim = ctypes.CDLL(None).malloc_trim
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        return trim
    except (AttributeError, OSError):
        return None  # Other allocators retain the existing admission guard.


def reclaim_idle_memory():
    trim = _malloc_trim()
    if trim is not None:
        # Python cycles and native free arenas are separate retention layers.
        # Neither operation drops live grading data or alters a frozen rubric.
        gc.collect()
        trim(0)
        return True
    return False
