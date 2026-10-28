"""Tests for the Redis-backed Idempotency-Key store."""

import pytest

from app.integrations.redis_client import get_redis_client
from app.services.idempotency import (
    IDEMPOTENCY_TTL,
    PENDING_TTL,
    IdempotencyConflictError,
    IdempotencyConflictReason,
    IdempotencyStore,
    StoredResponse,
    request_fingerprint,
)


@pytest.fixture
def store() -> IdempotencyStore:
    return IdempotencyStore("test")


def _ttl(store: IdempotencyStore, scope: str, key: str) -> int:
    return int(get_redis_client().ttl(store._redis_key(scope, key)))  # ty:ignore[invalid-argument-type]


class TestIdempotencyStore:
    def test_first_use_claims_the_key(self, store: IdempotencyStore) -> None:
        assert store.begin("user-1:workouts", "k", "fp") is None
        assert 0 < _ttl(store, "user-1:workouts", "k") <= PENDING_TTL.total_seconds()

    def test_completed_key_replays_the_response(self, store: IdempotencyStore) -> None:
        store.begin("s", "k", "fp")
        store.complete("s", "k", "fp", 201, {"workout_id": 5})

        assert store.begin("s", "k", "fp") == StoredResponse(status_code=201, body={"workout_id": 5})
        assert _ttl(store, "s", "k") > IDEMPOTENCY_TTL.total_seconds() - 60

    def test_key_in_use_is_a_conflict(self, store: IdempotencyStore) -> None:
        store.begin("s", "k", "fp")

        with pytest.raises(IdempotencyConflictError) as exc:
            store.begin("s", "k", "fp")
        assert exc.value.reason is IdempotencyConflictReason.IN_PROGRESS

    def test_different_body_is_a_conflict(self, store: IdempotencyStore) -> None:
        store.begin("s", "k", "fp-1")
        store.complete("s", "k", "fp-1", 201, {})

        with pytest.raises(IdempotencyConflictError) as exc:
            store.begin("s", "k", "fp-2")
        assert exc.value.reason is IdempotencyConflictReason.DIFFERENT_REQUEST

    def test_released_key_can_be_claimed_again(self, store: IdempotencyStore) -> None:
        store.begin("s", "k", "fp")
        store.release("s", "k")

        assert store.begin("s", "k", "fp") is None

    def test_keys_are_scoped(self, store: IdempotencyStore) -> None:
        store.begin("user-1:workouts", "k", "fp")

        assert store.begin("user-2:workouts", "k", "fp") is None
        assert IdempotencyStore("other").begin("user-1:workouts", "k", "fp") is None

    def test_fingerprint_ignores_key_order(self) -> None:
        assert request_fingerprint({"a": 1, "b": [1, 2]}) == request_fingerprint({"b": [1, 2], "a": 1})
        assert request_fingerprint({"a": 1}) != request_fingerprint({"a": 2})
