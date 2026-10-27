"""Request/response schemas for pushing workouts, schedules and courses to Garmin Connect.

Workout and course bodies are Garmin's own objects (Training API V2 / Courses API) passed through
as-is, so they are typed as plain JSON objects here rather than re-modelled.
"""

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field


class GarminPermissionsRead(BaseModel):
    connected: bool = Field(description="Whether the user has an active Garmin connection")
    garmin_user_id: str | None = Field(None, description="Garmin's API user id for this connection")
    permissions: list[str] = Field(
        default_factory=list,
        description="Permissions the user grants this app, e.g. WORKOUT_IMPORT, COURSE_IMPORT, ACTIVITY_EXPORT",
    )


class GarminWorkoutResult(BaseModel):
    workout_id: int = Field(description="Garmin workoutId")
    workout: dict[str, Any] = Field(description="The workout as Garmin stored it")


class GarminScheduleCreate(BaseModel):
    workout_id: int = Field(gt=0, description="Garmin workoutId of a workout already pushed for this user")
    date: dt.date = Field(description="Calendar day to schedule the workout on (YYYY-MM-DD)")


class GarminScheduleResult(BaseModel):
    schedule_id: int = Field(description="Garmin workout schedule id")


class GarminCourseResult(BaseModel):
    course_id: int = Field(description="Garmin courseId")
    course: dict[str, Any] = Field(description="The course as Garmin stored it")


class GarminErrorResponse(BaseModel):
    detail: str = Field(description="What went wrong, including Garmin's own message where it sent one")
    code: str = Field(
        description="garmin_not_connected | garmin_permission_missing | garmin_rate_limited | "
        "garmin_validation_error | garmin_upstream_error"
    )
