"""SmarTAI v2 — Reflex application entry.

Pages are auto-registered via @rx.page decorators (importing the pages module
is enough). The App's theme controls global Radix tokens.
"""
from __future__ import annotations

import reflex as rx

from smartai_v2 import pages  # noqa: F401 — triggers @rx.page registration
from smartai_v2.theme import theme


app = rx.App(
    theme=theme,
    style={
        "font_family": "Inter, 'Noto Sans SC', -apple-system, BlinkMacSystemFont, sans-serif",
    },
)
