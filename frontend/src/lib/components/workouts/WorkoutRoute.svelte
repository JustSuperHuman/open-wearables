<script lang="ts">
	import { MICRO } from '$lib/components/ui/typography';
	import { ROUTE_BOX, type RouteTrace } from '$lib/workouts/route';

	let { trace }: { trace: RouteTrace } = $props();

	const MARKER = 4.5;
</script>

<!-- No map tiles: the trace alone shows the shape of the route without sending
     anyone's whereabouts to a tile server. -->
<div class="flex flex-col gap-2">
	<svg
		viewBox="0 0 {ROUTE_BOX.width} {ROUTE_BOX.height}"
		class="h-44 w-full rounded-lg bg-surface-muted"
		role="img"
		aria-label="GPS route from start to finish"
	>
		<path
			d={trace.path}
			fill="none"
			stroke="var(--color-primary)"
			stroke-width="2.5"
			stroke-linecap="round"
			stroke-linejoin="round"
		/>
		<circle
			cx={trace.start.x}
			cy={trace.start.y}
			r={MARKER}
			fill="var(--color-surface)"
			stroke="var(--color-primary)"
			stroke-width="2"
		/>
		<circle cx={trace.end.x} cy={trace.end.y} r={MARKER} fill="var(--color-primary)" />
	</svg>

	<div class="flex gap-4 {MICRO}" aria-hidden="true">
		<span class="flex items-center gap-1.5">
			<span class="size-2 rounded-full border-2 border-primary bg-surface"></span>Start
		</span>
		<span class="flex items-center gap-1.5">
			<span class="size-2 rounded-full bg-primary"></span>Finish
		</span>
	</div>
</div>
