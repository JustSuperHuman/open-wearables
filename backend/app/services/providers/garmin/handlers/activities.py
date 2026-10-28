"""Garmin activity webhook handler.

Processes a single activity notification — either PUSH (inline data) or
PING (callbackURL to fetch from Garmin) — and ``manuallyUpdatedActivities``
entries, which carry an activity the user edited in Garmin Connect.

Per linked OW profile the result status is:

- ``saved``: a new workout was stored.
- ``updated``: a stored workout (matched by the Garmin activity id) changed.
- ``duplicate``: the workout was already stored exactly like this.
- ``conflict``: nothing was written because the activity's span collides with another stored
  workout of the same device; ``conflict_record_ids`` names it.
"""

import logging
import re
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.database import DbSession
from app.repositories import UserConnectionRepository
from app.schemas.providers.garmin import ActivityJSON as GarminActivityJSON
from app.schemas.sync_status import SyncRunContext, SyncScope, SyncSource, SyncStatus
from app.services.providers.garmin.backfill_state import get_trace_id
from app.services.providers.garmin.workouts import GarminWorkouts, WorkoutWrite, WorkoutWriteStatus
from app.services.sync_status_service import emit_sync_completed, new_run_id
from app.utils.context import sync_run_context
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

_NUMERIC_ID = re.compile(r"\d+")


def _result_status(writes: list[WorkoutWrite]) -> str:
    statuses = {write.status for write in writes}
    if WorkoutWriteStatus.CONFLICT in statuses:
        return "conflict"
    if WorkoutWriteStatus.CREATED in statuses:
        return "saved"
    if WorkoutWriteStatus.UPDATED in statuses:
        return "updated"
    return "duplicate"


def _write_result(base_result: dict[str, Any], internal_user_id: UUID, writes: list[WorkoutWrite]) -> dict[str, Any]:
    status = _result_status(writes)
    result: dict[str, Any] = {
        **base_result,
        "internal_user_id": str(internal_user_id),
        "record_ids": [str(w.record_id) for w in writes if w.status != WorkoutWriteStatus.CONFLICT],
        "status": status,
    }
    if status == "conflict":
        result["conflict_record_ids"] = [str(w.record_id) for w in writes if w.status == WorkoutWriteStatus.CONFLICT]
        result["error"] = (
            f"Garmin activity {base_result.get('activity_id')} was not applied: its time span collides with "
            "another stored workout of the same device"
        )
    return result


