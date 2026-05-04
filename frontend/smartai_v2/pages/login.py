"""Login page — /login"""
from __future__ import annotations

import reflex as rx

from smartai_v2.components.layout import public_layout
from smartai_v2.components.forms import labeled_input
from smartai_v2.state.auth import AuthState
from smartai_v2.theme import COLOR, SPACE


@rx.page(route="/login", title="Login | SmarTAI")
def login_page() -> rx.Component:
    return public_layout(
        rx.card(
            rx.vstack(
                rx.center(
                    rx.icon("graduation-cap", size=48, color=COLOR["primary"]),
                    width="100%",
                ),
                rx.heading("Welcome to SmarTAI", size="6", align="center"),
                rx.text("Sign in to your account", size="2", color=COLOR["text_muted"], align="center"),
                rx.divider(),
                labeled_input(
                    "Username",
                    AuthState.username_input,
                    AuthState.set_username_input,
                    placeholder="username",
                    required=True,
                ),
                labeled_input(
                    "Password",
                    AuthState.password_input,
                    AuthState.set_password_input,
                    placeholder="••••••••",
                    type="password",
                    required=True,
                ),
                rx.cond(
                    AuthState.error != "",
                    rx.callout(
                        AuthState.error,
                        icon="triangle-alert",
                        color_scheme="red",
                        size="1",
                    ),
                    rx.fragment(),
                ),
                rx.button(
                    rx.cond(AuthState.loading, rx.spinner(), rx.text("Sign in")),
                    on_click=AuthState.submit_login,
                    width="100%",
                    size="3",
                    disabled=AuthState.loading,
                ),
                rx.divider(),
                rx.text(
                    "测试账号请向管理员获取邀请。",
                    size="1",
                    color=COLOR["text_muted"],
                    align="center",
                ),
                rx.hstack(
                    rx.text("还没有账号？", size="2", color=COLOR["text_muted"]),
                    rx.link("申请注册", href="/register", color=COLOR["primary"], size="2"),
                    spacing="1",
                    justify="center",
                    width="100%",
                ),
                spacing="3",
                width="100%",
            ),
            size="3",
            width="100%",
        ),
    )
