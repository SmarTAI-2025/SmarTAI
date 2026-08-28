from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_render_blueprint_declares_email_registration_environment():
    blueprint = yaml.safe_load((REPO_ROOT / "backend" / "render.yaml").read_text(encoding="utf-8"))
    env_vars = {
        entry["key"]: entry
        for entry in blueprint["services"][0]["envVars"]
    }

    required = {
        "SMARTAI_SMTP_HOST",
        "SMARTAI_SMTP_PORT",
        "SMARTAI_SMTP_SECURITY",
        "SMARTAI_SMTP_USERNAME",
        "SMARTAI_SMTP_PASSWORD",
        "SMARTAI_MAIL_FROM_ADDRESS",
        "SMARTAI_ALLOWED_EMAIL_DOMAINS",
        "SMARTAI_PUBLIC_FRONTEND_URL",
    }
    assert required <= env_vars.keys()
    assert env_vars["SMARTAI_SMTP_HOST"]["value"] == "smtp.gmail.com"
    assert str(env_vars["SMARTAI_SMTP_PORT"]["value"]) == "587"
    assert env_vars["SMARTAI_SMTP_SECURITY"]["value"] == "starttls"
    assert env_vars["SMARTAI_ALLOWED_EMAIL_DOMAINS"]["value"] == "ustc.edu.cn"
    for secret_or_deployment_key in (
        "SMARTAI_SMTP_USERNAME",
        "SMARTAI_SMTP_PASSWORD",
        "SMARTAI_MAIL_FROM_ADDRESS",
        "SMARTAI_PUBLIC_FRONTEND_URL",
    ):
        assert env_vars[secret_or_deployment_key].get("sync") is False
