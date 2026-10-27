"""Tests for the Garmin write client and the workout/course checks done before calling Garmin."""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.auth import ConnectionStatus
from app.services.providers.garmin.oauth import GarminOAuth
from app.services.providers.garmin.training import (
    count_workout_steps,
    validate_course,
    validate_workout,
    with_provider_attribution,
)
from app.services.providers.garmin.write_client import (
    GarminApiError,
    GarminErrorCode,
    GarminWriteClient,
    stored_permissions,
)
from tests.factories import UserConnectionFactory, UserFactory

HTTPX_REQUEST = "app.services.providers.garmin.write_client.httpx.request"
API = "https://apis.garmin.com"
EXPIRED = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _step(order: int) -> dict[str, Any]:
    return {"type": "WorkoutStep", "stepOrder": order, "intensity": "ACTIVE", "durationType": "TIME"}


def _repeat(order: int, children: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "WorkoutRepeatStep", "stepOrder": order, "repeatValue": 4, "steps": children}


def _workout(sport: str = "RUNNING", segments: list[list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    segments = segments if segments is not None else [[_step(1)]]
    return {
        "workoutName": "Test",
        "sport": sport,
        "segments": [{"segmentOrder": i + 1, "sport": sport, "steps": steps} for i, steps in enumerate(segments)],
    }


def _response(status_code: int, body: Any = None, headers: dict[str, str] | None = None) -> httpx.Response:
    request = httpx.Request("GET", f"{API}/x")
    if body is None:
        return httpx.Response(status_code, headers=headers, request=request)
    if isinstance(body, str):
        return httpx.Response(status_code, text=body, headers=headers, request=request)
    return httpx.Response(status_code, json=body, headers=headers, request=request)


class TestWorkoutValidation:
    def test_counts_repeat_blocks_and_their_children(self) -> None:
        steps = [_step(1), _repeat(2, [_step(3), _step(4)]), _step(5)]
        assert count_workout_steps(steps) == 5

    def test_accepts_a_normal_workout(self) -> None:
        validate_workout(_workout(segments=[[_step(1), _repeat(2, [_step(3), _step(4)])]]))

    @pytest.mark.parametrize("sport", [None, "", "   ", 7])
    def test_rejects_missing_sport(self, sport: Any) -> None:
        workout = _workout()
        workout["sport"] = sport
        with pytest.raises(GarminApiError) as exc:
            validate_workout(workout)
        assert exc.value.code == GarminErrorCode.VALIDATION_ERROR
        assert exc.value.status_code == 422
        assert "sport" in exc.value.detail

    def test_single_sport_allows_100_steps(self) -> None:
        validate_workout(_workout(segments=[[_step(i) for i in range(100)]]))

    def test_single_sport_rejects_101_steps_counting_repeats(self) -> None:
        # 98 plain steps + a repeat block holding 2 steps = 101 steps
        steps = [_step(i) for i in range(98)] + [_repeat(99, [_step(100), _step(101)])]
        with pytest.raises(GarminApiError) as exc:
            validate_workout(_workout(segments=[steps]))
        assert "at most 100 steps" in exc.value.detail
        assert "101" in exc.value.detail

    def test_multi_sport_allows_250_steps_over_segments(self) -> None:
        validate_workout(_workout("MULTI_SPORT", segments=[[_step(i) for i in range(125)]] * 2))

    def test_multi_sport_rejects_251_steps(self) -> None:
        with pytest.raises(GarminApiError, match="at most 250 steps"):
            validate_workout(_workout("MULTI_SPORT", segments=[[_step(i) for i in range(125)], [_step(1)] * 126]))

    def test_multi_sport_rejects_26_segments(self) -> None:
        with pytest.raises(GarminApiError, match="at most 25 segments"):
            validate_workout(_workout("MULTI_SPORT", segments=[[_step(1)]] * 26))

    def test_rejects_segments_that_are_not_a_list(self) -> None:
        workout = _workout()
        workout["segments"] = {"steps": []}
        with pytest.raises(GarminApiError, match="segments must be a list"):
            validate_workout(workout)

    @pytest.mark.parametrize("field", ["workoutProvider", "workoutSourceId"])
    def test_rejects_provider_fields_over_20_characters(self, field: str) -> None:
        workout = {**_workout(), field: "x" * 21}
        with pytest.raises(GarminApiError, match=field):
            validate_workout(workout)


class TestProviderAttribution:
    def test_fills_provider_and_source_id_when_missing(self) -> None:
        result = with_provider_attribution(_workout())
        assert result["workoutProvider"] == settings.garmin_workout_provider == "JustGains"
        assert result["workoutSourceId"] == "JustGains"

    def test_keeps_callers_values(self) -> None:
        result = with_provider_attribution({**_workout(), "workoutProvider": "Coach Kim", "workoutSourceId": "kim-1"})
        assert result["workoutProvider"] == "Coach Kim"
        assert result["workoutSourceId"] == "kim-1"

    def test_source_id_follows_callers_provider(self) -> None:
        result = with_provider_attribution({**_workout(), "workoutProvider": "Coach Kim"})
        assert result["workoutSourceId"] == "Coach Kim"

    def test_does_not_mutate_the_input(self) -> None:
        workout = _workout()
        with_provider_attribution(workout)
        assert "workoutProvider" not in workout


class TestCourseValidation:
    @pytest.fixture
    def course(self) -> dict[str, Any]:
        return {
            "courseName": "Loop",
            "distance": 1609.3,
            "elevationGain": 10.0,
            "elevationLoss": 10,
            "geoPoints": [{"latitude": 1.0, "longitude": 2.0}],
        }

    def test_accepts_a_complete_course(self, course: dict[str, Any]) -> None:
        validate_course(course)

    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("courseName", "", "courseName"),
            ("geoPoints", [], "geoPoint"),
            ("distance", None, "distance"),
            ("elevationGain", None, "elevationGain"),
            ("elevationLoss", "10", "elevationLoss"),
        ],
    )
    def test_rejects_missing_required_fields(
        self, course: dict[str, Any], field: str, value: Any, message: str
    ) -> None:
        course[field] = value
        with pytest.raises(GarminApiError, match=message) as exc:
            validate_course(course)
        assert exc.value.code == GarminErrorCode.VALIDATION_ERROR


