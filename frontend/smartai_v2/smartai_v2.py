"""SmarTAI v2 — Reflex application entry.

Pages are auto-registered via @rx.page decorators (importing the pages module
is enough). The App's theme controls global Radix tokens.
"""
from __future__ import annotations

import reflex as rx
from starlette.types import ASGIApp, Receive, Scope, Send

from smartai_v2 import pages  # noqa: F401 — triggers @rx.page registration
from smartai_v2.theme import theme


def _normalize_reflex_event_path(asgi_app: ASGIApp) -> ASGIApp:
    """Accept Reflex Socket.IO websocket requests with or without a trailing slash."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") in {"http", "websocket"} and scope.get("path") == "/_event":
            scope = dict(scope)
            scope["path"] = "/_event/"
            if "raw_path" in scope:
                scope["raw_path"] = b"/_event/"
        await asgi_app(scope, receive, send)

    return app


app = rx.App(
    api_transformer=_normalize_reflex_event_path,
    theme=theme,
    style={
        "font_family": "Inter, 'Noto Sans SC', -apple-system, BlinkMacSystemFont, sans-serif",
    },
)
