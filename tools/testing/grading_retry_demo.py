"""Disposable local browser acceptance server. No real model or user data.

Run from the repository root with the standardized Python environment:
python -m tools.testing.grading_retry_demo
The printed manifest is consumed by e2e/grading-retry.spec.ts.
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path


def main():
    root = Path(tempfile.mkdtemp(prefix="smartai-retry-browser-"))
    os.environ.update({
        "SMARTAI_RUNTIME_ENVIRONMENT": "test",
        "SMARTAI_DATABASE_URL": f"sqlite:///{root / 'test.db'}",
        "SMARTAI_DATABASE_AUTO_CREATE": "true",
        "SMARTAI_STORAGE_ROOT": str(root / "uploads"),
        "SMARTAI_PROVIDER_ENCRYPTION_KEY": "test-suite-provider-master-key-0123456789abcdef",
        "SMARTAI_JWT_SECRET": "test-suite-jwt-secret-0123456789abcdef",
        "SMARTAI_SEED_TEST_USERS": "false",
        "SMARTAI_ALLOW_DEMO_TOKENS": "false",
        "SMARTAI_E2E_FAKE_PROVIDER": "false",
        "SMARTAI_GRADING_POLL_SECONDS": "1",
        "SMARTAI_FRONTEND_URLS": "http://127.0.0.1:5189",
    })
    from backend.auth import hash_password
    from backend.db.session import create_schema, session_scope
    from backend.db.models import UserRecord
    from backend.db import grading_repository, workflow_repository
    from backend.services import grading_adapter, grading_runs, task_facade
    from backend.testing.fake_provider import FakeProvider, fake_grade_batch
    from backend.tests.test_grading_source_readiness import _seed_legacy_structured_case

    create_schema()
    seed = _seed_legacy_structured_case(question_count=2)
    owner, task_id = seed["owner_id"], seed["assignment_id"]
    with session_scope() as session:
        session.get(UserRecord, owner).password_hash = hash_password("retry-browser-password")

    class Registry:
        def select(self, *_args, **_kwargs):
            return self

        def count(self):
            return 1

    grading_runs._registry_for = lambda _owner: Registry()
    calls = []

    async def controlled_batch(**kwargs):
        first = not calls
        calls.append({"attempt": len(calls) + 1, "failed_qids": ["q2"] if first else []})
        (root / "model-calls.json").write_text(json.dumps(calls, indent=2))
        if not first:
            await asyncio.sleep(5)  # Keep genuine in-progress UI observable.
        return await fake_grade_batch(**kwargs, provider=FakeProvider(fail_qids={"q2"} if first else set()))

    grading_adapter.grade_batch = controlled_batch
    first = task_facade.start_task_grading(
        task_id=task_id, owner_id=owner, request_id="browser-first-attempt",
        expected_workflow_revision=seed["workflow"].workflow_revision,
    )
    asyncio.run(grading_runs.process_run(run_id=first["job_id"], worker_id="browser-seed"))
    success = next(row for row in grading_repository.list_results_for_run(first["job_id"]) if row.q_id == "q1")
    grading_repository.add_teacher_review(success.id, teacher_id=owner, new_score=9,
                                         new_comment="Historical teacher edit", confirm=True)
    manifest = {
        "task_id": task_id, "old_run_id": first["job_id"],
        "username": owner, "password": "retry-browser-password",
        "model_calls_path": str(root / "model-calls.json"),
    }
    output = Path("output/playwright")
    output.mkdir(parents=True, exist_ok=True)
    (output / "grading-retry-demo.json").write_text(json.dumps(manifest, indent=2))
    print(f"Controlled mock only; database: {root}; task: {task_id}", flush=True)
    import uvicorn
    from backend.main import app
    uvicorn.run(app, host="127.0.0.1", port=8019)


if __name__ == "__main__":
    main()
