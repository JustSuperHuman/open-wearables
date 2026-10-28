"""Push workouts, workout schedules and courses to a user's Garmin Connect account.

Integrators hold no Garmin tokens; OW makes every Garmin write for them with the stored
connection. Every failure on these routes, authentication and request validation included, is
answered as ``{"detail": str, "code": str}`` so a client can branch on ``code`` alone.

Garmin calls block, and Garmin can be slow. The routes therefore run their work on a thread pool
of their own (GARMIN_WRITE_CONCURRENCY threads), not on the shared one that also serves webhook
receivers and every other sync route, and each request has a total time budget
(GARMIN_WRITE_TIMEOUT_SECONDS). POST routes accept an ``Idempotency-Key`` so a client retrying
after a timeout gets the first result instead of a second workout, schedule or course.
"""

import logging
from collections.abc import Callable, Coroutine, Mapping
from typing import Annotated, Any
from uuid import UUID

import anyio
from anyio.lowlevel import RunVar
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from redis.exceptions import RedisError

from app.config import settings
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
from app.services.idempotency import (
    MAX_KEY_LENGTH,
    IdempotencyConflictError,
    IdempotencyConflictReason,
    IdempotencyStore,
    request_fingerprint,
)
from app.services.providers.garmin.strategy import GarminStrategy
from app.services.providers.garmin.training import GarminTrainingService
from app.services.providers.garmin.write_client import GarminApiError, GarminErrorCode
from app.utils.structured_logging import log_structured

# How long a request waits for one of the GARMIN_WRITE_CONCURRENCY slots before answering garmin_busy.
ADMISSION_WAIT_SECONDS = 5.0
BUSY_RETRY_AFTER_SECONDS = "5"

logger = logging.getLogger(__name__)

_idempotency = IdempotencyStore("garmin")

# One slot pool and thread pool per event loop (one in production; tests start several).
_write_slots: RunVar[anyio.Semaphore] = RunVar("garmin_write_slots")
_write_threads: RunVar[anyio.CapacityLimiter] = RunVar("garmin_write_threads")


def _error_response(exc: GarminApiError, headers: Mapping[str, str] | None = None) -> JSONResponse:
    response_headers = dict(headers or {})
    if exc.retry_after:
        response_headers["Retry-After"] = exc.retry_after
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": exc.code.value},
        headers=response_headers or None,
    )


def _describe_validation_error(exc: RequestValidationError) -> str:
    problems = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
        problems.append(f"{location}: {error.get('msg')}" if location else str(error.get("msg")))
    return "Invalid request: " + "; ".join(problems)


class GarminRoute(APIRoute):
    """Turns Garmin, auth and request-validation failures into the routes' ``{"detail", "code"}`` errors."""

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
            except HTTPException as exc:
                if exc.status_code != status.HTTP_401_UNAUTHORIZED:
                    raise
                return _error_response(
                    GarminApiError(GarminErrorCode.UNAUTHORIZED, str(exc.detail)), headers=exc.headers
                )

        return garmin_route_handler


router = APIRouter(route_class=GarminRoute)


def _per_loop[T](var: RunVar[T], factory: Callable[[], T]) -> T:
    try:
        return var.get()
    except LookupError:
        value = factory()
        var.set(value)
        return value


async def run_garmin_write[T](work: Callable[[], T]) -> T:
    """Run blocking Garmin work on the Garmin-only thread pool.

    At most GARMIN_WRITE_CONCURRENCY requests run at once; one that can't get a slot within
    ADMISSION_WAIT_SECONDS is answered garmin_busy (503, Retry-After) instead of queueing behind a
    slow Garmin while its client times out and retries.
    """
    slots = _per_loop(_write_slots, lambda: anyio.Semaphore(settings.garmin_write_concurrency))
    threads = _per_loop(_write_threads, lambda: anyio.CapacityLimiter(settings.garmin_write_concurrency))
    with anyio.move_on_after(ADMISSION_WAIT_SECONDS) as waited:
        await slots.acquire()
    if waited.cancelled_caught:
        raise GarminApiError(
            GarminErrorCode.BUSY,
            "Too many Garmin requests are in flight for this server; retry shortly.",
            retry_after=BUSY_RETRY_AFTER_SECONDS,
        )
    try:
        return await anyio.to_thread.run_sync(work, limiter=threads)
    finally:
        slots.release()


