import { describe, expect, it } from 'vitest';
import Activity from '@lucide/svelte/icons/activity';
import Bike from '@lucide/svelte/icons/bike';
import CircleDot from '@lucide/svelte/icons/circle-dot';
import Cpu from '@lucide/svelte/icons/cpu';
import Mountain from '@lucide/svelte/icons/mountain';
import Smartphone from '@lucide/svelte/icons/smartphone';
import Watch from '@lucide/svelte/icons/watch';
import {
	DASH,
	formatDistance,
	formatDuration,
	formatLocalDay,
	formatLocalTime,
	formatNumber,
	formatPace
} from '$lib/utils/format';
import { workoutIcon } from './kinds';
import { zoneKinds } from './zones';
import { decodePolyline, traceRoute } from './route';
import { axisLabel, elevationPaths, elevationProfile, nearestAt } from './elevation';
import type { ElevationSample, Workout } from './types';
import { CHART_BOX } from '$lib/charts/geometry';
import { DEVICE_TYPES, deviceIcon, deviceLabel } from '$lib/providers/devices';
import { openapiEnum } from '$lib/testing/openapi';

describe('formatDuration', () => {
	it('carries hours only once there are some', () => {
		expect(formatDuration(2880)).toBe('48m');
		expect(formatDuration(5520)).toBe('1h 32m');
		expect(formatDuration(3600)).toBe('1h 0m');
	});

	// Zero seconds is a provider that sent the field empty, not a workout of no
	// length, and "0m" would read as a measurement.
	it('is a dash for nothing and for zero', () => {
		expect(formatDuration(null)).toBe(DASH);
		expect(formatDuration(0)).toBe(DASH);
	});
});

describe('formatDistance', () => {
	it('switches to kilometres at a kilometre', () => {
		expect(formatDistance(8200)).toBe('8.20 km');
		expect(formatDistance(1000)).toBe('1.00 km');
		expect(formatDistance(840)).toBe('840 m');
	});

	it('is a dash for a workout that covers no ground', () => {
		expect(formatDistance(null)).toBe(DASH);
		expect(formatDistance(0)).toBe(DASH);
	});
});

describe('formatPace', () => {
	it('reads as minutes and seconds per kilometre', () => {
		expect(formatPace(351)).toBe('5:51 /km');
		expect(formatPace(300)).toBe('5:00 /km');
		// Padded, so 4:05 does not come out as 4:5.
		expect(formatPace(245)).toBe('4:05 /km');
	});
});

describe('formatNumber', () => {
	it('keeps zero, which is a reading', () => {
		expect(formatNumber(0, ' kcal')).toBe('0 kcal');
		expect(formatNumber(null, ' kcal')).toBe(DASH);
	});
});

describe('formatLocalTime', () => {
	const morning = '2026-09-15T07:12:00Z';

	// The offset is the workout's own, so a run at 09:12 in Warsaw must not read
	// as 07:12 because the admin - or the CI box - sits in UTC.
	it('shows the clock the workout was recorded against', () => {
		expect(formatLocalTime(morning, '+02:00')).toBe('09:12');
		expect(formatLocalTime(morning, '-05:30')).toBe('01:42');
	});

	// Saying UTC out loud matters most for a user abroad: an unmarked 07:12 reads
	// as their morning when it is only the instant we stored.
	it('marks the fallback when the provider never sent an offset', () => {
		expect(formatLocalTime(morning, null)).toBe('07:12 UTC');
	});

	it('carries the shift across midnight into the day as well', () => {
		expect(formatLocalDay('2026-09-15T23:30:00Z', '+02:00')).toBe('Wed, 16 Sept 2026');
		expect(formatLocalDay('2026-09-15T00:30:00Z', '-05:30')).toBe('Mon, 14 Sept 2026');
	});
});

