"""Experts (BYOK) configuration — /experts"""
from __future__ import annotations

import reflex as rx

from smartai_v2.components.auth_guard import require_auth
from smartai_v2.components.cards import info_card, empty_state
from smartai_v2.components.forms import section_header, labeled_input, labeled_select
from smartai_v2.components.layout import with_layout
from smartai_v2.config import PROVIDER_TYPES
from smartai_v2.state.experts import ExpertsState
from smartai_v2.theme import COLOR, RADIUS


def _provider_card(p: dict) -> rx.Component:
    return rx.card(
        rx.hstack(
            rx.box(
                rx.icon("brain-circuit", size=20, color=COLOR["primary"]),
                background=COLOR["primary_subtle"],
                padding="10px",
                border_radius=RADIUS["sm"],
            ),
            rx.vstack(
                rx.heading(p["display_name"].to_string(), size="3"),
                rx.text(
                    p["provider_id"].to_string()
                    + " · max_concurrent=" + p["max_concurrent"].to_string(),
                    size="2",
                    color=COLOR["text_muted"],
                ),
                spacing="0",
                align="start",
            ),
            rx.spacer(),
            rx.switch(
                checked=p["enabled"].to(bool),
                on_change=lambda v: ExpertsState.toggle(p["provider_id"].to_string(), v),
            ),
            rx.icon_button(
                rx.icon("trash-2", size=14),
                color_scheme="red",
                variant="ghost",
                on_click=lambda: ExpertsState.remove(p["provider_id"].to_string()),
            ),
            align="center",
            spacing="3",
            width="100%",
        ),
        size="2",
        width="100%",
    )


def _add_form() -> rx.Component:
    return info_card(
        labeled_select("Provider type", PROVIDER_TYPES, ExpertsState.new_provider_type,
                       ExpertsState.set_new_provider_type),
        labeled_input("API key", ExpertsState.new_api_key, ExpertsState.set_new_api_key,
                      placeholder="sk-...", type="password", required=True),
        labeled_input("Model", ExpertsState.new_model, ExpertsState.set_new_model,
                      placeholder="gpt-4o-mini"),
        labeled_input("Base URL (optional)", ExpertsState.new_base_url,
                      ExpertsState.set_new_base_url, placeholder="https://..."),
        labeled_input(
            "Display name (optional)", ExpertsState.new_display_name,
            ExpertsState.set_new_display_name,
            placeholder="e.g. GLM Air, Gemini Flash — defaults to provider:model",
        ),
        labeled_input(
            "Max concurrent", ExpertsState.new_max_concurrent,
            ExpertsState.set_new_max_concurrent,
            placeholder="GLM Air: 5  ·  Gemini/OpenAI: 10",
        ),
        rx.button(
            rx.cond(ExpertsState.loading, rx.spinner(), rx.text("Add provider")),
            on_click=ExpertsState.add,
            disabled=ExpertsState.loading,
            size="3",
        ),
        title="Add a new provider",
    )


@rx.page(route="/experts", title="BYOK Experts | SmarTAI", on_load=ExpertsState.load)
def experts_page() -> rx.Component:
    return require_auth(
        with_layout(
            "BYOK Experts",
            section_header(
                "Multi-expert LLM configuration",
                "Bring your own API keys for OpenAI, Gemini, Anthropic, Zhipu. Multi-expert grading runs all enabled providers in parallel.",
            ),
            _add_form(),
            rx.heading("Configured providers", size="4"),
            rx.cond(
                ExpertsState.providers.length() > 0,
                rx.vstack(
                    rx.foreach(ExpertsState.providers, _provider_card),
                    spacing="2",
                    width="100%",
                ),
                empty_state("inbox", "No providers yet", "Add one above to enable AI grading."),
            ),
        ),
        require_role="teacher",
    )
