"""Idempotency-Key support for routes that create something outside OW.

A client whose create request timed out can't tell whether the object was created. Sending the
retry with the same ``Idempotency-Key`` returns the first result instead of creating a second
object. Keys are scoped by the caller (API key or developer, user, route) and remembered for
``IDEMPOTENCY_TTL`` once the request succeeded. Each key is tied to a fingerprint of the request
body, so a key reused for a different request is refused instead of answered with the wrong result.

Only successful responses are remembered. A request that fails releases its key, so the retry runs
again: whether to retry an error is the client's call, made with the error in hand.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any

from app.integrations.redis_client import get_redis_client

logger = logging.getLogger(__name__)

IDEMPOTENCY_TTL = timedelta(hours=24)
# How long a key stays claimed by a request still running. Longer than any request can take, so a
# crashed worker's claim lapses on its own.
PENDING_TTL = timedelta(minutes=2)

MAX_KEY_LENGTH = 255

_PENDING = "pending"
_DONE = "done"


class IdempotencyConflictReason(StrEnum):
    IN_PROGRESS = "in_progress"
    DIFFERENT_REQUEST = "different_request"


class IdempotencyConflictError(Exception):
    def __init__(self, reason: IdempotencyConflictReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True)
class StoredResponse:
    status_code: int
    body: Any


def request_fingerprint(payload: Any) -> str:
    """Stable hash of a JSON-compatible request body."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class IdempotencyStore:
    """Redis-backed claim/replay of Idempotency-Keys within one namespace."""

    def __init__(self, namespace: str) -> None:
        self.namespace = namespace

    def _redis_key(self, scope: str, key: str) -> str:
        # Hashed: the key is caller-supplied, so its length and characters are not ours to trust.
        digest = hashlib.sha256(f"{scope}\x00{key}".encode()).hexdigest()
        return f"idempotency:{self.namespace}:{digest}"

    def begin(self, scope: str, key: str, fingerprint: str) -> StoredResponse | None:
        """Claim ``key`` for this request, or return the response stored for it.

        None means the caller now owns the key and must call ``complete`` or ``release``.
        Raises IdempotencyConflictError when the key is held by a request still running, or was used
        for a different request body.
        """
        redis_client = get_redis_client()
        redis_key = self._redis_key(scope, key)
        claim = json.dumps({"state": _PENDING, "fingerprint": fingerprint})
        for _ in range(2):
            if redis_client.set(redis_key, claim, nx=True, ex=PENDING_TTL):
                return None
            raw = redis_client.get(redis_key)
            if raw is None:
                continue  # expired between the two calls: claim it on the next pass
            entry = json.loads(raw)
            if entry.get("fingerprint") != fingerprint:
                raise IdempotencyConflictError(IdempotencyConflictReason.DIFFERENT_REQUEST)
            if entry.get("state") != _DONE:
                raise IdempotencyConflictError(IdempotencyConflictReason.IN_PROGRESS)
            return StoredResponse(status_code=int(entry["status_code"]), body=entry["body"])
        raise IdempotencyConflictError(IdempotencyConflictReason.IN_PROGRESS)

    def complete(self, scope: str, key: str, fingerprint: str, status_code: int, body: Any) -> None:
        """Remember the successful response for ``key``."""
        entry = json.dumps({"state": _DONE, "fingerprint": fingerprint, "status_code": status_code, "body": body})
        get_redis_client().set(self._redis_key(scope, key), entry, ex=IDEMPOTENCY_TTL)

    def release(self, scope: str, key: str) -> None:
        """Give up the claim so a retry runs the request again."""
        get_redis_client().delete(self._redis_key(scope, key))
