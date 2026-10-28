"""Tests for the Garmin Training/Courses endpoints under /api/v1/users/{user_id}/garmin.

Garmin is faked at ``httpx.request``; every call it receives is recorded so tests can check
what OW sent (path, body, bearer token) and that it made no call when it should not have.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import anyio
import anyio.from_thread
import anyio.to_thread
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.routes.v1 import garmin_training
from app.config import settings
from app.models import UserConnection
from app.schemas.auth import ConnectionStatus
from app.services.idempotency import IdempotencyConflictError, IdempotencyConflictReason, IdempotencyStore
from app.services.providers.garmin.write_client import GarminApiError, GarminErrorCode
from tests.factories import ApiKeyFactory, UserConnectionFactory, UserFactory

HTTPX_REQUEST = "app.services.providers.garmin.write_client.httpx.request"
REFRESH = "app.services.providers.garmin.oauth.GarminOAuth.refresh_access_token"
API = "https://apis.garmin.com"
TOKEN = "garmin-secret-token"
ALL_PERMISSIONS = "ACTIVITY_EXPORT COURSE_IMPORT HEALTH_EXPORT WORKOUT_IMPORT"


@dataclass
class GarminCall:
    method: str
    path: str
    json: Any
    headers: dict[str, str]


@dataclass
class FakeGarmin:
    """Answers (method, path) with queued responses and records every call."""

    responses: dict[tuple[str, str], list[httpx.Response | Exception]] = field(default_factory=dict)
    calls: list[GarminCall] = field(default_factory=list)

    def on(
        self,
        method: str,
        path: str,
        status_code: int = 200,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> "FakeGarmin":
        request = httpx.Request(method, f"{API}{path}")
        if body is None:
            response = httpx.Response(status_code, headers=headers, request=request)
        else:
            response = httpx.Response(status_code, json=body, headers=headers, request=request)
        self.responses.setdefault((method, path), []).append(response)
        return self

    def __call__(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        path = url.removeprefix(API)
        self.calls.append(GarminCall(method, path, kwargs.get("json"), kwargs.get("headers") or {}))
        queued = self.responses.get((method, path))
        if not queued:
            raise AssertionError(f"Unexpected Garmin call: {method} {path}")
        response = queued.pop(0) if len(queued) > 1 else queued[0]
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def garmin() -> Any:
    fake = FakeGarmin()
    with patch(HTTPX_REQUEST, side_effect=fake):
        yield fake


@pytest.fixture
def headers(db: Session) -> dict[str, str]:
    return {"X-Open-Wearables-API-Key": ApiKeyFactory().plain_key}


_LONG_AGO = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _refresh_to(db: Session, connection: UserConnection, new_token: str) -> Callable[..., MagicMock]:
    """A refresh_access_token stand-in that stores ``new_token`` like the real one does."""

    def refresh(*_args: Any, **_kwargs: Any) -> MagicMock:
        connection.access_token = new_token
        db.commit()
        return MagicMock(access_token=new_token)

    return refresh


def _connect(scope: str | None = ALL_PERMISSIONS, **kwargs: Any) -> tuple[UUID, UserConnection]:
    user = UserFactory()
    connection = UserConnectionFactory(
        user=user,
        provider="garmin",
        provider_user_id="garmin-user-1",
        access_token=TOKEN,
        scope=scope,
        **kwargs,
    )
    return user.id, connection


def _workout(**overrides: Any) -> dict[str, Any]:
    return {
        "workoutName": "Tempo Tuesday",
        "sport": "RUNNING",
        "segments": [
            {
                "segmentOrder": 1,
                "sport": "RUNNING",
                "steps": [
                    {
                        "type": "WorkoutStep",
                        "stepOrder": 1,
                        "intensity": "INTERVAL",
                        "durationType": "TIME",
                        "durationValue": 300,
                        "targetType": "PACE",
                        "targetValueLow": 3.58,
                        "targetValueHigh": 3.83,
                    }
                ],
            }
        ],
        **overrides,
    }


def _course(**overrides: Any) -> dict[str, Any]:
    return {
        "courseName": "Riverside loop",
        "distance": 8046.7,
        "elevationGain": 31.0,
        "elevationLoss": 31.0,
        "activityType": "RUNNING",
        "coordinateSystem": "WGS84",
        "geoPoints": [{"latitude": 38.8977, "longitude": -77.0365, "elevation": 17.0}],
        **overrides,
    }


def _assert_error(response: httpx.Response, status_code: int, code: str) -> dict[str, Any]:
    assert response.status_code == status_code, response.text
    body = response.json()
    assert set(body) == {"detail", "code"}
    assert body["code"] == code
    assert isinstance(body["detail"], str)
    assert TOKEN not in response.text
    return body


class TestPermissions:
    def test_reads_live_permissions_and_stores_them(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, connection = _connect(scope="ACTIVITY_EXPORT")
        garmin.on(
            "GET",
            "/partner-gateway/rest/user/permissions",
            body={"permissions": ["WORKOUT_IMPORT", "ACTIVITY_EXPORT", "COURSE_IMPORT"]},
        )

        response = client.get(f"/api/v1/users/{user_id}/garmin/permissions", headers=headers)

        assert response.status_code == 200
        assert response.json() == {
            "connected": True,
            "status": "active",
            "garmin_user_id": "garmin-user-1",
            "permissions": ["ACTIVITY_EXPORT", "COURSE_IMPORT", "WORKOUT_IMPORT"],
        }
        assert garmin.calls[0].headers["Authorization"] == f"Bearer {TOKEN}"
        db.refresh(connection)
        assert connection.scope == "ACTIVITY_EXPORT COURSE_IMPORT WORKOUT_IMPORT"

    def test_falls_back_to_stored_permissions(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, connection = _connect(scope="CONNECT_READ HEALTH_EXPORT WORKOUT_IMPORT")
        garmin.on("GET", "/partner-gateway/rest/user/permissions", status_code=503)

        response = client.get(f"/api/v1/users/{user_id}/garmin/permissions", headers=headers)

        assert response.status_code == 200
        # The generic OAuth scope the token response carries is not a permission.
        assert response.json()["permissions"] == ["HEALTH_EXPORT", "WORKOUT_IMPORT"]
        db.refresh(connection)
        assert connection.scope == "CONNECT_READ HEALTH_EXPORT WORKOUT_IMPORT"

    def test_fallback_with_nothing_stored_is_empty(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect(scope=None)
        garmin.on("GET", "/partner-gateway/rest/user/permissions", status_code=503)

        response = client.get(f"/api/v1/users/{user_id}/garmin/permissions", headers=headers)

        assert response.json() == {
            "connected": True,
            "status": "active",
            "garmin_user_id": "garmin-user-1",
            "permissions": [],
        }

    def test_falls_back_when_garmin_is_unreachable(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect(scope="WORKOUT_IMPORT")
        garmin.responses[("GET", "/partner-gateway/rest/user/permissions")] = [httpx.ConnectError("down")]

        response = client.get(f"/api/v1/users/{user_id}/garmin/permissions", headers=headers)

        assert response.status_code == 200
        assert response.json()["permissions"] == ["WORKOUT_IMPORT"]

    def test_rate_limit_is_passed_on_not_hidden(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect(scope="WORKOUT_IMPORT")
        garmin.on("GET", "/partner-gateway/rest/user/permissions", status_code=429, headers={"Retry-After": "60"})

        response = client.get(f"/api/v1/users/{user_id}/garmin/permissions", headers=headers)

        _assert_error(response, 429, "garmin_rate_limited")
        assert response.headers["Retry-After"] == "60"

    def test_dead_refresh_token_answers_not_connected(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, connection = _connect(refresh_token="dead", token_expires_at=_LONG_AGO)
        rejected = httpx.Response(400, json={"error": "invalid_grant"}, request=httpx.Request("POST", "https://x"))

        with patch("app.services.providers.templates.base_oauth.httpx.post", return_value=rejected):
            response = client.get(f"/api/v1/users/{user_id}/garmin/permissions", headers=headers)

        assert response.status_code == 200
        assert response.json() == {
            "connected": False,
            "status": "expired",
            "garmin_user_id": "garmin-user-1",
            "permissions": [],
        }
        assert garmin.calls == []
        db.refresh(connection)
        assert connection.status == ConnectionStatus.EXPIRED

    def test_empty_permission_list_is_stored_as_empty(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, connection = _connect(scope="WORKOUT_IMPORT")
        garmin.on("GET", "/partner-gateway/rest/user/permissions", body={"permissions": []})

        response = client.get(f"/api/v1/users/{user_id}/garmin/permissions", headers=headers)

        assert response.json()["permissions"] == []
        db.refresh(connection)
        assert connection.scope == ""

    def test_user_without_connection(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        response = client.get(f"/api/v1/users/{uuid4()}/garmin/permissions", headers=headers)

        assert response.status_code == 200
        assert response.json() == {"connected": False, "status": None, "garmin_user_id": None, "permissions": []}
        assert garmin.calls == []

    def test_revoked_connection_is_not_connected(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect(status=ConnectionStatus.REVOKED)

        response = client.get(f"/api/v1/users/{user_id}/garmin/permissions", headers=headers)

        assert response.json()["connected"] is False

    def test_requires_api_key(self, client: TestClient, db: Session) -> None:
        response = client.get(f"/api/v1/users/{uuid4()}/garmin/permissions")
        _assert_error(response, 401, "unauthorized")


class TestCreateWorkout:
    def test_creates_workout_with_provider_attribution(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()
        garmin_workout = {**_workout(), "workoutId": 98765, "ownerId": 4242, "workoutProvider": "JustGains"}
        garmin.on("POST", "/workoutportal/workout/v2", body=garmin_workout)

        response = client.post(f"/api/v1/users/{user_id}/garmin/workouts", headers=headers, json=_workout())

        assert response.status_code == 201
        assert response.json() == {"workout_id": 98765, "workout": garmin_workout}
        [call] = garmin.calls
        assert call.headers["Authorization"] == f"Bearer {TOKEN}"
        assert call.json["workoutProvider"] == "JustGains"
        assert call.json["workoutSourceId"] == "JustGains"
        assert call.json["segments"] == _workout()["segments"]

    def test_keeps_callers_provider(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()
        garmin.on("POST", "/workoutportal/workout/v2", body={"workoutId": 1})

        client.post(
            f"/api/v1/users/{user_id}/garmin/workouts",
            headers=headers,
            json=_workout(workoutProvider="Coach Kim"),
        )

        assert garmin.calls[0].json["workoutProvider"] == "Coach Kim"
        assert garmin.calls[0].json["workoutSourceId"] == "Coach Kim"

    def test_not_connected(self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin) -> None:
        response = client.post(f"/api/v1/users/{uuid4()}/garmin/workouts", headers=headers, json=_workout())

        _assert_error(response, 404, "garmin_not_connected")
        assert garmin.calls == []

    def test_invalid_workout_never_reaches_garmin(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()
        workout = _workout()
        del workout["sport"]

        response = client.post(f"/api/v1/users/{user_id}/garmin/workouts", headers=headers, json=workout)

        body = _assert_error(response, 422, "garmin_validation_error")
        assert "sport" in body["detail"]
        assert garmin.calls == []

    def test_too_many_steps(self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin) -> None:
        user_id, _ = _connect()
        step = _workout()["segments"][0]["steps"][0]
        workout = _workout(segments=[{"segmentOrder": 1, "sport": "RUNNING", "steps": [step] * 101}])

        response = client.post(f"/api/v1/users/{user_id}/garmin/workouts", headers=headers, json=workout)

        _assert_error(response, 422, "garmin_validation_error")
        assert garmin.calls == []

    def test_body_must_be_an_object(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()

        response = client.post(f"/api/v1/users/{user_id}/garmin/workouts", headers=headers, json=[_workout()])

        _assert_error(response, 422, "garmin_validation_error")

    def test_permission_missing_is_confirmed_live(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect(scope="ACTIVITY_EXPORT HEALTH_EXPORT")
        garmin.on("GET", "/partner-gateway/rest/user/permissions", body={"permissions": ["ACTIVITY_EXPORT"]})

        response = client.post(f"/api/v1/users/{user_id}/garmin/workouts", headers=headers, json=_workout())

        body = _assert_error(response, 403, "garmin_permission_missing")
        assert "WORKOUT_IMPORT" in body["detail"]
        assert [c.path for c in garmin.calls] == ["/partner-gateway/rest/user/permissions"]

    def test_stale_stored_permissions_are_refreshed(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, connection = _connect(scope="ACTIVITY_EXPORT")
        garmin.on(
            "GET", "/partner-gateway/rest/user/permissions", body={"permissions": ["ACTIVITY_EXPORT", "WORKOUT_IMPORT"]}
        )
        garmin.on("POST", "/workoutportal/workout/v2", body={"workoutId": 5})

        response = client.post(f"/api/v1/users/{user_id}/garmin/workouts", headers=headers, json=_workout())

        assert response.status_code == 201
        db.refresh(connection)
        assert connection.scope == "ACTIVITY_EXPORT WORKOUT_IMPORT"

    def test_unknown_stored_permissions_go_straight_to_garmin(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect(scope="PARTNER_WRITE PARTNER_READ CONNECT_READ CONNECT_WRITE")
        garmin.on("POST", "/workoutportal/workout/v2", body={"workoutId": 5})

        response = client.post(f"/api/v1/users/{user_id}/garmin/workouts", headers=headers, json=_workout())

        assert response.status_code == 201
        assert [c.path for c in garmin.calls] == ["/workoutportal/workout/v2"]

    def test_garmin_412_is_permission_missing(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect(scope=None)
        garmin.on("POST", "/workoutportal/workout/v2", status_code=412, body={"message": "User permission error"})

        response = client.post(f"/api/v1/users/{user_id}/garmin/workouts", headers=headers, json=_workout())

        _assert_error(response, 403, "garmin_permission_missing")

    def test_rate_limited_passes_retry_after(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()
        garmin.on("POST", "/workoutportal/workout/v2", status_code=429, headers={"Retry-After": "60"})

        response = client.post(f"/api/v1/users/{user_id}/garmin/workouts", headers=headers, json=_workout())

        _assert_error(response, 429, "garmin_rate_limited")
        assert response.headers["Retry-After"] == "60"
        assert len(garmin.calls) == 1

    def test_rate_limited_without_retry_after(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()
        garmin.on("POST", "/workoutportal/workout/v2", status_code=429)

        response = client.post(f"/api/v1/users/{user_id}/garmin/workouts", headers=headers, json=_workout())

        _assert_error(response, 429, "garmin_rate_limited")
        assert "Retry-After" not in response.headers

    def test_garmin_rejection_carries_its_message(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()
        garmin.on("POST", "/workoutportal/workout/v2", status_code=400, body={"message": "Invalid targetType"})

        response = client.post(f"/api/v1/users/{user_id}/garmin/workouts", headers=headers, json=_workout())

        body = _assert_error(response, 422, "garmin_validation_error")
        assert "Invalid targetType" in body["detail"]

    def test_garmin_server_error(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()
        garmin.on("POST", "/workoutportal/workout/v2", status_code=500, body={"message": "boom"})

        response = client.post(f"/api/v1/users/{user_id}/garmin/workouts", headers=headers, json=_workout())

        _assert_error(response, 502, "garmin_upstream_error")

    def test_garmin_unreachable(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()
        garmin.responses[("POST", "/workoutportal/workout/v2")] = [httpx.ReadTimeout("slow")]

        response = client.post(f"/api/v1/users/{user_id}/garmin/workouts", headers=headers, json=_workout())

        _assert_error(response, 502, "garmin_upstream_error")

    def test_rejected_token_is_refreshed_and_retried(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, connection = _connect()
        garmin.on("POST", "/workoutportal/workout/v2", status_code=401)
        garmin.on("POST", "/workoutportal/workout/v2", body={"workoutId": 9})

        with patch(REFRESH, side_effect=_refresh_to(db, connection, "fresh")):
            response = client.post(f"/api/v1/users/{user_id}/garmin/workouts", headers=headers, json=_workout())

        assert response.status_code == 201
        assert [c.headers["Authorization"] for c in garmin.calls] == [f"Bearer {TOKEN}", "Bearer fresh"]

    def test_token_rejected_after_refresh_is_not_connected(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, connection = _connect()
        garmin.on("POST", "/workoutportal/workout/v2", status_code=401)

        with patch(REFRESH, side_effect=_refresh_to(db, connection, "fresh")):
            response = client.post(f"/api/v1/users/{user_id}/garmin/workouts", headers=headers, json=_workout())

        _assert_error(response, 404, "garmin_not_connected")
        assert len(garmin.calls) == 2
        db.refresh(connection)
        assert connection.status == ConnectionStatus.EXPIRED

    def test_garmin_403_is_forbidden(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()
        garmin.on("POST", "/workoutportal/workout/v2", status_code=403, body={"message": "Not allowed"})

        response = client.post(f"/api/v1/users/{user_id}/garmin/workouts", headers=headers, json=_workout())

        body = _assert_error(response, 403, "garmin_forbidden")
        assert "Not allowed" in body["detail"]

    def test_permission_check_under_rate_limit_is_429_not_403(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        # Stored scope says no, so OW confirms live; Garmin rate-limits that check.
        user_id, _ = _connect(scope="ACTIVITY_EXPORT")
        garmin.on("GET", "/partner-gateway/rest/user/permissions", status_code=429, headers={"Retry-After": "60"})

        response = client.post(f"/api/v1/users/{user_id}/garmin/workouts", headers=headers, json=_workout())

        _assert_error(response, 429, "garmin_rate_limited")
        assert response.headers["Retry-After"] == "60"

    def test_created_without_an_id_returns_what_garmin_sent(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()
        garmin.on("POST", "/workoutportal/workout/v2", body={"workoutName": "Tempo Tuesday", "sport": "RUNNING"})

        response = client.post(f"/api/v1/users/{user_id}/garmin/workouts", headers=headers, json=_workout())

        # A 502 here would invite a retry that creates the workout twice.
        assert response.status_code == 201
        assert response.json() == {
            "workout_id": None,
            "workout": {"workoutName": "Tempo Tuesday", "sport": "RUNNING"},
        }

    def test_single_sport_with_two_segments_never_reaches_garmin(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()
        segment = _workout()["segments"][0]
        workout = _workout(segments=[segment, {**segment, "segmentOrder": 2}])

        response = client.post(f"/api/v1/users/{user_id}/garmin/workouts", headers=headers, json=workout)

        _assert_error(response, 422, "garmin_validation_error")
        assert garmin.calls == []


class TestUpdateAndDeleteWorkout:
    def test_update_with_owner_id(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()
        garmin.on("PUT", "/training-api/workout/v2/98765", status_code=204)

        response = client.put(
            f"/api/v1/users/{user_id}/garmin/workouts/98765",
            headers=headers,
            json=_workout(ownerId=4242, workoutName="Tempo Tuesday v2", workoutProvider="JustGains"),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["workout_id"] == 98765
        assert body["workout"]["workoutName"] == "Tempo Tuesday v2"
        [call] = garmin.calls
        assert call.json["workoutId"] == 98765
        assert call.json["ownerId"] == 4242
        assert call.json["workoutProvider"] == "JustGains"

    def test_update_keeps_the_existing_provider_when_omitted(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()
        current = {**_workout(), "workoutId": 98765, "ownerId": 4242, "workoutProvider": "Coach Kim"}
        current["workoutSourceId"] = "kim-plan-7"
        garmin.on("GET", "/training-api/workout/v2/98765", body=current)
        garmin.on("PUT", "/training-api/workout/v2/98765", status_code=204)

        response = client.put(
            f"/api/v1/users/{user_id}/garmin/workouts/98765", headers=headers, json=_workout(ownerId=4242)
        )

        assert response.status_code == 200
        put = garmin.calls[-1]
        assert put.json["workoutProvider"] == "Coach Kim"
        assert put.json["workoutSourceId"] == "kim-plan-7"

    def test_update_of_missing_workout_is_not_found(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()
        garmin.on("PUT", "/training-api/workout/v2/98765", status_code=404)

        response = client.put(
            f"/api/v1/users/{user_id}/garmin/workouts/98765",
            headers=headers,
            json=_workout(ownerId=4242, workoutProvider="JustGains"),
        )

        _assert_error(response, 404, "garmin_not_found")

    def test_owner_lookup_of_missing_workout_is_not_found(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()
        garmin.on("GET", "/training-api/workout/v2/98765", status_code=404)

        response = client.put(f"/api/v1/users/{user_id}/garmin/workouts/98765", headers=headers, json=_workout())

        _assert_error(response, 404, "garmin_not_found")
        assert [c.method for c in garmin.calls] == ["GET"]

    def test_update_looks_up_owner_id(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()
        garmin.on("GET", "/training-api/workout/v2/98765", body={**_workout(), "workoutId": 98765, "ownerId": 4242})
        garmin.on("PUT", "/training-api/workout/v2/98765", body={**_workout(), "workoutId": 98765, "ownerId": 4242})

        response = client.put(f"/api/v1/users/{user_id}/garmin/workouts/98765", headers=headers, json=_workout())

        assert response.status_code == 200
        assert [c.method for c in garmin.calls] == ["GET", "PUT"]
        assert garmin.calls[1].json["ownerId"] == 4242
        assert response.json()["workout"]["ownerId"] == 4242

    def test_update_validates(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()

        response = client.put(
            f"/api/v1/users/{user_id}/garmin/workouts/98765", headers=headers, json=_workout(sport=None)
        )

        _assert_error(response, 422, "garmin_validation_error")
        assert garmin.calls == []

    def test_update_non_numeric_id(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()

        response = client.put(f"/api/v1/users/{user_id}/garmin/workouts/abc", headers=headers, json=_workout())

        body = _assert_error(response, 422, "garmin_validation_error")
        assert "workout_id" in body["detail"]

    def test_delete(self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin) -> None:
        user_id, _ = _connect()
        garmin.on("DELETE", "/training-api/workout/v2/98765", status_code=204)

        response = client.delete(f"/api/v1/users/{user_id}/garmin/workouts/98765", headers=headers)

        assert response.status_code == 204
        assert response.content == b""

    def test_delete_of_missing_workout_succeeds(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()
        garmin.on("DELETE", "/training-api/workout/v2/98765", status_code=404)

        response = client.delete(f"/api/v1/users/{user_id}/garmin/workouts/98765", headers=headers)

        assert response.status_code == 204

    def test_delete_without_permission(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect(scope="ACTIVITY_EXPORT")
        garmin.on("GET", "/partner-gateway/rest/user/permissions", body={"permissions": ["ACTIVITY_EXPORT"]})

        response = client.delete(f"/api/v1/users/{user_id}/garmin/workouts/98765", headers=headers)

        _assert_error(response, 403, "garmin_permission_missing")


class TestSchedules:
    def test_create(self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin) -> None:
        user_id, _ = _connect()
        garmin.on("POST", "/training-api/schedule/", body={"scheduleId": 555, "workoutId": 98765, "date": "2026-11-03"})

        response = client.post(
            f"/api/v1/users/{user_id}/garmin/schedules",
            headers=headers,
            json={"workout_id": 98765, "date": "2026-11-03"},
        )

        assert response.status_code == 201
        assert response.json() == {"schedule_id": 555}
        assert garmin.calls[0].json == {"workoutId": 98765, "date": "2026-11-03"}

    def test_create_accepts_bare_id_response(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()
        garmin.on("POST", "/training-api/schedule/", body=556)

        response = client.post(
            f"/api/v1/users/{user_id}/garmin/schedules",
            headers=headers,
            json={"workout_id": 98765, "date": "2026-11-03"},
        )

        assert response.json() == {"schedule_id": 556}

    @pytest.mark.parametrize(
        "payload",
        [{"workout_id": 98765, "date": "11/03/2026"}, {"date": "2026-11-03"}, {"workout_id": 0, "date": "2026-11-03"}],
    )
    def test_create_rejects_bad_body(
        self,
        client: TestClient,
        db: Session,
        headers: dict[str, str],
        garmin: FakeGarmin,
        payload: dict[str, Any],
    ) -> None:
        user_id, _ = _connect()

        response = client.post(f"/api/v1/users/{user_id}/garmin/schedules", headers=headers, json=payload)

        _assert_error(response, 422, "garmin_validation_error")
        assert garmin.calls == []

    def test_create_unknown_workout(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()
        garmin.on("POST", "/training-api/schedule/", status_code=400, body={"message": "Workout not found"})

        response = client.post(
            f"/api/v1/users/{user_id}/garmin/schedules",
            headers=headers,
            json={"workout_id": 1, "date": "2026-11-03"},
        )

        body = _assert_error(response, 422, "garmin_validation_error")
        assert "Workout not found" in body["detail"]

    def test_delete(self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin) -> None:
        user_id, _ = _connect()
        garmin.on("DELETE", "/training-api/schedule/555", status_code=204)

        response = client.delete(f"/api/v1/users/{user_id}/garmin/schedules/555", headers=headers)

        assert response.status_code == 204

    def test_not_connected(self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin) -> None:
        response = client.delete(f"/api/v1/users/{uuid4()}/garmin/schedules/555", headers=headers)
        _assert_error(response, 404, "garmin_not_connected")


class TestCourses:
    def test_create(self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin) -> None:
        user_id, _ = _connect()
        garmin.on("POST", "/training-api/courses/v1/course", body={**_course(), "courseId": 41887203})

        response = client.post(f"/api/v1/users/{user_id}/garmin/courses", headers=headers, json=_course())

        assert response.status_code == 201
        assert response.json()["course_id"] == 41887203
        assert response.json()["course"]["courseName"] == "Riverside loop"
        assert garmin.calls[0].json == _course()

    def test_needs_course_import(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect(scope="ACTIVITY_EXPORT WORKOUT_IMPORT")
        garmin.on(
            "GET",
            "/partner-gateway/rest/user/permissions",
            body={"permissions": ["ACTIVITY_EXPORT", "WORKOUT_IMPORT"]},
        )

        response = client.post(f"/api/v1/users/{user_id}/garmin/courses", headers=headers, json=_course())

        body = _assert_error(response, 403, "garmin_permission_missing")
        assert "COURSE_IMPORT" in body["detail"]

    def test_validates(self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin) -> None:
        user_id, _ = _connect()

        response = client.post(f"/api/v1/users/{user_id}/garmin/courses", headers=headers, json=_course(geoPoints=[]))

        _assert_error(response, 422, "garmin_validation_error")
        assert garmin.calls == []

    def test_rejects_boolean_distance(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()

        response = client.post(f"/api/v1/users/{user_id}/garmin/courses", headers=headers, json=_course(distance=True))

        body = _assert_error(response, 422, "garmin_validation_error")
        assert "distance" in body["detail"]
        assert garmin.calls == []

    def test_delete(self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin) -> None:
        user_id, _ = _connect()
        garmin.on("DELETE", "/training-api/courses/v1/course/41887203", status_code=204)

        response = client.delete(f"/api/v1/users/{user_id}/garmin/courses/41887203", headers=headers)

        assert response.status_code == 204

    def test_delete_rate_limited(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()
        garmin.on("DELETE", "/training-api/courses/v1/course/1", status_code=429, headers={"Retry-After": "30"})

        response = client.delete(f"/api/v1/users/{user_id}/garmin/courses/1", headers=headers)

        _assert_error(response, 429, "garmin_rate_limited")
        assert response.headers["Retry-After"] == "30"


class TestAuthErrors:
    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("GET", "permissions", None),
            ("POST", "workouts", {"sport": "RUNNING"}),
            ("PUT", "workouts/1", {"sport": "RUNNING"}),
            ("DELETE", "workouts/1", None),
            ("POST", "schedules", {"workout_id": 1, "date": "2026-11-01"}),
            ("DELETE", "schedules/1", None),
            ("POST", "courses", {}),
            ("DELETE", "courses/1", None),
        ],
    )
    def test_missing_api_key_uses_the_error_shape(
        self, client: TestClient, db: Session, garmin: FakeGarmin, method: str, path: str, body: Any
    ) -> None:
        response = client.request(method, f"/api/v1/users/{uuid4()}/garmin/{path}", json=body)

        _assert_error(response, 401, "unauthorized")
        assert garmin.calls == []

    def test_invalid_api_key(self, client: TestClient, db: Session, garmin: FakeGarmin) -> None:
        response = client.post(
            f"/api/v1/users/{uuid4()}/garmin/workouts",
            headers={"X-Open-Wearables-API-Key": "sk-not-a-real-key"},
            json=_workout(),
        )

        _assert_error(response, 401, "unauthorized")


class TestIdempotencyKey:
    def test_retry_with_the_same_key_returns_the_first_result(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()
        garmin.on("POST", "/workoutportal/workout/v2", body={"workoutId": 111})
        garmin.on("POST", "/workoutportal/workout/v2", body={"workoutId": 222})
        keyed = {**headers, "Idempotency-Key": "plan-42-week-1-tue"}

        first = client.post(f"/api/v1/users/{user_id}/garmin/workouts", headers=keyed, json=_workout())
        retry = client.post(f"/api/v1/users/{user_id}/garmin/workouts", headers=keyed, json=_workout())

        assert first.status_code == retry.status_code == 201
        assert retry.json() == first.json()
        assert retry.json()["workout_id"] == 111
        assert retry.headers["Idempotent-Replayed"] == "true"
        assert len(garmin.calls) == 1

    def test_without_a_key_every_request_creates(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()
        garmin.on("POST", "/workoutportal/workout/v2", body={"workoutId": 111})
        garmin.on("POST", "/workoutportal/workout/v2", body={"workoutId": 222})

        for _ in range(2):
            client.post(f"/api/v1/users/{user_id}/garmin/workouts", headers=headers, json=_workout())

        assert len(garmin.calls) == 2

    def test_same_key_with_a_different_body_is_a_conflict(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()
        garmin.on("POST", "/workoutportal/workout/v2", body={"workoutId": 111})
        keyed = {**headers, "Idempotency-Key": "k-1"}

        client.post(f"/api/v1/users/{user_id}/garmin/workouts", headers=keyed, json=_workout())
        response = client.post(
            f"/api/v1/users/{user_id}/garmin/workouts", headers=keyed, json=_workout(workoutName="Other")
        )

        body = _assert_error(response, 409, "idempotency_conflict")
        assert "different request" in body["detail"]
        assert len(garmin.calls) == 1

    def test_a_failed_create_can_be_retried_with_the_same_key(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()
        garmin.on("POST", "/workoutportal/workout/v2", status_code=503)
        garmin.on("POST", "/workoutportal/workout/v2", body={"workoutId": 333})
        keyed = {**headers, "Idempotency-Key": "k-2"}

        failed = client.post(f"/api/v1/users/{user_id}/garmin/workouts", headers=keyed, json=_workout())
        retried = client.post(f"/api/v1/users/{user_id}/garmin/workouts", headers=keyed, json=_workout())

        _assert_error(failed, 502, "garmin_upstream_error")
        assert retried.status_code == 201
        assert retried.json()["workout_id"] == 333

    def test_key_still_running_is_a_conflict(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()
        running = IdempotencyConflictError(IdempotencyConflictReason.IN_PROGRESS)

        with patch.object(IdempotencyStore, "begin", side_effect=running):
            response = client.post(
                f"/api/v1/users/{user_id}/garmin/schedules",
                headers={**headers, "Idempotency-Key": "k-3"},
                json={"workout_id": 98765, "date": "2026-11-03"},
            )

        body = _assert_error(response, 409, "idempotency_conflict")
        assert "still running" in body["detail"]
        assert garmin.calls == []

    def test_keys_are_per_route(
        self, client: TestClient, db: Session, headers: dict[str, str], garmin: FakeGarmin
    ) -> None:
        user_id, _ = _connect()
        garmin.on("POST", "/training-api/schedule/", body={"scheduleId": 555})
        garmin.on("POST", "/training-api/courses/v1/course", body={**_course(), "courseId": 41887203})
        keyed = {**headers, "Idempotency-Key": "shared"}

        schedule = client.post(
            f"/api/v1/users/{user_id}/garmin/schedules", headers=keyed, json={"workout_id": 1, "date": "2026-11-03"}
        )
        course = client.post(f"/api/v1/users/{user_id}/garmin/courses", headers=keyed, json=_course())

        assert schedule.json() == {"schedule_id": 555}
        assert course.json()["course_id"] == 41887203

    def test_key_is_documented_in_openapi(self, client: TestClient) -> None:
        spec = client.get("/openapi.json").json()
        for path in ("workouts", "schedules", "courses"):
            parameters = spec["paths"][f"/api/v1/users/{{user_id}}/garmin/{path}"]["post"]["parameters"]
            assert any(p["name"] == "Idempotency-Key" and p["in"] == "header" for p in parameters)


class TestWriteConcurrency:
    async def test_busy_when_no_slot_frees_up(self) -> None:
        release = anyio.Event()

        def slow() -> str:
            anyio.from_thread.run(release.wait)
            return "done"

        with (
            patch.object(settings, "garmin_write_concurrency", 1),
            patch.object(garmin_training, "ADMISSION_WAIT_SECONDS", 0.05),
        ):
            async with anyio.create_task_group() as tg:
                tg.start_soon(garmin_training.run_garmin_write, slow)
                await anyio.sleep(0.05)
                try:
                    with pytest.raises(GarminApiError) as exc:
                        await garmin_training.run_garmin_write(lambda: "second")
                finally:
                    release.set()

        assert exc.value.code == GarminErrorCode.BUSY
        assert exc.value.status_code == 503
        assert exc.value.retry_after

    async def test_runs_off_the_shared_thread_pool(self) -> None:
        default_limiter = anyio.to_thread.current_default_thread_limiter()
        before = default_limiter.borrowed_tokens

        def work() -> int:
            return default_limiter.borrowed_tokens

        borrowed_during = await garmin_training.run_garmin_write(work)

        # The shared pool (webhook receivers, other sync routes) lends nothing to Garmin work.
        assert borrowed_during == before
