"""Request/response schemas for pushing workouts, schedules and courses to Garmin Connect.

Workout and course bodies are Garmin's own objects (Training API V2 / Courses API) passed through
as-is, so they are typed as plain JSON objects here rather than re-modelled.
"""

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.auth import ConnectionStatus


class GarminPermissionsRead(BaseModel):
    connected: bool = Field(description="Whether OW can call Garmin for this user (an active Garmin connection)")
    status: ConnectionStatus | None = Field(
        None,
        description="The Garmin connection's status: active; expired when Garmin stopped accepting OW's tokens "
        "(pushed data is still stored, but writes need the user to reconnect Garmin); revoked after a "
        "disconnect or deregistration. null when the user never connected Garmin.",
    )
    garmin_user_id: str | None = Field(None, description="Garmin's API user id for this connection")
    permissions: list[str] = Field(
        default_factory=list,
        description="Permissions the user grants this app, e.g. WORKOUT_IMPORT, COURSE_IMPORT, ACTIVITY_EXPORT",
    )


class GarminWorkoutResult(BaseModel):
    workout_id: int | None = Field(
        description="Garmin workoutId. null only if Garmin created the workout without returning an id; "
        "do not retry the create in that case, it exists in Garmin Connect."
    )
    workout: dict[str, Any] | None = Field(description="The workout as Garmin stored it")


class GarminScheduleCreate(BaseModel):
    workout_id: int = Field(gt=0, description="Garmin workoutId of a workout already pushed for this user")
    date: dt.date = Field(description="Calendar day to schedule the workout on (YYYY-MM-DD)")


class GarminScheduleResult(BaseModel):
    schedule_id: int | None = Field(
        description="Garmin workout schedule id. null only if Garmin created the schedule without returning an id."
    )


class GarminCourseResult(BaseModel):
    course_id: int | None = Field(
        description="Garmin courseId. null only if Garmin created the course without returning an id; "
        "do not retry the create in that case, it exists in Garmin Connect."
    )
    course: dict[str, Any] | None = Field(description="The course as Garmin stored it")


class GarminErrorResponse(BaseModel):
    detail: str = Field(description="What went wrong, including Garmin's own message where it sent one")
    code: str = Field(
        description="garmin_not_connected | garmin_permission_missing | garmin_forbidden | garmin_not_found | "
        "garmin_rate_limited | garmin_validation_error | garmin_upstream_error | garmin_busy | "
        "idempotency_conflict | unauthorized"
    )
