import pytest

from backend.services.email_sender import EmailDeliveryError, verification_message


def test_production_email_links_require_https(monkeypatch):
    monkeypatch.setattr("backend.services.email_sender.settings.runtime_environment", "production")
    monkeypatch.setattr(
        "backend.services.email_sender.settings.public_frontend_url",
        "http://smartai.example.com",
    )

    with pytest.raises(EmailDeliveryError):
        verification_message("teacher", "token")


def test_development_email_links_allow_loopback_http(monkeypatch):
    monkeypatch.setattr("backend.services.email_sender.settings.runtime_environment", "development")
    monkeypatch.setattr(
        "backend.services.email_sender.settings.public_frontend_url",
        "http://127.0.0.1:3000",
    )

    _, text, _ = verification_message("teacher", "token")

    assert "http://127.0.0.1:3000/register/verify#token=token" in text


def test_production_email_links_accept_https_origin(monkeypatch):
    monkeypatch.setattr("backend.services.email_sender.settings.runtime_environment", "production")
    monkeypatch.setattr(
        "backend.services.email_sender.settings.public_frontend_url",
        "https://smartai.example.com",
    )

    _, text, _ = verification_message("teacher", "token")

    assert "https://smartai.example.com/register/verify#token=token" in text


@pytest.mark.parametrize(
    "configured_url",
    [
        "https://smartai.example.com/app",
        "https://smartai.example.com?next=elsewhere",
        "https://smartai.example.com#fragment",
        "https://user:password@smartai.example.com",
    ],
)
def test_email_links_reject_values_that_are_not_origins(monkeypatch, configured_url: str):
    monkeypatch.setattr("backend.services.email_sender.settings.runtime_environment", "production")
    monkeypatch.setattr("backend.services.email_sender.settings.public_frontend_url", configured_url)

    with pytest.raises(EmailDeliveryError):
        verification_message("teacher", "token")


@pytest.mark.parametrize(
    "configured_url",
    [
        "https://smartai.example.com:not-a-port",
        "https://smartai.example.com:65536",
    ],
)
def test_email_links_reject_invalid_ports(monkeypatch, configured_url: str):
    monkeypatch.setattr("backend.services.email_sender.settings.runtime_environment", "production")
    monkeypatch.setattr("backend.services.email_sender.settings.public_frontend_url", configured_url)

    with pytest.raises(EmailDeliveryError):
        verification_message("teacher", "token")
