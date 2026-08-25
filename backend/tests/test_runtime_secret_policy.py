import pytest

from backend.config import Settings, validate_runtime_secret_policy


PRIVATE_PROVIDER_KEY = "provider-private-test-key-0123456789abcdef"
PRIVATE_JWT_SECRET = "jwt-private-test-secret-0123456789abcdef"


def _production_settings(**overrides: str | bool) -> Settings:
    values: dict[str, str | bool] = {
        "runtime_environment": "production",
        "provider_encryption_key": PRIVATE_PROVIDER_KEY,
        "jwt_secret": PRIVATE_JWT_SECRET,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_development_can_start_without_provider_encryption_key():
    config = Settings(
        _env_file=None,
        runtime_environment="development",
        provider_encryption_key="",
    )

    validate_runtime_secret_policy(config)
    assert config.provider_encryption_key == ""


@pytest.mark.parametrize("runtime_environment", ["development", "test"])
@pytest.mark.parametrize(
    ("provider_key", "jwt_secret"),
    [
        ("", PRIVATE_JWT_SECRET),
        ("   ", PRIVATE_JWT_SECRET),
        ("short", PRIVATE_JWT_SECRET),
        ("smartai-dev-provider-key-change-in-prod", PRIVATE_JWT_SECRET),
        ("smartai-dev-secret-change-in-prod", PRIVATE_JWT_SECRET),
        ("replace-with-a-long-random-secret", PRIVATE_JWT_SECRET),
        (PRIVATE_JWT_SECRET, PRIVATE_JWT_SECRET),
    ],
)
def test_nonproduction_normalizes_unsafe_provider_key_to_unconfigured(
    runtime_environment,
    provider_key,
    jwt_secret,
):
    config = Settings(
        _env_file=None,
        runtime_environment=runtime_environment,
        provider_encryption_key=provider_key,
        jwt_secret=jwt_secret,
    )

    validate_runtime_secret_policy(config)

    assert config.provider_encryption_key == ""


def test_nonproduction_keeps_distinct_private_provider_key():
    config = Settings(
        _env_file=None,
        runtime_environment="development",
        provider_encryption_key=PRIVATE_PROVIDER_KEY,
        jwt_secret=PRIVATE_JWT_SECRET,
    )

    validate_runtime_secret_policy(config)

    assert config.provider_encryption_key == PRIVATE_PROVIDER_KEY


@pytest.mark.parametrize(
    ("overrides", "expected_message"),
    [
        (
            {"provider_encryption_key": ""},
            "SMARTAI_PROVIDER_ENCRYPTION_KEY",
        ),
        (
            {"provider_encryption_key": "short"},
            "SMARTAI_PROVIDER_ENCRYPTION_KEY",
        ),
        (
            {"provider_encryption_key": "smartai-dev-provider-key-change-in-prod"},
            "SMARTAI_PROVIDER_ENCRYPTION_KEY",
        ),
        (
            {"jwt_secret": ""},
            "SMARTAI_JWT_SECRET must contain at least 32 private random bytes",
        ),
        (
            {"jwt_secret": "smartai-dev-secret-change-in-prod"},
            "SMARTAI_JWT_SECRET must contain at least 32 private random bytes",
        ),
        (
            {"jwt_secret": "short"},
            "SMARTAI_JWT_SECRET must contain at least 32 private random bytes",
        ),
        (
            {"jwt_secret": PRIVATE_PROVIDER_KEY},
            "must differ from SMARTAI_JWT_SECRET",
        ),
    ],
)
def test_production_rejects_unsafe_secret_configuration(overrides, expected_message):
    config = _production_settings(**overrides)

    with pytest.raises(RuntimeError, match=expected_message):
        validate_runtime_secret_policy(config)


def test_production_accepts_distinct_private_secrets_with_test_user_seeding():
    config = _production_settings(seed_test_users=True)

    validate_runtime_secret_policy(config)


def test_validation_error_never_echoes_secret_values():
    exposed_value = "same-private-secret-value-0123456789abcdef"
    config = _production_settings(
        provider_encryption_key=exposed_value,
        jwt_secret=exposed_value,
    )

    with pytest.raises(RuntimeError) as exc_info:
        validate_runtime_secret_policy(config)

    assert exposed_value not in str(exc_info.value)


def test_legacy_jwt_secret_is_not_used_in_production(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", PRIVATE_JWT_SECRET)
    monkeypatch.delenv("SMARTAI_JWT_SECRET", raising=False)
    config = Settings(
        _env_file=None,
        runtime_environment="production",
        provider_encryption_key=PRIVATE_PROVIDER_KEY,
    )

    with pytest.raises(RuntimeError, match="SMARTAI_JWT_SECRET"):
        validate_runtime_secret_policy(config)
