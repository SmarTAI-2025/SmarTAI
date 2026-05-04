"""UI state — sidebar, modals, theme preferences."""
from __future__ import annotations

import reflex as rx




class UIState(rx.State):
    # Reflex's LocalStorage only supports str values. Persist as "1"/"0" and
    # expose a computed bool to keep the rest of the app idiomatic.
    sidebar_collapsed_raw: str = rx.LocalStorage("0", name="smartai_sidebar_collapsed")
    theme_mode: str = rx.LocalStorage("light", name="smartai_theme_mode")

    confirm_open: bool = False
    confirm_message: str = ""
    confirm_action: str = ""
    confirm_payload: str = ""

    @rx.var
    def sidebar_collapsed(self) -> bool:
        return self.sidebar_collapsed_raw == "1"

    @rx.event
    def toggle_sidebar(self):
        self.sidebar_collapsed_raw = "0" if self.sidebar_collapsed_raw == "1" else "1"

    @rx.event
    def set_theme_mode(self, mode: str):
        self.theme_mode = mode

    @rx.event
    def open_confirm(self, message: str, action: str, payload: str = ""):
        self.confirm_message = message
        self.confirm_action = action
        self.confirm_payload = payload
        self.confirm_open = True

    @rx.event
    def close_confirm(self):
        self.confirm_open = False
        self.confirm_message = ""
        self.confirm_action = ""
        self.confirm_payload = ""
