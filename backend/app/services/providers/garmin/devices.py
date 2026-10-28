"""One spelling per Garmin device, so a device's workouts and samples share a data source.

Garmin spells the same device differently across summary types: the Activity API's own examples
show "Garmin fenix 8" on activityDetails and "Garmin Fenix 8" on activityFiles. Data sources are
matched on the exact device model, so the FIT samples of a workout would land on a second source
for the same watch. The first spelling stored for the user wins; later spellings that differ only
in case are mapped to it.
"""

from uuid import UUID

from app.constants.devices_map import normalize_device_model
from app.database import DbSession
from app.repositories.data_source_repository import DataSourceRepository
from app.schemas.enums import ProviderName


class GarminDeviceNames:
    """The canonical device-model spelling for one user's Garmin data. Build one per batch."""

    def __init__(self, db: DbSession, user_id: UUID, data_source_repo: DataSourceRepository | None = None) -> None:
        repo = data_source_repo or DataSourceRepository()
        stored = repo.get_device_models(db, user_id, ProviderName.GARMIN)
        self._by_folded: dict[str, str] = {}
        for model in stored:
            self._by_folded.setdefault(model.casefold(), model)

    def canonical(self, device_name: str | None) -> str | None:
        """The stored spelling of this device, or its own (normalized) name when it is new."""
        normalized = normalize_device_model(device_name)
        if normalized is None:
            return None
        return self._by_folded.setdefault(normalized.casefold(), normalized)
