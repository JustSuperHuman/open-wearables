"""Push workouts, workout schedules and courses to a user's Garmin Connect account.

Bodies are Garmin's own Training API V2 workout and Courses API course objects. OW passes them
through after a light check of the limits Garmin documents, so a payload that can never succeed
fails fast with a clear message instead of costing one of the user's 200 daily Garmin calls.
Everything else (field values, exercise names, target types) is left for Garmin to judge.

Every public method runs inside a time budget (GARMIN_WRITE_TIMEOUT_SECONDS) covering all the
Garmin calls it makes.
"""

import datetime as dt
import logging
import time
from collections.abc import Callable
from functools import wraps
from typing import Any
from uuid import UUID

from app.config import settings
from app.database import DbSession
from app.models import UserConnection
from app.repositories import UserConnectionRepository
from app.schemas.auth import ConnectionStatus
from app.schemas.providers.garmin.training import (
    GarminCourseResult,
    GarminPermissionsRead,
    GarminScheduleResult,
    GarminWorkoutResult,
)
from app.services.providers.garmin.permissions import apply_permissions
from app.services.providers.garmin.write_client import (
    COURSE_IMPORT,
    PROVIDER,
    WORKOUT_IMPORT,
    GarminApiError,
    GarminErrorCode,
    GarminWriteClient,
    stored_permissions,
    time_budget,
)
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

WORKOUT_CREATE_PATH = "/workoutportal/workout/v2"
WORKOUT_PATH = "/training-api/workout/v2/{workout_id}"
SCHEDULE_CREATE_PATH = "/training-api/schedule/"
SCHEDULE_PATH = "/training-api/schedule/{schedule_id}"
COURSE_CREATE_PATH = "/training-api/courses/v1/course"
COURSE_PATH = "/training-api/courses/v1/course/{course_id}"

# Training API V2 limits.
MULTI_SPORT = "MULTI_SPORT"
MAX_STEPS_SINGLE_SPORT = 100
MAX_STEPS_MULTI_SPORT = 250
MAX_SEGMENTS_MULTI_SPORT = 25
MAX_PROVIDER_FIELD_LENGTH = 20
PROVIDER_FIELDS = ("workoutProvider", "workoutSourceId")

# Courses API: these may not be null.
REQUIRED_COURSE_NUMBERS = ("distance", "elevationGain", "elevationLoss")
# Courses API: "around 10,000" geoPoints per course.
MAX_COURSE_GEO_POINTS = 10_000

# Statuses under which Garmin still holds a grant for the connection.
_CONNECTED_STATUSES = (ConnectionStatus.ACTIVE,)


def _invalid(detail: str) -> GarminApiError:
    return GarminApiError(GarminErrorCode.VALIDATION_ERROR, detail)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def count_workout_steps(steps: Any) -> int:
    """Count steps the way the device stores them: a repeat block is a step, and so is each step inside it."""
    if not isinstance(steps, list):
        return 0
    return sum(1 + count_workout_steps(step.get("steps")) for step in steps if isinstance(step, dict))


def validate_workout(workout: dict[str, Any]) -> None:
    """Reject workouts Garmin is documented to refuse: no sport, or more steps/segments than it allows."""
    sport = workout.get("sport")
    if not isinstance(sport, str) or not sport.strip():
        raise _invalid("Workout must set sport, e.g. RUNNING, CYCLING, LAP_SWIMMING, STRENGTH_TRAINING or MULTI_SPORT.")

    segments = workout.get("segments")
    if segments is not None and not isinstance(segments, list):
        raise _invalid("Workout segments must be a list.")
    segments = [s for s in segments or [] if isinstance(s, dict)]

    steps = sum(count_workout_steps(segment.get("steps")) for segment in segments)
    steps += count_workout_steps(workout.get("steps"))

    if sport.strip().upper() == MULTI_SPORT:
        if len(segments) > MAX_SEGMENTS_MULTI_SPORT:
            raise _invalid(
                f"Multisport workouts allow at most {MAX_SEGMENTS_MULTI_SPORT} segments; this one has {len(segments)}."
            )
        if steps > MAX_STEPS_MULTI_SPORT:
            raise _invalid(f"Multisport workouts allow at most {MAX_STEPS_MULTI_SPORT} steps; this one has {steps}.")
    else:
        if len(segments) > 1:
            raise _invalid(
                f"A single-sport workout has one segment; this one has {len(segments)}. "
                f"Use sport {MULTI_SPORT} for a workout with several sports."
            )
        if steps > MAX_STEPS_SINGLE_SPORT:
            raise _invalid(f"Single-sport workouts allow at most {MAX_STEPS_SINGLE_SPORT} steps; this one has {steps}.")

    for field in PROVIDER_FIELDS:
        value = workout.get(field)
        if value is not None and (not isinstance(value, str) or len(value) > MAX_PROVIDER_FIELD_LENGTH):
            raise _invalid(f"{field} must be a string of at most {MAX_PROVIDER_FIELD_LENGTH} characters.")


