from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from app.config import settings
from app.constants.entry_source import get_unified_strava_entry_source
from app.constants.workout_types import get_unified_strava_workout_type
from app.database import DbSession
from app.schemas.enums import EventCategory, SeriesType, WorkoutType
from app.schemas.model_crud.activities import (
    EventRecordCreate,
    EventRecordDetailCreate,
    EventRecordMetrics,
    TimeSeriesSampleCreate,
)
from app.schemas.providers.strava import (
    ActivityJSON as StravaActivityJSON,
)
from app.schemas.providers.strava import (
    StravaStreamSet,
    StravaStreamSetJSON,
)
from app.services.event_record_service import event_record_service
from app.services.providers.strava.coverage import STREAM_KEY_SERIES_TYPE, STREAM_KEYS_PARAM
from app.services.providers.templates.base_workouts import BaseWorkoutsTemplate
from app.services.timeseries_service import timeseries_service
from app.utils.conversion import kilojoules_to_kcal
from app.utils.dates import offset_to_iso
from app.utils.sentry_helpers import log_and_capture_error
from app.utils.structured_logging import log_structured

# Cap on stored elevation samples per workout. Strava's altitude stream can be
# 10k+ points; this keeps the profile compact for transport while preserving the
# full shape (and the true low/high points, which downsampling always retains).
ELEVATION_PROFILE_MAX_POINTS = 300


def _downsample_elevation_points(points: list[dict[str, Any]], max_points: int) -> list[dict[str, Any]]:
    """Uniformly downsample elevation samples, always keeping the first, last and
    the true min/max-altitude points so the chart's low/high markers stay exact."""
    n = len(points)
    if n <= max_points:
        return points

    altitudes = [p["altitude_m"] for p in points]
    keep: set[int] = {0, n - 1, altitudes.index(min(altitudes)), altitudes.index(max(altitudes))}

    stride = n / max_points
    keep.update(int(k * stride) for k in range(max_points))

    return [points[i] for i in sorted(keep)]


def build_elevation_profile(streams: StravaStreamSetJSON) -> list[dict[str, Any]] | None:
    """Build an ordered elevation-over-distance profile from Strava streams.

    Pairs each altitude sample with its distance (meters) and elapsed time
    (seconds). Returns None when there is no usable altitude data (e.g. indoor
    or manual activities without an altitude stream).
    """
    if streams.altitude is None or not streams.altitude.data:
        return None

    altitudes = streams.altitude.data
    distances = streams.distance.data if streams.distance else []
    times = streams.time.data if streams.time else []

    points: list[dict[str, Any]] = []
    for i, altitude in enumerate(altitudes):
        if altitude is None:
            continue
        distance = distances[i] if i < len(distances) else None
        time_s = times[i] if i < len(times) else None
        points.append(
            {
                "distance_m": round(float(distance), 1) if distance is not None else None,
                "altitude_m": round(float(altitude), 1),
                "time_s": int(time_s) if time_s is not None else None,
            }
        )

    if len(points) < 2:
        return None

    return _downsample_elevation_points(points, ELEVATION_PROFILE_MAX_POINTS)