describe('device types', () => {
	it('knows every device type the API has, and no others', () => {
		expect([...DEVICE_TYPES].sort()).toEqual(openapiEnum('DeviceType').sort());
	});

	it('names the types a humanised slug would mangle', () => {
		expect(deviceLabel('hr_sensor')).toBe('HR sensor');
		expect(deviceLabel('bp_monitor')).toBe('BP monitor');
		expect(deviceLabel('head_mounted')).toBe('Headset');
	});

	it('tells the kinds of device apart', () => {
		expect(deviceIcon('watch')).toBe(Watch);
		expect(deviceIcon('ring')).toBe(CircleDot);
		expect(deviceIcon('phone')).toBe(Smartphone);
	});

	// `unknown` and `other` are real DeviceType members, and a null arrives
	// whenever the provider named no device at all.
	it('falls back rather than guessing a shape', () => {
		expect(deviceIcon('unknown')).toBe(Cpu);
		expect(deviceIcon(null)).toBe(Cpu);
	});
});

describe('workoutIcon', () => {
	// Both patterns match `mountain_biking`; the order of the table is what
	// decides, and getting it wrong is silent.
	it('reads a compound slug as the sport, not the terrain', () => {
		expect(workoutIcon('mountain_biking')).toBe(Bike);
		expect(workoutIcon('mountaineering')).toBe(Mountain);
	});

	it('gives an unknown type the generic mark rather than nothing', () => {
		expect(workoutIcon('underwater_basket_weaving')).toBe(Activity);
		expect(workoutIcon('other')).toBe(Activity);
	});
});

describe('zoneKinds', () => {
	const workout = (hr: boolean, power: boolean) =>
		({
			hr_zones: hr ? { zones: [{ zone: 0, seconds: 60, max_bpm: 120 }], max_hr: 190 } : null,
			power_zones: power ? { zones: [{ zone: 0, seconds: 60, max_watts: 150 }] } : null
		}) as Workout;

	// Both shapes name their ceiling differently, and everything downstream - the
	// bands, the ranges in the strip - reads one `max`.
	it('flattens either ceiling to the same field', () => {
		expect(zoneKinds(workout(true, true)).map((kind) => [kind.type, kind.zones[0].max])).toEqual([
			['heart_rate', 120],
			['power', 150]
		]);
	});

	it('offers only what the provider sent', () => {
		expect(zoneKinds(workout(true, false)).map((kind) => kind.type)).toEqual(['heart_rate']);
		expect(zoneKinds(workout(false, false))).toEqual([]);
	});
});

// Google's worked example from the algorithm's documentation.
const DOCUMENTED = '_p~iF~ps|U_ulLnnqC_mqNvxq`@';

describe('decodePolyline', () => {
	it('decodes the documented example', () => {
		expect(decodePolyline(DOCUMENTED)).toEqual([
			{ lat: 38.5, lng: -120.2 },
			{ lat: 40.7, lng: -120.95 },
			{ lat: 43.252, lng: -126.453 }
		]);
	});

	it('keeps the points before a cut instead of inventing one from half of it', () => {
		expect(decodePolyline('_p~iF~ps|U_ulL')).toEqual([{ lat: 38.5, lng: -120.2 }]);
		expect(decodePolyline('')).toEqual([]);
	});
});

