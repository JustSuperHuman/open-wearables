<script lang="ts">
	import HoverReadout from '$lib/components/charts/HoverReadout.svelte';
	import { MICRO } from '$lib/components/ui/typography';
	import { CHART_BOX, type Point } from '$lib/charts/geometry';
	import { formatNumber } from '$lib/utils/format';
	import {
		axisLabel,
		elevationPaths,
		nearestAt,
		placeOf,
		type ElevationProfile
	} from '$lib/workouts/elevation';

	let { profile }: { profile: ElevationProfile } = $props();

	const { width: W, height: H } = CHART_BOX;
	const TONE = 'var(--color-primary)';

	const metres = (value: number) => formatNumber(value, ' m');

	const paths = $derived(elevationPaths(profile, CHART_BOX));
	const first = $derived(profile.points[0]);
	const last = $derived(profile.points[profile.points.length - 1]);

	// The highest label sits above its point and the lowest below, so the two
	// never cover the line between them.
	const marks = $derived([
		{ key: 'high', point: profile.high, above: true },
		{ key: 'low', point: profile.low, above: false }
	]);

	let hovered = $state<Point | null>(null);

	function track(event: PointerEvent) {
		const frame = event.currentTarget as HTMLElement;
		hovered = nearestAt(
			profile,
			(event.clientX - frame.getBoundingClientRect().left) / frame.clientWidth
		);
	}

	const cursor = $derived(hovered && placeOf(profile, hovered, CHART_BOX));
</script>

<div class="flex flex-col gap-2">
	<div
		class="relative"
		onpointermove={track}
		onpointerleave={() => (hovered = null)}
		role="img"
		aria-label="Elevation profile, lowest {metres(profile.low.value)}, highest {metres(
			profile.high.value
		)}"
	>
		<!-- The viewBox stretches to the card, so strokes do not scale and every
		     dot and label is HTML laid over it, where a circle stays round. -->
		<svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" class="h-44 w-full">
			<path d={paths.area} fill={TONE} opacity="0.12" />
			<path
				d={paths.line}
				fill="none"
				stroke={TONE}
				stroke-width="2"
				stroke-linejoin="round"
				vector-effect="non-scaling-stroke"
			/>
			{#if cursor}
				<line
					x1={(cursor.x / 100) * W}
					x2={(cursor.x / 100) * W}
					y1="0"
					y2={H}
					stroke="var(--color-foreground)"
					stroke-width="1"
					opacity="0.35"
					vector-effect="non-scaling-stroke"
				/>
			{/if}
		</svg>

		{#each marks as mark (mark.key)}
			{@const place = placeOf(profile, mark.point, CHART_BOX)}
			<span
				aria-hidden="true"
				class="pointer-events-none absolute size-2 -translate-x-1/2 -translate-y-1/2 rounded-full bg-foreground ring-2 ring-surface"
				style="left: {place.x}%; top: {place.y}%"
			></span>
			<span
				class="pointer-events-none absolute -translate-x-1/2 rounded bg-surface/85 px-0.5 text-xs
					text-foreground tabular-nums {mark.above ? '-translate-y-[calc(100%+6px)]' : 'translate-y-1.5'}"
				style="left: clamp(1.75rem, {place.x}%, calc(100% - 1.75rem)); top: {place.y}%"
			>
				{metres(mark.point.value)}
			</span>
		{/each}

		{#if hovered && cursor}
			<HoverReadout
				heading={axisLabel(hovered.at, profile.axis)}
				readings={[{ type: 'elevation', label: 'Elevation', unit: 'm', value: hovered.value }]}
				at={cursor.x}
				colourFor={() => TONE}
				format={metres}
			/>
		{/if}
	</div>

	<div class="flex justify-between tabular-nums {MICRO}">
		<span>{axisLabel(first.at, profile.axis)}</span>
		<span>{axisLabel(last.at, profile.axis)}</span>
	</div>
</div>
