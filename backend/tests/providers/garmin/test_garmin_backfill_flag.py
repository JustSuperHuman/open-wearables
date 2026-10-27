"""GARMIN_BACKFILL_ENABLED=false (Starter tier): no Garmin backfill is ever triggered.

Covers every entry point: the OAuth callback, /sync/historical, the backfill Celery tasks
and the webhook-driven chaining. A fresh connection must come out ACTIVE with no backfill
state, sync run or Garmin backfill call behind it.
"""

from collections.abc import Generator
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.integrations.celery.tasks.garmin.backfill_task import start_full_backfill, trigger_next_pending_type
from app.integrations.celery.tasks.garmin.backfill_trigger import trigger_backfill_for_type
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.auth import ConnectionStatus
from app.schemas.model_crud.credentials import OAuthState
from app.services.providers.garmin.backfill_state import get_backfill_status
from app.services.providers.garmin.oauth import GarminOAuth
from app.services.providers.garmin.strategy import GarminStrategy
from app.utils.exceptions import UnsupportedProviderError
from tests.factories import ApiKeyFactory, UserConnectionFactory, UserFactory

TASK_MODULE = "app.integrations.celery.tasks.garmin.backfill_task"
TRIGGER_MODULE = "app.integrations.celery.tasks.garmin.backfill_trigger"
HANDLER_MODULE = "app.services.providers.garmin.webhook_handler"


@pytest.fixture
def backfill_disabled() -> Generator[None, None, None]:
    with patch.object(settings, "garmin_backfill_enabled", False):
        yield


class TestOAuthCallback:
    def _connect(self, client: TestClient, user_id: UUID) -> int:
        def handle_callback(oauth: GarminOAuth, db: Session, code: str, state: str) -> OAuthState:
            """Stand in for the token exchange: store an ACTIVE connection like a real callback does."""
            UserConnectionFactory(
                user=UserFactory(id=user_id),
                provider="garmin",
                provider_user_id="garmin-user-1",
                scope="ACTIVITY_EXPORT HEALTH_EXPORT WORKOUT_IMPORT COURSE_IMPORT",
            )
            return OAuthState(user_id=user_id, provider="garmin")

        with patch.object(GarminOAuth, "handle_callback", autospec=True, side_effect=handle_callback):
            response = client.get(
                "/api/v1/oauth/garmin/callback",
                params={"code": "auth-code", "state": "state"},
                follow_redirects=False,
            )
        return response.status_code

    @pytest.mark.usefixtures("backfill_disabled")
    def test_fresh_starter_connection_starts_no_backfill(self, client: TestClient, db: Session) -> None:
        user_id = uuid4()
        with (
            patch("app.api.routes.v1.oauth.start_garmin_full_backfill") as backfill,
            patch("app.api.routes.v1.oauth.sync_vendor_data") as pull_sync,
        ):
            status_code = self._connect(client, user_id)

        assert status_code == 303
        backfill.delay.assert_not_called()
        pull_sync.delay.assert_not_called()

        connection = UserConnectionRepository().get_by_user_and_provider(db, user_id, "garmin")
        assert connection is not None
        assert connection.status == ConnectionStatus.ACTIVE
        assert get_backfill_status(user_id)["overall_status"] not in ("in_progress", "retry_in_progress")

    def test_enabled_backfill_still_starts_on_connect(self, client: TestClient, db: Session) -> None:
        with patch("app.api.routes.v1.oauth.start_garmin_full_backfill") as backfill:
            self._connect(client, uuid4())

        backfill.delay.assert_called_once()


