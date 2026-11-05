"""Inspect programming execution as the service user: python -m backend.tools.programming_readiness."""
import asyncio
import json
import re

from backend.config import settings
from backend.tools.oci_grading_runner import IsolatedRunnerUnavailable, run_python_oci
from tools.runner_spike.worker import SCHEMA_VERSION


async def capabilities():
    match = re.fullmatch(r"(?:[a-zA-Z0-9._:/-]+@)?(sha256:[a-f0-9]{64})", settings.program_runner_image)
    result = {"status": "disabled", "contract_version": SCHEMA_VERSION,
              "runtime": settings.program_runner_runtime, "image_digest": match[1] if match else None,
              "languages": [], "modes": []}
    if settings.program_runner_mode != "oci":
        return result
    result["status"] = "unavailable"
    if not match or settings.runtime_environment != "production":
        return result
    try:
        stdin = await run_python_oci("print(int(input()) + 1)", "4", timeout=3, bind_request=True)
        function = await run_python_oci("def add(a, *, b): return a + b", timeout=3, bind_request=True,
            function_call={"name": "add", "args": [2], "kwargs": {"b": 3}})
    except IsolatedRunnerUnavailable:
        return result
    if (stdin.passed and stdin.stdout == "5\n" and function.passed and function.return_value == "5"
            and stdin.isolation_mode == function.isolation_mode == "oci_container"):
        result.update(status="ready", languages=["python"], modes=["stdin", "function"])
    return result


if __name__ == "__main__":
    observed = asyncio.run(capabilities())
    print(json.dumps(observed, sort_keys=True))
    raise SystemExit(1 if observed["status"] == "unavailable" else 0)
