from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from html import escape
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from backend.config import settings


class EmailSender(Protocol):
    def send(self, to_email: str, subject: str, text_body: str, html_body: str) -> None:
        ...


class EmailDeliveryError(RuntimeError):
    code = "registration_email_delivery_failed"


def _frontend_origin() -> str:
    parts = urlsplit(settings.public_frontend_url.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.query or parts.fragment:
        raise EmailDeliveryError()
    return urlunsplit((parts.scheme, parts.netloc, "", "", "")).rstrip("/")


def verification_message(token: str) -> tuple[str, str, str]:
    link = f"{_frontend_origin()}/register/verify#token={token}"
    subject = "确认 SmarTAI 教师账号"
    text = (
        "你好，\n\n请点击下面的链接确认 SmarTAI 教师账号。链接 30 分钟内有效，"
        "并且需要在页面上再次点击确认：\n"
        f"{link}\n\n如果不是你本人操作，可以忽略此邮件。"
    )
    html = (
        "<p>你好，</p><p>请点击下面的按钮确认 SmarTAI 教师账号。链接 30 分钟内有效，"
        "打开页面后仍需再次点击确认。</p>"
        f'<p><a href="{escape(link, quote=True)}">确认教师账号</a></p>'
        f"<p>纯文本链接：{escape(link)}</p><p>如果不是你本人操作，可以忽略此邮件。</p>"
    )
    return subject, text, html


def password_reset_message(token: str) -> tuple[str, str, str]:
    link = f"{_frontend_origin()}/reset-password#token={token}"
    subject = "SmarTAI 密码重置"
    text = (
        "你好，\n\n你请求了 SmarTAI 密码重置。链接 30 分钟内有效，请打开后设置新密码：\n"
        f"{link}\n\n如果不是你本人操作，可以忽略此邮件，原密码不会改变。"
    )
    html = (
        "<p>你好，</p><p>你请求了 SmarTAI 密码重置。链接 30 分钟内有效，"
        "请点击下面的按钮设置新密码。</p>"
        f'<p><a href="{escape(link, quote=True)}">重置密码</a></p>'
        f"<p>纯文本链接：{escape(link)}</p>"
        "<p>如果不是你本人操作，可以忽略此邮件，原密码不会改变。</p>"
    )
    return subject, text, html


class SMTPEmailSender:
    def send(self, to_email: str, subject: str, text_body: str, html_body: str) -> None:
        if not settings.smtp_host or not settings.smtp_username or not settings.smtp_password:
            raise EmailDeliveryError()
        from_address = settings.mail_from_address.strip() or settings.smtp_username.strip()
        if not from_address:
            raise EmailDeliveryError()
        message = EmailMessage()
        message["From"] = formataddr((settings.mail_from_name.strip() or "SmarTAI", from_address))
        message["To"] = to_email
        message["Subject"] = subject
        message.set_content(text_body)
        message.add_alternative(html_body, subtype="html")
        try:
            if settings.smtp_security == "ssl":
                with smtplib.SMTP_SSL(
                    settings.smtp_host,
                    settings.smtp_port,
                    timeout=settings.smtp_timeout_seconds,
                    context=ssl.create_default_context(),
                ) as client:
                    client.login(settings.smtp_username, settings.smtp_password)
                    client.send_message(message)
            else:
                with smtplib.SMTP(
                    settings.smtp_host,
                    settings.smtp_port,
                    timeout=settings.smtp_timeout_seconds,
                ) as client:
                    client.ehlo()
                    client.starttls(context=ssl.create_default_context())
                    client.ehlo()
                    client.login(settings.smtp_username, settings.smtp_password)
                    client.send_message(message)
        except Exception as exc:
            raise EmailDeliveryError() from exc


def get_email_sender() -> EmailSender:
    return SMTPEmailSender()
