"""Disposable U3 worker. Abrupt exit is intentional fault injection, never production."""
import asyncio
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from safety import block_network, require_inside, require_isolated_run

isolated_root = require_isolated_run()
network_attempts = block_network()
state_path = require_inside(sys.argv[1], isolated_root)
mode = sys.argv[2]
if mode not in {"crash", "resume", "ack"}:
    raise RuntimeError("Invalid synthetic worker mode")
state = json.loads(state_path.read_text())
for key in ("storage", "pdf", "calls", "result"):
    require_inside(state[key], isolated_root)

from backend.agents.recognition_agent import RecognitionReadRequestV1
from backend.recognition.runtime import RecognitionCapacity
from backend.services.recognition_artifacts import RecognitionArtifactStore
from backend.services.recognition_runs import RecognitionRunService, _GuardedCalls
from backend.storage.local import LocalStorage
from backend.tests.test_recognition_recheck import Engine

request = RecognitionReadRequestV1.model_validate(state["request"])
engine = Engine(initial=state["texts"])
engine.capabilities = engine.capabilities.model_copy(update={
    "semantic_repair": False, "response_recheck": False,
})
original_read = engine.recognize

async def read(unit):
    with Path(state["calls"]).open("a") as out:
        out.write(json.dumps({"mode": mode, "page": unit.page_number}) + "\n")
        out.flush()
        os.fsync(out.fileno())
    if mode == "crash" and state["cut"] == "pending_second" and unit.page_number == 2:
        os._exit(86)
    return await original_read(unit)

engine.recognize = read
original_before = _GuardedCalls.before_dispatch
count = 0

async def before(self, *args, **kwargs):
    global count
    count += 1
    if mode == "crash" and state["cut"] == "before_second" and count == 2:
        os._exit(86)
    return await original_before(self, *args, **kwargs)

_GuardedCalls.before_dispatch = before

async def main():
    service = RecognitionRunService(store=RecognitionArtifactStore(LocalStorage(Path(state["storage"]))),
                                    capacity=RecognitionCapacity())
    args = {}
    if mode == "ack":
        args = {"retry_key": "u3-explicit-ack-1", "acknowledge_pending_retry": True}
    result = await service.run(request, Path(state["pdf"]).read_bytes(), engine=engine,
        prompt_version="faithful-reader-v1", authorized_owner_id=request.source.owner_id, **args)
    payload = {"status": result.status, "safe_error_code": result.safe_error_code,
        "current_calls": result.current_usage.total_calls, "operation_id": result.operation_id,
        "assembly": result.assembly.model_dump(mode="json") if result.assembly else None}
    Path(state["result"]).write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    if network_attempts:
        raise RuntimeError("Unexpected network attempt in synthetic worker")

asyncio.run(main())
