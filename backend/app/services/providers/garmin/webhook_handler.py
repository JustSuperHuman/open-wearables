"""Garmin webhook handler.

Garmin delivers data inline (PUSH mode). ``dispatch()`` stores the raw
payload, enqueues a Celery task for async processing, and returns
``{"status": "accepted"}`` immediately so Garmin's 30-second timeout is
never exceeded.

PING (callbackURL-based) mode is not supported — those notifications are
ignored and the endpoint still returns 200 so Garmin stops retrying. The same
goes for push types OW has no mapping for (see ``UNMAPPED_TYPES``): they are
logged at debug level and dropped.
"""

import contextlib
import hmac
import json
import logging
from typing import Any
from uuid import UUID, uuid4

from celery import current_app as celery_app
from fastapi import HTTPException, Request

from app.config import settings
from app.constants.celery_tasks import CeleryQueue, CeleryTask
from app.database import DbSession
from app.repositories import UserConnectionRepository
from app.schemas.auth import ConnectionStatus
from app.services.providers.garmin.backfill_state import (
    get_backfill_status,
    get_trace_id,
    mark_type_success,
)
from app.services.providers.garmin.data_247 import Garmin247Data
from app.services.providers.garmin.handlers.activities import (
    process_activity_notification,
    process_manual_activity_update,
)
from app.services.providers.garmin.handlers.lifecycle import (
    process_deregistrations,
    process_user_permissions,
)
from app.services.providers.garmin.handlers.wellness import process_wellness_items
from app.services.providers.garmin.oauth import GarminOAuth
from app.services.providers.garmin.workouts import GarminWorkouts
from app.services.providers.templates.base_webhook_handler import BaseWebhookHandler
from app.services.raw_payload_storage import store_raw_payload
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

WELLNESS_TYPES: list[str] = [
    "dailies",
    "epochs",
    "sleeps",
    "bodyComps",
    "hrv",
    "stressDetails",
    "allDayRespiration",
    "pulseox",
    "healthSnapshot",
    "skinTemp",
    "moveIQActivities",
    "mct",
    "userMetrics",
    "bloodPressures",
    "nutrition",
    "hydration",
    "activityDetails",
    "activityFiles",
]

# Push types Garmin sends that OW deliberately stores nothing for. breathingVariations reports a
# sleep breathing-variation level (MINIMAL / FEW / OCCASIONAL / FREQUENT) that no series type fits;
# like every Garmin push, the raw body is still kept by raw payload storage when RAW_PAYLOAD_STORAGE
# is enabled. Unknown types Garmin adds later are treated the same way.
UNMAPPED_TYPES: frozenset[str] = frozenset({"breathingVariations"})

# Connections pushes are stored for. EXPIRED ones lost their tokens, not the user's consent:
# Garmin keeps pushing until the user deregisters, and pushes need no token of ours.
RECEIVING_STATUSES: frozenset[ConnectionStatus] = frozenset({ConnectionStatus.ACTIVE, ConnectionStatus.EXPIRED})