class TestStoredPermissions:
    def test_none_when_scope_missing(self) -> None:
        assert stored_permissions(None) is None
        assert stored_permissions("") is None

    def test_oauth_scopes_alone_are_unknown(self) -> None:
        assert stored_permissions("PARTNER_WRITE PARTNER_READ CONNECT_READ CONNECT_WRITE") is None

    def test_drops_oauth_scopes(self) -> None:
        assert stored_permissions("CONNECT_READ WORKOUT_IMPORT ACTIVITY_EXPORT") == [
            "ACTIVITY_EXPORT",
            "WORKOUT_IMPORT",
        ]


class TestGarminWriteClient:
    @pytest.fixture
    def client(self) -> GarminWriteClient:
        connection_repo = UserConnectionRepository()
        oauth = GarminOAuth(
            user_repo=MagicMock(),
            connection_repo=connection_repo,
            provider_name="garmin",
            api_base_url=API,
        )
        return GarminWriteClient(oauth=oauth, connection_repo=connection_repo, api_base_url=API)

    @pytest.fixture
    def user_id(self, db: Session) -> Any:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin", access_token="garmin-secret-token")
        return user.id

    def test_sends_bearer_token_and_json(self, client: GarminWriteClient, db: Session, user_id: Any) -> None:
        with patch(HTTPX_REQUEST, return_value=_response(200, {"workoutId": 1})) as request:
            result = client.request(db, user_id, "POST", "/workoutportal/workout/v2", json_body={"sport": "RUNNING"})

        assert result.body == {"workoutId": 1}
        method, url = request.call_args.args
        assert (method, url) == ("POST", f"{API}/workoutportal/workout/v2")
        assert request.call_args.kwargs["headers"]["Authorization"] == "Bearer garmin-secret-token"
        assert request.call_args.kwargs["json"] == {"sport": "RUNNING"}

    def test_empty_success_body_is_none(self, client: GarminWriteClient, db: Session, user_id: Any) -> None:
        with patch(HTTPX_REQUEST, return_value=_response(204)):
            assert client.request(db, user_id, "DELETE", "/training-api/workout/v2/1").body is None

    @pytest.mark.parametrize(
        ("status_code", "code", "http_status"),
        [
            (401, GarminErrorCode.NOT_CONNECTED, 404),
            (412, GarminErrorCode.PERMISSION_MISSING, 403),
            (429, GarminErrorCode.RATE_LIMITED, 429),
            (400, GarminErrorCode.VALIDATION_ERROR, 422),
            (403, GarminErrorCode.VALIDATION_ERROR, 422),
            (404, GarminErrorCode.VALIDATION_ERROR, 422),
            (500, GarminErrorCode.UPSTREAM_ERROR, 502),
            (503, GarminErrorCode.UPSTREAM_ERROR, 502),
        ],
    )
    def test_maps_garmin_errors(
        self,
        client: GarminWriteClient,
        db: Session,
        user_id: Any,
        status_code: int,
        code: GarminErrorCode,
        http_status: int,
    ) -> None:
        with (
            patch(HTTPX_REQUEST, return_value=_response(status_code, {"message": "nope"})),
            pytest.raises(GarminApiError) as exc,
        ):
            client.request(db, user_id, "POST", "/workoutportal/workout/v2", json_body={})

        assert exc.value.code == code
        assert exc.value.status_code == http_status
        assert exc.value.garmin_status == status_code

    def test_validation_error_carries_garmins_message(
        self, client: GarminWriteClient, db: Session, user_id: Any
    ) -> None:
        body = {"errorMessage": "Invalid exerciseName BARBELL_SQUATZ"}
        with patch(HTTPX_REQUEST, return_value=_response(400, body)), pytest.raises(GarminApiError) as exc:
            client.request(db, user_id, "POST", "/workoutportal/workout/v2", json_body={})
        assert "Invalid exerciseName BARBELL_SQUATZ" in exc.value.detail
        assert "HTTP 400" in exc.value.detail

    def test_plain_text_error_body_is_passed_on(self, client: GarminWriteClient, db: Session, user_id: Any) -> None:
        with patch(HTTPX_REQUEST, return_value=_response(400, "bad pool length")), pytest.raises(GarminApiError) as exc:
            client.request(db, user_id, "POST", "/workoutportal/workout/v2", json_body={})
        assert "bad pool length" in exc.value.detail

    def test_rate_limit_keeps_retry_after(self, client: GarminWriteClient, db: Session, user_id: Any) -> None:
        response = _response(429, {"message": "quota"}, headers={"Retry-After": "120"})
        with patch(HTTPX_REQUEST, return_value=response), pytest.raises(GarminApiError) as exc:
            client.request(db, user_id, "POST", "/workoutportal/workout/v2", json_body={})
        assert exc.value.retry_after == "120"

    def test_rate_limit_is_not_retried(self, client: GarminWriteClient, db: Session, user_id: Any) -> None:
        with patch(HTTPX_REQUEST, return_value=_response(429)) as request, pytest.raises(GarminApiError):
            client.request(db, user_id, "POST", "/workoutportal/workout/v2", json_body={})
        assert request.call_count == 1

    def test_transport_error_is_upstream_error(self, client: GarminWriteClient, db: Session, user_id: Any) -> None:
        with (
            patch(HTTPX_REQUEST, side_effect=httpx.ConnectTimeout("timed out")),
            pytest.raises(GarminApiError) as exc,
        ):
            client.request(db, user_id, "GET", "/partner-gateway/rest/user/permissions")
        assert exc.value.code == GarminErrorCode.UPSTREAM_ERROR
        assert "ConnectTimeout" in exc.value.detail

    def test_rejected_refresh_token_means_not_connected(self, client: GarminWriteClient, db: Session) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin", token_expires_at=EXPIRED)

        with (
            patch.object(
                GarminOAuth,
                "refresh_access_token",
                side_effect=HTTPException(status_code=401, detail="Refresh token rejected"),
            ),
            patch(HTTPX_REQUEST) as request,
            pytest.raises(GarminApiError) as exc,
        ):
            client.request(db, user.id, "POST", "/workoutportal/workout/v2", json_body={})

        assert exc.value.code == GarminErrorCode.NOT_CONNECTED
        request.assert_not_called()

    def test_token_endpoint_failure_is_upstream_error(self, client: GarminWriteClient, db: Session) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin", token_expires_at=EXPIRED)

        with (
            patch.object(
                GarminOAuth,
                "refresh_access_token",
                side_effect=HTTPException(status_code=400, detail="Failed to refresh token: 503"),
            ),
            pytest.raises(GarminApiError) as exc,
        ):
            client.request(db, user.id, "POST", "/workoutportal/workout/v2", json_body={})

        assert exc.value.code == GarminErrorCode.UPSTREAM_ERROR

    def test_errors_and_logs_never_contain_the_token(
        self, client: GarminWriteClient, db: Session, user_id: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch(HTTPX_REQUEST, return_value=_response(400, {"message": "bad"})),
            pytest.raises(GarminApiError) as exc,
        ):
            client.request(db, user_id, "POST", "/workoutportal/workout/v2", json_body={})

        logged = capsys.readouterr().out
        assert "Garmin write request failed" in logged
        assert "garmin-secret-token" not in logged
        assert "garmin-secret-token" not in exc.value.detail

    def test_get_active_connection_rejects_revoked(self, client: GarminWriteClient, db: Session) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin", status=ConnectionStatus.REVOKED)
        with pytest.raises(GarminApiError) as exc:
            client.get_active_connection(db, user.id)
        assert exc.value.code == GarminErrorCode.NOT_CONNECTED

    def test_get_active_connection_missing(self, client: GarminWriteClient, db: Session) -> None:
        with pytest.raises(GarminApiError) as exc:
            client.get_active_connection(db, uuid4())
        assert exc.value.status_code == 404

    def test_fetch_permissions(self, client: GarminWriteClient, db: Session, user_id: Any) -> None:
        body = {"permissions": ["WORKOUT_IMPORT", "ACTIVITY_EXPORT", "WORKOUT_IMPORT"]}
        with patch(HTTPX_REQUEST, return_value=_response(200, body)):
            assert client.fetch_permissions(db, user_id) == ["ACTIVITY_EXPORT", "WORKOUT_IMPORT"]

    def test_fetch_permissions_unexpected_shape(self, client: GarminWriteClient, db: Session, user_id: Any) -> None:
        with (
            patch(HTTPX_REQUEST, return_value=_response(200, ["WORKOUT_IMPORT"])),
            pytest.raises(GarminApiError) as exc,
        ):
            client.fetch_permissions(db, user_id)
        assert exc.value.code == GarminErrorCode.UPSTREAM_ERROR
