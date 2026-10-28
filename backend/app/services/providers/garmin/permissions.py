"""The Garmin permissions stored on a connection, written in order.

Two things write them: userPermissionsChange pushes, which carry Garmin's ``changeTimeInSeconds``
and can arrive late or twice, and live reads of Garmin's permissions endpoint, which are current
as of the moment they're made. Every write goes through ``apply_permissions`` under a per-connection
lock and records the time it is current as of, so an older state never overwrites a newer one.

The time is kept in Redis rather than on the connection row: losing it (a Redis flush) only means
the next write is accepted whatever its age, which is how pushes were handled before.
"""

import logging
from collections.abc import Iterable

from app.database import DbSession
from app.integrations.redis_client import get_redis_client
from app.models import UserConnection
from app.repositories import UserConnectionRepository
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

_AS_OF_KEY = "garmin:permissions_as_of:{connection_id}"
_LOCK_KEY = "garmin:permissions_lock:{connection_id}"
# Longer than any redelivery delay worth guarding against.
_AS_OF_TTL_SECONDS = 120 * 24 * 3600


def scope_from_permissions(permissions: Iterable[str]) -> str:
    """The stored form: sorted and space-separated. Granting nothing is an empty string, not None
    (None means we don't know)."""
    return " ".join(sorted({str(p) for p in permissions}))


def apply_permissions(
    db: DbSession,
    connection_repo: UserConnectionRepository,
    connection: UserConnection,
    permissions: Iterable[str],
    *,
    as_of: float,
) -> bool:
    """Store ``permissions`` on the connection unless a newer state is already stored.

    ``as_of`` is the Unix time the permissions are current as of. Returns False when they were
    older than what's stored and were left out.
    """
    redis_client = get_redis_client()
    as_of_key = _AS_OF_KEY.format(connection_id=connection.id)
    with redis_client.lock(_LOCK_KEY.format(connection_id=connection.id), timeout=30, blocking_timeout=10):
        stored_as_of = redis_client.get(as_of_key)
        if stored_as_of is not None and float(stored_as_of) > as_of:
            log_structured(
                logger,
                "info",
                "Ignored Garmin permissions older than the stored ones",
                provider="garmin",
                action="garmin_permissions_stale",
                user_id=str(connection.user_id),
                as_of=as_of,
                stored_as_of=float(stored_as_of),
            )
            return False
        db.refresh(connection)
        new_scope = scope_from_permissions(permissions)
        if connection.scope != new_scope:
            connection_repo.update_scope(db, connection, new_scope)
        redis_client.set(as_of_key, str(as_of), ex=_AS_OF_TTL_SECONDS)
    return True
