"""Keep the test suite on an isolated SQLite database and storage directory."""

import os
import tempfile
from pathlib import Path

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="smartai-tests-"))
os.environ.setdefault("SMARTAI_DATABASE_URL", f"sqlite:///{(_TEST_ROOT / 'test.db').as_posix()}")
os.environ.setdefault("SMARTAI_DATABASE_AUTO_CREATE", "true")
os.environ.setdefault("SMARTAI_SEED_TEST_USERS", "false")
os.environ.setdefault("SMARTAI_STORAGE_ROOT", str(_TEST_ROOT / "uploads"))
# Tests must never inherit developer or deployment secrets.  Use explicit,
# independent fake values that satisfy the runtime contract.
os.environ["SMARTAI_RUNTIME_ENVIRONMENT"] = "test"
os.environ["SMARTAI_PROVIDER_ENCRYPTION_KEY"] = (
    "test-suite-provider-master-key-0123456789abcdef"
)
os.environ["SMARTAI_JWT_SECRET"] = "test-suite-jwt-secret-0123456789abcdef"

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

from backend.db.base import Base  # noqa: E402
from backend.db.session import configure_database  # noqa: E402
from backend.tests.ci_support import assign_modules, restore_sqlite_database  # noqa: E402


def pytest_addoption(parser):
    parser.addoption("--test-shard", help="Run module-preserving CI shard INDEX/COUNT (1-based)")


def pytest_collection_modifyitems(config, items):
    option = config.getoption("--test-shard")
    if option is None:
        return
    try:
        index, count = map(int, option.split("/"))
        if not 1 <= index <= count:
            raise ValueError
    except ValueError:
        raise pytest.UsageError("--test-shard must be INDEX/COUNT with 1 <= INDEX <= COUNT") from None
    assignments = assign_modules([item.nodeid for item in items], count)
    selected, deselected = [], []
    for item in items:
        group = selected if assignments[item.nodeid.split("::", 1)[0]] == index else deselected
        group.append(item)
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)


@pytest.fixture(scope="session")
def sqlite_schema_template(tmp_path_factory):
    # Collection has imported the ORM models before this fixture runs. Build
    # the empty schema once; each test still gets a fresh, file-backed copy.
    template = tmp_path_factory.mktemp("sqlite-schema") / "empty.db"
    engine = create_engine(f"sqlite:///{template.as_posix()}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    return template


@pytest.fixture(autouse=True)
def isolated_database(request):
    database_url = os.environ["SMARTAI_DATABASE_URL"]
    postgres_url = os.environ.get("SMARTAI_TEST_POSTGRES_URL")
    if postgres_url and database_url == postgres_url:
        # The focused PostgreSQL suite owns its schema lifecycle through its
        # ``pg_database`` fixture.  Do not apply the SQLite file reset before
        # that fixture has a chance to run.
        yield
        return

    engine = configure_database(database_url)
    prefix = "sqlite:///"
    if not database_url.startswith(prefix) or database_url.endswith(":memory:"):
        raise RuntimeError("backend tests require their disposable SQLite database")
    restore_sqlite_database(
        engine, request.getfixturevalue("sqlite_schema_template"),
        Path(database_url[len(prefix):]),
    )
    yield


_ZERO_OCR_MODULES = {
    "test_question_generation_concurrency.py", "test_normalized_analytics.py",
    "test_grading_run_lifecycle.py", "test_grading_runner.py",
    "test_programming_skill.py", "test_objective_grading.py",
    "test_provider_persistence.py", "test_custom_provider_api.py",
    "test_baidu_unlimited_ocr_credentials.py",
    "test_course_library_tags.py", "test_task_history_progress.py",
    "test_task_finalization_contract.py", "test_workflow_facade_integrity.py",
    "test_knowledge_activity.py", "test_task_kb_contract.py",
    "test_ocr_readonly_routes.py",
}
_ZERO_OCR_READS = {
    "test_search_citation_api_is_owner_scoped_and_read_only",
    "test_content_route_returns_true_mime_and_frozen_security_headers",
    "test_active_or_disguised_current_sources_are_never_inlined",
    "test_owner_task_current_source_and_non_source_boundaries_are_uniform_404",
    "test_missing_object_is_unavailable_and_content_is_not_empty_200",
    "test_descriptor_storage_race_projects_current_lifecycle_state",
    "test_content_storage_race_projects_current_lifecycle_error",
    "test_content_disposition_strips_paths_and_header_controls",
    "test_storage_read_failure_is_safe_retryable_503",
    "test_source_file_routes_require_authentication",
    "test_unsafe_test_candidates_never_mutate_or_consume_plan",
    "test_atomic_question_batch_rolls_back_before_any_partial_write",
    "test_task_tombstone_fences_atomic_question_patch_publication",
    "test_auxiliary_failure_keeps_questions_usable_and_retry_success_clears_error",
    "test_material_apply_failure_after_plan_ready_keeps_attention_without_blocking_task",
}


@pytest.fixture(autouse=True)
def non_recognition_paths_never_dispatch_ocr(request, monkeypatch):
    """Instrument existing action tests, without replaying them in a second suite."""
    if (request.path.name not in _ZERO_OCR_MODULES
            and request.node.originalname not in _ZERO_OCR_READS):
        yield
        return
    from backend.services.recognition_runs import RecognitionRunService
    from backend.skills.ocr_ingest import LLMVisionOCRSkill, BaiduUnlimitedOCRSkill
    from backend.skills.recognition_reader import LLMRecognitionEngine, BaiduRecognitionEngine
    from backend.tools.baidu_unlimited_ocr import BaiduUnlimitedOCRClient
    from backend.llm.providers import BaseProvider
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append(request.node.nodeid)
        pytest.fail("Non-recognition action attempted OCR/vision dispatch")

    for cls, method in (
        (RecognitionRunService, "run"), (LLMVisionOCRSkill, "recognize_images"),
        (BaiduUnlimitedOCRSkill, "recognize_document"), (BaiduUnlimitedOCRSkill, "recognize_images"),
        (LLMRecognitionEngine, "_call_vision"), (BaiduRecognitionEngine, "recognize"),
        (BaiduUnlimitedOCRClient, "_submit_once"), (BaseProvider, "ainvoke_vision"),
    ):
        monkeypatch.setattr(cls, method, forbidden)
    yield
    assert calls == []


@pytest.fixture(autouse=True)
def material_candidates_never_execute_code(request, monkeypatch):
    if request.path.name not in {
        "test_material_recognition_integration.py", "test_ocr_mounted_routes.py",
        "test_auxiliary_operation_recovery.py", "test_question_generation_concurrency.py",
    }:
        yield
        return
    from backend.tools import code_interpreter, grading_runner
    from backend.skills import programming
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append(request.node.nodeid)
        pytest.fail("Material recognition/generation/review must not execute code")

    for module, name in ((code_interpreter, "run_sandbox"), (code_interpreter, "run_python_subprocess"),
                         (code_interpreter, "_run_function_call"), (grading_runner, "run_grading_request"),
                         (programming, "run_sandbox")):
        monkeypatch.setattr(module, name, forbidden)
    yield
    assert calls == []
