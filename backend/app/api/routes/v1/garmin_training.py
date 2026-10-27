"""Push workouts, workout schedules and courses to a user's Garmin Connect account.

Integrators hold no Garmin tokens; OW makes every Garmin write for them with the stored
connection. Every failure on these routes, request validation included, is answered as
``{"detail": str, "code": str}`` so a client can branch on ``code`` alone.
"""

from collections.abc import Callable, Coroutine
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from app.database import DbSession
from app.schemas.providers.garmin.training import (
    GarminCourseResult,
    GarminErrorResponse,
    GarminPermissionsRead,
    GarminScheduleCreate,
    GarminScheduleResult,
    GarminWorkoutResult,
)
from app.services import ApiKeyDep
from app.services.providers.garmin.strategy import GarminStrategy
from app.services.providers.garmin.training import GarminTrainingService
from app.services.providers.garmin.write_client import GarminApiError, GarminErrorCode


def _error_response(exc: GarminApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": exc.code.value},
        headers={"Retry-After": exc.retry_after} if exc.retry_after else None,
    )


def _describe_validation_error(exc: RequestValidationError) -> str:
    problems = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
        problems.append(f"{location}: {error.get('msg')}" if location else str(error.get("msg")))
    return "Invalid request: " + "; ".join(problems)


