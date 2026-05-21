"""Experts (BYOK) state."""
from __future__ import annotations

from typing import Any

import reflex as rx

from smartai_v2.api import experts as experts_api
from smartai_v2.api.client import APIError
from smartai_v2.config import PROVIDER_TYPES, PROVIDER_DEFAULT_MODELS
from smartai_v2.state.auth import AuthState


class ExpertsState(rx.State):
    providers: list[dict[str, Any]] = []
    error: str = ""
    loading: bool = False

    new_provider_type: str = "openai"
    new_api_key: str = ""
    new_model: str = "gpt-4o-mini"
    new_base_url: str = ""
    new_display_name: str = ""
    new_max_concurrent: str = "5"
    new_rpm: str = "0"

    @rx.var
    def enabled_providers(self) -> list[dict[str, Any]]:
        return [p for p in self.providers if p.get("enabled", True)]

    @rx.var
    def provider_count(self) -> int:
        return len(self.providers)

    @rx.event
    def set_new_provider_type(self, v: str):
        self.new_provider_type = v
        self.new_model = PROVIDER_DEFAULT_MODELS.get(v, "")

    @rx.event
    def set_new_api_key(self, v: str):
        self.new_api_key = v

    @rx.event
    def set_new_model(self, v: str):
        self.new_model = v

    @rx.event
    def set_new_base_url(self, v: str):
        self.new_base_url = v

    @rx.event
    def set_new_display_name(self, v: str):
        self.new_display_name = v

    @rx.event
    def set_new_max_concurrent(self, v: str):
        self.new_max_concurrent = v

    @rx.event
    def set_new_rpm(self, v: str):
        self.new_rpm = v

    @rx.event
    async def load(self):
        try:
            auth = await self.get_state(AuthState)
            data = await experts_api.list_available(token=auth.token or None)
            self.providers = data if isinstance(data, list) else []
        except APIError as e:
            self.error = e.message

    @rx.event
    async def add(self):
        if not self.new_api_key.strip():
            return rx.toast.error("API key cannot be empty")
        try:
            mc = int(self.new_max_concurrent or "5")
            if mc < 1:
                mc = 1
        except ValueError:
            return rx.toast.error("Max concurrent must be an integer ≥ 1")
        try:
            rpm = int(self.new_rpm or "0")
            if rpm < 0:
                rpm = 0
        except ValueError:
            return rx.toast.error("RPM must be a non-negative integer (0 = unlimited)")
        self.loading = True
        try:
            auth = await self.get_state(AuthState)
            await experts_api.add_key(
                self.new_provider_type, self.new_api_key, self.new_model,
                self.new_base_url or None,
                display_name=self.new_display_name.strip() or None,
                max_concurrent=mc,
                rpm=rpm,
                token=auth.token or None,
            )
            self.new_api_key = ""
            self.new_base_url = ""
            self.new_display_name = ""
            self.new_max_concurrent = "5"
            self.new_rpm = "0"
            await self.load()
            self.loading = False
            return rx.toast.success("Provider added")
        except APIError as e:
            self.loading = False
            return rx.toast.error(f"Failed: {e.message}")

    @rx.event
    async def toggle(self, provider_id: str, enabled: bool):
        try:
            auth = await self.get_state(AuthState)
            await experts_api.select(provider_id, enabled, token=auth.token or None)
            await self.load()
        except APIError as e:
            return rx.toast.error(f"Failed: {e.message}")

    @rx.event
    async def remove(self, provider_id: str):
        try:
            auth = await self.get_state(AuthState)
            await experts_api.remove(provider_id, token=auth.token or None)
            await self.load()
            return rx.toast.success("Provider removed")
        except APIError as e:
            return rx.toast.error(f"Failed: {e.message}")
