from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

# Average cadence per sport, first match wins. Garmin sends the one for the activity's sport.
_CADENCE_FIELDS = (
    "averageRunCadenceInStepsPerMinute",
    "averageBikeCadenceInRoundsPerMinute",
    "averageSwimCadenceInStrokesPerMinute",
    "averagePushCadenceInPushesPerMinute",
)


class ActivityJSON(BaseModel):
    """Garmin activity summary (Activity API: activities, manuallyUpdatedActivities, activityDetails.summary).

    Field names and types follow the Activity API summary model. Garmin documents most measurements
    as floating point, so they are typed as floats here even where values are usually whole (a max
    heart rate of 171.5 must not invalidate the whole activity); the few documented as integers
    (counts, timestamps) stay int.
    """

    model_config = ConfigDict(populate_by_name=True)

    # Required fields
    userId: str
    activityId: int | str  # Push sends int, API may send str
    activityType: str
    startTimeInSeconds: int
    durationInSeconds: int

    # Identity and provenance
    summaryId: str | None = None
    activityName: str | None = None
    startTimeOffsetInSeconds: int | None = None
    deviceName: str | None = None
    manual: bool | None = None
    isWebUpload: bool | None = None
    isParent: bool | None = None
    parentSummaryId: str | int | None = None

    # Totals
    distanceInMeters: float | None = None
    activeKilocalories: float | None = None
    steps: int | None = None
    pushes: int | None = None
    numberOfActiveLengths: int | None = None
    totalElevationGainInMeters: float | None = None
    totalElevationLossInMeters: float | None = None

    # Heart rate
    averageHeartRateInBeatsPerMinute: float | None = None
    maxHeartRateInBeatsPerMinute: float | None = None

    # Speed and pace
    averageSpeedInMetersPerSecond: float | None = None
    maxSpeedInMetersPerSecond: float | None = None
    averagePaceInMinutesPerKilometer: float | None = None
    maxPaceInMinutesPerKilometer: float | None = None

    # Cadence
    averageRunCadenceInStepsPerMinute: float | None = None
    maxRunCadenceInStepsPerMinute: float | None = None
    averageBikeCadenceInRoundsPerMinute: float | None = None
    maxBikeCadenceInRoundsPerMinute: float | None = None
    averageSwimCadenceInStrokesPerMinute: float | None = None
    averagePushCadenceInPushesPerMinute: float | None = None
    maxPushCadenceInPushesPerMinute: float | None = None

    # Start position
    startingLatitudeInDegree: float | None = None
    startingLongitudeInDegree: float | None = None

    # Derived: the average cadence of whichever sport the activity is.
    averageCadence: float | None = None

    @model_validator(mode="before")
    @classmethod
    def _unify_cadence(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if data.get("averageCadence") is None:
            # A zero for another sport's cadence is not this activity's cadence.
            data["averageCadence"] = next((data[field] for field in _CADENCE_FIELDS if data.get(field)), None)
        return data


class RootJSON(BaseModel):
    activities: list[ActivityJSON]
    error: str | None = None