class GarminRoute(APIRoute):
    """Turns Garmin and request-validation failures into the routes' ``{"detail", "code"}`` errors."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def garmin_route_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except GarminApiError as exc:
                return _error_response(exc)
            except RequestValidationError as exc:
                return _error_response(
                    GarminApiError(GarminErrorCode.VALIDATION_ERROR, _describe_validation_error(exc))
                )

        return garmin_route_handler


router = APIRouter(route_class=GarminRoute)


def get_garmin_training_service() -> GarminTrainingService:
    return GarminStrategy().training


GarminTrainingDep = Annotated[GarminTrainingService, Depends(get_garmin_training_service)]

_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": GarminErrorResponse, "description": "garmin_permission_missing"},
    404: {"model": GarminErrorResponse, "description": "garmin_not_connected"},
    422: {"model": GarminErrorResponse, "description": "garmin_validation_error"},
    429: {"model": GarminErrorResponse, "description": "garmin_rate_limited (Retry-After passed on from Garmin)"},
    502: {"model": GarminErrorResponse, "description": "garmin_upstream_error"},
}

_WORKOUT_EXAMPLE: dict[str, Any] = {
    "workoutName": "Tempo Tuesday",
    "description": "15 min easy, 4 x 5 min at tempo, 10 min easy",
    "sport": "RUNNING",
    "segments": [
        {
            "segmentOrder": 1,
            "sport": "RUNNING",
            "steps": [
                {
                    "type": "WorkoutStep",
                    "stepOrder": 1,
                    "intensity": "WARMUP",
                    "durationType": "TIME",
                    "durationValue": 900,
                    "targetType": "OPEN",
                },
                {
                    "type": "WorkoutRepeatStep",
                    "stepOrder": 2,
                    "repeatType": "REPEAT_UNTIL_STEPS_CMPLT",
                    "repeatValue": 4,
                    "steps": [
                        {
                            "type": "WorkoutStep",
                            "stepOrder": 3,
                            "intensity": "INTERVAL",
                            "durationType": "TIME",
                            "durationValue": 300,
                            "targetType": "PACE",
                            "targetValueLow": 3.58,
                            "targetValueHigh": 3.83,
                        },
                        {
                            "type": "WorkoutStep",
                            "stepOrder": 4,
                            "intensity": "RECOVERY",
                            "durationType": "TIME",
                            "durationValue": 90,
                            "targetType": "OPEN",
                        },
                    ],
                },
                {
                    "type": "WorkoutStep",
                    "stepOrder": 5,
                    "intensity": "COOLDOWN",
                    "durationType": "TIME",
                    "durationValue": 600,
                    "targetType": "OPEN",
                },
            ],
        }
    ],
}

_COURSE_EXAMPLE: dict[str, Any] = {
    "courseName": "Riverside loop",
    "description": "Flat 5 mi loop along the river path",
    "distance": 8046.7,
    "elevationGain": 31.0,
    "elevationLoss": 31.0,
    "activityType": "RUNNING",
    "coordinateSystem": "WGS84",
    "geoPoints": [
        {"latitude": 38.8977, "longitude": -77.0365, "elevation": 17.0},
        {"latitude": 38.8985, "longitude": -77.0352, "elevation": 18.5},
    ],
}

WorkoutBody = Annotated[
    dict[str, Any],
    Body(
        description="A Garmin Training API V2 workout object. workoutProvider and workoutSourceId default to "
        "GARMIN_WORKOUT_PROVIDER when left out.",
        openapi_examples={"tempo_run": {"summary": "Single-sport running workout", "value": _WORKOUT_EXAMPLE}},
    ),
]

CourseBody = Annotated[
    dict[str, Any],
    Body(
        description="A Garmin Courses API course object.",
        openapi_examples={"loop": {"summary": "Running course", "value": _COURSE_EXAMPLE}},
    ),
]


@router.get("/users/{user_id}/garmin/permissions", responses=_ERRORS)
def get_garmin_permissions(
    user_id: UUID,
    db: DbSession,
    _api_key: ApiKeyDep,
    training: GarminTrainingDep,
) -> GarminPermissionsRead:
    """Permissions the user grants this app in Garmin Connect.

    Read live from Garmin and stored on the connection. When Garmin can't be reached, the stored
    permissions (kept current by Garmin's userPermissionsChange pushes) are returned instead. A user
    without an active Garmin connection gets `connected: false` and no permissions.
    """
    return training.get_permissions(db, user_id)


@router.post(
    "/users/{user_id}/garmin/workouts",
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
def create_garmin_workout(
    user_id: UUID,
    workout: WorkoutBody,
    db: DbSession,
    _api_key: ApiKeyDep,
    training: GarminTrainingDep,
) -> GarminWorkoutResult:
    """Create a workout in the user's Garmin Connect account (needs WORKOUT_IMPORT).

    Checked before sending: `sport` is set, at most 100 steps for a single-sport workout, and at
    most 25 segments and 250 steps for `MULTI_SPORT`. Repeat blocks count as steps.
    """
    return training.create_workout(db, user_id, workout)


@router.put("/users/{user_id}/garmin/workouts/{workout_id}", responses=_ERRORS)
def update_garmin_workout(
    user_id: UUID,
    workout_id: int,
    workout: WorkoutBody,
    db: DbSession,
    _api_key: ApiKeyDep,
    training: GarminTrainingDep,
) -> GarminWorkoutResult:
    """Replace a workout with a full new version (needs WORKOUT_IMPORT).

    Garmin requires `ownerId` on updates. Send the one from the create response; when it is left
    out, OW reads it from Garmin first, which costs one extra Garmin call.
    """
    return training.update_workout(db, user_id, workout_id, workout)


@router.delete(
    "/users/{user_id}/garmin/workouts/{workout_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_ERRORS,
)
def delete_garmin_workout(
    user_id: UUID,
    workout_id: int,
    db: DbSession,
    _api_key: ApiKeyDep,
    training: GarminTrainingDep,
) -> Response:
    """Delete a workout from Garmin Connect. A workout Garmin no longer has counts as deleted."""
    training.delete_workout(db, user_id, workout_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/users/{user_id}/garmin/schedules",
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
def create_garmin_schedule(
    user_id: UUID,
    schedule: GarminScheduleCreate,
    db: DbSession,
    _api_key: ApiKeyDep,
    training: GarminTrainingDep,
) -> GarminScheduleResult:
    """Put a pushed workout on the user's Garmin calendar for one day (needs WORKOUT_IMPORT)."""
    return training.create_schedule(db, user_id, schedule.workout_id, schedule.date)


@router.delete(
    "/users/{user_id}/garmin/schedules/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_ERRORS,
)
def delete_garmin_schedule(
    user_id: UUID,
    schedule_id: int,
    db: DbSession,
    _api_key: ApiKeyDep,
    training: GarminTrainingDep,
) -> Response:
    """Remove a scheduled workout from the user's Garmin calendar. Already gone counts as deleted."""
    training.delete_schedule(db, user_id, schedule_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/users/{user_id}/garmin/courses",
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
def create_garmin_course(
    user_id: UUID,
    course: CourseBody,
    db: DbSession,
    _api_key: ApiKeyDep,
    training: GarminTrainingDep,
) -> GarminCourseResult:
    """Create a course (route) in the user's Garmin Connect account (needs COURSE_IMPORT).

    Checked before sending: a non-empty `courseName`, at least one geoPoint, and numeric
    `distance`, `elevationGain` and `elevationLoss`, which Garmin does not accept as null.
    """
    return training.create_course(db, user_id, course)


@router.delete(
    "/users/{user_id}/garmin/courses/{course_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_ERRORS,
)
def delete_garmin_course(
    user_id: UUID,
    course_id: int,
    db: DbSession,
    _api_key: ApiKeyDep,
    training: GarminTrainingDep,
) -> Response:
    """Delete a course from Garmin Connect. A course Garmin no longer has counts as deleted."""
    training.delete_course(db, user_id, course_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
