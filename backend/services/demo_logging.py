"""Keep temporary guest details in task records, not shared service journals.

Task progress/errors already have owner-scoped durable records which task
deletion removes. Service logs retain only a generic diagnostic for guests;
ordinary users' log records are unchanged. Context follows asyncio tasks and
threadpool calls, and workers restore it from their durable owner identity.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import logging

import jwt

_temporary_demo = ContextVar("temporary_demo_logging", default=False)


@contextmanager
def demo_log_context(owner_id: str = "", *, temporary: bool = False):
    from backend.services.frontier_demo import USER_PREFIX
    token = _temporary_demo.set(temporary or str(owner_id).startswith(USER_PREFIX))
    try:
        yield
    except Exception as exc:
        if _temporary_demo.get():
            # ASGI servers may log an uncaught exception after our context exits.
            exc._smartai_temporary_demo = True
        raise
    finally:
        _temporary_demo.reset(token)


def install_demo_log_redaction():
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_smartai_demo_redaction", False):
        return

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        exception = record.exc_info[1] if record.exc_info else None
        if _temporary_demo.get() or getattr(exception, "_smartai_temporary_demo", False):
            if record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) == 5:
                # Uvicorn's AccessFormatter requires its five positional fields.
                record.args = ("-", "-", "/temporary-demo", "-", record.args[4])
            else:
                record.msg = "Temporary demo activity" if record.levelno < logging.WARNING else "Temporary demo operation needs attention"
                record.args = ()
            record.exc_info = record.exc_text = record.stack_info = None
        return record

    factory._smartai_demo_redaction = True
    logging.setLogRecordFactory(factory)


class DemoLoggingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from backend.config import settings
        from backend.services.frontier_demo import USER_PREFIX
        temporary = scope["path"].rstrip("/") in {
            "/auth/frontier-demo-session", "/auth/frontier-demo-session/end",
        }
        authorization = dict(scope.get("headers", [])).get(b"authorization", b"").decode("latin1")
        scheme, _, bearer = authorization.partition(" ")
        if scheme.lower() == "bearer" and bearer:
            try:
                payload = jwt.decode(bearer, settings.jwt_secret,
                    algorithms=[settings.jwt_algorithm], options={"verify_exp": False})
                temporary |= (payload.get("scope") == "frontier_demo"
                    and str(payload.get("sub", "")).startswith(USER_PREFIX))
            except jwt.InvalidTokenError:
                pass

        async def tagged_send(message):
            if temporary and message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []),
                    (b"x-smartai-transient-demo", b"1")]}
            await send(message)

        with demo_log_context(temporary=temporary):
            await self.app(scope, receive, tagged_send)
