import { linePath, scaleY, type Box, type Point } from '$lib/charts/geometry';
import { formatDecimal } from '$lib/utils/format';
import type { ElevationSample } from './types';

/** What the horizontal axis counts: distance where every sample has one, then time, then order. */
export type ElevationAxis = 'km' | 'min' | 'index';

/** `at` is the position on the axis, `value` the altitude in metres. */
export type ElevationProfile = {
	axis: ElevationAxis;
	points: Point[];
	low: Point;
	high: Point;
	/** The altitudes drawn against, padded so the line never runs along the frame. */
	range: { low: number; high: number };
};

/** Headroom over the climb, so a flat ride is not drawn as a mountain range. */
const MIN_PAD_M = 5;
const PAD_SHARE = 0.18;

const has = (profile: ElevationSample[], key: 'distance_m' | 'time_s') =>
	profile.every((sample) => sample[key] !== null);

/** Null under two samples: one altitude is a number, not a profile. */
export function elevationProfile(profile: ElevationSample[]): ElevationProfile | null {
	if (profile.length < 2) return null;

	// A partial distance stream would put its gaps back at zero and fold the line onto itself.
	const axis: ElevationAxis = has(profile, 'distance_m')
		? 'km'
		: has(profile, 'time_s')
			? 'min'
			: 'index';

	const points = profile.map((sample, index) => ({
		at: axis === 'km' ? sample.distance_m! / 1000 : axis === 'min' ? sample.time_s! / 60 : index,
		value: sample.altitude_m
	}));

	const low = points.reduce((best, point) => (point.value < best.value ? point : best));
	const high = points.reduce((best, point) => (point.value > best.value ? point : best));
	const pad = Math.max((high.value - low.value) * PAD_SHARE, MIN_PAD_M);

	return { axis, points, low, high, range: { low: low.value - pad, high: high.value + pad } };
}

export const axisLabel = (at: number, axis: ElevationAxis): string =>
	axis === 'km'
		? `${formatDecimal(at, at < 10 ? 2 : 1)} km`
		: axis === 'min'
			? `${Math.round(at)} min`
			: `#${Math.round(at) + 1}`;

const windowOf = (profile: ElevationProfile) => ({
	from: profile.points[0].at,
	to: profile.points[profile.points.length - 1].at
});

/** The line, and the same line closed along the floor of the box to fill under it. */
export function elevationPaths(
	profile: ElevationProfile,
	box: Box
): { line: string; area: string } {
	const line = linePath(profile.points, windowOf(profile), profile.range, box);
	return { line, area: `${line} L${box.width} ${box.height} L0 ${box.height} Z` };
}

/** A point's place in the frame, in percent, for HTML laid over a stretched SVG. */
export function placeOf(
	profile: ElevationProfile,
	point: Point,
	box: Box
): { x: number; y: number } {
	const { from, to } = windowOf(profile);
	const y = scaleY(profile.range.low, profile.range.high, box);
	return {
		x: ((point.at - from) / Math.max(to - from, Number.EPSILON)) * 100,
		y: (y(point.value) / box.height) * 100
	};
}

/** The sample nearest a fraction (0 to 1) of the way along the axis. */
export function nearestAt(profile: ElevationProfile, fraction: number): Point {
	const { from, to } = windowOf(profile);
	const at = from + Math.min(Math.max(fraction, 0), 1) * (to - from);
	return profile.points.reduce((best, point) =>
		Math.abs(point.at - at) < Math.abs(best.at - at) ? point : best
	);
}