def with_provider_attribution(workout: dict[str, Any]) -> dict[str, Any]:
    """Fill workoutProvider (shown to the user) and workoutSourceId when the caller left them out.

    Garmin wants workoutSourceId to match workoutProvider unless there's a reason not to.
    """
    result = dict(workout)
    if not result.get("workoutProvider"):
        result["workoutProvider"] = settings.garmin_workout_provider
    if not result.get("workoutSourceId"):
        result["workoutSourceId"] = result["workoutProvider"]
    return result


def validate_course(course: dict[str, Any]) -> None:
    """Reject courses missing the fields the Courses API says cannot be empty or null."""
    name = course.get("courseName")
    if not isinstance(name, str) or not name.strip():
        raise _invalid("Course must set a non-empty courseName.")
    geo_points = course.get("geoPoints")
    if not isinstance(geo_points, list) or not geo_points:
        raise _invalid("Course must include at least one geoPoint.")
    if len(geo_points) > MAX_COURSE_GEO_POINTS:
        raise _invalid(
            f"Garmin accepts at most {MAX_COURSE_GEO_POINTS:,} geoPoints per course; this one has {len(geo_points):,}. "
            "Thin the track (Garmin suggests one point every 100 m at most)."
        )
    missing = [f for f in REQUIRED_COURSE_NUMBERS if not _is_number(course.get(f))]
    if missing:
        raise _invalid(f"Course must set {', '.join(missing)} as numbers (meters; Garmin does not accept null).")


def _as_int_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _id_from_body(body: Any, *keys: str) -> int | None:
    """Garmin returns created objects as JSON, but a bare id is accepted too."""
    if isinstance(body, dict):
        for key in keys:
            if (found := _as_int_id(body.get(key))) is not None:
                return found
        return None
    return _as_int_id(body)


