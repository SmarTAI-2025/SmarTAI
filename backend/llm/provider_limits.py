"""Recognize explicit daily limits without guessing from an undifferentiated 429."""
import re


def is_daily_quota_error(value) -> bool:
    text = str(value).lower()
    # Provider metric names (Gemini), natural-language errors and relay codes.
    daily = re.search(r"per[ _-]?day|daily|requestsperday|tokensperday|\brpd\b|\btpd\b|每日|每天|日配额|日限额", text)
    exhausted = re.search(r"quota|limit|exceed|exhaust|reached|resource.?exhausted|限额|配额|超限|耗尽", text)
    return bool(daily and exhausted)
