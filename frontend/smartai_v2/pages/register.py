"""Register page — /register

Registration is currently invite-only. This page is kept as a facade so
the link from /login still works, but submitting will surface a "registration
closed, please contact admin" message rather than creating an account.
"""
from __future__ import annotations

import reflex as rx

from smartai_v2.components.layout import public_layout
from smartai_v2.components.forms import labeled_input
from smartai_v2.state.auth import AuthState
from smartai_v2.theme import COLOR


@rx.page(route="/register", title="Register | SmarTAI")
def register_page() -> rx.Component:
    return public_layout(
        rx.card(
            rx.vstack(
                rx.center(rx.icon("user-plus", size=48, color=COLOR["primary"]), width="100%"),
                rx.heading("申请注册", size="6", align="center"),
                rx.text(
                    "目前注册尚未开放，仅限受邀测试用户使用预发账号登录。",
                    size="2", color=COLOR["text_muted"], align="center",
                ),
                rx.divider(),
                labeled_input("Username", AuthState.username_input, AuthState.set_username_input,
                              placeholder="username", required=True),
                labeled_input("Email", AuthState.email_input, AuthState.set_email_input,
                              placeholder="you@example.com", type="email", required=True),
                labeled_input("Password", AuthState.password_input, AuthState.set_password_input,
                              placeholder="••••••••", type="password", required=True),
                labeled_input("Invite code (邀请码)", AuthState.invite_code_input,
                              AuthState.set_invite_code_input, placeholder="（暂未开放）"),
                rx.cond(
                    AuthState.error != "",
                    rx.callout(AuthState.error, icon="triangle-alert", color_scheme="orange", size="1"),
                    rx.fragment(),
                ),
                rx.button(
                    rx.cond(AuthState.loading, rx.spinner(), rx.text("Create account")),
                    on_click=AuthState.submit_register,
                    width="100%",
                    size="3",
                    disabled=AuthState.loading,
                ),
                rx.hstack(
                    rx.text("Already have an account?", size="2", color=COLOR["text_muted"]),
                    rx.link("Sign in", href="/login", color=COLOR["primary"], size="2"),
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