class TestStrategy:
    @pytest.mark.usefixtures("backfill_disabled")
    def test_capabilities_drop_webhook_callback(self) -> None:
        caps = GarminStrategy().capabilities
        assert caps.webhook_callback is False
        assert caps.webhook_stream is True

    def test_capabilities_default_keep_webhook_callback(self) -> None:
        assert GarminStrategy().capabilities.webhook_callback is True

    @pytest.mark.usefixtures("backfill_disabled")
    def test_historical_sync_is_unsupported(self) -> None:
        with (
            patch("app.services.providers.garmin.strategy.start_garmin_full_backfill") as backfill,
            pytest.raises(UnsupportedProviderError, match="GARMIN_BACKFILL_ENABLED"),
        ):
            GarminStrategy().start_historical_sync(uuid4(), days=30)
        backfill.delay.assert_not_called()

    @pytest.mark.usefixtures("backfill_disabled")
    def test_historical_sync_endpoint_returns_400(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin")
        headers = {"X-Open-Wearables-API-Key": ApiKeyFactory().plain_key}

        response = client.post(f"/api/v1/providers/garmin/users/{user.id}/sync/historical", headers=headers)

        assert response.status_code == 400
        assert "GARMIN_BACKFILL_ENABLED" in response.json()["detail"]

    @pytest.mark.usefixtures("backfill_disabled")
    def test_connections_report_no_history_delivery(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin")
        headers = {"X-Open-Wearables-API-Key": ApiKeyFactory().plain_key}

        response = client.get(f"/api/v1/users/{user.id}/connections", headers=headers)

        [garmin] = [c for c in response.json() if c["provider"] == "garmin"]
        assert garmin["webhook_callback"] is False


@pytest.mark.usefixtures("backfill_disabled")
class TestBackfillTasks:
    def test_start_full_backfill_skips_before_touching_state(self) -> None:
        with (
            patch(f"{TASK_MODULE}.acquire_backfill_lock") as lock,
            patch(f"{TASK_MODULE}.emit_sync_started") as sync_started,
            patch(f"{TASK_MODULE}.trigger_backfill_for_type") as trigger,
            patch(f"{TASK_MODULE}.SessionLocal") as session,
        ):
            result = start_full_backfill(str(uuid4()))

        assert result["status"] == "skipped"
        assert "GARMIN_BACKFILL_ENABLED" in result["reason"]
        lock.assert_not_called()
        sync_started.assert_not_called()
        trigger.apply_async.assert_not_called()
        session.assert_not_called()

    def test_trigger_for_type_makes_no_garmin_call(self) -> None:
        with patch(f"{TRIGGER_MODULE}.GarminBackfillService") as service:
            result = trigger_backfill_for_type(str(uuid4()), "sleeps")

        assert result["status"] == "skipped"
        service.assert_not_called()

    def test_trigger_next_pending_type_stops_the_chain(self) -> None:
        with patch(f"{TASK_MODULE}.trigger_backfill_for_type") as trigger:
            result = trigger_next_pending_type(str(uuid4()))

        assert result["status"] == "skipped"
        trigger.apply_async.assert_not_called()


@pytest.mark.usefixtures("backfill_disabled")
def test_webhook_does_not_chain_backfills(db: Session) -> None:
    """Even with leftover in-progress state in Redis, live pushes don't trigger backfill calls."""
    user = UserFactory()
    UserConnectionFactory(user=user, provider="garmin", provider_user_id="garmin-user-1")
    payload = {
        "dailies": [
            {
                "userId": "garmin-user-1",
                "summaryId": "daily-1",
                "calendarDate": "2026-10-26",
                "steps": 7694,
                "startTimeInSeconds": 1793001600,
                "startTimeOffsetInSeconds": -18000,
            }
        ]
    }

    with (
        patch(f"{HANDLER_MODULE}.mark_type_success", return_value=True),
        patch(
            f"{HANDLER_MODULE}.get_backfill_status",
            return_value={"overall_status": "in_progress", "current_window": 0, "total_windows": 1},
        ),
        patch(f"{HANDLER_MODULE}.celery_app") as celery,
    ):
        handler = GarminStrategy().webhooks
        assert handler is not None
        result = handler.process_payload(db, payload, "trace-1")

    assert result["backfill_chained"] == []
    celery.send_task.assert_not_called()
    assert result["wellness"]["dailies"]["processed"] == 1
