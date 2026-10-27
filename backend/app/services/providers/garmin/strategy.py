from uuid import UUID

from app.config import settings
from app.integrations.celery.tasks.garmin.backfill_task import start_full_backfill as start_garmin_full_backfill
from app.services.providers.base_strategy import (
    BaseProviderStrategy,
    HistoricalSyncResult,
    ProviderCapabilities,
    ProviderCoverage,
)
from app.services.providers.garmin.coverage import (
    HEALTH_SCORES,
    MENSTRUAL_CYCLE_FIELDS,
    SLEEP_FIELDS,
    TIMESERIES,
    WORKOUT_FIELDS,
)
from app.services.providers.garmin.data_247 import Garmin247Data
from app.services.providers.garmin.oauth import GarminOAuth
from app.services.providers.garmin.training import GarminTrainingService
from app.services.providers.garmin.webhook_handler import GarminWebhookHandler
from app.services.providers.garmin.workouts import GarminWorkouts
from app.services.providers.garmin.write_client import GarminWriteClient
from app.utils.exceptions import UnsupportedProviderError


class GarminStrategy(BaseProviderStrategy):
    """Garmin provider implementation.

    Supports:
    - OAuth 2.0 with PKCE
    - Workouts/activities
    - 24/7 data (sleep, dailies, epochs, body composition)
    - Pushing workouts, workout schedules and courses (Training API V2, Courses API)
    """

    def __init__(self):
        super().__init__()
        self.oauth = GarminOAuth(
            user_repo=self.user_repo,
            connection_repo=self.connection_repo,
            provider_name=self.name,
            api_base_url=self.api_base_url,
        )
        self.workouts = GarminWorkouts(
            workout_repo=self.workout_repo,
            connection_repo=self.connection_repo,
            provider_name=self.name,
            api_base_url=self.api_base_url,
            oauth=self.oauth,
        )
        # 24/7 data handler for sleep, dailies, epochs, body composition
        self.data_247 = Garmin247Data(
            provider_name=self.name,
            api_base_url=self.api_base_url,
            oauth=self.oauth,
        )
        self.webhooks = GarminWebhookHandler(
            garmin_workouts=self.workouts,
            garmin_247=self.data_247,
            oauth=self.oauth,
        )
        # Outbound: workouts, schedules and courses written to the user's Garmin Connect account
        self.training = GarminTrainingService(
            client=GarminWriteClient(
                oauth=self.oauth,
                connection_repo=self.connection_repo,
                api_base_url=self.api_base_url,
            ),
            connection_repo=self.connection_repo,
        )

    @property
    def name(self) -> str:
        return "garmin"

    @property
    def api_base_url(self) -> str:
        return "https://apis.garmin.com"

    @property
    def capabilities(self) -> ProviderCapabilities:
        # Garmin delivers the full data payload inside every webhook (PUSH) and
        # also supports an async backfill flow (PING → callback URL fetch).
        # There is no plain REST polling path for wellness data; all data
        # arrives via the push/backfill mechanism. The backfill API is
        # Enterprise-only, so it is advertised only while GARMIN_BACKFILL_ENABLED
        # is on; with it off, connecting a user starts live push data only.
        return ProviderCapabilities(
            webhook_stream=True,
            webhook_callback=settings.garmin_backfill_enabled,
            max_historical_days=30,
        )

    @property
    def coverage(self) -> ProviderCoverage:
        return ProviderCoverage(
            timeseries=TIMESERIES,
            workout_fields=WORKOUT_FIELDS,
            sleep_fields=SLEEP_FIELDS,
            menstrual_cycle_fields=MENSTRUAL_CYCLE_FIELDS,
            health_scores=HEALTH_SCORES,
        )

    def start_historical_sync(self, user_id: UUID, days: int) -> HistoricalSyncResult:
        """Trigger Garmin's webhook-based 30-day backfill.

        The ``days`` parameter is ignored - Garmin limits historical access
        to 30 days before the user's consent date.

        Raises UnsupportedProviderError when GARMIN_BACKFILL_ENABLED is off
        (Starter-tier apps have no access to historical data).
        """
        if not settings.garmin_backfill_enabled:
            raise UnsupportedProviderError(
                self.name, "historical sync on this deployment (GARMIN_BACKFILL_ENABLED=false)"
            )
        task = start_garmin_full_backfill.delay(str(user_id))
        return HistoricalSyncResult(
            task_id=task.id,
            method="webhook_backfill",
            message=f"Garmin {self.capabilities.max_historical_days}-day backfill started. "
            "Progress available via backfill/status.",
            days=None,
        )
