import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any, Iterable, NamedTuple
from uuid import UUID, uuid4

from redis.exceptions import RedisError

from app.constants.devices_map import normalize_device_model
from app.constants.entry_source import get_unified_garmin_entry_source
from app.constants.workout_types.garmin import get_unified_workout_type
from app.database import DbSession
from app.integrations.redis_client import get_redis_client
from app.schemas.enums import EventCategory
from app.schemas.model_crud.activities import (
    EventRecordCreate,
    EventRecordDetailCreate,
    EventRecordMetrics,
)
from app.schemas.providers.garmin import ActivityJSON as GarminActivityJSON
from app.services.event_record_service import event_record_service
from app.services.providers.garmin.devices import GarminDeviceNames
from app.services.providers.templates.base_workouts import BaseWorkoutsTemplate
from app.utils.dates import offset_to_iso
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

# Activities the user edited in Garmin Connect, per OW user, so a late copy of the original summary
# doesn't overwrite the edit. Long enough to outlast any redelivery; losing it (a Redis flush) only
# lets such a late original through.
_EDITED_KEY = "garmin:edited_activity:{user_id}:{activity_id}"
_EDITED_TTL_SECONDS = 30 * 24 * 3600


class WorkoutWriteStatus(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    CONFLICT = "conflict"


class WorkoutWrite(NamedTuple):
    """What happened to one stored workout when a Garmin activity was applied."""

    record_id: UUID
    status: WorkoutWriteStatus


def _was_edited(user_id: UUID, activity_id: str) -> bool:
    try:
        return bool(get_redis_client().exists(_EDITED_KEY.format(user_id=user_id, activity_id=activity_id)))
    except RedisError:
        logger.warning("Could not check whether a Garmin activity was edited", exc_info=True)
        return False


def _mark_edited(user_id: UUID, activity_id: str) -> None:
    try:
        get_redis_client().set(
            _EDITED_KEY.format(user_id=user_id, activity_id=activity_id), "1", ex=_EDITED_TTL_SECONDS
        )
    except RedisError:
        logger.warning("Could not record a Garmin activity edit", exc_info=True)


class GarminWorkouts(BaseWorkoutsTemplate):
    """Garmin implementation of workouts template."""

    def get_workouts(
        self,
        db: DbSession,
        user_id: UUID,
        start_date: datetime,
        end_date: datetime,
    ) -> list[Any]:
        """Get activities from Garmin API for a single time range.

        Note: Garmin API has a maximum range of ~24 hours per request.
        For longer date ranges, use get_workouts_historical().
        """
        # Garmin API uses seconds for timestamps
        start_ts = int(start_date.timestamp())
        end_ts = int(end_date.timestamp())

        params = {
            "uploadStartTimeInSeconds": start_ts,
            "uploadEndTimeInSeconds": end_ts,
        }

        return self._make_api_request(
            db,
            user_id,
            "/wellness-api/rest/activities",
            params=params,
        )

    def get_workouts_historical(
        self,
        db: DbSession,
        user_id: UUID,
        start_date: datetime,
        end_date: datetime,
        chunk_hours: int = 24,
    ) -> list[Any]:
        """Fetch workouts in 24-hour chunks for extended date ranges.

        Garmin API limits requests to ~24 hours. This method fetches data
        in chunks to support historical backfill of any date range.

        Args:
            db: Database session
            user_id: User ID
            start_date: Start of date range
            end_date: End of date range
            chunk_hours: Size of each chunk in hours (default 24)

        Returns:
            List of all activities from the date range
        """
        all_activities: list[Any] = []
        current_start = start_date

        while current_start < end_date:
            current_end = min(current_start + timedelta(hours=chunk_hours), end_date)

            try:
                activities = self.get_workouts(db, user_id, current_start, current_end)
                if isinstance(activities, list):
                    all_activities.extend(activities)
            except Exception as e:
                # Log error but continue with other chunks
                log_structured(
                    logger,
                    "warning",
                    f"Error fetching activities chunk ({current_start.isoformat()} to {current_end.isoformat()}): {e}",
                    provider="garmin",
                    task="get_workouts_historical",
                    user_id=str(user_id),
                )

            current_start = current_end

        return all_activities

    def get_workouts_from_api(self, db: DbSession, user_id: UUID, **kwargs: Any) -> Any:
        """Get activities from Garmin API with options.

        Supports extended date ranges by automatically chunking requests
        when the range exceeds 24 hours (Garmin API limit).
        """
        summary_start_time = kwargs.get("summary_start_time")
        summary_end_time = kwargs.get("summary_end_time")

        start_ts = self._parse_timestamp(summary_start_time)
        end_ts = self._parse_timestamp(summary_end_time)

        # Default to last 24 hours if no time range provided
        now = datetime.now(timezone.utc)
        if not start_ts:
            start_ts = int((now - timedelta(hours=24)).timestamp())
        if not end_ts:
            end_ts = int(now.timestamp())

        # Convert to datetime for chunked fetching
        start_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(end_ts, tz=timezone.utc)

        # Check if range exceeds 24 hours - if so, use chunked fetching
        if (end_dt - start_dt) > timedelta(hours=24):
            return self.get_workouts_historical(db, user_id, start_dt, end_dt)

        # Single request for ranges <= 24 hours
        params = {
            "uploadStartTimeInSeconds": start_ts,
            "uploadEndTimeInSeconds": end_ts,
        }

        return self._make_api_request(
            db,
            user_id,
            "/wellness-api/rest/activities",
            params=params,
        )

    def get_workout_detail_from_api(self, db: DbSession, user_id: UUID, workout_id: str, **kwargs: Any) -> Any:
        """Get detailed activity data from Garmin API."""
        return self.get_activity_detail(db, user_id, workout_id)

    def _parse_timestamp(self, value: str | None) -> int | None:
        """Parse timestamp from string (Unix timestamp or ISO 8601 date)."""
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return int(dt.timestamp())
        except (ValueError, AttributeError):
            return None

    def _extract_dates(self, start_timestamp: int, end_timestamp: int) -> tuple[datetime, datetime]:
        """Garmin timestamps are Unix seconds (UTC); keep them timezone-aware."""
        start_date = datetime.fromtimestamp(start_timestamp, tz=timezone.utc)
        end_date = datetime.fromtimestamp(end_timestamp, tz=timezone.utc)
        return start_date, end_date

    @staticmethod
    def _decimal(value: float | int | None) -> Decimal | None:
        return Decimal(str(value)) if value is not None else None

    def _build_metrics(self, raw_workout: GarminActivityJSON) -> EventRecordMetrics:
        heart_rate_max = raw_workout.maxHeartRateInBeatsPerMinute
        metrics: EventRecordMetrics = {
            # Garmin activity payloads carry only average and max heart rate
            "heart_rate_min": None,
            # Garmin documents max HR as floating point; the column holds whole beats.
            "heart_rate_max": round(heart_rate_max) if heart_rate_max is not None else None,
            "heart_rate_avg": self._decimal(raw_workout.averageHeartRateInBeatsPerMinute),
            "steps_count": raw_workout.steps,
            "energy_burned": self._decimal(raw_workout.activeKilocalories),
            "distance": self._decimal(raw_workout.distanceInMeters),
            "average_cadence": self._decimal(raw_workout.averageCadence),
            "average_speed": self._decimal(raw_workout.averageSpeedInMetersPerSecond),
            "max_speed": self._decimal(raw_workout.maxSpeedInMetersPerSecond),
            "total_elevation_gain": self._decimal(raw_workout.totalElevationGainInMeters),
        }

        entry_source = get_unified_garmin_entry_source(raw_workout.manual, raw_workout.isWebUpload)
        if entry_source is not None:
            metrics["entry_source"] = entry_source

        if raw_workout.activityName:
            metrics["label"] = raw_workout.activityName

        return metrics

    def _normalize_workout(
        self,
        raw_workout: GarminActivityJSON,
        user_id: UUID,
    ) -> tuple[EventRecordCreate, EventRecordDetailCreate]:
        """Normalize Garmin activity to EventRecordCreate and EventRecordDetailCreate."""
        workout_id = uuid4()

        workout_type = get_unified_workout_type(raw_workout.activityType)

        start_date, end_date = self._extract_dates(
            raw_workout.startTimeInSeconds,
            raw_workout.startTimeInSeconds + raw_workout.durationInSeconds,
        )
        duration_seconds = raw_workout.durationInSeconds

        metrics = self._build_metrics(raw_workout)

        # Garmin sends "unknown" for activities typed into Connect; its brand rules say to show plain
        # "Garmin" whenever the device model isn't known.
        device_name = normalize_device_model(raw_workout.deviceName) or "Garmin"

        zone_offset = offset_to_iso(raw_workout.startTimeOffsetInSeconds)

        record = EventRecordCreate(
            category=EventCategory.WORKOUT,
            type=workout_type.value,
            source_name=device_name,
            device_model=raw_workout.deviceName,
            duration_seconds=duration_seconds,
            start_datetime=start_date,
            end_datetime=end_date,
            zone_offset=zone_offset,
            id=workout_id,
            external_id=str(raw_workout.activityId),  # Convert to str (push sends int)
            source="garmin",
            user_id=user_id,
        )

        detail = EventRecordDetailCreate(
            record_id=workout_id,
            **metrics,
        )

        return record, detail

    def _build_bundles(
        self,
        raw: list[GarminActivityJSON],
        user_id: UUID,
    ) -> Iterable[tuple[EventRecordCreate, EventRecordDetailCreate]]:
        """Build event record payloads for Garmin activities."""
        for raw_workout in raw:
            yield self._normalize_workout(raw_workout, user_id)

    def load_data(
        self,
        db: DbSession,
        user_id: UUID,
        **kwargs: Any,
    ) -> int:
        """No-op: Garmin activity data arrives via webhooks.

        REST/summary endpoints are not used. Historical data is fetched
        via the backfill API which delivers data through webhooks.
        """
        self.logger.info(f"Garmin activities for user {user_id} arrive via webhooks (no REST fetch)")
        return 0

    def get_activity_detail(
        self,
        db: DbSession,
        user_id: UUID,
        activity_id: str,
    ) -> dict:
        """Get detailed activity data from Garmin API."""
        return self._make_api_request(db, user_id, f"/wellness-api/rest/activities/{activity_id}")

    def process_push_activities(
        self,
        db: DbSession,
        activities: list[GarminActivityJSON],
        user_id: UUID,
    ) -> list[WorkoutWrite]:
        """Store activities from an ``activities`` push.

        A workout already stored under the Garmin activity id is updated in place rather than
        inserted again: workouts are otherwise deduplicated on (data source, start, end), which an
        edit or a different device name defeats. The exception is a workout the user edited in
        Garmin Connect: Garmin can deliver (or a retried task can re-process) the original summary
        after the edit, and applying it would undo the edit, so it is left alone.
        """
        device_names = GarminDeviceNames(db, user_id)
        writes: list[WorkoutWrite] = []
        for record, detail in self._build_bundles(activities, user_id):
            record = self._with_canonical_device(record, device_names)
            if record.external_id and _was_edited(user_id, record.external_id):
                stored = event_record_service.get_workout_ids_by_external_id(
                    db, user_id, self.provider_name, record.external_id
                )
                if stored:
                    writes.extend(WorkoutWrite(record_id, WorkoutWriteStatus.UNCHANGED) for record_id in stored)
                    continue
            writes.extend(self._write(db, user_id, record, detail))
        return writes

    def process_manual_updates(
        self,
        db: DbSession,
        activities: list[GarminActivityJSON],
        user_id: UUID,
    ) -> list[WorkoutWrite]:
        """Apply edits the user made in Garmin Connect (``manuallyUpdatedActivities``).

        Garmin re-sends the whole activity summary. The workout stored under the Garmin activity id
        is overwritten in place; an activity we never stored (e.g. recorded before the user connected)
        is created. ``manual`` marks the edit here, not a hand-typed entry, so entry_source is left
        as it was stored.
        """
        device_names = GarminDeviceNames(db, user_id)
        writes: list[WorkoutWrite] = []
        for record, detail in self._build_bundles(activities, user_id):
            record = self._with_canonical_device(record, device_names)
            detail = detail.model_copy(update={"entry_source": None})
            written = self._write(db, user_id, record, detail)
            if record.external_id and any(w.status != WorkoutWriteStatus.CONFLICT for w in written):
                _mark_edited(user_id, record.external_id)
            writes.extend(written)
        return writes

    def _write(
        self, db: DbSession, user_id: UUID, record: EventRecordCreate, detail: EventRecordDetailCreate
    ) -> list[WorkoutWrite]:
        """Update the workouts stored under the activity id, or create the workout if there are none."""
        revision = event_record_service.apply_workout_revision(db, user_id, self.provider_name, record, detail)
        if revision.found:
            return [
                *(WorkoutWrite(record_id, WorkoutWriteStatus.UPDATED) for record_id in revision.updated),
                *(WorkoutWrite(record_id, WorkoutWriteStatus.UNCHANGED) for record_id in revision.unchanged),
                *(WorkoutWrite(record_id, WorkoutWriteStatus.CONFLICT) for record_id in revision.conflicts),
            ]

        created = event_record_service.create(db, record)
        if created.id != record.id:
            # create() found another workout of this device with exactly this span and returned it:
            # this activity was not stored, and that workout is not ours to overwrite.
            log_structured(
                self.logger,
                "warning",
                "Garmin activity collides with another stored workout, not saved",
                provider=self.provider_name,
                action="workout_create_conflict",
                user_id=str(user_id),
                record_id=str(created.id),
            )
            return [WorkoutWrite(created.id, WorkoutWriteStatus.CONFLICT)]
        event_record_service.create_detail(db, detail.model_copy(update={"record_id": created.id}))
        return [WorkoutWrite(created.id, WorkoutWriteStatus.CREATED)]

    @staticmethod
    def _with_canonical_device(record: EventRecordCreate, device_names: GarminDeviceNames) -> EventRecordCreate:
        device_model = device_names.canonical(record.device_model)
        if device_model == record.device_model:
            return record
        return record.model_copy(update={"device_model": device_model, "source_name": device_model or "Garmin"})
