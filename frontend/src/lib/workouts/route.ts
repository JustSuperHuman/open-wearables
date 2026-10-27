import type { Box } from '$lib/charts/geometry';

export type LatLng = { lat: number; lng: number };

type Spot = { x: number; y: number };

/** A route fitted into a box: the path to stroke and where it starts and ends. */
export type RouteTrace = { path: string; start: Spot; end: Spot };

export const ROUTE_BOX: Box = { width: 400, height: 180, pad: 14 };

/**
 * Google's Encoded Polyline Algorithm, the format of Strava's `map.polyline`.
 * A string cut off mid-point keeps the points before the cut rather than
 * inventing one from the half that arrived.
 */
export function decodePolyline(encoded: string, precision = 5): LatLng[] {
	const factor = 10 ** precision;
	const points: LatLng[] = [];
	let index = 0;
	let lat = 0;
	let lng = 0;

	const next = (): number | null => {
		let shift = 0;
		let result = 0;
		let byte: number;
		do {
			if (index >= encoded.length) return null;
			byte = encoded.charCodeAt(index++) - 63;
			result |= (byte & 0x1f) << shift;
			shift += 5;
		} while (byte >= 0x20);
		return result & 1 ? ~(result >> 1) : result >> 1;
	};

	while (index < encoded.length) {
		const dLat = next();
		const dLng = next();
		if (dLat === null || dLng === null) break;
		lat += dLat;
		lng += dLng;
		points.push({ lat: lat / factor, lng: lng / factor });
	}

	return points;
}

/**
 * Projects the route flat and fits it, centred, inside the box. Longitude is
 * scaled by the cosine of the route's latitude: a degree east is shorter than a
 * degree north everywhere but the equator, and without it every route away from
 * the equator came out stretched sideways. Null when there is no line to draw.
 */
export function traceRoute(points: LatLng[], box: Box = ROUTE_BOX): RouteTrace | null {
	if (points.length < 2) return null;

	const midLat = points.reduce((sum, point) => sum + point.lat, 0) / points.length;
	const cosLat = Math.cos((midLat * Math.PI) / 180);
	// SVG y grows downwards, north does not.
	const flat = points.map((point) => ({ x: point.lng * cosLat, y: -point.lat }));

	const xs = flat.map((point) => point.x);
	const ys = flat.map((point) => point.y);
	const minX = Math.min(...xs);
	const minY = Math.min(...ys);
	const spanX = Math.max(...xs) - minX;
	const spanY = Math.max(...ys) - minY;
	if (spanX === 0 && spanY === 0) return null;

	// A dead-straight north-south or east-west route has no span on one axis,
	// so only the other may set the scale.
	const scale = Math.min(
		spanX ? (box.width - box.pad * 2) / spanX : Infinity,
		spanY ? (box.height - box.pad * 2) / spanY : Infinity
	);
	const offsetX = (box.width - spanX * scale) / 2;
	const offsetY = (box.height - spanY * scale) / 2;

	const spots = flat.map((point) => ({
		x: offsetX + (point.x - minX) * scale,
		y: offsetY + (point.y - minY) * scale
	}));

	return {
		path: spots
			.map((spot, rank) => `${rank ? 'L' : 'M'}${spot.x.toFixed(1)} ${spot.y.toFixed(1)}`)
			.join(' '),
		start: spots[0],
		end: spots[spots.length - 1]
	};
}
