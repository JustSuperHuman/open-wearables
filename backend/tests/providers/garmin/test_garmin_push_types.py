"""Garmin push types added for the Starter app: manuallyUpdatedActivities, nutrition, hydration,
breathingVariations (unmapped), plus device attribution on what Garmin names a device for.

Payloads go through GarminWebhookHandler.process_payload, the code the Celery task runs.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.constants.workout_types.garmin import get_unified_workout_type
from app.models import DataPointSeries, DataSource, EventRecord, EventRecordDetail, UserConnection
from app.repositories.event_record_detail_repository import EventRecordDetailRepository
from app.repositories.event_record_repository import EventRecordRepository
from app.schemas.auth import ConnectionStatus
from app.schemas.enums import EntrySource, EventCategory, SeriesType, get_series_type_id
from app.services.providers.garmin.strategy import GarminStrategy
from app.services.providers.garmin.webhook_handler import GarminWebhookHandler
from app.services.providers.garmin.write_client import stored_permissions
from tests.factories import ApiKeyFactory, UserConnectionFactory, UserFactory

GARMIN_USER = "garmin-user-1"
ACTIVITY_ID = 21047282990


@pytest.fixture
def handler() -> GarminWebhookHandler:
    webhooks = GarminStrategy().webhooks
    assert isinstance(webhooks, GarminWebhookHandler)
    return webhooks


@pytest.fixture
def user_id(db: Session) -> UUID:
    user = UserFactory()
    UserConnectionFactory(user=user, provider="garmin", provider_user_id=GARMIN_USER)
    return user.id


def _activity(**overrides: Any) -> dict[str, Any]:
    return {
        "userId": GARMIN_USER,
        "summaryId": str(ACTIVITY_ID),
        "activityId": ACTIVITY_ID,
        "activityName": "Morning Run",
        "activityType": "RUNNING",
        "startTimeInSeconds": 1793001600,
        "startTimeOffsetInSeconds": -18000,
        "durationInSeconds": 3600,
        "distanceInMeters": 10000.0,
        "activeKilocalories": 650,
        "averageHeartRateInBeatsPerMinute": 145,
        "deviceName": "Forerunner 965",
        "manual": False,
        "isWebUpload": False,
        **overrides,
    }


def _workouts(db: Session, user_id: UUID, activity_id: int = ACTIVITY_ID) -> list[EventRecord]:
    return EventRecordRepository(EventRecord).get_all_by_external_id(
        db, user_id, str(activity_id), "garmin", EventCategory.WORKOUT
    )


def _detail(db: Session, record_id: UUID) -> Any:
    return EventRecordDetailRepository(EventRecordDetail).get_by_record_id(db, record_id, EventCategory.WORKOUT)


def _series(db: Session, user_id: UUID, series_type: SeriesType) -> list[DataPointSeries]:
    return (
        db.query(DataPointSeries)
        .join(DataSource, DataPointSeries.data_source_id == DataSource.id)
        .filter(
            DataSource.user_id == user_id,
            DataPointSeries.series_type_definition_id == get_series_type_id(series_type),
        )
        .all()
    )


class TestManuallyUpdatedActivities:
    def test_edit_overwrites_the_stored_workout(
        self, db: Session, handler: GarminWebhookHandler, user_id: UUID
    ) -> None:
        handler.process_payload(db, {"activities": [_activity()]}, "t1")
        [original] = _workouts(db, user_id)
        original_id, original_start = original.id, original.start_datetime

        edited = _activity(
            activityType="TRAIL_RUNNING",
            startTimeInSeconds=1793001000,
            durationInSeconds=4200,
            distanceInMeters=11250.5,
            manual=True,
        )
        result = handler.process_payload(db, {"manuallyUpdatedActivities": [edited]}, "t2")

        assert result["manuallyUpdatedActivities"]["updated"] == 1
        assert result["manuallyUpdatedActivities"]["saved"] == 0
        assert result["errors"] == []
        [workout] = _workouts(db, user_id)
        db.refresh(workout)
        assert workout.id == original_id
        assert workout.duration_seconds == 4200
        assert original_start == datetime.fromtimestamp(1793001600, tz=timezone.utc)
        assert workout.start_datetime == datetime.fromtimestamp(1793001000, tz=timezone.utc)
        assert workout.end_datetime == datetime.fromtimestamp(1793001000 + 4200, tz=timezone.utc)
        assert workout.type == get_unified_workout_type("TRAIL_RUNNING").value
        detail = _detail(db, workout.id)
        assert detail.distance == Decimal("11250.500")
        # `manual` on an edit means "edited in Connect", not "typed in": entry_source stays as recorded.
        assert detail.entry_source == EntrySource.AUTOMATIC

    def test_edit_keeps_fields_the_summary_does_not_carry(
        self, db: Session, handler: GarminWebhookHandler, user_id: UUID
    ) -> None:
        handler.process_payload(db, {"activities": [_activity()]}, "t1")
        [workout] = _workouts(db, user_id)
        segments = [{"kind": "lap", "index": 0, "distance_m": 1609.3}]
        EventRecordDetailRepository(EventRecordDetail).update_workout_fields(db, workout.id, {"segments": segments})
        db.commit()

        handler.process_payload(db, {"manuallyUpdatedActivities": [_activity(activityName="Renamed run")]}, "t2")

        detail = _detail(db, workout.id)
        db.refresh(detail)
        assert detail.segments == segments
        assert detail.label == "Renamed run"
        assert detail.heart_rate_avg == Decimal("145.00")

    def test_edit_of_unknown_activity_creates_it(
        self, db: Session, handler: GarminWebhookHandler, user_id: UUID
    ) -> None:
        result = handler.process_payload(db, {"manuallyUpdatedActivities": [_activity(manual=True)]}, "t1")

        assert result["manuallyUpdatedActivities"]["saved"] == 1
        [workout] = _workouts(db, user_id)
        assert _detail(db, workout.id).entry_source is None

    def test_activity_id_falls_back_to_summary_id(
        self, db: Session, handler: GarminWebhookHandler, user_id: UUID
    ) -> None:
        handler.process_payload(db, {"activities": [_activity()]}, "t1")
        edited = _activity(durationInSeconds=1800)
        del edited["activityId"]

        result = handler.process_payload(db, {"manuallyUpdatedActivities": [edited]}, "t2")

        assert result["manuallyUpdatedActivities"]["updated"] == 1
        [workout] = _workouts(db, user_id)
        db.refresh(workout)
        assert workout.duration_seconds == 1800

    def test_unknown_garmin_user_is_reported(self, db: Session, handler: GarminWebhookHandler) -> None:
        result = handler.process_payload(db, {"manuallyUpdatedActivities": [_activity(userId="someone-else")]}, "t1")

        assert result["manuallyUpdatedActivities"]["updated"] == 0
        assert len(result["errors"]) == 1

    def test_invalid_entry_is_reported(self, db: Session, handler: GarminWebhookHandler, user_id: UUID) -> None:
        broken = _activity()
        del broken["startTimeInSeconds"]

        result = handler.process_payload(db, {"manuallyUpdatedActivities": [broken]}, "t1")

        assert result["manuallyUpdatedActivities"]["details"][0]["status"] == "validation_error"
        assert _workouts(db, user_id) == []


class TestNutritionAndHydration:
    def _nutrition(self, kcal: int = 1840, protein: float = 118) -> dict[str, Any]:
        return {
            "userId": GARMIN_USER,
            "summaryId": "nutrition-2026-10-26",
            "calendarDate": "2026-10-26",
            "dailyNutritionIntake": {
                "kiloCalories": kcal,
                "carbsInGrams": 212.5,
                "fatInGrams": 64,
                "proteinInGrams": protein,
            },
            "loggedFoods": [
                {
                    "mealConsumedTimeInLocal": "07:00:00",
                    "nutritionContentIntake": {"kiloCalories": kcal, "proteinInGrams": protein},
                }
            ],
        }

    def test_nutrition_becomes_daily_totals(self, db: Session, handler: GarminWebhookHandler, user_id: UUID) -> None:
        result = handler.process_payload(db, {"nutrition": [self._nutrition()]}, "t1")

        assert result["wellness"]["nutrition"]["processed"] == 1
        noon = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
        expected = {
            SeriesType.dietary_energy_consumed: Decimal("1840"),
            SeriesType.dietary_carbohydrates: Decimal("212.5"),
            SeriesType.dietary_fat_total: Decimal("64"),
            SeriesType.dietary_protein: Decimal("118"),
        }
        for series_type, value in expected.items():
            [sample] = _series(db, user_id, series_type)
            assert sample.value == value
            assert sample.recorded_at == noon
            assert sample.is_daily_total is True

    def test_resent_nutrition_updates_in_place(self, db: Session, handler: GarminWebhookHandler, user_id: UUID) -> None:
        handler.process_payload(db, {"nutrition": [self._nutrition()]}, "t1")
        handler.process_payload(db, {"nutrition": [self._nutrition(kcal=1210, protein=72.5)]}, "t2")

        [energy] = _series(db, user_id, SeriesType.dietary_energy_consumed)
        [protein] = _series(db, user_id, SeriesType.dietary_protein)
        db.refresh(energy)
        db.refresh(protein)
        assert energy.value == Decimal("1210")
        assert protein.value == Decimal("72.5")

    def test_no_meals_are_created(self, db: Session, handler: GarminWebhookHandler, user_id: UUID) -> None:
        handler.process_payload(db, {"nutrition": [self._nutrition()]}, "t1")

        meals = (
            db.query(EventRecord)
            .join(DataSource, EventRecord.data_source_id == DataSource.id)
            .filter(DataSource.user_id == user_id, EventRecord.category == EventCategory.MEAL)
            .all()
        )
        assert meals == []

    def test_hydration(self, db: Session, handler: GarminWebhookHandler, user_id: UUID) -> None:
        payload = {
            "hydration": [
                {
                    "userId": GARMIN_USER,
                    "summaryId": "summary-2026-10-26",
                    "calendarDate": "2026-10-26",
                    "fluidIntakeInML": 500,
                }
            ]
        }

        result = handler.process_payload(db, payload, "t1")

        assert result["wellness"]["hydration"]["processed"] == 1
        [sample] = _series(db, user_id, SeriesType.hydration)
        assert sample.value == Decimal("500")
        assert sample.recorded_at == datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
        assert sample.is_daily_total is True

    def test_entries_without_a_date_are_skipped(
        self, db: Session, handler: GarminWebhookHandler, user_id: UUID
    ) -> None:
        payload = {
            "hydration": [{"userId": GARMIN_USER, "summaryId": "h1", "fluidIntakeInML": 500}],
            "nutrition": [{**self._nutrition(), "calendarDate": None}],
        }

        handler.process_payload(db, payload, "t1")

        assert _series(db, user_id, SeriesType.hydration) == []
        assert _series(db, user_id, SeriesType.dietary_energy_consumed) == []

    def test_timeseries_endpoint_serves_hydration(
        self, client: TestClient, db: Session, handler: GarminWebhookHandler, user_id: UUID
    ) -> None:
        payload = {
            "hydration": [
                {"userId": GARMIN_USER, "summaryId": "h1", "calendarDate": "2026-10-26", "fluidIntakeInML": 750}
            ]
        }
        handler.process_payload(db, payload, "t1")

        response = client.get(
            f"/api/v1/users/{user_id}/timeseries",
            headers={"X-Open-Wearables-API-Key": ApiKeyFactory().plain_key},
            params={
                "types": "hydration",
                "start_time": "2026-10-26T00:00:00Z",
                "end_time": "2026-10-27T00:00:00Z",
            },
        )

        assert response.status_code == 200, response.text
        [sample] = response.json()["data"]
        assert sample["value"] == 750
        assert sample["unit"] == "mL"
        assert sample["source"]["provider"] == "garmin"


class TestUnmappedTypes:
    def test_breathing_variations_are_ignored_and_logged(
        self,
        db: Session,
        handler: GarminWebhookHandler,
        user_id: UUID,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        payload = {
            "breathingVariations": [
                {
                    "userId": GARMIN_USER,
                    "summaryId": "summary-2026-10-26",
                    "calendarDate": "2026-10-26",
                    "variation": "MINIMAL",
                    "readings": [
                        {"variation": "FEW", "startTimeInSeconds": 1793000000, "endTimeInSeconds": 1793000300}
                    ],
                }
            ],
            "somethingGarminAddsLater": [{"userId": GARMIN_USER}],
        }

        result = handler.process_payload(db, payload, "t1")

        assert result["ignored"] == ["breathingVariations", "somethingGarminAddsLater"]
        assert result["errors"] == []
        logged = capsys.readouterr().out
        assert "Ignoring Garmin push types OW does not map" in logged
        assert '"level": "debug"' in logged
        assert db.query(DataSource).filter(DataSource.user_id == user_id).count() == 0

    def test_dispatch_accepts_unmapped_types(self, db: Session, handler: GarminWebhookHandler) -> None:
        with patch("app.services.providers.garmin.webhook_handler.celery_app") as celery:
            celery.send_task.return_value.id = "task-1"
            response = handler.dispatch(db, {"breathingVariations": [{"userId": GARMIN_USER}]})

        assert response == {"status": "accepted"}
        celery.send_task.assert_called_once()


class TestDeviceAttribution:
    def test_workout_source_device_comes_from_device_name(
        self, client: TestClient, db: Session, handler: GarminWebhookHandler, user_id: UUID
    ) -> None:
        handler.process_payload(db, {"activities": [_activity()]}, "t1")

        response = client.get(
            f"/api/v1/users/{user_id}/events/workouts",
            headers={"X-Open-Wearables-API-Key": ApiKeyFactory().plain_key},
            params={"start_date": "2026-10-01T00:00:00Z", "end_date": "2026-11-01T00:00:00Z"},
        )

        assert response.status_code == 200, response.text
        [workout] = response.json()["data"]
        assert workout["source"]["provider"] == "garmin"
        assert workout["source"]["device"] == "Forerunner 965"

    def test_unknown_device_is_plain_garmin(self, db: Session, handler: GarminWebhookHandler, user_id: UUID) -> None:
        handler.process_payload(db, {"activities": [_activity(deviceName="unknown", manual=True)]}, "t1")

        [workout] = _workouts(db, user_id)
        assert workout.source_name == "Garmin"
        source = db.get(DataSource, workout.data_source_id)
        assert source is not None
        assert source.device_model is None

    def test_activity_detail_samples_carry_the_device(
        self, db: Session, handler: GarminWebhookHandler, user_id: UUID
    ) -> None:
        payload = {
            "activityDetails": [
                {
                    "userId": GARMIN_USER,
                    "summaryId": f"{ACTIVITY_ID}-detail",
                    "activityId": ACTIVITY_ID,
                    "summary": {
                        "activityId": ACTIVITY_ID,
                        "activityType": "RUNNING",
                        "startTimeInSeconds": 1793001600,
                        "durationInSeconds": 60,
                        "deviceName": "Forerunner 965",
                    },
                    "samples": [{"startTimeInSeconds": 1793001600, "heartRate": 120}],
                }
            ]
        }

        with patch.object(settings, "ingest_workout_samples", True):
            handler.process_payload(db, payload, "t1")

        [sample] = _series(db, user_id, SeriesType.heart_rate)
        source = db.get(DataSource, sample.data_source_id)
        assert source is not None
        assert source.device_model == "Forerunner 965"


class TestActivityRedeliveryAndEdits:
    """Workouts are matched on the Garmin activity id first, not only on (device, start, end)."""

    def test_original_redelivered_after_a_duration_edit_keeps_one_workout(
        self, db: Session, handler: GarminWebhookHandler, user_id: UUID
    ) -> None:
        handler.process_payload(db, {"activities": [_activity()]}, "t1")
        handler.process_payload(db, {"manuallyUpdatedActivities": [_activity(durationInSeconds=1800)]}, "t2")

        result = handler.process_payload(db, {"activities": [_activity()]}, "t3")

        [workout] = _workouts(db, user_id)
        db.refresh(workout)
        # The late original does not undo the edit.
        assert workout.duration_seconds == 1800
        assert result["activities"][0]["status"] == "duplicate"
        assert result["errors"] == []

    def test_edit_arriving_before_the_original_keeps_one_workout(
        self, db: Session, handler: GarminWebhookHandler, user_id: UUID
    ) -> None:
        handler.process_payload(db, {"manuallyUpdatedActivities": [_activity(durationInSeconds=1800)]}, "t1")
        handler.process_payload(db, {"activities": [_activity()]}, "t2")

        [workout] = _workouts(db, user_id)
        db.refresh(workout)
        assert workout.duration_seconds == 1800

    def test_resent_activity_with_new_values_updates_in_place(
        self, db: Session, handler: GarminWebhookHandler, user_id: UUID
    ) -> None:
        handler.process_payload(db, {"activities": [_activity()]}, "t1")
        [original] = _workouts(db, user_id)

        result = handler.process_payload(db, {"activities": [_activity(durationInSeconds=3660)]}, "t2")

        [workout] = _workouts(db, user_id)
        db.refresh(workout)
        assert workout.id == original.id
        assert workout.duration_seconds == 3660
        assert result["activities"][0]["status"] == "updated"

    def test_identical_redelivery_is_a_duplicate(
        self, db: Session, handler: GarminWebhookHandler, user_id: UUID
    ) -> None:
        handler.process_payload(db, {"activities": [_activity()]}, "t1")

        result = handler.process_payload(db, {"activities": [_activity()]}, "t2")

        assert result["activities"][0]["status"] == "duplicate"
        assert result["saved"] == 0
        assert len(_workouts(db, user_id)) == 1

    def test_edit_colliding_with_another_workout_reports_a_conflict(
        self, db: Session, handler: GarminWebhookHandler, user_id: UUID
    ) -> None:
        other_id = ACTIVITY_ID + 1
        handler.process_payload(db, {"activities": [_activity()]}, "t1")
        handler.process_payload(
            db,
            {"activities": [_activity(activityId=other_id, startTimeInSeconds=1793010000, activityName="Evening")]},
            "t2",
        )
        [mine] = _workouts(db, user_id)
        [other] = _workouts(db, user_id, other_id)

        # The user moves this activity onto the other one's exact time span.
        result = handler.process_payload(
            db, {"manuallyUpdatedActivities": [_activity(startTimeInSeconds=1793010000, activityName="Moved")]}, "t3"
        )

        [detail] = result["manuallyUpdatedActivities"]["details"]
        assert detail["status"] == "conflict"
        assert detail["conflict_record_ids"] == [str(mine.id)]
        assert result["manuallyUpdatedActivities"]["conflicts"] == 1
        assert result["manuallyUpdatedActivities"]["saved"] == 0
        assert len(result["errors"]) == 1
        # Neither workout was touched.
        db.refresh(mine)
        assert mine.start_datetime == datetime.fromtimestamp(1793001600, tz=timezone.utc)
        assert _detail(db, mine.id).label == "Morning Run"
        assert _detail(db, other.id).label == "Evening"

    def test_non_numeric_summary_id_does_not_become_a_workout(
        self, db: Session, handler: GarminWebhookHandler, user_id: UUID
    ) -> None:
        handler.process_payload(db, {"activities": [_activity()]}, "t1")
        edited = _activity(durationInSeconds=1800, summaryId=f"{ACTIVITY_ID}-manual")
        del edited["activityId"]

        result = handler.process_payload(db, {"manuallyUpdatedActivities": [edited]}, "t2")

        assert result["manuallyUpdatedActivities"]["details"][0]["status"] == "validation_error"
        total = db.query(EventRecord).filter(EventRecord.category == EventCategory.WORKOUT).count()
        assert total == 1

    def test_times_are_stored_in_utc(self, db: Session, handler: GarminWebhookHandler, user_id: UUID) -> None:
        handler.process_payload(db, {"activities": [_activity()]}, "t1")

        [workout] = _workouts(db, user_id)
        assert workout.start_datetime == datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)
        assert workout.end_datetime == datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)


class TestActivityFields:
    def test_cycling_cadence_speed_and_elevation(
        self, db: Session, handler: GarminWebhookHandler, user_id: UUID
    ) -> None:
        cycling = _activity(
            activityType="CYCLING",
            averageBikeCadenceInRoundsPerMinute=85.0,
            averageSpeedInMetersPerSecond=8.75,
            maxSpeedInMetersPerSecond=14.2,
            totalElevationGainInMeters=420.5,
        )

        handler.process_payload(db, {"activities": [cycling]}, "t1")

        [workout] = _workouts(db, user_id)
        detail = _detail(db, workout.id)
        assert detail.average_cadence == Decimal("85.00")
        assert detail.average_speed == Decimal("8.75")
        assert detail.max_speed == Decimal("14.20")
        assert detail.total_elevation_gain == Decimal("420.500")

    def test_fractional_heart_rate_does_not_drop_the_activity(
        self, db: Session, handler: GarminWebhookHandler, user_id: UUID
    ) -> None:
        result = handler.process_payload(
            db,
            {"activities": [_activity(maxHeartRateInBeatsPerMinute=171.5, averageHeartRateInBeatsPerMinute=144.6)]},
            "t1",
        )

        assert result["activities"][0]["status"] == "saved"
        [workout] = _workouts(db, user_id)
        detail = _detail(db, workout.id)
        assert detail.heart_rate_max == 172
        assert detail.heart_rate_avg == Decimal("144.60")

    def test_activity_details_summary_uses_the_spec_field_names(
        self, db: Session, handler: GarminWebhookHandler, user_id: UUID
    ) -> None:
        payload = {
            "activityDetails": [
                {
                    "userId": GARMIN_USER,
                    "summaryId": f"{ACTIVITY_ID}-detail",
                    "activityId": ACTIVITY_ID,
                    "summary": {
                        "activityId": ACTIVITY_ID,
                        "activityType": "CYCLING",
                        "startTimeInSeconds": 1793001600,
                        "durationInSeconds": 3600,
                        "deviceName": "Edge 1050",
                        "totalElevationGainInMeters": 420.5,
                        "averageBikeCadenceInRoundsPerMinute": 85.0,
                        "maxHeartRateInBeatsPerMinute": 171.5,
                    },
                    "samples": [],
                }
            ]
        }

        handler.process_payload(db, payload, "t1")

        [workout] = _workouts(db, user_id)
        detail = _detail(db, workout.id)
        assert detail.average_cadence == Decimal("85.00")
        assert detail.total_elevation_gain == Decimal("420.500")
        assert detail.heart_rate_max == 172


class TestWorkoutWebhooks:
    @pytest.fixture
    def emitted(self) -> Any:
        with (
            patch("app.services.event_record_service.svix_service.is_enabled", return_value=True),
            patch("app.services.event_record_service.on_workout_created") as created,
            patch("app.services.event_record_service.on_workout_updated") as updated,
        ):
            yield created, updated

    def test_created_once_then_updated_on_edit(
        self, db: Session, handler: GarminWebhookHandler, user_id: UUID, emitted: Any
    ) -> None:
        created, updated = emitted

        handler.process_payload(db, {"activities": [_activity()]}, "t1")
        handler.process_payload(db, {"activities": [_activity()]}, "t1-again")
        assert (created.call_count, updated.call_count) == (1, 0)

        handler.process_payload(db, {"manuallyUpdatedActivities": [_activity(durationInSeconds=1800)]}, "t2")
        assert (created.call_count, updated.call_count) == (1, 1)

        [workout] = _workouts(db, user_id)
        payload = updated.call_args.kwargs
        assert payload["record_id"] == workout.id == created.call_args.kwargs["record_id"]
        assert payload["duration_seconds"] == 1800
        assert payload["end_time"] == datetime.fromtimestamp(1793001600 + 1800, tz=timezone.utc).isoformat()

    def test_identical_edit_sends_no_update(
        self, db: Session, handler: GarminWebhookHandler, user_id: UUID, emitted: Any
    ) -> None:
        _created, updated = emitted
        handler.process_payload(db, {"manuallyUpdatedActivities": [_activity(activityName="Renamed")]}, "t1")
        handler.process_payload(db, {"manuallyUpdatedActivities": [_activity(activityName="Renamed")]}, "t2")

        assert updated.call_count == 0

    def test_rename_is_an_update(self, db: Session, handler: GarminWebhookHandler, user_id: UUID, emitted: Any) -> None:
        _created, updated = emitted
        handler.process_payload(db, {"activities": [_activity()]}, "t1")

        handler.process_payload(db, {"manuallyUpdatedActivities": [_activity(activityName="Renamed")]}, "t2")

        assert updated.call_count == 1

    def test_conflict_sends_nothing(
        self, db: Session, handler: GarminWebhookHandler, user_id: UUID, emitted: Any
    ) -> None:
        created, updated = emitted
        handler.process_payload(db, {"activities": [_activity()]}, "t1")
        handler.process_payload(
            db, {"activities": [_activity(activityId=ACTIVITY_ID + 1, startTimeInSeconds=1793010000)]}, "t2"
        )

        handler.process_payload(db, {"manuallyUpdatedActivities": [_activity(startTimeInSeconds=1793010000)]}, "t3")

        assert (created.call_count, updated.call_count) == (2, 0)


class TestExpiredConnections:
    """A connection whose tokens Garmin rejected still receives pushes until the user deregisters."""

    def test_pushes_are_stored_for_an_expired_connection(self, db: Session, handler: GarminWebhookHandler) -> None:
        user = UserFactory()
        UserConnectionFactory(
            user=user, provider="garmin", provider_user_id=GARMIN_USER, status=ConnectionStatus.EXPIRED
        )

        result = handler.process_payload(
            db,
            {
                "activities": [_activity()],
                "hydration": [
                    {"userId": GARMIN_USER, "summaryId": "h1", "calendarDate": "2026-10-26", "fluidIntakeInML": 500}
                ],
            },
            "t1",
        )

        assert result["errors"] == []
        assert len(_workouts(db, user.id)) == 1
        assert len(_series(db, user.id, SeriesType.hydration)) == 1

    def test_revoked_connections_receive_nothing(self, db: Session, handler: GarminWebhookHandler) -> None:
        user = UserFactory()
        UserConnectionFactory(
            user=user, provider="garmin", provider_user_id=GARMIN_USER, status=ConnectionStatus.REVOKED
        )

        result = handler.process_payload(db, {"activities": [_activity()]}, "t1")

        assert result["activities"][0]["status"] == "user_not_found"
        assert _workouts(db, user.id) == []

    def test_deregistration_revokes_an_expired_connection(self, db: Session, handler: GarminWebhookHandler) -> None:
        user = UserFactory()
        connection = UserConnectionFactory(
            user=user, provider="garmin", provider_user_id=GARMIN_USER, status=ConnectionStatus.EXPIRED
        )

        with patch("app.services.providers.garmin.handlers.lifecycle.on_connection_revoked") as revoked:
            result = handler.process_payload(db, {"deregistrations": [{"userId": GARMIN_USER}]}, "t1")

        assert result["deregistrations"]["revoked"] == 1
        db.refresh(connection)
        assert connection.status == ConnectionStatus.REVOKED
        assert revoked.call_args.kwargs["reason"] == "deregistration"


class TestLifecycleFanOut:
    def test_permission_change_reaches_every_linked_profile(self, db: Session, handler: GarminWebhookHandler) -> None:
        first, second = UserFactory(), UserFactory()
        c1 = UserConnectionFactory(user=first, provider="garmin", provider_user_id=GARMIN_USER, scope="WORKOUT_IMPORT")
        c2 = UserConnectionFactory(
            user=second,
            provider="garmin",
            provider_user_id=GARMIN_USER,
            scope="WORKOUT_IMPORT",
            status=ConnectionStatus.EXPIRED,
        )

        result = handler.process_payload(
            db,
            {"userPermissionsChange": [{"userId": GARMIN_USER, "permissions": ["ACTIVITY_EXPORT"]}]},
            "t1",
        )

        assert result["userPermissionsChange"]["updated"] == 2
        db.refresh(c1)
        db.refresh(c2)
        assert c1.scope == c2.scope == "ACTIVITY_EXPORT"

    def test_older_permission_change_does_not_roll_back_a_newer_one(
        self, db: Session, handler: GarminWebhookHandler, user_id: UUID
    ) -> None:
        def change(permissions: list[str], at: int) -> dict[str, Any]:
            return {
                "userPermissionsChange": [
                    {"userId": GARMIN_USER, "permissions": permissions, "changeTimeInSeconds": at}
                ]
            }

        handler.process_payload(db, change(["ACTIVITY_EXPORT", "WORKOUT_IMPORT"], 1_800_000_200), "t1")
        late = handler.process_payload(db, change(["ACTIVITY_EXPORT"], 1_800_000_100), "t2")

        assert late["userPermissionsChange"]["ignored_stale"] == 1
        connection = db.query(UserConnection).filter(UserConnection.user_id == user_id).one()
        db.refresh(connection)
        assert connection.scope == "ACTIVITY_EXPORT WORKOUT_IMPORT"

    def test_all_permissions_withdrawn_is_stored_as_empty(
        self, db: Session, handler: GarminWebhookHandler, user_id: UUID
    ) -> None:
        handler.process_payload(db, {"userPermissionsChange": [{"userId": GARMIN_USER, "permissions": []}]}, "t1")

        connection = db.query(UserConnection).filter(UserConnection.user_id == user_id).one()
        db.refresh(connection)
        assert connection.scope == ""
        assert stored_permissions(connection.scope) == []

    def test_deregistration_reaches_every_linked_profile(self, db: Session, handler: GarminWebhookHandler) -> None:
        connections = [
            UserConnectionFactory(
                user=UserFactory(), provider="garmin", provider_user_id=GARMIN_USER, access_token=None
            )
            for _ in range(2)
        ]

        with patch("app.services.providers.garmin.handlers.lifecycle.on_connection_revoked") as revoked:
            result = handler.process_payload(db, {"deregistrations": [{"userId": GARMIN_USER}]}, "t1")

        assert result["deregistrations"]["revoked"] == 2
        assert revoked.call_count == 2
        for connection in connections:
            db.refresh(connection)
            assert connection.status == ConnectionStatus.REVOKED


class TestDeviceNameCase:
    def test_samples_and_workouts_share_one_source_across_spellings(
        self, db: Session, handler: GarminWebhookHandler, user_id: UUID
    ) -> None:
        details = {
            "activityDetails": [
                {
                    "userId": GARMIN_USER,
                    "summaryId": f"{ACTIVITY_ID}-detail",
                    "activityId": ACTIVITY_ID,
                    "summary": {
                        "activityId": ACTIVITY_ID,
                        "activityType": "RUNNING",
                        "startTimeInSeconds": 1793001600,
                        "durationInSeconds": 60,
                        "deviceName": "Garmin fenix 8",
                    },
                    "samples": [{"startTimeInSeconds": 1793001600, "heartRate": 120}],
                }
            ]
        }

        with patch.object(settings, "ingest_workout_samples", True):
            handler.process_payload(db, details, "t1")
        handler.process_payload(
            db, {"activities": [_activity(activityId=ACTIVITY_ID + 5, deviceName="Garmin Fenix 8")]}, "t2"
        )

        sources = db.query(DataSource).filter(DataSource.user_id == user_id, DataSource.provider == "garmin").all()
        assert [source.device_model for source in sources] == ["Garmin fenix 8"]


class TestNutritionRepush:
    def test_macro_left_out_of_a_repush_becomes_zero(
        self, db: Session, handler: GarminWebhookHandler, user_id: UUID
    ) -> None:
        def nutrition(intake: dict[str, Any]) -> dict[str, Any]:
            return {
                "nutrition": [
                    {
                        "userId": GARMIN_USER,
                        "summaryId": "n-2026-10-26",
                        "calendarDate": "2026-10-26",
                        "dailyNutritionIntake": intake,
                    }
                ]
            }

        handler.process_payload(
            db, nutrition({"kiloCalories": 900, "carbsInGrams": 40, "fatInGrams": 61, "proteinInGrams": 50}), "t1"
        )
        # The user deleted the only food with fat in it; Garmin re-sends the day without fatInGrams.
        handler.process_payload(db, nutrition({"kiloCalories": 400, "carbsInGrams": 40, "proteinInGrams": 50}), "t2")

        [fat] = _series(db, user_id, SeriesType.dietary_fat_total)
        [energy] = _series(db, user_id, SeriesType.dietary_energy_consumed)
        db.refresh(fat)
        db.refresh(energy)
        assert fat.value == Decimal("0")
        assert energy.value == Decimal("400")
