"""Authentication state — JWT token persisted in browser localStorage."""
from __future__ import annotations

import json
from typing import Any, Optional

import reflex as rx

from smartai_v2.api import auth as auth_api
from smartai_v2.api.client import APIError
from smartai_v2.config import ROLE_TEACHER


class AuthState(rx.State):
    token: str = rx.LocalStorage("", name="smartai_token")
    user_json: str = rx.LocalStorage("{}", name="smartai_user")

    error: str = ""
    loading: bool = False

    username_input: str = ""
    password_input: str = ""
    email_input: str = ""
    role_input: str = ROLE_TEACHER
    invite_code_input: str = ""

    @rx.var
    def is_authenticated(self) -> bool:
        return bool(self.token)

    @rx.var
    def user(self) -> dict[str, Any]:
        try:
            return json.loads(self.user_json) if self.user_json else {}
        except json.JSONDecodeError:
            return {}

    @rx.var
    def username(self) -> str:
        return self.user.get("username", "")

    @rx.var
    def role(self) -> str:
        return self.user.get("role", "")

    @rx.var
    def is_teacher(self) -> bool:
        return self.role in ("teacher", "admin")

    @rx.var
    def is_student(self) -> bool:
        return self.role == "student"

    @rx.event
    def set_username_input(self, v: str):
        self.username_input = v

    @rx.event
    def set_password_input(self, v: str):
        self.password_input = v

    @rx.event
    def set_email_input(self, v: str):
        self.email_input = v

    @rx.event
    def set_role_input(self, v: str):
        self.role_input = v

    @rx.event
    def set_invite_code_input(self, v: str):
        self.invite_code_input = v

    @rx.event
    def clear_error(self):
        self.error = ""

    @rx.event
    async def submit_login(self):
        self.loading = True
        self.error = ""
        try:
            data = await auth_api.login(self.username_input, self.password_input)
            self.token = data.get("token", "")
            self.user_json = json.dumps(data.get("user", {}))
            self.password_input = ""
            self.loading = False
            yield rx.redirect("/")
        except APIError as e:
            self.loading = False
            self.error = e.message or "登录失败"
        except Exception as e:
            self.loading = False
            self.error = f"登录失败：{e}"

    def _demo_login(self, role: str, username: str):
        """Set token in `demo-<role>-<username>` format which backend's auth
        module decodes as a synthetic User (see backend/auth/__init__.py).

        Kept private — student demo / one-click demo buttons were removed
        when the product became teacher-only with invite-code login.
        """
        import json as _json
        self.token = f"demo-{role}-{username}"
        self.user_json = _json.dumps({
            "id": f"demo_{username}",
            "username": username,
            "role": role,
            "email": f"{username}@demo.local",
        })
        self.password_input = ""
        self.error = ""
        return rx.redirect("/")

    @rx.event
    async def submit_register(self):
        """Registration is currently closed — surface the backend's "closed"
        message inline instead of completing the flow. The page UI is kept
        as a facade so the link from /login still works.
        """
        self.loading = True
        self.error = ""
        try:
            await auth_api.register(
                self.username_input, self.password_input, self.email_input,
                ROLE_TEACHER, self.invite_code_input or None,
            )
            self.loading = False
            self.error = "注册暂未开放。如需测试请联系管理员获取受邀账号。"
        except APIError as e:
            self.loading = False
            self.error = e.message or "注册暂未开放，请联系管理员。"
        except Exception:
            self.loading = False
            self.error = "注册暂未开放，请联系管理员。"

    @rx.event
    def logout(self):
        self.token = ""
        self.user_json = "{}"
        self.username_input = ""
        self.password_input = ""
        return rx.redirect("/login")
