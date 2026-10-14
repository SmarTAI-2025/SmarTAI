"""Exercise custom expert ratios through the real saved/frozen setup contract."""
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_saved_expert_weights_reload_and_freeze_with_the_grading_run():
    from backend.api import tasks
    from backend.auth import create_token
    from backend.config import settings
    from backend.db.provider_repository import upsert_provider_config
    from backend.db import workflow_repository
    from backend.models import ProviderConfig
    from backend.services import task_facade
    from backend.tests.test_grading_source_readiness import _seed_legacy_structured_case

    seeded = _seed_legacy_structured_case()
    owner_id = seeded["owner_id"]
    task_id = seeded["assignment_id"]
    original = seeded["workflow"]
    first_id = original.grading_setup["selected_provider_ids"][0]
    second = upsert_provider_config(
        owner_id,
        ProviderConfig(
            provider_type="openai", api_key="synthetic-second-secret",
            model="gpt-second-test", base_url="https://api.openai.com/v1",
        ),
        master_key=settings.provider_encryption_key,
    )
    setup = {
        **original.grading_setup,
        "selected_provider_ids": [first_id, second.id],
        "primary_provider_id": first_id,
        "aggregation_method": "weighted_average",
        "expert_weights": {first_id: 1, second.id: 3},
    }
    app = FastAPI()
    app.include_router(tasks.router)
    headers = {"Authorization": f"Bearer {create_token(owner_id, 'teacher')}"}
    route = f"/tasks/{task_id}/grading-setup"
    client = TestClient(app)
    saved = client.put(route, headers=headers, json={
        "expected_workflow_revision": original.workflow_revision,
        "grading_setup": setup,
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()["status"] == "saved"
    first_fingerprint = saved.json()["grading_setup_fingerprint"]
    persisted = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    assert persisted.grading_setup["expert_weights"] == setup["expert_weights"]
    assert persisted.grading_setup_fingerprint == first_fingerprint

    # A ratio-only edit must change the persisted idempotency fingerprint too.
    setup["expert_weights"] = {first_id: 3, second.id: 1}
    changed = client.put(route, headers=headers, json={
        "expected_workflow_revision": persisted.workflow_revision,
        "grading_setup": setup,
    })
    assert changed.status_code == 200, changed.text
    assert changed.json()["grading_setup_fingerprint"] != first_fingerprint
    reloaded = TestClient(app).get(route, headers=headers)
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()["grading_setup"]["expert_weights"] == setup["expert_weights"]
    persisted = workflow_repository.get_workflow(task_id, owner_id=owner_id)
    unchanged = client.put(route, headers=headers, json={
        "expected_workflow_revision": persisted.workflow_revision,
        "grading_setup": setup,
    })
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["status"] == "unchanged"
    assert workflow_repository.get_workflow(
        task_id, owner_id=owner_id,
    ).workflow_revision == persisted.workflow_revision

    started = task_facade.start_task_grading(
        task_id=task_id, owner_id=owner_id,
        expected_workflow_revision=persisted.workflow_revision,
    )
    frozen = workflow_repository.get_run_setup(started["job_id"])
    assert frozen is not None
    assert frozen.setup["selected_provider_ids"] == [first_id, second.id]
    assert frozen.setup["expert_weights"] == setup["expert_weights"]
    assert frozen.setup == persisted.grading_setup
