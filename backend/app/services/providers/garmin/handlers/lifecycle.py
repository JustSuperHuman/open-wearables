"""Garmin connection lifecycle event handlers.

Handles webhook events that change the state of a user's Garmin connection:
- userPermissionsChange: update the permissions stored on the connection
- deregistrations: mark the connection as revoked, unless Garmin confirms the current grant is active

Both apply to every OW profile linked to the Garmin account, like data pushes do, including
connections whose tokens Garmin rejected (status EXPIRED): those still receive data, so they
still follow consent and permission changes.
"""

import logging
import time
from typing import Any

from app.database import DbSession
from app.models import UserConnection
from app.repositories import UserConnectionRepository
from app.schemas.auth import ConnectionStatus
from app.services.outgoing_webhooks.events import on_connection_revoked
from app.services.providers.api_client import get_valid_token
from app.services.providers.garmin.oauth import GarminOAuth
from app.services.providers.garmin.permissions import apply_permissions
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)


def _current_grant_is_active(
    db: DbSession,
    connection_repo: UserConnectionRepository,
    oauth: GarminOAuth,
    connection: UserConnection,
    garmin_user_id: str,
    trace_id: str,
) -> bool:
    """Garmin keeps the same userId across re-authorizations and can deliver the deregistration
    ping for an old grant after the user has reconnected, so the ping alone does not prove the
    current grant is gone. Anything short of a confirmed active grant counts as deregistered.
    """
    # An expired connection's tokens were already rejected; there is no grant left to confirm.
    if not connection.access_token or connection.status != ConnectionStatus.ACTIVE:
        return False
    try:
        # The caller revokes with reason "deregistration"; a failed refresh must not revoke
        # first with "refresh_failed".
        access_token = get_valid_token(
            db, connection.user_id, "garmin", connection_repo, oauth, revoke_on_failure=False
        )
        return oauth.get_user_id(access_token) == garmin_user_id
    except Exception as e:
        log_structured(
            logger,
            "info",
            "Garmin did not confirm an active grant for deregistration ping",
            provider="garmin",
            trace_id=trace_id,
            garmin_user_id=garmin_user_id,
            user_id=str(connection.user_id),
            error=str(e),
        )
        return False


def _change_time(entry: dict[str, Any]) -> float:
    """When Garmin says the permissions changed; entries without it are treated as current."""
    change_time = entry.get("changeTimeInSeconds")
    if isinstance(change_time, int | float) and not isinstance(change_time, bool):
        return float(change_time)
    return time.time()


def process_user_permissions(
    db: DbSession,
    connection_repo: UserConnectionRepository,
    permissions_list: list[dict[str, Any]],
    trace_id: str,
) -> dict[str, Any]:
    """Handle userPermissionsChange entries — update the permissions on every linked connection."""
    results: dict[str, Any] = {"updated": 0, "ignored_stale": 0, "errors": []}

    if not isinstance(permissions_list, list):
        return {"updated": 0, "errors": ["Invalid userPermissions payload format"]}

    for entry in permissions_list:
        if not isinstance(entry, dict):
            results["errors"].append("Invalid userPermissions entry format")
            continue

        garmin_user_id: str | None = entry.get("userId")
        if not garmin_user_id:
            results["errors"].append("Missing userId in userPermissions entry")
            continue

        # A user who switched every permission off is reported with an empty (or no) list.
        permissions = entry.get("permissions") or []
        if not isinstance(permissions, list):
            results["errors"].append("Invalid permissions in userPermissions entry")
            continue

        connections = connection_repo.get_all_by_provider_user_id(db, "garmin", garmin_user_id, include_expired=True)
        if not connections:
            log_structured(
                logger,
                "info",
                "No connection found for Garmin user (userPermissions) — skipping",
                provider="garmin",
                trace_id=trace_id,
                garmin_user_id=garmin_user_id,
            )
            continue

        as_of = _change_time(entry)
        for connection in connections:
            old_scope = connection.scope
            if not apply_permissions(db, connection_repo, connection, permissions, as_of=as_of):
                results["ignored_stale"] += 1
                continue
            log_structured(
                logger,
                "info",
                "Updated user permissions scope",
                provider="garmin",
                trace_id=trace_id,
                garmin_user_id=garmin_user_id,
                user_id=str(connection.user_id),
                old_scope=old_scope,
                new_scope=connection.scope,
            )
            results["updated"] += 1

    return results


def process_deregistrations(
    db: DbSession,
    connection_repo: UserConnectionRepository,
    oauth: GarminOAuth,
    deregistrations_list: list[dict[str, Any]],
    trace_id: str,
) -> dict[str, Any]:
    """Handle deregistration entries — mark every linked connection as revoked."""
    results: dict[str, Any] = {"revoked": 0, "ignored_stale": 0, "errors": []}

    if not isinstance(deregistrations_list, list):
        return {"revoked": 0, "errors": ["Invalid deregistrations payload format"]}

    for entry in deregistrations_list:
        if not isinstance(entry, dict):
            results["errors"].append("Invalid deregistrations entry format")
            continue

        garmin_user_id: str | None = entry.get("userId")
        if not garmin_user_id:
            results["errors"].append("Missing userId in deregistrations entry")
            continue

        connections = connection_repo.get_all_by_provider_user_id(db, "garmin", garmin_user_id, include_expired=True)
        if not connections:
            log_structured(
                logger,
                "info",
                "No connection found for Garmin user (deregistration) — skipping",
                provider="garmin",
                trace_id=trace_id,
                garmin_user_id=garmin_user_id,
            )
            continue

        for connection in connections:
            if _current_grant_is_active(db, connection_repo, oauth, connection, garmin_user_id, trace_id):
                log_structured(
                    logger,
                    "info",
                    "Ignored stale Garmin deregistration ping, current grant is active",
                    provider="garmin",
                    trace_id=trace_id,
                    garmin_user_id=garmin_user_id,
                    user_id=str(connection.user_id),
                )
                results["ignored_stale"] += 1
                continue

            # _current_grant_is_active may have refreshed the row; an earlier revoke (our own
            # disconnect) must not emit a second connection.revoked.
            db.refresh(connection)
            if connection.status == ConnectionStatus.REVOKED:
                continue
            connection_repo.mark_as_revoked(db, connection)
            on_connection_revoked(
                user_id=connection.user_id,
                provider="garmin",
                connection_id=connection.id,
                reason="deregistration",
                revoked_at=connection.updated_at.isoformat(),
            )

            log_structured(
                logger,
                "info",
                "Revoked connection via deregistration webhook",
                action="connection_revoked",
                reason="deregistration",
                provider="garmin",
                trace_id=trace_id,
                garmin_user_id=garmin_user_id,
                user_id=str(connection.user_id),
            )
            results["revoked"] += 1

    return results
