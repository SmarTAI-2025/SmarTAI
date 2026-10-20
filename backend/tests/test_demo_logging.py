import asyncio
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from uvicorn.logging import AccessFormatter

from backend.auth import create_token
from backend.models import User
from backend.services import frontier_demo
from backend.services.demo_logging import DemoLoggingMiddleware, demo_log_context, install_demo_log_redaction


@pytest.mark.asyncio
async def test_guest_context_isolated_from_other_tasks_threads_and_exception_details(caplog):
    install_demo_log_redaction()
    logger = logging.getLogger("demo-regression")
    with caplog.at_level(logging.INFO):
        async def guest():
            with demo_log_context("frontier_demo_example"):
                await asyncio.to_thread(logger.info, "guest private path %s", "secret-task")
                await asyncio.sleep(0)
                try:
                    raise ValueError("guest private exception")
                except ValueError:
                    logger.exception("guest private traceback")
        async def teacher():
            await asyncio.sleep(0)
            logger.warning("teacher diagnostic unchanged")
        await asyncio.gather(guest(), teacher())
        logger.info("outside context unchanged")
    assert "guest private" not in caplog.text and "secret-task" not in caplog.text
    assert "teacher diagnostic unchanged" in caplog.text
    assert "outside context unchanged" in caplog.text
    assert "Temporary demo operation needs attention" in caplog.text


def test_uvicorn_access_format_and_outer_exception_logging_remain_safe(caplog):
    install_demo_log_redaction()
    formatter = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False)
    with demo_log_context("frontier_demo_example"):
        record = logging.getLogRecordFactory()("uvicorn.access", logging.INFO, "server", 1,
            '%s - "%s %s HTTP/%s" %d', ("127.0.0.1", "GET", "/tasks/private-id", "1.1", 200), None)
    assert "private-id" not in formatter.format(record)
    assert "200" in formatter.format(record)
    try:
        with demo_log_context("frontier_demo_example"):
            raise ValueError("late guest details")
    except ValueError:
        logging.getLogger("server").exception("ASGI failure")
    assert "late guest details" not in caplog.text


def test_only_signed_guest_requests_are_tagged_and_redacted(caplog):
    install_demo_log_redaction()
    app = FastAPI()
    app.add_middleware(DemoLoggingMiddleware)
    @app.get("/tasks/synthetic-id")
    def echo():
        logging.getLogger("request").warning("request path synthetic-id")
        return {"ok": True}
    client = TestClient(app)
    guest = User(id="frontier_demo_example", username="guest", role="teacher", password_hash="")
    token = frontier_demo.create_token(guest)
    response = client.get("/tasks/synthetic-id", headers={"Authorization": "Bearer " + token})
    assert response.headers["x-smartai-transient-demo"] == "1"
    assert "request path synthetic-id" not in caplog.text
    caplog.clear()
    response = client.get("/tasks/synthetic-id", headers={"Authorization": "Bearer " + create_token("teacher", "teacher")})
    assert "x-smartai-transient-demo" not in response.headers
    assert "request path synthetic-id" in caplog.text
    response = client.get("/tasks/synthetic-id", headers={"Authorization": "Bearer invalid"})
    assert "x-smartai-transient-demo" not in response.headers
