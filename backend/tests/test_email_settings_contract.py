from backend.config import Settings


def test_mail_defaults_match_the_frozen_candidate_contract():
    assert Settings.model_fields["email_verification_hourly_email_limit"].default == 5
    assert Settings.model_fields["smtp_timeout_seconds"].default == 10.0