class GarminWebhookHandler(BaseWebhookHandler):
    """Webhook handler for Garmin Health API push/ping events.

    Garmin does not sign request bodies with HMAC; it identifies itself via the
    ``garmin-client-id`` request header.  ``verify_signature`` checks that this
    header matches the configured ``settings.garmin_client_id`` (constant-time),
    failing closed when the value is not configured.

    The client id is not a secret (it is in every OAuth URL), so a shared token can be put on the
    push URLs registered in Garmin's portal (``?token=``). With ``GARMIN_WEBHOOK_TOKEN`` set, a
    matching token is checked in constant time; ``GARMIN_WEBHOOK_TOKEN_REQUIRED`` decides whether a
    missing or wrong one is rejected (401) or only logged, so the portal URLs can be changed before
    the token is enforced.
    """

    def __init__(self, garmin_workouts: GarminWorkouts, garmin_247: Garmin247Data, oauth: GarminOAuth) -> None:
        super().__init__("garmin")
        self.garmin_workouts = garmin_workouts
        self.garmin_247 = garmin_247
        self.oauth = oauth
        self.connection_repo = UserConnectionRepository()

    def extract_user_id(self, payload: dict[str, Any]) -> str | None:
        """Garmin batches items under data-type keys; userId lives per item."""
        user_ids = {
            str(item["userId"])
            for items in payload.values()
            if isinstance(items, list)
            for item in items
            if isinstance(item, dict) and item.get("userId")
        }
        return ",".join(sorted(user_ids)) or None

    # ------------------------------------------------------------------
    # BaseWebhookHandler interface
    # ------------------------------------------------------------------

    def verify_signature(self, request: Request, body: bytes) -> bool:
        expected = settings.garmin_client_id
        if not expected:
            log_structured(
                logger,
                "error",
                "GARMIN_CLIENT_ID not configured; rejecting webhook",
                provider="garmin",
                action="webhook_signature_missing_secret",
            )
            return False

        provided = request.headers.get("garmin-client-id", "")
        if not provided:
            log_structured(
                logger,
                "warning",
                "Missing garmin-client-id header",
                provider="garmin",
                action="webhook_signature_invalid",
            )
            return False

        if not self._verify_token(expected, provided):
            log_structured(
                logger,
                "warning",
                "garmin-client-id header does not match configured value",
                provider="garmin",
                action="webhook_signature_invalid",
            )
            return False

        return self._verify_url_token(request)

    @staticmethod
    def _verify_url_token(request: Request) -> bool:
        """Check the shared ``?token=`` from the registered push URL; only fails while it is required."""
        expected = settings.garmin_webhook_token.get_secret_value() if settings.garmin_webhook_token else ""
        required = settings.garmin_webhook_token_required
        if not expected:
            # Settings refuse REQUIRED without a token; fail closed should that ever be bypassed.
            return not required

        provided = request.query_params.get("token")
        # compare_digest raises TypeError on non-ASCII str; the token is caller-supplied.
        if provided and hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
            return True

        if required:
            message, action = "Rejected Garmin push without a valid URL token", "webhook_token_invalid"
        else:
            message = "Accepted Garmin push without a valid URL token (GARMIN_WEBHOOK_TOKEN_REQUIRED is off)"
            action = "webhook_token_not_enforced"
        log_structured(
            logger,
            "warning",
            message,
            provider="garmin",
            action=action,
            token_present=bool(provided),
            path=request.url.path,
        )
        return not required

    def parse_payload(self, body: bytes) -> dict[str, Any]:
        try:
            return json.loads(body)
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    def dispatch(self, db: DbSession, payload: dict[str, Any]) -> dict[str, Any]:
        """Accept the webhook and enqueue async processing.

        Returns ``{"status": "accepted"}`` immediately so Garmin's 30-second
        timeout is never exceeded. Actual processing runs in ``process_webhook_push``
        Celery task.
        """
        request_trace_id = str(uuid4())[:8]

        item_counts = {k: len(v) if isinstance(v, list) else 1 for k, v in payload.items()}
        garmin_user_ids = list(
            {
                item.get("userId")
                for items in payload.values()
                if isinstance(items, list)
                for item in items
                if isinstance(item, dict) and item.get("userId")
            }
        )
        log_structured(
            logger,
            "info",
            "Received Garmin webhook",
            provider="garmin",
            trace_id=request_trace_id,
            item_counts=item_counts,
            garmin_user_ids=garmin_user_ids,
        )

        store_raw_payload(source="webhook", provider="garmin", payload=payload, trace_id=request_trace_id)

        # garmin_sync is isolated from the default queue so high-volume live-push
        # events and backfill-chain tasks don't starve each other.
        task = celery_app.send_task(
            CeleryTask.PROCESS_WEBHOOK_PUSH, args=["garmin", payload, request_trace_id], queue=CeleryQueue.GARMIN_SYNC
        )
        log_structured(
            logger,
            "info",
            "Enqueued Garmin webhook processing task",
            provider="garmin",
            trace_id=request_trace_id,
            task_id=getattr(task, "id", None),
        )

        return {"status": "accepted"}

    def process_payload(self, db: DbSession, payload: dict[str, Any], trace_id: str) -> dict[str, Any]:
        """Process a Garmin PUSH payload synchronously.

        Called by the ``process_webhook_push`` Celery task with its own DB session.
        Raises on infrastructure errors so the task can retry.
        """
        errors: list[str] = []
        synced_user_ids: set[UUID] = set()
        users_with_new_success: set[str] = set()

        # --- data processing ---------------------------------------------
        act_result = self._process_activities(db, payload, errors, synced_user_ids, users_with_new_success, trace_id)
        manual_result = self._process_manual_activity_updates(db, payload, errors, synced_user_ids, trace_id)
        well_result = self._process_wellness(db, payload, errors, synced_user_ids, users_with_new_success, trace_id)
        ignored = self._log_unmapped_types(payload, trace_id)

        # --- commit + update last_synced_at ------------------------------
        db.commit()

        for uid in synced_user_ids:
            with contextlib.suppress(Exception):
                connection = self.connection_repo.get_by_user_and_provider(db, uid, "garmin")
                if connection and connection.status in RECEIVING_STATUSES:
                    self.connection_repo.update_last_synced_at(db, connection)

        # --- build response ----------------------------------------------
        response: dict[str, Any] = {
            "processed": act_result["processed_count"],
            "saved": act_result["saved_count"],
            "errors": errors,
            "activities": act_result["details"],
            "manuallyUpdatedActivities": manual_result,
            "wellness": well_result,
            "ignored": ignored,
            "backfill_chained": [],
        }

        self._process_lifecycle_events(db, payload, response, trace_id)
        response["backfill_chained"] = self._chain_backfills(users_with_new_success, trace_id)
        return response

    # ------------------------------------------------------------------
    # Private dispatch helpers
    # ------------------------------------------------------------------

    def _process_activities(
        self,
        db: DbSession,
        payload: dict[str, Any],
        errors: list[str],
        synced_user_ids: set[UUID],
        users_with_new_success: set[str],
        request_trace_id: str,
    ) -> dict[str, Any]:
        processed_count = 0
        saved_count = 0
        details: list[dict[str, Any]] = []

        for notification in payload.get("activities", []):
            per_profile_results = process_activity_notification(
                db, self.connection_repo, self.garmin_workouts, notification, request_trace_id
            )
            details.extend(per_profile_results)
            for result in per_profile_results:
                status = result.get("status")
                uid_str = result.get("internal_user_id")
                if status in ("saved", "updated", "fetched"):
                    processed_count += 1
                    if status == "saved":
                        saved_count += len(result.get("record_ids", []))
                    if uid_str:
                        synced_user_ids.add(UUID(uid_str))
                        if mark_type_success(uid_str, "activities"):
                            users_with_new_success.add(uid_str)
                elif status == "duplicate":
                    if uid_str and mark_type_success(uid_str, "activities"):
                        users_with_new_success.add(uid_str)
                elif status in ("error", "user_not_found", "conflict"):
                    errors.append(result.get("error", "Unknown error"))

        return {"processed_count": processed_count, "saved_count": saved_count, "details": details}

    def _process_manual_activity_updates(
        self,
        db: DbSession,
        payload: dict[str, Any],
        errors: list[str],
        synced_user_ids: set[UUID],
        request_trace_id: str,
    ) -> dict[str, Any]:
        """Apply activities the user edited in Garmin Connect to the stored workouts."""
        updated_count = 0
        saved_count = 0
        conflict_count = 0
        details: list[dict[str, Any]] = []

        for notification in payload.get("manuallyUpdatedActivities") or []:
            if not isinstance(notification, dict):
                continue
            per_profile_results = process_manual_activity_update(
                db, self.connection_repo, self.garmin_workouts, notification, request_trace_id
            )
            details.extend(per_profile_results)
            for result in per_profile_results:
                status = result.get("status")
                uid_str = result.get("internal_user_id")
                if status in ("updated", "saved"):
                    if status == "updated":
                        updated_count += 1
                    else:
                        saved_count += 1
                    if uid_str:
                        synced_user_ids.add(UUID(uid_str))
                elif status == "conflict":
                    conflict_count += 1
                    errors.append(result.get("error", "Edited activity conflicts with another stored workout"))
                elif status in ("validation_error", "user_not_found"):
                    errors.append(result.get("error", "Unknown error"))

        return {"updated": updated_count, "saved": saved_count, "conflicts": conflict_count, "details": details}

    def _log_unmapped_types(self, payload: dict[str, Any], request_trace_id: str) -> list[str]:
        """Debug-log push types OW stores nothing for; returns their names."""
        handled = set(self.supported_event_types())
        ignored = {
            key: len(value) if isinstance(value, list) else 1 for key, value in payload.items() if key not in handled
        }
        if ignored:
            log_structured(
                logger,
                "debug",
                "Ignoring Garmin push types OW does not map",
                provider="garmin",
                trace_id=request_trace_id,
                item_counts=ignored,
                known_unmapped=sorted(UNMAPPED_TYPES & ignored.keys()),
            )
        return sorted(ignored)

    def _process_wellness(
        self,
        db: DbSession,
        payload: dict[str, Any],
        errors: list[str],
        synced_user_ids: set[UUID],
        users_with_new_success: set[str],
        request_trace_id: str,
    ) -> dict[str, Any]:
        wellness_results: dict[str, Any] = {}

        for data_type in WELLNESS_TYPES:
            if not payload.get(data_type):
                continue

            items = payload[data_type]
            log_structured(
                logger,
                "info",
                "Processing wellness notifications",
                provider="garmin",
                trace_id=request_trace_id,
                summary_type=data_type,
                count=len(items),
            )

            result = process_wellness_items(
                db, self.connection_repo, self.garmin_247, data_type, items, errors, synced_user_ids, request_trace_id
            )
            wellness_results[data_type] = {"processed": result["processed"], "saved": result["saved"]}

            for uid_str in result.get("succeeded_users", []):
                if mark_type_success(uid_str, data_type):
                    users_with_new_success.add(uid_str)

        return wellness_results

    def _process_lifecycle_events(
        self,
        db: DbSession,
        payload: dict[str, Any],
        response: dict[str, Any],
        request_trace_id: str,
    ) -> None:
        if "userPermissionsChange" in payload:
            try:
                response["userPermissionsChange"] = process_user_permissions(
                    db, self.connection_repo, payload["userPermissionsChange"], request_trace_id
                )
            except Exception as e:
                log_structured(
                    logger,
                    "error",
                    "Failed to process permission changes",
                    provider="garmin",
                    trace_id=request_trace_id,
                    error=str(e),
                )
                response["userPermissionsChange"] = {"updated": 0, "errors": [str(e)]}

        if "deregistrations" in payload:
            try:
                response["deregistrations"] = process_deregistrations(
                    db, self.connection_repo, self.oauth, payload["deregistrations"], request_trace_id
                )
            except Exception as e:
                log_structured(
                    logger,
                    "error",
                    "Failed to process deregistrations",
                    provider="garmin",
                    trace_id=request_trace_id,
                    error=str(e),
                )
                response["deregistrations"] = {"revoked": 0, "errors": [str(e)]}

    def _chain_backfills(self, users_with_new_success: set[str], request_trace_id: str) -> list[str]:
        if not settings.garmin_backfill_enabled:
            return []
        backfill_triggered: list[str] = []
        for user_id_str in users_with_new_success:
            backfill_status = get_backfill_status(user_id_str)
            if backfill_status["overall_status"] in ("in_progress", "retry_in_progress"):
                trace_id = get_trace_id(user_id_str) or request_trace_id
                log_structured(
                    logger,
                    "info",
                    "Triggering next backfill",
                    provider="garmin",
                    trace_id=trace_id,
                    user_id=user_id_str,
                    current_window=backfill_status["current_window"],
                    total_windows=backfill_status["total_windows"],
                )
                celery_app.send_task(CeleryTask.GARMIN_TRIGGER_NEXT_PENDING_TYPE, args=[user_id_str])
                backfill_triggered.append(user_id_str)
        return backfill_triggered

    def supported_event_types(self) -> list[str]:
        return ["activities", "manuallyUpdatedActivities", *WELLNESS_TYPES, "userPermissionsChange", "deregistrations"]
