"""Tests for the Garmin token upkeep tasks: proactive refresh and Garmin user id lookup."""

from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from celery.exceptions import Retry
from sqlalchemy.orm import Session

from app.config import settings
from app.constants.celery_tasks import CeleryTask
from app.integrations.celery.core import create_celery
from app.integrations.celery.tasks.garmin.token_task import refresh_garmin_tokens, resolve_garmin_user_id
from app.integrations.redis_client import get_redis_client
from app.models import UserConnection
from app.schemas.auth import ConnectionStatus
from app.services.providers.garmin.oauth import GarminOAuth
from app.services.providers.garmin.token_maintenance import refresh_cutoff
from tests.factories import UserConnectionFactory, UserFactory

MODULE = "app.integrations.celery.tasks.garmin.token_task"
TOKEN_URL = "https://diauth.garmin.com/di-oauth2-service/oauth/token"


@pytest.fixture
def task_db(db: Session) -> Generator[Session, None, None]:
    with patch(f"{MODULE}.SessionLocal", return_value=db), patch.object(db, "close", MagicMock()):
        yield db


def _token_response(access_token: str = "new-access", refresh_token: str = "new-refresh") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": access_token,
            "token_type": "bearer",
            "refresh_token": refresh_token,
            "expires_in": 86400,
            "refresh_token_expires_in": 7775998,
        },
        request=httpx.Request("POST", TOKEN_URL),
    )


def _connection(*, refreshed_days_ago: float, **kwargs: Any) -> UserConnection:
    """A Garmin connection whose tokens were last issued ``refreshed_days_ago`` days ago."""
    issued = datetime.now(timezone.utc) - timedelta(days=refreshed_days_ago)
    return UserConnectionFactory(
        user=UserFactory(),
        provider="garmin",
        refresh_token="old-refresh",
        token_expires_at=issued + timedelta(days=1),
        **kwargs,
    )


class TestRefreshGarminTokens:
    def test_refreshes_tokens_older_than_the_max_age(self, task_db: Session) -> None:
        old = _connection(refreshed_days_ago=61)
        young = _connection(refreshed_days_ago=10)

        with patch("app.services.providers.templates.base_oauth.httpx.post", return_value=_token_response()) as post:
            result = refresh_garmin_tokens.run()

        assert result["tokens"]["due"] == 1
        assert result["tokens"]["refreshed"] == 1
        assert post.call_count == 1
        assert post.call_args.kwargs["data"]["refresh_token"] == "old-refresh"
        task_db.refresh(old)
        task_db.refresh(young)
        assert old.refresh_token == "new-refresh"
        assert old.access_token == "new-access"
        assert old.token_expires_at > datetime.now(timezone.utc) + timedelta(hours=23)
        assert young.refresh_token == "old-refresh"

    def test_running_again_refreshes_nothing(self, task_db: Session) -> None:
        _connection(refreshed_days_ago=70)

        with patch("app.services.providers.templates.base_oauth.httpx.post", return_value=_token_response()) as post:
            refresh_garmin_tokens.run()
            second = refresh_garmin_tokens.run()

        assert post.call_count == 1
        assert second["tokens"]["due"] == 0

    def test_rejected_refresh_token_expires_the_connection(self, task_db: Session) -> None:
        connection = _connection(refreshed_days_ago=80)
        rejected = httpx.Response(400, json={"error": "invalid_grant"}, request=httpx.Request("POST", TOKEN_URL))

        with (
            patch("app.services.providers.templates.base_oauth.httpx.post", return_value=rejected),
            patch("app.services.providers.templates.base_oauth.on_connection_revoked") as revoked,
        ):
            result = refresh_garmin_tokens.run()

        assert result["tokens"]["rejected"] == 1
        task_db.refresh(connection)
        # Expired, not revoked: Garmin keeps pushing for the user, writes need a reconnect.
        assert connection.status == ConnectionStatus.EXPIRED
        assert revoked.call_args.kwargs["reason"] == "refresh_failed"

    def test_transient_failure_is_counted_and_retried_next_run(self, task_db: Session) -> None:
        connection = _connection(refreshed_days_ago=65)

        with patch("app.services.providers.templates.base_oauth.httpx.post", side_effect=httpx.ConnectError("down")):
            result = refresh_garmin_tokens.run()

        assert result["tokens"]["failed"] == 1
        task_db.refresh(connection)
        assert connection.status == ConnectionStatus.ACTIVE
        assert connection.refresh_token == "old-refresh"

    def test_skips_connections_that_are_not_active(self, task_db: Session) -> None:
        _connection(refreshed_days_ago=70, status=ConnectionStatus.EXPIRED)
        _connection(refreshed_days_ago=70, status=ConnectionStatus.REVOKED)

        with patch("app.services.providers.templates.base_oauth.httpx.post") as post:
            result = refresh_garmin_tokens.run()

        assert result["tokens"]["due"] == 0
        post.assert_not_called()

    def test_overlapping_run_is_skipped(self, task_db: Session) -> None:
        get_redis_client().set("garmin:token_maintenance_lock", "someone-else")

        assert refresh_garmin_tokens.run()["status"] == "skipped"

    def test_cutoff_follows_the_setting(self) -> None:
        now = datetime(2026, 10, 27, tzinfo=timezone.utc)
        with patch.object(settings, "garmin_refresh_token_max_age_days", 30):
            # Issued 30 days ago means its 24-hour access token expired 29 days ago.
            assert refresh_cutoff(now) == now - timedelta(days=29)

    def test_fills_in_a_missing_garmin_user_id(self, task_db: Session) -> None:
        connection = UserConnectionFactory(
            user=UserFactory(), provider="garmin", provider_user_id=None, scope="CONNECT_READ"
        )
        permissions = httpx.Response(
            200, json={"permissions": ["ACTIVITY_EXPORT"]}, request=httpx.Request("GET", "https://apis.garmin.com")
        )

        with (
            patch.object(GarminOAuth, "get_user_id", return_value="garmin-user-9"),
            patch("app.services.providers.garmin.write_client.httpx.request", return_value=permissions),
        ):
            result = refresh_garmin_tokens.run()

        assert result["user_ids"] == {"missing": 1, "resolved": 1, "failed": 0}
        task_db.refresh(connection)
        assert connection.provider_user_id == "garmin-user-9"
        assert connection.scope == "ACTIVITY_EXPORT"