class StravaWorkouts(BaseWorkoutsTemplate):
    """Strava implementation of workouts template."""

    @property
    def events_per_page(self) -> int:
        """Get the number of events per page."""
        return settings.strava_events_per_page

    def get_workouts(
        self,
        db: DbSession,
        user_id: UUID,
        start_date: datetime,
        end_date: datetime,
    ) -> list[Any]:
        """Get activities from Strava API with page-based pagination.

        Strava API uses epoch timestamps for after/before parameters
        and supports up to 200 activities per page.
        """
        all_activities: list[Any] = []
        page = 1
        per_page = self.events_per_page

        after = int(start_date.timestamp())
        before = int(end_date.timestamp())

        while True:
            params: dict[str, Any] = {
                "after": after,
                "before": before,
                "page": page,
                "per_page": per_page,
            }

            try:
                response = self._make_api_request(
                    db,
                    user_id,
                    # hard-coded value - update with base template changes
                    "/api/v3/athlete/activities",
                    params=params,
                )

                if not isinstance(response, list):
                    break

                all_activities.extend(response)

                # Stop if fewer results than page size (last page)
                if len(response) < per_page:
                    break

                page += 1

            except Exception as e:
                log_structured(
                    self.logger,
                    "error",
                    "Error fetching Strava activities page",
                    provider="strava",
                    action="strava_fetch_page_error",
                    page=page,
                    user_id=str(user_id),
                    error=str(e),
                )
                if all_activities:
                    log_structured(
                        self.logger,
                        "warning",
                        "Returning partial activity data due to error",
                        provider="strava",
                        action="strava_partial_data",
                        activities_count=len(all_activities),
                        user_id=str(user_id),
                        error=str(e),
                    )
                    break
                raise

        return all_activities

    def get_workouts_from_api(self, db: DbSession, user_id: UUID, **kwargs: Any) -> Any:
        """Get activities from Strava API with specific options."""
        page = kwargs.get("page", 1)
        per_page = self.events_per_page

        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
        }

        after = kwargs.get("after")
        before = kwargs.get("before")
        if after:
            params["after"] = int(after)
        if before:
            params["before"] = int(before)

        # hard-coded value - update with base template changes
        return self._make_api_request(db, user_id, "/api/v3/athlete/activities", params=params)

    def get_workout_detail_from_api(self, db: DbSession, user_id: UUID, workout_id: str, **kwargs: Any) -> Any:
        """Get detailed activity data from Strava API."""
        # hard-coded value - update with base template changes
        return self._make_api_request(db, user_id, f"/api/v3/activities/{workout_id}")

    def get_activity_streams(self, db: DbSession, user_id: UUID, activity_id: str) -> Any:
        """Fetch altitude/distance/time streams for an activity (key_by_type)."""
        return self._make_api_request(
            db,
            user_id,
            f"/api/v3/activities/{activity_id}/streams",
            params={"keys": "distance,altitude,time", "key_by_type": "true"},
        )

    def _maybe_fetch_elevation_profile(
        self,
        db: DbSession,
        user_id: UUID,
        raw_workout: StravaActivityJSON,
    ) -> list[dict[str, Any]] | None:
        """Fetch and build the elevation profile for GPS/outdoor activities.

        Skips activities with no GPS or elevation (e.g. indoor workouts) to avoid
        wasted API calls. Network/parse failures are captured but never abort the
        surrounding sync — the workout is still saved with its summary metrics.
        """
        has_gps = raw_workout.map is not None and bool(raw_workout.map.summary_polyline or raw_workout.map.polyline)
        has_elevation = raw_workout.total_elevation_gain is not None or raw_workout.elev_high is not None
        if not (has_gps or has_elevation):
            return None

        try:
            raw_streams = self.get_activity_streams(db, user_id, str(raw_workout.id))
            if not isinstance(raw_streams, dict):
                return None
            return build_elevation_profile(StravaStreamSetJSON(**raw_streams))
        except Exception as e:
            log_and_capture_error(
                e,
                self.logger,
                f"Failed to fetch Strava elevation streams for activity {raw_workout.id}: {e}",
                extra={"activity_id": raw_workout.id, "user_id": str(user_id)},
            )
            return None

    def _extract_dates_from_iso(self, start_iso: str, elapsed_time: int) -> tuple[datetime, datetime]:
        """Extract start and end dates from ISO string and elapsed time."""
        start_date = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        if start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=timezone.utc)
        end_date = start_date + timedelta(seconds=elapsed_time)
        return start_date, end_date

    def _build_metrics(self, raw_workout: StravaActivityJSON) -> EventRecordMetrics:
        """Build metrics from Strava activity data."""
        metrics: EventRecordMetrics = {}

        # Heart rate
        if raw_workout.average_heartrate is not None:
            metrics["heart_rate_avg"] = Decimal(raw_workout.average_heartrate)
        if raw_workout.max_heartrate is not None:
            metrics["heart_rate_max"] = int(raw_workout.max_heartrate)

        # Distance (meters)
        if raw_workout.distance is not None:
            metrics["distance"] = Decimal(raw_workout.distance)

        # Speed (m/s)
        if raw_workout.average_speed is not None:
            metrics["average_speed"] = Decimal(raw_workout.average_speed)
        if raw_workout.max_speed is not None:
            metrics["max_speed"] = Decimal(raw_workout.max_speed)

        # Power (watts)
        if raw_workout.average_watts is not None:
            metrics["average_watts"] = Decimal(raw_workout.average_watts)
        if raw_workout.max_watts is not None:
            metrics["max_watts"] = Decimal(raw_workout.max_watts)

        # Elevation
        if raw_workout.total_elevation_gain is not None:
            metrics["total_elevation_gain"] = Decimal(raw_workout.total_elevation_gain)
        if raw_workout.elev_high is not None:
            metrics["elev_high"] = Decimal(raw_workout.elev_high)
        if raw_workout.elev_low is not None:
            metrics["elev_low"] = Decimal(raw_workout.elev_low)

        # Energy: prefer calories (if available and non-zero), fallback to kilojoules.
        # Strava's list endpoint often returns calories=None, so we fall back to kilojoules.
        # Standard exercise approximation: kcal ≈ kJ (human efficiency ~25% cancels the unit factor).
        if raw_workout.calories is not None and raw_workout.calories > 0:
            metrics["energy_burned"] = Decimal(raw_workout.calories)
        elif raw_workout.kilojoules is not None:
            metrics["energy_burned"] = kilojoules_to_kcal(raw_workout.kilojoules)

        # Moving time
        if raw_workout.moving_time is not None:
            metrics["moving_time_seconds"] = raw_workout.moving_time

        # GPS route — prefer the full-resolution polyline (DetailedActivity),
        # fall back to summary_polyline (present on list/webhook payloads).
        if raw_workout.map is not None:
            polyline = raw_workout.map.polyline or raw_workout.map.summary_polyline
            if polyline:
                metrics["route_polyline"] = polyline

        entry_source = get_unified_strava_entry_source(raw_workout.manual)
        if entry_source is not None:
            metrics["entry_source"] = entry_source

        if raw_workout.name:
            metrics["label"] = raw_workout.name

        return metrics

    def _normalize_workout(
        self,
        raw_workout: StravaActivityJSON,
        user_id: UUID,
    ) -> tuple[EventRecordCreate, EventRecordDetailCreate]:
        """Normalize Strava activity to EventRecordCreate and EventRecordDetailCreate."""
        workout_id = uuid4()

        # Use sport_type for more specific mapping, fallback to type
        workout_type = get_unified_strava_workout_type(raw_workout.sport_type)
        if workout_type is WorkoutType.OTHER:
            workout_type = get_unified_strava_workout_type(raw_workout.type)

        duration_seconds = raw_workout.elapsed_time
        start_date, end_date = self._extract_dates_from_iso(raw_workout.start_date, duration_seconds)

        zone_offset = None
        if raw_workout.utc_offset is not None:
            zone_offset = offset_to_iso(int(raw_workout.utc_offset))

        metrics = self._build_metrics(raw_workout)

        source_name = raw_workout.device_name or "Strava"
        device_model = raw_workout.device_name or None

        record = EventRecordCreate(
            category=EventCategory.WORKOUT,
            type=workout_type.value,
            name=raw_workout.name[:255] if raw_workout.name else None,
            source_name=source_name,
            device_model=device_model,
            duration_seconds=duration_seconds,
            start_datetime=start_date,
            end_datetime=end_date,
            zone_offset=zone_offset,
            id=workout_id,
            external_id=str(raw_workout.id),
            source="strava",
            user_id=user_id,
        )

        detail = EventRecordDetailCreate(
            record_id=workout_id,
            **metrics,
        )

        return record, detail

    def _build_workout_samples(
        self,
        db: DbSession,
        user_id: UUID,
        strava_activity_id: int | str,
        start_dt: datetime,
        zone_offset: str | None,
        device_model: str | None,
    ) -> list[TimeSeriesSampleCreate]:
        """Fetch full-fidelity Strava streams and normalize to TimeSeriesSampleCreate rows."""
        raw = self._make_api_request(
            db,
            user_id,
            f"/api/v3/activities/{strava_activity_id}/streams",
            params={"keys": STREAM_KEYS_PARAM, "key_by_type": "true"},
        )

        # key_by_type=true returns an object keyed by stream type, not a list.
        if not isinstance(raw, dict):
            return []

        streams = StravaStreamSet.model_validate(raw)

        time_data = streams.time.data if streams.time else []
        if not time_data:
            return []

        mapped: list[tuple[list[Any], SeriesType]] = []
        for key, series_type in STREAM_KEY_SERIES_TYPE.items():
            stream = getattr(streams, key)
            if stream and stream.data:
                mapped.append((stream.data, series_type))

        if not mapped:
            return []

        result: list[TimeSeriesSampleCreate] = []
        for i, offset in enumerate(time_data):
            if offset is None:
                continue
            recorded_at = start_dt + timedelta(seconds=int(offset))
            for data, series_type in mapped:
                if i >= len(data):
                    continue
                value = data[i]
                if value is None:
                    continue
                result.append(
                    TimeSeriesSampleCreate(
                        id=uuid4(),
                        user_id=user_id,
                        source="strava",
                        device_model=device_model,
                        recorded_at=recorded_at,
                        zone_offset=zone_offset,
                        value=Decimal(str(value)),
                        series_type=series_type,
                    )
                )
        return result

    def _ingest_workout_streams(
        self,
        db: DbSession,
        activity: StravaActivityJSON,
        user_id: UUID,
        record: EventRecordCreate,
    ) -> int:
        """Fetch + persist per-sample streams on workout arrival, flag-gated and failure-isolated."""
        if not settings.ingest_workout_samples:
            return 0
        try:
            samples = self._build_workout_samples(
                db,
                user_id,
                activity.id,
                record.start_datetime,
                record.zone_offset,
                activity.device_name or None,
            )
        except Exception as exc:
            log_and_capture_error(
                exc,
                self.logger,
                "Failed to fetch Strava workout streams, skipping samples",
                extra={"activity_id": activity.id},
            )
            return 0
        if not samples:
            return 0
        # Savepoint so a failed bulk insert rolls back only the samples and
        # leaves the workout's outer transaction usable (no PendingRollbackError).
        nested = db.begin_nested()
        try:
            timeseries_service.bulk_create_samples(db, samples)
            nested.commit()
            return len(samples)
        except Exception as exc:
            nested.rollback()
            log_and_capture_error(
                exc,
                self.logger,
                "Strava workout sample ingestion failed; continuing",
                extra={"activity_id": activity.id, "sample_count": len(samples)},
            )
            return 0

    def _save_detail_with_gps(
        self,
        db: DbSession,
        record_id: UUID,
        detail: EventRecordDetailCreate,
        profile: list[dict[str, Any]] | None,
    ) -> None:
        """Persist a workout's detail, attaching the elevation profile.

        For new workouts create_detail inserts everything. For workouts that
        already exist, create_detail is a no-op (handle_duplicates), so we also
        backfill the GPS-derived columns (route_polyline, elevation_profile) so a
        re-sync refreshes them on previously-synced workouts.
        """
        update: dict[str, Any] = {"record_id": record_id}
        if profile:
            update["elevation_profile"] = profile
        detail_for_record = detail.model_copy(update=update)
        event_record_service.create_detail(db, detail_for_record)

        gps_fields: dict[str, Any] = {}
        if profile:
            gps_fields["elevation_profile"] = profile
        if detail_for_record.route_polyline:
            gps_fields["route_polyline"] = detail_for_record.route_polyline
        event_record_service.backfill_workout_detail_fields(db, record_id, gps_fields)

    def load_data(
        self,
        db: DbSession,
        user_id: UUID,
        **kwargs: Any,
    ) -> int:
        """Load data from Strava API (historical backfill).

        Fetches all activities in a date range using page-based pagination.
        """
        # Get start/end dates from kwargs
        start = kwargs.get("start") or kwargs.get("start_date")
        end = kwargs.get("end") or kwargs.get("end_date")

        # Default to last 30 days if no dates provided
        if not start:
            start_dt = datetime.now(timezone.utc) - timedelta(days=30)
        elif isinstance(start, datetime):
            start_dt = start
        elif isinstance(start, str):
            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        else:
            start_dt = datetime.now(timezone.utc) - timedelta(days=30)

        if not end:
            end_dt = datetime.now(timezone.utc)
        elif isinstance(end, datetime):
            end_dt = end
        elif isinstance(end, str):
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        else:
            end_dt = datetime.now(timezone.utc)

        # Fetch all activities
        raw_activities = self.get_workouts(db, user_id, start_dt, end_dt)

        # Parse and save
        parsed_activities = []
        for raw in raw_activities:
            try:
                activity = StravaActivityJSON(**raw) if isinstance(raw, dict) else raw
                parsed_activities.append(activity)
            except Exception as e:
                log_structured(
                    self.logger,
                    "warning",
                    "Failed to parse Strava activity",
                    provider="strava",
                    action="strava_parse_error",
                    user_id=str(user_id),
                    error=str(e),
                )

        count = 0
        for activity in parsed_activities:
            record, detail = self._normalize_workout(activity, user_id)
            created_record = event_record_service.create(db, record)
            profile = self._maybe_fetch_elevation_profile(db, user_id, activity)
            self._save_detail_with_gps(db, created_record.id, detail, profile)
            count += 1
            self._ingest_workout_streams(db, activity, user_id, record)

        return count

    def process_push_activity(
        self,
        db: DbSession,
        activity: StravaActivityJSON,
        user_id: UUID,
    ) -> list[UUID]:
        """Process a single activity from webhook and save to database.

        Args:
            db: Database session
            activity: Parsed Strava activity data
            user_id: Internal user ID (already mapped from Strava athlete ID)

        Returns:
            List of created event record IDs
        """
        created_ids: list[UUID] = []

        record, detail = self._normalize_workout(activity, user_id)
        created_record = event_record_service.create(db, record)
        profile = self._maybe_fetch_elevation_profile(db, user_id, activity)
        self._save_detail_with_gps(db, created_record.id, detail, profile)
        created_ids.append(created_record.id)
        self._ingest_workout_streams(db, activity, user_id, record)

        return created_ids
