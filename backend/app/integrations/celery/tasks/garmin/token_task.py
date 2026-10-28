"""Celery tasks that keep Garmin connections usable: token refresh and Garmin user id lookup.

See ``app.services.providers.garmin.token_maintenance`` for why they exist.
"""

import contextlib
from logging import getLogger
from typing import Any
from uuid import UUID

from celery import Task, shared_task
from redis.exceptions import LockError

from app.constants.celery_tasks import CeleryTask
from app.database import SessionLocal
from app.integrations.redis_client import get_redis_client
from app.models import User
from app.repositories import UserConnectionRepository
from app.repositories.user_repository import UserRepository
from app.services.providers.garmin.oauth import GarminOAuth
from app.services.providers.garmin.token_maintenance import GarminTokenMaintenance
from app.utils.structured_logging import log_structured

logger = getLogger(__name__)

GARMIN_API_BASE_URL = "https://apis.garmin.com"

# Held for the whole run so overlapping beat runs (or a manual run) don't do the same work twice.
_RUN_LOCK_KEY = "garmin:token_maintenance_lock"
_RUN_LOCK_SECONDS = 30 * 60

# Retries of the user id lookup queued at connect time: 1, 2, 4, 8, 16 minutes.
_RESOLVE_MAX_RETRIES = 5
_RESOLVE_RETRY_BASE_SECONDS = 60


def _maintenance() -> GarminTokenMaintenance:
    connection_repo = UserConnectionRepository()
    oauth = GarminOAuth(
        user_repo=UserRepository(User),
        connection_repo=connection_repo,
        provider_name="garmin",
        api_base_url=GARMIN_API_BASE_URL,
    )
    return GarminTokenMaintenance(oauth=oauth, connection_repo=connection_repo, api_base_url=GARMIN_API_BASE_URL)


@shared_task(name=CeleryTask.GARMIN_REFRESH_TOKENS)
def refresh_garmin_tokens() -> dict[str, Any]:
    """Refresh Garmin tokens nearing the refresh-token age limit and fill in missing Garmin user ids."""
    lock = get_redis_client().lock(_RUN_LOCK_KEY, timeout=_RUN_LOCK_SECONDS, blocking=False)
    if not lock.acquire():
        return {"status": "skipped", "reason": "another run is in progress"}
    try:
        maintenance = _maintenance()
        with SessionLocal() as db:
            user_ids = maintenance.resolve_missing_user_ids(db)
            tokens = maintenance.refresh_aging_tokens(db)
        log_structured(
            logger,
            "info",
            "Garmin token maintenance finished",
            provider="garmin",
            action="garmin_token_maintenance",
            **{f"tokens_{k}": v for k, v in tokens.items()},
            **{f"user_ids_{k}": v for k, v in user_ids.items()},
        )
        return {"status": "done", "tokens": tokens, "user_ids": user_ids}
    finally:
        # The lock may have lapsed on a very long run; nothing to release then.
        with contextlib.suppress(LockError):
            lock.release()


@shared_task(name=CeleryTask.GARMIN_RESOLVE_USER_ID, bind=True, max_retries=_RESOLVE_MAX_RETRIES)
def resolve_garmin_user_id(self: Task, user_id: str) -> dict[str, Any]:
    """Look up and store the Garmin user id of a connection saved without one (connect-time lookup failed)."""
    try:
        with SessionLocal() as db:
            garmin_user_id = _maintenance().resolve_user_id(db, UUID(user_id))
    except Exception as exc:
        log_structured(
            logger,
            "warning",
            "Garmin user id lookup failed, retrying",
            provider="garmin",
            action="garmin_user_id_resolve_retry",
            user_id=user_id,
            attempt=self.request.retries + 1,
            error_type=type(exc).__name__,
        )
        # After the last retry the hourly maintenance job keeps trying.
        raise self.retry(exc=exc, countdown=_RESOLVE_RETRY_BASE_SECONDS * 2**self.request.retries)
    if garmin_user_id is None:
        return {"status": "skipped", "reason": "no active Garmin connection"}
    return {"status": "resolved"}
