import { useId, useMemo } from 'react';
import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceDot,
  XAxis,
  YAxis,
} from 'recharts';
import { ArrowDownRight, ArrowUpRight, Mountain } from 'lucide-react';
import { ChartContainer, ChartTooltip } from '@/components/ui/chart';
import { ELEVATION_CHART_CONFIG } from '@/lib/utils/chart-config';
import type { ElevationSample } from '@/lib/api/types';

/**
 * Renders a workout's elevation-over-distance profile as a filled line chart.
 * The x-axis is distance (km) when available, otherwise elapsed time (min). The
 * lowest and highest points are marked so the climb is readable at a glance.
 */

const LOW_COLOR = '#34d399'; // emerald-400
const HIGH_COLOR = '#fbbf24'; // amber-400

type XUnit = 'km' | 'min' | 'pt';

interface ChartPoint {
  x: number;
  altitude: number;
}

function formatX(value: number, unit: XUnit): string {
  if (unit === 'km') return `${value.toFixed(value < 10 ? 2 : 1)} km`;
  if (unit === 'min') return `${Math.round(value)} min`;
  return `${Math.round(value)}`;
}

function ElevationTooltip({
  active,
  payload,
  xUnit,
}: {
  active?: boolean;
  payload?: { payload: ChartPoint }[];
  xUnit: XUnit;
}) {
  if (!active || !payload?.length) return null;
  const point = payload[0].payload;
  return (
    <div className="rounded-lg border border-border bg-card/90 px-2.5 py-1.5 text-xs shadow-xl">
      <div className="font-mono font-medium tabular-nums text-foreground">
        {Math.round(point.altitude).toLocaleString()} m
      </div>
      <div className="text-muted-foreground">{formatX(point.x, xUnit)}</div>
    </div>
  );
}

function StatChip({
  icon,
  label,
  value,
  color,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  color: string;
}) {
  return (
    <div className="flex items-center gap-1.5">
      <span style={{ color }}>{icon}</span>
      <div className="leading-tight">
        <p className="text-sm font-medium text-foreground tabular-nums">
          {value}
        </p>
        <p className="text-[10px] uppercase tracking-wider text-muted-foreground">
          {label}
        </p>
      </div>
    </div>
  );
}

