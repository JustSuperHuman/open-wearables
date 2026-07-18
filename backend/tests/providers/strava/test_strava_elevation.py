"""Unit tests for the Strava elevation-profile builder and downsampler."""

from app.schemas.providers.strava import StravaStreamSetJSON
from app.services.providers.strava.workouts import (
    ELEVATION_PROFILE_MAX_POINTS,
    _downsample_elevation_points,
    build_elevation_profile,
)


def test_build_elevation_profile_pairs_altitude_distance_time() -> None:
    streams = StravaStreamSetJSON(
        altitude={"data": [10.0, 12.5, 9.0]},
        distance={"data": [0.0, 100.0, 200.0]},
        time={"data": [0, 30, 60]},
    )

    profile = build_elevation_profile(streams)

    assert profile == [
        {"distance_m": 0.0, "altitude_m": 10.0, "time_s": 0},
        {"distance_m": 100.0, "altitude_m": 12.5, "time_s": 30},
        {"distance_m": 200.0, "altitude_m": 9.0, "time_s": 60},
    ]


def test_build_elevation_profile_without_altitude_returns_none() -> None:
    # Indoor/manual activities have no altitude stream.
    streams = StravaStreamSetJSON(distance={"data": [0.0, 100.0]}, time={"data": [0, 30]})
    assert build_elevation_profile(streams) is None

    # Empty altitude data is also unusable.
    assert build_elevation_profile(StravaStreamSetJSON(altitude={"data": []})) is None


def test_build_elevation_profile_handles_missing_distance() -> None:
    streams = StravaStreamSetJSON(
        altitude={"data": [5.0, 6.0]},
        time={"data": [0, 10]},
    )

    profile = build_elevation_profile(streams)

    assert profile == [
        {"distance_m": None, "altitude_m": 5.0, "time_s": 0},
        {"distance_m": None, "altitude_m": 6.0, "time_s": 10},
    ]


def test_build_elevation_profile_downsamples_but_keeps_extremes() -> None:
    n = 5000
    # Build a wave with a unique global min near the start and max near the end.
    altitudes = [100.0 + (i % 50) for i in range(n)]
    altitudes[10] = -999.0  # unique lowest point
    altitudes[4990] = 9999.0  # unique highest point
    distances = [float(i) for i in range(n)]
    times = list(range(n))

    streams = StravaStreamSetJSON(
        altitude={"data": altitudes},
        distance={"data": distances},
        time={"data": times},
    )

    profile = build_elevation_profile(streams)
    assert profile is not None

    # Capped near the max (plus the few forced first/last/min/max indices).
    assert len(profile) <= ELEVATION_PROFILE_MAX_POINTS + 4

    profile_altitudes = [p["altitude_m"] for p in profile]
    # True low/high points survive downsampling so the chart markers stay exact.
    assert min(profile_altitudes) == -999.0
    assert max(profile_altitudes) == 9999.0
    # First and last samples are preserved (ordered).
    assert profile[0]["distance_m"] == 0.0
    assert profile[-1]["distance_m"] == float(n - 1)


def test_downsample_is_a_noop_below_cap() -> None:
    points = [{"distance_m": float(i), "altitude_m": float(i), "time_s": i} for i in range(10)]
    assert _downsample_elevation_points(points, 300) == points
