"""Webhook tokens carried in a URL path are masked before anything logs the path."""

import pytest

from app.utils.url_secrets import REDACTED, redact_path_secrets


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/v1/providers/garmin/webhooks/t/9f8e7d6c", f"/api/v1/providers/garmin/webhooks/t/{REDACTED}"),
        ("/api/v1/garmin/webhooks/push/t/9f8e7d6c", f"/api/v1/garmin/webhooks/push/t/{REDACTED}"),
        ("/api/v1/garmin/webhooks/ping/t/9f8e7d6c", f"/api/v1/garmin/webhooks/ping/t/{REDACTED}"),
        (
            "https://wearables.example.com/api/v1/providers/garmin/webhooks/t/9f8e7d6c?x=1",
            f"https://wearables.example.com/api/v1/providers/garmin/webhooks/t/{REDACTED}?x=1",
        ),
        # Inside a repr: the closing quote survives.
        ("b'/api/v1/providers/garmin/webhooks/t/9f8e7d6c'", f"b'/api/v1/providers/garmin/webhooks/t/{REDACTED}'"),
    ],
)
def test_masks_the_token_segment(path: str, expected: str) -> None:
    assert redact_path_secrets(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/providers/garmin/webhooks",
        "/api/v1/providers/garmin/webhooks/subscriptions/sub-1",
        "/api/v1/users/4f6c9c2e/garmin/workouts/98765",
        "/api/v1/garmin/webhooks/push",
    ],
)
def test_leaves_other_paths_alone(path: str) -> None:
    assert redact_path_secrets(path) == path


def test_custom_replacement() -> None:
    assert redact_path_secrets("/webhooks/t/abc", "[Filtered]") == "/webhooks/t/[Filtered]"
