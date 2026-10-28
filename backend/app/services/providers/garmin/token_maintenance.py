"""Keep Garmin connections usable while nothing else touches their tokens.

Garmin pushes data without needing our token, so on the Starter tier OW may not use a user's
token for months (only writes and FIT downloads do). Garmin refresh tokens expire 90 days after
they are issued and every refresh issues a new one, so a token left alone that long dies and the
connection with it. The periodic job refreshes any token older than
GARMIN_REFRESH_TOKEN_MAX_AGE_DAYS, and fills in the Garmin user id of connections saved without
one (the lookup at connect time failed), without which pushes can't be matched to them.

Refresh-token age isn't stored, but it follows from ``token_expires_at``: the code exchange and
every refresh issue both tokens together, and Garmin access tokens live 24 hours, so a refresh
token was issued at ``token_expires_at - 24h``.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import HTTPException
from redis.exceptions import LockError

from app.config import settings
from app.database import DbSession
from app.models import UserConnection
from app.repositories import UserConnectionRepository
from app.schemas.auth import ConnectionStatus
from app.services.providers.api_client import get_valid_token, refresh_tokens_if
from app.services.providers.garmin.oauth import PERMISSIONS_PATH, GarminOAuth
from app.services.providers.garmin.permissions import apply_permissions
from app.services.providers.garmin.write_client import GarminApiError, GarminWriteClient, stored_permissions
from app.utils.sentry_helpers import log_and_capture_error
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

PROVIDER = "garmin"

# Lifetime of the access token Garmin issues with every refresh token (expires_in: 86400).
ACCESS_TOKEN_LIFETIME = timedelta(days=1)


def refresh_cutoff(now: datetime | None = None) -> datetime:
    """``token_expires_at`` before which a connection's refresh token is older than the max age."""
    now = now or datetime.now(timezone.utc)
    return now - timedelta(days=settings.garmin_refresh_token_max_age_days) + ACCESS_TOKEN_LIFETIME


class GarminTokenMaintenance:
    def __init__(self, oauth: GarminOAuth, connection_repo: UserConnectionRepository, api_base_url: str) -> None:
        self.oauth = oauth
        self.connection_repo = connection_repo
        self.client = GarminWriteClient(oauth=oauth, connection_repo=connection_repo, api_base_url=api_base_url)

    def refresh_aging_tokens(self, db: DbSession, now: datetime | None = None) -> dict[str, int]:
        """Refresh every active connection whose refresh token is older than the max age.

        Safe to run repeatedly or concurrently: each refresh happens under the per-user lock the
        rest of OW uses and only if the token is still old once the lock is held, and a refreshed
        connection no longer matches the query.
        """
        cutoff = refresh_cutoff(now)

        def still_old(connection: UserConnection) -> bool:
            return (
                connection.status == ConnectionStatus.ACTIVE
                and connection.token_expires_at is not None
                and connection.token_expires_at < cutoff
            )

        counts = {"due": 0, "refreshed": 0, "rejected": 0, "busy": 0, "failed": 0}
        for connection in self.connection_repo.get_active_with_tokens_expiring_before(db, PROVIDER, cutoff):
            counts["due"] += 1
            user_id = connection.user_id
            try:
                refresh_tokens_if(db, user_id, PROVIDER, self.connection_repo, self.oauth, still_old)
            except LockError:
                counts["busy"] += 1  # another worker holds the refresh lock; the next run checks again
                continue
            except HTTPException as exc:
                if exc.status_code == 401:
                    # Garmin rejected the refresh token: mark_tokens_rejected already marked the
                    # connection EXPIRED and sent connection.revoked.
                    counts["rejected"] += 1
                else:
                    counts["failed"] += 1
                    self._log_failure(exc, user_id)
                continue
            except Exception as exc:
                counts["failed"] += 1
                self._log_failure(exc, user_id)
                continue
            db.refresh(connection)
            if not still_old(connection):
                counts["refreshed"] += 1
        return counts

    def resolve_missing_user_ids(self, db: DbSession) -> dict[str, int]:
        """Fill in the Garmin user id of active connections saved without one."""
        counts = {"missing": 0, "resolved": 0, "failed": 0}
        for connection in self.connection_repo.get_active_without_provider_user_id(db, PROVIDER):
            counts["missing"] += 1
            try:
                resolved = self.resolve_user_id(db, connection.user_id)
            except Exception as exc:
                counts["failed"] += 1
                self._log_failure(exc, connection.user_id, action="garmin_user_id_resolve_failed")
                continue
            if resolved:
                counts["resolved"] += 1
        return counts

    def resolve_user_id(self, db: DbSession, user_id: UUID) -> str | None:
        """Read the Garmin user id for the user's connection and store it if it differs.

        Also stores the user's permissions when they aren't known yet (the connect-time read is
        skipped when the user id lookup fails). Returns the Garmin user id; None when the user has
        no active Garmin connection. Raises when Garmin can't be asked.
        """
        connection = self.connection_repo.get_by_user_and_provider(db, user_id, PROVIDER)
        if connection is None or connection.status != ConnectionStatus.ACTIVE or not connection.access_token:
            return None

        access_token = get_valid_token(db, user_id, PROVIDER, self.connection_repo, self.oauth)
        garmin_user_id = self.oauth.get_user_id(access_token)
        if not garmin_user_id:
            raise ValueError("Garmin returned no userId")
        garmin_user_id = str(garmin_user_id)
        db.refresh(connection)
        if connection.provider_user_id != garmin_user_id:
            self.connection_repo.update_provider_user_id(db, connection, garmin_user_id)
            log_structured(
                logger,
                "info",
                "Stored Garmin user id for connection",
                provider=PROVIDER,
                action="garmin_user_id_resolved",
                user_id=str(user_id),
            )

        if stored_permissions(connection.scope) is None:
            try:
                permissions = self.client.fetch_permissions(db, user_id)
                apply_permissions(db, self.connection_repo, connection, permissions, as_of=time.time())
            except GarminApiError as exc:
                log_structured(
                    logger,
                    "info",
                    "Could not read Garmin permissions while resolving the user id",
                    provider=PROVIDER,
                    action="garmin_permissions_deferred",
                    user_id=str(user_id),
                    code=exc.code.value,
                    path=PERMISSIONS_PATH,
                )
        return garmin_user_id

    @staticmethod
    def _log_failure(exc: Exception, user_id: UUID, action: str = "garmin_token_refresh_failed") -> None:
        log_and_capture_error(
            exc,
            logger,
            "Garmin token maintenance failed for a connection",
            extra={"user_id": str(user_id), "provider": PROVIDER, "action": action},
        )
