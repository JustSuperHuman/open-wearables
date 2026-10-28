import logging
import time
from uuid import UUID

import httpx
from celery import current_app as celery_app

from app.config import settings
from app.constants.celery_tasks import CeleryTask
from app.database import DbSession
from app.schemas.auth import (
    AuthenticationMethod,
    ConnectionStatus,
)
from app.schemas.enums import ProviderName
from app.schemas.model_crud.credentials import (
    OAuthState,
    OAuthTokenResponse,
    ProviderCredentials,
    ProviderEndpoints,
)
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

USER_ID_PATH = "/partner-gateway/rest/user/id"
PERMISSIONS_PATH = "/partner-gateway/rest/user/permissions"

# Attempts at reading the Garmin user id right after the code exchange, and the pause before each retry.
USER_ID_ATTEMPTS = 3
USER_ID_RETRY_DELAYS_SECONDS = (0.5, 1.5)
# When every attempt fails, a task looks it up again after this long (and retries on its own).
USER_ID_RESOLVE_COUNTDOWN_SECONDS = 30


class GarminOAuth(BaseOAuthTemplate):
    """Garmin OAuth 2.0 with PKCE implementation."""

    # Garmin pushes data whether or not our tokens are alive; only its deregistration push (or our
    # own disconnect) ends consent. A rejected refresh token therefore marks the connection EXPIRED:
    # pushes keep being stored, writes answer garmin_not_connected until the user reconnects.
    token_rejected_status = ConnectionStatus.EXPIRED

    @property
    def endpoints(self) -> ProviderEndpoints:
        return ProviderEndpoints(
            authorize_url="https://connect.garmin.com/oauth2Confirm",
            token_url="https://diauth.garmin.com/di-oauth2-service/oauth/token",
        )

    @property
    def credentials(self) -> ProviderCredentials:
        return ProviderCredentials(
            client_id=settings.garmin_client_id or "",
            client_secret=(settings.garmin_client_secret.get_secret_value() if settings.garmin_client_secret else ""),
            redirect_uri=settings.oauth_redirect_uri(ProviderName.GARMIN),
            default_scope=settings.garmin_default_scope,
        )

    use_pkce = True
    auth_method = AuthenticationMethod.BODY

    def deregister_user(self, access_token: str, provider_user_id: str | None = None) -> None:
        """Call Garmin's user deregistration endpoint to remove the app association."""
        response = httpx.delete(
            f"{self.api_base_url}/partner-gateway/rest/user/registration",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30.0,
        )
        response.raise_for_status()

    def get_user_id(self, access_token: str) -> str | None:
        response = httpx.get(
            f"{self.api_base_url}{USER_ID_PATH}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30.0,
        )
        response.raise_for_status()
        return response.json().get("userId")

    def _get_user_id_with_retries(self, access_token: str) -> str | None:
        """Garmin's user id, retried a few times: a connection without it can't be matched to pushes."""
        for attempt in range(USER_ID_ATTEMPTS):
            try:
                user_id = self.get_user_id(access_token)
                if user_id:
                    return str(user_id)
            except Exception as exc:
                log_structured(
                    logger,
                    "warning",
                    "Garmin user id lookup failed",
                    provider=self.provider_name,
                    action="garmin_user_id_lookup_failed",
                    attempt=attempt + 1,
                    error_type=type(exc).__name__,
                )
            if attempt < len(USER_ID_RETRY_DELAYS_SECONDS):
                time.sleep(USER_ID_RETRY_DELAYS_SECONDS[attempt])
        return None

    def _get_provider_user_info(self, token_response: OAuthTokenResponse, user_id: str) -> dict[str, str | None]:
        """Fetches Garmin user ID and API permissions."""
        provider_user_id = self._get_user_id_with_retries(token_response.access_token)
        if provider_user_id is None:
            # Saved anyway (the tokens are good and Garmin already counts the user as registered);
            # _save_connection queues a task that fills the id in.
            return {"user_id": None, "username": None, "scope": None}

        # Fetch permissions (best-effort - fail returns scope as None)
        scope: str | None = None
        try:
            permissions_response = httpx.get(
                f"{self.api_base_url}{PERMISSIONS_PATH}",
                headers={"Authorization": f"Bearer {token_response.access_token}"},
                timeout=30.0,
            )
            permissions_response.raise_for_status()
            data = permissions_response.json()
            # Response format: {"permissions": ["ACTIVITY_EXPORT", "HEALTH_EXPORT", ...]}
            permissions = data.get("permissions", [])
            if permissions:
                scope = " ".join(sorted(permissions))
        except Exception:
            logger.warning(
                "Failed to fetch Garmin permissions for user %s, will retry via webhook",
                provider_user_id,
                exc_info=True,
            )

        return {"user_id": provider_user_id, "username": None, "scope": scope}

    def _save_connection(
        self,
        db: DbSession,
        user_id: UUID,
        token_response: OAuthTokenResponse,
        user_info: dict[str, str | None],
        oauth_state: OAuthState,
    ) -> None:
        super()._save_connection(db, user_id, token_response, user_info, oauth_state)
        if user_info.get("user_id"):
            return
        # Without the Garmin user id, pushes for this user can't be matched to the connection. The
        # task reads it again (and, on a reconnect that kept an older id, corrects that one too).
        log_structured(
            logger,
            "warning",
            "Garmin connection saved without a Garmin user id, resolving it in the background",
            provider=self.provider_name,
            action="garmin_user_id_deferred",
            user_id=str(user_id),
        )
        try:
            celery_app.send_task(
                CeleryTask.GARMIN_RESOLVE_USER_ID,
                args=[str(user_id)],
                countdown=USER_ID_RESOLVE_COUNTDOWN_SECONDS,
            )
        except Exception:
            # The hourly token maintenance job picks up connections without a Garmin user id too.
            logger.warning("Could not enqueue the Garmin user id lookup", exc_info=True)