def process_activity_notification(
    db: DbSession,
    connection_repo: UserConnectionRepository,
    garmin_workouts: GarminWorkouts,
    notification: dict[str, Any],
    request_trace_id: str,
) -> list[dict[str, Any]]:
    """Process a single Garmin activity notification for all linked OW profiles.

    Returns one result dict per linked profile.  The first connection is treated
    as primary (WEBHOOK source); any additional profiles sharing the same Garmin
    account receive LINKED_ACCOUNT events.
    """
    garmin_user_id: str | None = notification.get("userId")
    activity_id = notification.get("activityId")
    activity_name = notification.get("activityName")
    activity_type = notification.get("activityType")

    base_result: dict[str, Any] = {
        "activity_id": activity_id,
        "name": activity_name,
        "type": activity_type,
        "garmin_user_id": garmin_user_id,
    }

    if not garmin_user_id:
        return [{**base_result, "status": "user_not_found", "error": "Missing userId in activity notification"}]

    connections = connection_repo.get_all_by_provider_user_id(db, "garmin", garmin_user_id, include_expired=True)
    if not connections:
        log_structured(
            logger,
            "warning",
            "No connection found for Garmin user",
            provider="garmin",
            trace_id=request_trace_id,
            garmin_user_id=garmin_user_id,
        )
        return [{**base_result, "status": "user_not_found", "error": f"User {garmin_user_id} not connected"}]

    if "callbackURL" in notification:
        # PING not supported; Garmin must be configured for PUSH-only delivery.
        return [{**base_result, "status": "skipped"} for _ in connections]

    # Parse activity data once — reused for every linked profile.
    try:
        activity = GarminActivityJSON(**notification)
    except ValidationError as e:
        log_structured(
            logger,
            "error",
            "Failed to parse activity data",
            provider="garmin",
            trace_id=request_trace_id,
            activity_id=activity_id,
            error=str(e),
        )
        err_result = {**base_result, "status": "validation_error", "error": f"Invalid activity data: {e}"}
        return [err_result for _ in connections]

    primary_user_id: UUID = connections[0].user_id
    results: list[dict[str, Any]] = []

    for i, connection in enumerate(connections):
        internal_user_id = connection.user_id
        trace_id = get_trace_id(internal_user_id) or request_trace_id
        is_primary = i == 0
        sync_source = SyncSource.WEBHOOK if is_primary else SyncSource.LINKED_ACCOUNT

        log_structured(
            logger,
            "info" if is_primary else "debug",
            "New Garmin activity received",
            provider="garmin",
            trace_id=trace_id,
            activity_name=activity_name,
            activity_type=activity_type,
            activity_id=activity_id,
            garmin_user_id=garmin_user_id,
            user_id=str(internal_user_id),
            is_primary=is_primary,
        )
        run_id = new_run_id(prefix=f"garmin_webhook_activity_{activity_id}")
        try:
            with sync_run_context(SyncRunContext(run_id=run_id, source=sync_source, scope=SyncScope.LIVE)):
                writes = garmin_workouts.process_push_activities(
                    db=db,
                    activities=[activity],
                    user_id=internal_user_id,
                )
        except IntegrityError:
            db.rollback()
            log_structured(
                logger,
                "info",
                "Activity already exists, skipping",
                provider="garmin",
                trace_id=trace_id,
                activity_id=activity_id,
                user_id=str(internal_user_id),
            )
            results.append({**base_result, "internal_user_id": str(internal_user_id), "status": "duplicate"})
            continue

        result = _write_result(base_result, internal_user_id, writes)
        log_structured(
            logger,
            "info" if is_primary else "debug",
            "Applied Garmin activity",
            provider="garmin",
            trace_id=trace_id,
            activity_id=activity_id,
            user_id=str(internal_user_id),
            status=result["status"],
            record_ids=result["record_ids"],
        )
        emit_sync_completed(
            internal_user_id,
            "garmin",
            sync_source,
            run_id=run_id,
            status=SyncStatus.SUCCESS,
            message=f"Garmin activity received: {activity_name}",
            items_processed=len(result["record_ids"]),
            primary_user_id=None if is_primary else primary_user_id,
            metadata={"trace_id": trace_id, "activity_id": activity_id, "activity_type": activity_type},
        )
        results.append(result)

    return results


def _manual_update_activity_id(notification: dict[str, Any]) -> Any:
    """The Garmin activity id of a manuallyUpdatedActivities entry.

    Garmin uses the activity id as an activity's summaryId, and the spec's example for this type
    leaves activityId out, so a purely numeric summaryId stands in for it. Any other summaryId
    names the summary, not the activity, and would file the edit as a new workout.
    """
    activity_id = notification.get("activityId")
    if activity_id is not None and activity_id != "":
        return activity_id
    summary_id = notification.get("summaryId")
    if isinstance(summary_id, int) and not isinstance(summary_id, bool):
        return summary_id
    if isinstance(summary_id, str) and _NUMERIC_ID.fullmatch(summary_id.strip()):
        return summary_id.strip()
    return None


