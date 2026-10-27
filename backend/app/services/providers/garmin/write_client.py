"""Authenticated calls to Garmin's write APIs (Training API V2, Courses API, user permissions).

Integrators hold no Garmin tokens, so every write to a user's Garmin Connect account goes
through OW. Token lookup and refresh reuse ``get_valid_token`` (per-user Redis lock, revoke on a
dead refresh token). What this module adds is what a synchronous proxy needs on top of it:

- Garmin's error semantics mapped to a small set of stable error codes callers can branch on.
- No sleeping retries. ``make_authenticated_request`` backs off for up to two minutes on a 429,
  which is right for background sync but would hold an API request open; a 429 is surfaced
  straight away, with Garmin's ``Retry-After`` when it sends one.

Tokens never appear in errors or logs: requests are logged by method, path and status only.
"""

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

import httpx
from fastapi import HTTPException

from app.config import settings
from app.database import DbSession
from app.models import UserConnection
from app.repositories import UserConnectionRepository
from app.services.providers.api_client import get_valid_token
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

PROVIDER = "garmin"

# Garmin permission names (OAuth2 PKCE spec, user permissions endpoint).
WORKOUT_IMPORT = "WORKOUT_IMPORT"
COURSE_IMPORT = "COURSE_IMPORT"

# Scopes Garmin puts in every token response. The OAuth callback stores them as the connection
# scope when the permissions fetch fails, so they say nothing about what the user granted.
GARMIN_OAUTH_SCOPES = frozenset({"PARTNER_WRITE", "PARTNER_READ", "CONNECT_READ", "CONNECT_WRITE"})

PERMISSIONS_PATH = "/partner-gateway/rest/user/permissions"

# Garmin error bodies are short; anything longer is not a message worth passing on.
_MAX_MESSAGE_LENGTH = 500


class GarminErrorCode(StrEnum):
    NOT_CONNECTED = "garmin_not_connected"
    PERMISSION_MISSING = "garmin_permission_missing"
    RATE_LIMITED = "garmin_rate_limited"
    VALIDATION_ERROR = "garmin_validation_error"
    UPSTREAM_ERROR = "garmin_upstream_error"


_HTTP_STATUS_BY_CODE: dict[GarminErrorCode, int] = {
    GarminErrorCode.NOT_CONNECTED: 404,
    GarminErrorCode.PERMISSION_MISSING: 403,
    GarminErrorCode.RATE_LIMITED: 429,
    GarminErrorCode.VALIDATION_ERROR: 422,
    GarminErrorCode.UPSTREAM_ERROR: 502,
}


class GarminApiError(Exception):
    """A Garmin write failed in a way the caller can act on.

    ``status_code`` is the HTTP status OW answers with; ``garmin_status`` is what Garmin
    returned, when the failure came from Garmin at all.
    """

    def __init__(
        self,
        code: GarminErrorCode,
        detail: str,
        *,
        retry_after: str | None = None,
        garmin_status: int | None = None,
    ) -> None:
        self.code = code
        self.detail = detail
        self.status_code = _HTTP_STATUS_BY_CODE[code]
        self.retry_after = retry_after
        self.garmin_status = garmin_status
        super().__init__(detail)


@dataclass(frozen=True)
class GarminResponse:
    status_code: int
    # Parsed JSON when Garmin sent JSON, the raw text when it sent something else, None when empty.
    body: Any


def stored_permissions(scope: str | None) -> list[str] | None:
    """Permissions recorded on the connection, or None when we don't actually know them.

    None covers a missing scope and a scope that only holds Garmin's generic OAuth scopes.
    """
    if not scope:
        return None
    permissions = sorted({p for p in scope.split() if p not in GARMIN_OAUTH_SCOPES})
    return permissions or None