def _within_time_budget[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    """Run the method's Garmin calls under one GARMIN_WRITE_TIMEOUT_SECONDS budget."""

    @wraps(method)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        with time_budget(settings.garmin_write_timeout_seconds):
            return method(*args, **kwargs)

    return wrapper


class GarminTrainingService:
    """Workouts, workout schedules and courses for one Garmin user, plus their permissions."""

    def __init__(self, client: GarminWriteClient, connection_repo: UserConnectionRepository) -> None:
        self.client = client
        self.connection_repo = connection_repo

    # ------------------------------------------------------------------
    # Permissions
    # ------------------------------------------------------------------

    @_within_time_budget
    def get_permissions(self, db: DbSession, user_id: UUID) -> GarminPermissionsRead:
        """Live permissions from Garmin, falling back to the stored ones when Garmin can't be reached."""
        connection = self.connection_repo.get_by_user_and_provider(db, user_id, PROVIDER)
        if connection is None or connection.status not in _CONNECTED_STATUSES or not connection.access_token:
            return self._not_connected(connection)

        try:
            permissions = self._live_permissions(db, user_id, connection)
        except GarminApiError as exc:
            if exc.code != GarminErrorCode.NOT_CONNECTED:
                raise
            # The live check found the authorization gone and marked the connection; answer with
            # the connection as it stands now rather than the "connected" we started from.
            db.refresh(connection)
            if connection.status in _CONNECTED_STATUSES:
                raise
            return self._not_connected(connection)

        if permissions is None:
            permissions = stored_permissions(connection.scope) or []
        return GarminPermissionsRead(
            connected=True,
            status=ConnectionStatus(connection.status),
            garmin_user_id=connection.provider_user_id,
            permissions=permissions,
        )

    @staticmethod
    def _not_connected(connection: UserConnection | None) -> GarminPermissionsRead:
        return GarminPermissionsRead(
            connected=False,
            status=ConnectionStatus(connection.status) if connection else None,
            garmin_user_id=connection.provider_user_id if connection else None,
            permissions=[],
        )

    def _live_permissions(self, db: DbSession, user_id: UUID, connection: UserConnection) -> list[str] | None:
        """Fetch permissions from Garmin and store them on the connection.

        Returns None only when Garmin couldn't be reached (transport error, Garmin 5xx, a failing
        token endpoint): then the stored permissions are the best answer. Everything else, a rate
        limit or a dead authorization included, is raised to the caller.
        """
        try:
            permissions = self.client.fetch_permissions(db, user_id)
        except GarminApiError as exc:
            if exc.code != GarminErrorCode.UPSTREAM_ERROR:
                raise
            log_structured(
                logger,
                "info",
                "Using stored Garmin permissions, Garmin could not be reached",
                provider=PROVIDER,
                action="garmin_permissions_fallback",
                user_id=str(user_id),
                code=exc.code.value,
                garmin_status=exc.garmin_status,
            )
            return None

        # Same store (and ordering against late pushes) the userPermissionsChange webhook uses.
        apply_permissions(db, self.connection_repo, connection, permissions, as_of=time.time())
        return permissions

    def _require_permission(self, db: DbSession, user_id: UUID, connection: UserConnection, permission: str) -> None:
        """Fail with garmin_permission_missing when the user hasn't granted ``permission``.

        A stored scope can be stale (a missed userPermissionsChange push), so a stored "no" is
        confirmed live before refusing. Unknown stored permissions go straight to Garmin, which
        answers 412 if the permission really is missing. A live check that can't be completed
        (rate limit, dead authorization) fails the request with that error, never with a "no".
        """
        stored = stored_permissions(connection.scope)
        if stored is None or permission in stored:
            return
        live = self._live_permissions(db, user_id, connection)
        if permission in (live if live is not None else stored):
            return
        raise GarminApiError(
            GarminErrorCode.PERMISSION_MISSING,
            f"The user hasn't granted {permission} to this app in Garmin Connect.",
        )

    def _connection_with(self, db: DbSession, user_id: UUID, permission: str) -> UserConnection:
        connection = self.client.get_active_connection(db, user_id)
        self._require_permission(db, user_id, connection, permission)
        return connection

    # ------------------------------------------------------------------
    # Workouts
    # ------------------------------------------------------------------

    @_within_time_budget
    def create_workout(self, db: DbSession, user_id: UUID, workout: dict[str, Any]) -> GarminWorkoutResult:
        connection = self.client.get_active_connection(db, user_id)
        validate_workout(workout)
        self._require_permission(db, user_id, connection, WORKOUT_IMPORT)

        response = self.client.request(
            db, user_id, "POST", WORKOUT_CREATE_PATH, json_body=with_provider_attribution(workout)
        )
        workout_id = _id_from_body(response.body, "workoutId")
        stored = response.body if isinstance(response.body, dict) else None
        if workout_id is None:
            self._log_missing_id(user_id, "workoutId", response.body)
        self._log_write(user_id, "garmin_workout_created", workout_id=workout_id)
        return GarminWorkoutResult(workout_id=workout_id, workout=stored)

    @_within_time_budget
    def update_workout(
        self, db: DbSession, user_id: UUID, workout_id: int, workout: dict[str, Any]
    ) -> GarminWorkoutResult:
        """Replace a workout with a full new version.

        Garmin requires ownerId on updates, and a PUT replaces workoutProvider too. When the body
        lacks ownerId or workoutProvider, the current workout is read first and its values kept;
        the GARMIN_WORKOUT_PROVIDER default only fills a provider the workout never had.
        """
        connection = self.client.get_active_connection(db, user_id)
        validate_workout(workout)
        self._require_permission(db, user_id, connection, WORKOUT_IMPORT)

        path = WORKOUT_PATH.format(workout_id=workout_id)
        payload = dict(workout)
        payload["workoutId"] = workout_id
        if payload.get("ownerId") is None or not payload.get("workoutProvider"):
            current = self.client.request(db, user_id, "GET", path).body
            if isinstance(current, dict):
                if payload.get("ownerId") is None and current.get("ownerId") is not None:
                    payload["ownerId"] = current["ownerId"]
                if not payload.get("workoutProvider"):
                    for field in PROVIDER_FIELDS:
                        if not payload.get(field) and current.get(field):
                            payload[field] = current[field]
        payload = with_provider_attribution(payload)

        response = self.client.request(db, user_id, "PUT", path, json_body=payload)
        # Garmin may answer an update with an empty 204; the payload it accepted is the workout then.
        stored = response.body if isinstance(response.body, dict) else payload
        self._log_write(user_id, "garmin_workout_updated", workout_id=workout_id)
        return GarminWorkoutResult(workout_id=workout_id, workout=stored)

    @_within_time_budget
    def delete_workout(self, db: DbSession, user_id: UUID, workout_id: int) -> None:
        self._connection_with(db, user_id, WORKOUT_IMPORT)
        self._delete(db, user_id, WORKOUT_PATH.format(workout_id=workout_id))
        self._log_write(user_id, "garmin_workout_deleted", workout_id=workout_id)

    # ------------------------------------------------------------------
    # Schedules
    # ------------------------------------------------------------------

    @_within_time_budget
    def create_schedule(self, db: DbSession, user_id: UUID, workout_id: int, day: dt.date) -> GarminScheduleResult:
        self._connection_with(db, user_id, WORKOUT_IMPORT)
        response = self.client.request(
            db,
            user_id,
            "POST",
            SCHEDULE_CREATE_PATH,
            json_body={"workoutId": workout_id, "date": day.isoformat()},
        )
        schedule_id = _id_from_body(response.body, "scheduleId", "workoutScheduleId", "id")
        if schedule_id is None:
            self._log_missing_id(user_id, "scheduleId", response.body)
        self._log_write(user_id, "garmin_schedule_created", workout_id=workout_id, schedule_id=schedule_id)
        return GarminScheduleResult(schedule_id=schedule_id)

    @_within_time_budget
    def delete_schedule(self, db: DbSession, user_id: UUID, schedule_id: int) -> None:
        self._connection_with(db, user_id, WORKOUT_IMPORT)
        self._delete(db, user_id, SCHEDULE_PATH.format(schedule_id=schedule_id))
        self._log_write(user_id, "garmin_schedule_deleted", schedule_id=schedule_id)

    # ------------------------------------------------------------------
    # Courses
    # ------------------------------------------------------------------

    @_within_time_budget
    def create_course(self, db: DbSession, user_id: UUID, course: dict[str, Any]) -> GarminCourseResult:
        connection = self.client.get_active_connection(db, user_id)
        validate_course(course)
        self._require_permission(db, user_id, connection, COURSE_IMPORT)

        response = self.client.request(db, user_id, "POST", COURSE_CREATE_PATH, json_body=course)
        course_id = _id_from_body(response.body, "courseId")
        stored = response.body if isinstance(response.body, dict) else None
        if course_id is None:
            self._log_missing_id(user_id, "courseId", response.body)
        self._log_write(user_id, "garmin_course_created", course_id=course_id)
        return GarminCourseResult(course_id=course_id, course=stored)

    @_within_time_budget
    def delete_course(self, db: DbSession, user_id: UUID, course_id: int) -> None:
        self._connection_with(db, user_id, COURSE_IMPORT)
        self._delete(db, user_id, COURSE_PATH.format(course_id=course_id))
        self._log_write(user_id, "garmin_course_deleted", course_id=course_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _delete(self, db: DbSession, user_id: UUID, path: str) -> None:
        """Delete on Garmin. Already gone (404) counts as deleted, so retried deletes stay safe."""
        try:
            self.client.request(db, user_id, "DELETE", path)
        except GarminApiError as exc:
            if exc.code != GarminErrorCode.NOT_FOUND:
                raise

    @staticmethod
    def _log_missing_id(user_id: UUID, id_field: str, body: Any) -> None:
        # The object exists at Garmin now; answering with an error would invite a retry that
        # creates it a second time, so the caller gets what Garmin returned instead.
        log_structured(
            logger,
            "warning",
            "Garmin accepted a create but returned no id",
            provider=PROVIDER,
            action="garmin_create_without_id",
            user_id=str(user_id),
            id_field=id_field,
            body_type=type(body).__name__,
        )

    @staticmethod
    def _log_write(user_id: UUID, action: str, **ids: int | None) -> None:
        log_structured(
            logger,
            "info",
            "Garmin write succeeded",
            provider=PROVIDER,
            action=action,
            user_id=str(user_id),
            **ids,
        )