def process_manual_activity_update(
    db: DbSession,
    connection_repo: UserConnectionRepository,
    garmin_workouts: GarminWorkouts,
    notification: dict[str, Any],
    request_trace_id: str,
) -> list[dict[str, Any]]:
    """Apply one ``manuallyUpdatedActivities`` entry (an activity edited in Garmin Connect).

    Every OW profile linked to the Garmin account gets the edit. Returns one result per profile,
    with the statuses listed in the module docstring.
    """
    garmin_user_id: str | None = notification.get("userId")
    activity_id = _manual_update_activity_id(notification)
    base_result: dict[str, Any] = {
        "activity_id": activity_id,
        "type": notification.get("activityType"),
        "garmin_user_id": garmin_user_id,
        "manual_update": True,
    }

    if not garmin_user_id:
        return [{**base_result, "status": "user_not_found", "error": "Missing userId in manual activity update"}]

    connections = connection_repo.get_all_by_provider_user_id(db, "garmin", garmin_user_id, include_expired=True)
    if not connections:
        log_structured(
            logger,
            "warning",
            "No connection found for Garmin user (manual activity update)",
            provider="garmin",
            trace_id=request_trace_id,
            garmin_user_id=garmin_user_id,
        )
        return [{**base_result, "status": "user_not_found", "error": f"User {garmin_user_id} not connected"}]

    if activity_id is None:
        log_structured(
            logger,
            "warning",
            "Manually updated activity has no activity id, skipping",
            provider="garmin",
            trace_id=request_trace_id,
            garmin_user_id=garmin_user_id,
            summary_id=notification.get("summaryId"),
        )
        error = "Manually updated activity carries neither an activityId nor a numeric summaryId"
        return [{**base_result, "status": "validation_error", "error": error} for _ in connections]

    try:
        activity = GarminActivityJSON.model_validate({**notification, "activityId": activity_id})
    except ValidationError as e:
        log_structured(
            logger,
            "error",
            "Failed to parse manually updated activity",
            provider="garmin",
            trace_id=request_trace_id,
            activity_id=activity_id,
            error=str(e),
        )
        err_result = {**base_result, "status": "validation_error", "error": f"Invalid activity data: {e}"}
        return [err_result for _ in connections]

    primary_user_id: UUID = connections[0].user_id
    results: list[dict[str, Any]] = []

    for i, connection in enumerate(connections):
        internal_user_id = connection.user_id
        trace_id = get_trace_id(internal_user_id) or request_trace_id
        is_primary = i == 0
        sync_source = SyncSource.WEBHOOK if is_primary else SyncSource.LINKED_ACCOUNT
        run_id = new_run_id(prefix=f"garmin_webhook_activity_update_{activity_id}")
        try:
            with sync_run_context(SyncRunContext(run_id=run_id, source=sync_source, scope=SyncScope.LIVE)):
                writes = garmin_workouts.process_manual_updates(db=db, activities=[activity], user_id=internal_user_id)
        except IntegrityError:
            db.rollback()
            log_structured(
                logger,
                "info",
                "Manually updated activity conflicts with a stored record, skipping",
                provider="garmin",
                trace_id=trace_id,
                activity_id=activity_id,
                user_id=str(internal_user_id),
            )
            results.append({**base_result, "internal_user_id": str(internal_user_id), "status": "conflict"})
            continue

        result = _write_result(base_result, internal_user_id, writes)
        log_structured(
            logger,
            "info" if is_primary else "debug",
            "Applied Garmin manual activity update",
            provider="garmin",
            trace_id=trace_id,
            activity_id=activity_id,
            user_id=str(internal_user_id),
            status=result["status"],
            record_ids=result["record_ids"],
        )
        emit_sync_completed(
            internal_user_id,
            "garmin",
            sync_source,
            run_id=run_id,
            status=SyncStatus.SUCCESS,
            message="Garmin activity edited in Garmin Connect",
            items_processed=len(result["record_ids"]),
            primary_user_id=None if is_primary else primary_user_id,
            metadata={"trace_id": trace_id, "activity_id": activity_id, "manual_update": True},
        )
        results.append(result)

    return results