def _parse_body(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


def _error_message(body: Any) -> str:
    """Pull the human-readable part out of a Garmin error body."""
    if isinstance(body, dict):
        for key in ("message", "errorMessage", "error_description", "error", "detail", "errors"):
            value = body.get(key)
            if value:
                return str(value)[:_MAX_MESSAGE_LENGTH]
    if body:
        return str(body)[:_MAX_MESSAGE_LENGTH]
    return "no error message"


class GarminWriteClient:
    """Makes authenticated Garmin API calls for a single OW user and maps Garmin's errors."""

    def __init__(
        self,
        oauth: BaseOAuthTemplate,
        connection_repo: UserConnectionRepository,
        api_base_url: str,
    ) -> None:
        self.oauth = oauth
        self.connection_repo = connection_repo
        self.api_base_url = api_base_url

    def get_active_connection(self, db: DbSession, user_id: UUID) -> UserConnection:
        connection = self.connection_repo.get_active_connection(db, user_id, PROVIDER)
        if connection is None or not connection.access_token:
            raise GarminApiError(GarminErrorCode.NOT_CONNECTED, "User has no active Garmin connection.")
        return connection

    def request(
        self,
        db: DbSession,
        user_id: UUID,
        method: str,
        path: str,
        *,
        json_body: Any = None,
    ) -> GarminResponse:
        """Call Garmin as the user and return the parsed response; raise GarminApiError on failure."""
        access_token = self._access_token(db, user_id)
        try:
            response = httpx.request(
                method,
                f"{self.api_base_url}{path}",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                json=json_body,
                timeout=settings.provider_request_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            log_structured(
                logger,
                "warning",
                "Garmin write request did not complete",
                provider=PROVIDER,
                action="garmin_write_transport_error",
                user_id=str(user_id),
                method=method,
                path=path,
                error_type=type(exc).__name__,
            )
            raise GarminApiError(
                GarminErrorCode.UPSTREAM_ERROR, f"Garmin did not respond ({type(exc).__name__})."
            ) from None

        body = _parse_body(response)
        if response.is_success:
            return GarminResponse(status_code=response.status_code, body=body)

        error = self._map_error(response.status_code, body, response.headers.get("Retry-After"))
        log_structured(
            logger,
            "warning",
            "Garmin write request failed",
            provider=PROVIDER,
            action="garmin_write_failed",
            user_id=str(user_id),
            method=method,
            path=path,
            garmin_status=response.status_code,
            code=error.code.value,
            garmin_message=_error_message(body),
        )
        raise error

    def fetch_permissions(self, db: DbSession, user_id: UUID) -> list[str]:
        """Permissions the user currently grants this app, read live from Garmin."""
        response = self.request(db, user_id, "GET", PERMISSIONS_PATH)
        permissions = response.body.get("permissions") if isinstance(response.body, dict) else None
        if not isinstance(permissions, list):
            raise GarminApiError(GarminErrorCode.UPSTREAM_ERROR, "Garmin returned an unexpected permissions response.")
        return sorted({str(p) for p in permissions})

    def _access_token(self, db: DbSession, user_id: UUID) -> str:
        try:
            return get_valid_token(db, user_id, PROVIDER, self.connection_repo, self.oauth)
        except HTTPException as exc:
            # 401 covers a missing connection, a missing refresh token and a refresh token
            # Garmin rejected (the connection is revoked in that case). Anything else is the
            # token endpoint misbehaving.
            if exc.status_code == 401:
                raise GarminApiError(
                    GarminErrorCode.NOT_CONNECTED,
                    "The Garmin authorization has expired or was withdrawn; the user needs to reconnect Garmin.",
                ) from None
            raise GarminApiError(GarminErrorCode.UPSTREAM_ERROR, "Could not refresh the Garmin access token.") from None
        except Exception as exc:
            log_structured(
                logger,
                "warning",
                "Could not obtain a Garmin access token",
                provider=PROVIDER,
                action="garmin_write_token_error",
                user_id=str(user_id),
                error_type=type(exc).__name__,
            )
            raise GarminApiError(GarminErrorCode.UPSTREAM_ERROR, "Could not obtain a Garmin access token.") from None

    @staticmethod
    def _map_error(status_code: int, body: Any, retry_after: str | None) -> GarminApiError:
        message = _error_message(body)
        match status_code:
            case 401:
                return GarminApiError(
                    GarminErrorCode.NOT_CONNECTED,
                    "Garmin no longer accepts this user's authorization; the user needs to reconnect Garmin.",
                    garmin_status=status_code,
                )
            case 412:
                return GarminApiError(
                    GarminErrorCode.PERMISSION_MISSING,
                    f"Garmin refused the call because the user hasn't granted the permission it needs: {message}",
                    garmin_status=status_code,
                )
            case 429:
                return GarminApiError(
                    GarminErrorCode.RATE_LIMITED,
                    "Garmin rate limit reached for this app or user; retry later.",
                    retry_after=retry_after,
                    garmin_status=status_code,
                )
            case _ if 400 <= status_code < 500:
                return GarminApiError(
                    GarminErrorCode.VALIDATION_ERROR,
                    f"Garmin rejected the request (HTTP {status_code}): {message}",
                    garmin_status=status_code,
                )
            case _:
                return GarminApiError(
                    GarminErrorCode.UPSTREAM_ERROR,
                    f"Garmin returned HTTP {status_code}.",
                    garmin_status=status_code,
                )