export function WorkoutElevationChart({
  profile,
  gainMeters,
  className,
}: {
  profile: ElevationSample[];
  gainMeters?: number | null;
  className?: string;
}) {
  const gradientId = useId().replace(/:/g, '');

  const { data, xUnit, low, high, yDomain } = useMemo(() => {
    const hasDistance = profile.some(
      (p) => p.distance_m !== null && p.distance_m !== undefined
    );
    const hasTime = profile.some(
      (p) => p.time_s !== null && p.time_s !== undefined
    );
    const unit: XUnit = hasDistance ? 'km' : hasTime ? 'min' : 'pt';

    const points: ChartPoint[] = profile.map((p, i) => {
      let x: number;
      if (unit === 'km') x = (p.distance_m ?? 0) / 1000;
      else if (unit === 'min') x = (p.time_s ?? 0) / 60;
      else x = i;
      return { x, altitude: p.altitude_m };
    });

    // Locate the true lowest/highest samples for the reference markers.
    let lowIdx = 0;
    let highIdx = 0;
    points.forEach((p, i) => {
      if (p.altitude < points[lowIdx].altitude) lowIdx = i;
      if (p.altitude > points[highIdx].altitude) highIdx = i;
    });

    const altitudes = points.map((p) => p.altitude);
    const minA = Math.min(...altitudes);
    const maxA = Math.max(...altitudes);
    // Pad the y-domain so the line and the low/high labels aren't clipped.
    const pad = Math.max((maxA - minA) * 0.18, 5);

    return {
      data: points,
      xUnit: unit,
      low: points[lowIdx],
      high: points[highIdx],
      yDomain: [Math.floor(minA - pad), Math.ceil(maxA + pad)] as [
        number,
        number,
      ],
    };
  }, [profile]);

  if (data.length < 2) return null;

  return (
    <div className={className}>
      {/* Low / High / Climb summary */}
      <div className="mb-3 flex items-center gap-6">
        <StatChip
          icon={<ArrowDownRight className="h-4 w-4" />}
          label="Lowest"
          value={`${Math.round(low.altitude).toLocaleString()} m`}
          color={LOW_COLOR}
        />
        <StatChip
          icon={<ArrowUpRight className="h-4 w-4" />}
          label="Highest"
          value={`${Math.round(high.altitude).toLocaleString()} m`}
          color={HIGH_COLOR}
        />
        {typeof gainMeters === 'number' && gainMeters > 0 && (
          <StatChip
            icon={<Mountain className="h-4 w-4" />}
            label="Climb"
            value={`${Math.round(gainMeters).toLocaleString()} m`}
            color="#818cf8"
          />
        )}
      </div>

      <ChartContainer
        config={ELEVATION_CHART_CONFIG}
        className="h-[200px] w-full"
      >
        <AreaChart
          data={data}
          margin={{ top: 16, left: 8, right: 12, bottom: 0 }}
        >
          <defs>
            <linearGradient
              id={`${gradientId}-fill`}
              x1="0"
              y1="0"
              x2="0"
              y2="1"
            >
              <stop offset="0%" stopColor={HIGH_COLOR} stopOpacity={0.35} />
              <stop offset="100%" stopColor={LOW_COLOR} stopOpacity={0.04} />
            </linearGradient>
            <linearGradient
              id={`${gradientId}-stroke`}
              x1="0"
              y1="0"
              x2="0"
              y2="1"
            >
              <stop offset="0%" stopColor={HIGH_COLOR} />
              <stop offset="100%" stopColor={LOW_COLOR} />
            </linearGradient>
          </defs>
          <CartesianGrid vertical={false} strokeDasharray="3 3" />
          <XAxis
            dataKey="x"
            type="number"
            domain={['dataMin', 'dataMax']}
            tickLine={false}
            axisLine={false}
            tickMargin={8}
            tickCount={5}
            tick={{ fill: '#71717a', fontSize: 10 }}
            tickFormatter={(v: number) => formatX(v, xUnit)}
          />
          <YAxis
            domain={yDomain}
            tickLine={false}
            axisLine={false}
            tickMargin={8}
            width={40}
            tick={{ fill: '#71717a', fontSize: 10 }}
            tickFormatter={(v: number) => `${Math.round(v)}`}
            unit="m"
          />
          <ChartTooltip
            cursor={{ stroke: '#52525b', strokeDasharray: '3 3' }}
            content={<ElevationTooltip xUnit={xUnit} />}
          />
          <Area
            dataKey="altitude"
            type="monotone"
            stroke={`url(#${gradientId}-stroke)`}
            strokeWidth={2.5}
            fill={`url(#${gradientId}-fill)`}
            isAnimationActive={false}
            dot={false}
            activeDot={{ r: 4, fill: HIGH_COLOR, stroke: 'transparent' }}
          />
          {/* Lowest and highest elevation markers */}
          <ReferenceDot
            x={high.x}
            y={high.altitude}
            r={4}
            fill={HIGH_COLOR}
            stroke="#18181b"
            strokeWidth={1.5}
            label={{
              value: `${Math.round(high.altitude)} m`,
              position: 'top',
              fill: HIGH_COLOR,
              fontSize: 11,
              fontWeight: 600,
            }}
          />
          <ReferenceDot
            x={low.x}
            y={low.altitude}
            r={4}
            fill={LOW_COLOR}
            stroke="#18181b"
            strokeWidth={1.5}
            label={{
              value: `${Math.round(low.altitude)} m`,
              position: 'bottom',
              fill: LOW_COLOR,
              fontSize: 11,
              fontWeight: 600,
            }}
          />
        </AreaChart>
      </ChartContainer>
    </div>
  );
}
