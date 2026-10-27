"""Garmin push types added for the Starter app: manuallyUpdatedActivities, nutrition, hydration,
breathingVariations (unmapped), plus device attribution on what Garmin names a device for.

Payloads go through GarminWebhookHandler.process_payload, the code the Celery task runs.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.constants.workout_types.garmin import get_unified_workout_type
from app.models import DataPointSeries, DataSource, EventRecord, EventRecordDetail
from app.repositories.event_record_detail_repository import EventRecordDetailRepository
from app.repositories.event_record_repository import EventRecordRepository
from app.schemas.enums import EntrySource, EventCategory, SeriesType, get_series_type_id
from app.services.providers.garmin.strategy import GarminStrategy
from app.services.providers.garmin.webhook_handler import GarminWebhookHandler
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


def _workouts(db: Session, user_id: UUID) -> list[EventRecord]:
    return EventRecordRepository(EventRecord).get_all_by_external_id(
        db, user_id, str(ACTIVITY_ID), "garmin", EventCategory.WORKOUT
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
        # Compared relative to the original: GarminWorkouts reads epoch seconds as host-local time.
        assert original_start - workout.start_datetime == timedelta(seconds=600)
        assert workout.end_datetime - workout.start_datetime == timedelta(seconds=4200)
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