describe('traceRoute', () => {
	const box = { width: 400, height: 180, pad: 14 };
	const coordinates = (path: string) => path.match(/-?\d+(\.\d+)?/g)!.map(Number);

	it('fits the route inside the padded box and centres it on the other axis', () => {
		const trace = traceRoute(decodePolyline(DOCUMENTED), box)!;
		const numbers = coordinates(trace.path);
		const xs = numbers.filter((_, index) => index % 2 === 0);
		const ys = numbers.filter((_, index) => index % 2 === 1);

		// Taller than wide once projected, so the height sets the scale.
		expect(Math.min(...ys)).toBeCloseTo(box.pad, 0);
		expect(Math.max(...ys)).toBeCloseTo(box.height - box.pad, 0);
		expect(Math.min(...xs)).toBeCloseTo(box.width - Math.max(...xs), 0);
		expect(trace.start.x).toBeCloseTo(xs[0], 1);
		expect(trace.end.y).toBeCloseTo(ys[ys.length - 1], 1);
	});

	// The documented example heads north-west, so it ends higher up and further left.
	it('draws north up and west to the left', () => {
		const trace = traceRoute(decodePolyline(DOCUMENTED), box)!;
		expect(trace.end.y).toBeLessThan(trace.start.y);
		expect(trace.end.x).toBeLessThan(trace.start.x);
	});

	it('draws a dead-straight route without dividing by its missing width', () => {
		const trace = traceRoute(
			[
				{ lat: 50, lng: 10 },
				{ lat: 51, lng: 10 }
			],
			box
		)!;
		expect(trace.start.x).toBe(box.width / 2);
		expect(trace.end.x).toBe(box.width / 2);
	});

	it('has nothing to draw for one point, or for many in the same place', () => {
		expect(traceRoute([{ lat: 50, lng: 10 }], box)).toBeNull();
		expect(
			traceRoute(
				[
					{ lat: 50, lng: 10 },
					{ lat: 50, lng: 10 }
				],
				box
			)
		).toBeNull();
	});
});

describe('elevationProfile', () => {
	const sample = (altitude_m: number, distance_m: number | null, time_s: number | null) =>
		({ altitude_m, distance_m, time_s }) satisfies ElevationSample;

	it('finds the lowest and highest sample and pads the range around them', () => {
		const profile = elevationProfile([
			sample(120, 0, 0),
			sample(95, 1500, 400),
			sample(240, 3000, 900),
			sample(180, 4200, 1300)
		])!;

		expect(profile.axis).toBe('km');
		expect(profile.low).toEqual({ at: 1.5, value: 95 });
		expect(profile.high).toEqual({ at: 3, value: 240 });
		expect(profile.range.low).toBeLessThan(95);
		expect(profile.range.high).toBeGreaterThan(240);
	});

	// A missing distance would land at zero and fold the line back onto itself.
	it('falls back to time, then to order, unless every sample has a distance', () => {
		const timed = elevationProfile([sample(10, 0, 0), sample(12, null, 600)])!;
		expect(timed.axis).toBe('min');
		expect(timed.points[1].at).toBe(10);
		expect(elevationProfile([sample(10, null, 0), sample(12, 50, null)])!.axis).toBe('index');
	});

	it('keeps a flat ride flat rather than stretching a metre across the frame', () => {
		const profile = elevationProfile([sample(100, 0, 0), sample(101, 1000, 300)])!;
		expect(profile.range.high - profile.range.low).toBeGreaterThanOrEqual(10);
	});

	it('needs two samples to be a profile', () => {
		expect(elevationProfile([sample(100, 0, 0)])).toBeNull();
		expect(elevationProfile([])).toBeNull();
	});

	it('closes the area along the floor of the box', () => {
		const { line, area } = elevationPaths(
			elevationProfile([sample(100, 0, 0), sample(140, 2000, 600)])!,
			CHART_BOX
		);
		expect(line.startsWith('M0.0 ')).toBe(true);
		expect(area).toBe(`${line} L${CHART_BOX.width} ${CHART_BOX.height} L0 ${CHART_BOX.height} Z`);
	});

	it('reads the sample nearest the pointer, clamped to the ends', () => {
		const profile = elevationProfile([
			sample(100, 0, 0),
			sample(110, 1000, 300),
			sample(130, 4000, 1200)
		])!;
		expect(nearestAt(profile, 0.2).value).toBe(110);
		expect(nearestAt(profile, -1).value).toBe(100);
		expect(nearestAt(profile, 2).value).toBe(130);
	});

	it('labels the axis in the unit it counts', () => {
		expect(axisLabel(0, 'km')).toBe('0 km');
		expect(axisLabel(4.256, 'km')).toBe('4.26 km');
		expect(axisLabel(42.25, 'km')).toBe('42.3 km');
		expect(axisLabel(47.6, 'min')).toBe('48 min');
		expect(axisLabel(0, 'index')).toBe('#1');
	});
});