class TestResolveGarminUserId:
    def test_stores_the_user_id(self, task_db: Session) -> None:
        connection = UserConnectionFactory(
            user=UserFactory(), provider="garmin", provider_user_id=None, scope="WORKOUT_IMPORT"
        )

        with patch.object(GarminOAuth, "get_user_id", return_value="garmin-user-9"):
            result = resolve_garmin_user_id.run(str(connection.user_id))

        assert result == {"status": "resolved"}
        task_db.refresh(connection)
        assert connection.provider_user_id == "garmin-user-9"

    def test_corrects_an_id_kept_from_another_account(self, task_db: Session) -> None:
        connection = UserConnectionFactory(
            user=UserFactory(), provider="garmin", provider_user_id="previous-account", scope="WORKOUT_IMPORT"
        )

        with patch.object(GarminOAuth, "get_user_id", return_value="current-account"):
            resolve_garmin_user_id.run(str(connection.user_id))

        task_db.refresh(connection)
        assert connection.provider_user_id == "current-account"

    def test_retries_when_garmin_fails(self, task_db: Session) -> None:
        connection = UserConnectionFactory(user=UserFactory(), provider="garmin", provider_user_id=None)

        with (
            patch.object(GarminOAuth, "get_user_id", side_effect=httpx.ConnectError("down")),
            patch.object(resolve_garmin_user_id, "retry", side_effect=Retry()) as retry,
            pytest.raises(Retry),
        ):
            resolve_garmin_user_id.run(str(connection.user_id))

        assert retry.call_args.kwargs["countdown"] == 60

    def test_no_active_connection(self, task_db: Session) -> None:
        connection = UserConnectionFactory(user=UserFactory(), provider="garmin", status=ConnectionStatus.REVOKED)

        assert resolve_garmin_user_id.run(str(connection.user_id))["status"] == "skipped"


def test_refresh_runs_from_beat() -> None:
    schedule = create_celery().conf.beat_schedule
    entry = schedule["refresh-garmin-tokens-hourly"]
    assert entry["task"] == CeleryTask.GARMIN_REFRESH_TOKENS
