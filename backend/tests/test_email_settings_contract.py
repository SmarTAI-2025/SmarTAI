from backend.config import Settings


def test_default_registration_email_hourly_limit_is_ten():
    assert Settings.model_fields["email_verification_hourly_email_limit"].default == 10