def _idempotent_create[M: BaseModel](
    scope: str, key: str | None, request_body: Any, create: Callable[[], M]
) -> M | JSONResponse:
    """Run ``create`` once per Idempotency-Key; a repeated key gets the first 201 back."""
    if key is None:
        return create()

    fingerprint = request_fingerprint(request_body)
    try:
        replay = _idempotency.begin(scope, key, fingerprint)
    except IdempotencyConflictError as exc:
        if exc.reason is IdempotencyConflictReason.DIFFERENT_REQUEST:
            detail = "This Idempotency-Key was already used for a different request; send a new key."
        else:
            detail = "A request with this Idempotency-Key is still running; retry once it has finished."
        raise GarminApiError(GarminErrorCode.IDEMPOTENCY_CONFLICT, detail) from None
    except RedisError as exc:
        # The key can't be checked; running the create is still better than refusing it.
        log_structured(
            logger,
            "warning",
            "Idempotency store unavailable, running the create without it",
            provider="garmin",
            action="garmin_idempotency_unavailable",
            error_type=type(exc).__name__,
        )
        return create()
    if replay is not None:
        return JSONResponse(
            status_code=replay.status_code, content=replay.body, headers={"Idempotent-Replayed": "true"}
        )

    try:
        result = create()
    except BaseException:
        _idempotency.release(scope, key)
        raise
    _idempotency.complete(scope, key, fingerprint, status.HTTP_201_CREATED, result.model_dump(mode="json"))
    return result


def get_garmin_training_service() -> GarminTrainingService:
    return GarminStrategy().training


GarminTrainingDep = Annotated[GarminTrainingService, Depends(get_garmin_training_service)]

IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=MAX_KEY_LENGTH,
        description="Optional. A unique value (e.g. a UUID) per create. Retrying with the same key within 24 h "
        "returns the original 201 response (with `Idempotent-Replayed: true`) instead of creating the object "
        "again. Only successful creates are remembered; reusing a key for a different body, or while the first "
        "request is still running, answers 409 `idempotency_conflict`.",
    ),
]

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": GarminErrorResponse, "description": "unauthorized (missing or invalid API key)"},
    403: {"model": GarminErrorResponse, "description": "garmin_permission_missing | garmin_forbidden"},
    404: {"model": GarminErrorResponse, "description": "garmin_not_connected | garmin_not_found"},
    422: {"model": GarminErrorResponse, "description": "garmin_validation_error"},
    429: {"model": GarminErrorResponse, "description": "garmin_rate_limited (Retry-After passed on from Garmin)"},
    502: {"model": GarminErrorResponse, "description": "garmin_upstream_error"},
    503: {"model": GarminErrorResponse, "description": "garmin_busy (Retry-After)"},
}
_CREATE_ERRORS: dict[int | str, dict[str, Any]] = {
    **_ERRORS,
    409: {"model": GarminErrorResponse, "description": "idempotency_conflict"},
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
        description="A Garmin Training API V2 workout object. On create, workoutProvider and workoutSourceId "
        "default to GARMIN_WORKOUT_PROVIDER when left out.",
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
async def get_garmin_permissions(
    user_id: UUID,
    db: DbSession,
    _api_key: ApiKeyDep,
    training: GarminTrainingDep,
) -> GarminPermissionsRead:
    """Permissions the user grants this app in Garmin Connect.

    Read live from Garmin and stored on the connection. When Garmin can't be reached (network error
    or a Garmin 5xx), the stored permissions (kept current by Garmin's userPermissionsChange pushes)
    are returned instead; a Garmin rate limit is answered as `garmin_rate_limited`. A user without
    an active Garmin connection gets `connected: false` and no permissions; `status: expired` means
    Garmin stopped accepting OW's tokens and the user has to reconnect Garmin before any write.
    """
    return await run_garmin_write(lambda: training.get_permissions(db, user_id))


@router.post(
    "/users/{user_id}/garmin/workouts",
    status_code=status.HTTP_201_CREATED,
    response_model=GarminWorkoutResult,
    responses=_CREATE_ERRORS,
)
async def create_garmin_workout(
    user_id: UUID,
    workout: WorkoutBody,
    db: DbSession,
    api_key: ApiKeyDep,
    training: GarminTrainingDep,
    idempotency_key: IdempotencyKey = None,
) -> GarminWorkoutResult | JSONResponse:
    """Create a workout in the user's Garmin Connect account (needs WORKOUT_IMPORT).

    Checked before sending: `sport` is set, a single-sport workout has one segment and at most 100
    steps, and `MULTI_SPORT` has at most 25 segments and 250 steps. Repeat blocks count as steps.
    """
    return await run_garmin_write(
        lambda: _idempotent_create(
            f"{api_key}:{user_id}:workouts",
            idempotency_key,
            workout,
            lambda: training.create_workout(db, user_id, workout),
        )
    )


@router.put("/users/{user_id}/garmin/workouts/{workout_id}", responses=_ERRORS)
async def update_garmin_workout(
    user_id: UUID,
    workout_id: int,
    workout: WorkoutBody,
    db: DbSession,
    _api_key: ApiKeyDep,
    training: GarminTrainingDep,
) -> GarminWorkoutResult:
    """Replace a workout with a full new version (needs WORKOUT_IMPORT).

    Garmin requires `ownerId` on updates. Send the one from the create response; when it is left
    out, OW reads it from Garmin first, which costs one extra Garmin call. The same read keeps the
    workout's current `workoutProvider` and `workoutSourceId` when the body leaves them out.
    """
    return await run_garmin_write(lambda: training.update_workout(db, user_id, workout_id, workout))


@router.delete(
    "/users/{user_id}/garmin/workouts/{workout_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_ERRORS,
)
async def delete_garmin_workout(
    user_id: UUID,
    workout_id: int,
    db: DbSession,
    _api_key: ApiKeyDep,
    training: GarminTrainingDep,
) -> Response:
    """Delete a workout from Garmin Connect. A workout Garmin no longer has counts as deleted."""
    await run_garmin_write(lambda: training.delete_workout(db, user_id, workout_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/users/{user_id}/garmin/schedules",
    status_code=status.HTTP_201_CREATED,
    response_model=GarminScheduleResult,
    responses=_CREATE_ERRORS,
)
async def create_garmin_schedule(
    user_id: UUID,
    schedule: GarminScheduleCreate,
    db: DbSession,
    api_key: ApiKeyDep,
    training: GarminTrainingDep,
    idempotency_key: IdempotencyKey = None,
) -> GarminScheduleResult | JSONResponse:
    """Put a pushed workout on the user's Garmin calendar for one day (needs WORKOUT_IMPORT)."""
    return await run_garmin_write(
        lambda: _idempotent_create(
            f"{api_key}:{user_id}:schedules",
            idempotency_key,
            schedule.model_dump(mode="json"),
            lambda: training.create_schedule(db, user_id, schedule.workout_id, schedule.date),
        )
    )


@router.delete(
    "/users/{user_id}/garmin/schedules/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_ERRORS,
)
async def delete_garmin_schedule(
    user_id: UUID,
    schedule_id: int,
    db: DbSession,
    _api_key: ApiKeyDep,
    training: GarminTrainingDep,
) -> Response:
    """Remove a scheduled workout from the user's Garmin calendar. Already gone counts as deleted."""
    await run_garmin_write(lambda: training.delete_schedule(db, user_id, schedule_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/users/{user_id}/garmin/courses",
    status_code=status.HTTP_201_CREATED,
    response_model=GarminCourseResult,
    responses=_CREATE_ERRORS,
)
async def create_garmin_course(
    user_id: UUID,
    course: CourseBody,
    db: DbSession,
    api_key: ApiKeyDep,
    training: GarminTrainingDep,
    idempotency_key: IdempotencyKey = None,
) -> GarminCourseResult | JSONResponse:
    """Create a course (route) in the user's Garmin Connect account (needs COURSE_IMPORT).

    Checked before sending: a non-empty `courseName`, 1 to 10,000 geoPoints, and numeric
    `distance`, `elevationGain` and `elevationLoss`, which Garmin does not accept as null.
    """
    return await run_garmin_write(
        lambda: _idempotent_create(
            f"{api_key}:{user_id}:courses",
            idempotency_key,
            course,
            lambda: training.create_course(db, user_id, course),
        )
    )


@router.delete(
    "/users/{user_id}/garmin/courses/{course_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_ERRORS,
)
async def delete_garmin_course(
    user_id: UUID,
    course_id: int,
    db: DbSession,
    _api_key: ApiKeyDep,
    training: GarminTrainingDep,
) -> Response:
    """Delete a course from Garmin Connect. A course Garmin no longer has counts as deleted."""
    await run_garmin_write(lambda: training.delete_course(db, user_id, course_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
