/**
 * RetentionChart — audience-retention curve annotated with the
 * video's script-section boundaries (Round 5, worker W3).
 *
 * A hand-rolled SVG chart: the codebase has no charting dependency
 * (the previous Analytics screen hand-rendered bars with divs), and
 * this chart needs exactly one polyline, section bands and a drop
 * marker — not enough to justify adding a heavy dependency.
 *
 * Honesty rules:
 * - The curve is drawn ONLY from the points passed in; nothing is
 *   interpolated into existence beyond connecting consecutive real
 *   samples (the standard rendering of this API's curve).
 * - Section bands render only for sections whose position can be
 *   placed (ratios, or seconds + a known duration). A section that
 *   cannot be placed is omitted from the chart and still listed in
 *   the legend below it by the caller.
 * - `audienceWatchRatio` can exceed 1.0 (rewatches) — the y axis
 *   grows to fit instead of clipping the signal.
 */

import { useMemo, useRef, useState } from "react";
import type {
  RetentionPoint,
  RetentionSection,
  RetentionSteepestDrop,
} from "../../api/queries/analytics";

export interface RetentionChartProps {
  points: RetentionPoint[];
  sections: RetentionSection[];
  /** Video duration in seconds; null → the x axis is labelled in %. */
  durationS: number | null;
  steepestDrop?: RetentionSteepestDrop | null;
}

const WIDTH = 720;
const HEIGHT = 264;
const MARGIN = { top: 30, right: 14, bottom: 30, left: 46 };
const PLOT_W = WIDTH - MARGIN.left - MARGIN.right;
const PLOT_H = HEIGHT - MARGIN.top - MARGIN.bottom;

export function formatClock(totalSeconds: number): string {
  const safe = Math.max(0, Math.round(totalSeconds));
  const minutes = Math.floor(safe / 60);
  const seconds = safe % 60;
  return `${minutes}:${seconds.toString().padStart(2, "0")}`;
}

function sectionRatio(
  section: RetentionSection,
  durationS: number | null,
  which: "startRatio" | "endRatio",
): number | null {
  const direct = section[which];
  if (direct !== null) return direct;
  if (durationS && durationS > 0) {
    return (which === "startRatio" ? section.startS : section.endS) / durationS;
  }
  return null;
}

interface PlacedSection {
  section: RetentionSection;
  startRatio: number;
  endRatio: number;
}

export function RetentionChart({
  points,
  sections,
  durationS,
  steepestDrop = null,
}: RetentionChartProps) {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);

  const yMax = useMemo(() => {
    let max = 1;
    for (const point of points) max = Math.max(max, point.watchRatio);
    return Math.ceil(max * 10) / 10;
  }, [points]);

  const placed = useMemo<PlacedSection[]>(() => {
    const out: PlacedSection[] = [];
    for (const section of sections) {
      const startRatio = sectionRatio(section, durationS, "startRatio");
      const endRatio = sectionRatio(section, durationS, "endRatio");
      if (startRatio === null || endRatio === null || endRatio <= startRatio) {
        continue;
      }
      out.push({ section, startRatio, endRatio });
    }
    return out;
  }, [sections, durationS]);

  const x = (ratio: number) => MARGIN.left + ratio * PLOT_W;
  const y = (watch: number) => MARGIN.top + (1 - watch / yMax) * PLOT_H;

  const line = useMemo(
    () =>
      points
        .map((p) => `${x(p.elapsedRatio).toFixed(1)},${y(p.watchRatio).toFixed(1)}`)
        .join(" "),
    // x/y are pure functions of fixed layout constants + yMax.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [points, yMax],
  );

  const area = useMemo(() => {
    if (points.length === 0) return "";
    const baseline = y(0);
    const first = points[0];
    const last = points[points.length - 1];
    return [
      `${x(first.elapsedRatio).toFixed(1)},${baseline.toFixed(1)}`,
      ...points.map(
        (p) => `${x(p.elapsedRatio).toFixed(1)},${y(p.watchRatio).toFixed(1)}`,
      ),
      `${x(last.elapsedRatio).toFixed(1)},${baseline.toFixed(1)}`,
    ].join(" ");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [points, yMax]);

  const dropRatio =
    steepestDrop && durationS && durationS > 0
      ? steepestDrop.atS / durationS
      : null;
  const dropWatch = useMemo(() => {
    if (dropRatio === null || points.length === 0) return null;
    let best = points[0];
    for (const point of points) {
      if (Math.abs(point.elapsedRatio - dropRatio) < Math.abs(best.elapsedRatio - dropRatio)) {
        best = point;
      }
    }
    return best.watchRatio;
  }, [points, dropRatio]);

  const hoverPoint = hoverIndex !== null ? points[hoverIndex] : null;

  const handleMove = (event: React.MouseEvent<SVGSVGElement>) => {
    const svg = svgRef.current;
    if (!svg || points.length === 0) return;
    const rect = svg.getBoundingClientRect();
    if (rect.width === 0) return;
    const ratio = ((event.clientX - rect.left) / rect.width) * WIDTH;
    const elapsed = (ratio - MARGIN.left) / PLOT_W;
    let best = 0;
    for (let i = 1; i < points.length; i += 1) {
      if (Math.abs(points[i].elapsedRatio - elapsed) < Math.abs(points[best].elapsedRatio - elapsed)) {
        best = i;
      }
    }
    setHoverIndex(best);
  };

  const timeLabel = (ratio: number): string =>
    durationS ? formatClock(ratio * durationS) : `${Math.round(ratio * 100)}%`;

  const firstWatch = points.length > 0 ? points[0].watchRatio : null;
  const lastWatch = points.length > 0 ? points[points.length - 1].watchRatio : null;
  const ariaLabel =
    firstWatch !== null && lastWatch !== null
      ? `Audience retention curve: ${Math.round(firstWatch * 100)}% of viewers at the start, ${Math.round(lastWatch * 100)}% still watching at the end${placed.length > 0 ? `, with ${placed.length} script sections marked` : ""}.`
      : "Audience retention curve.";

  const gridValues = [0, 0.25, 0.5, 0.75, 1].filter((v) => v <= yMax);

  return (
    <div>
      <svg
        ref={svgRef}
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="w-full"
        role="img"
        aria-label={ariaLabel}
        onMouseMove={handleMove}
        onMouseLeave={() => setHoverIndex(null)}
      >
        {/* Section bands + boundaries (behind the curve) */}
        {placed.map(({ section, startRatio, endRatio }, i) => {
          const bandX = x(startRatio);
          const bandW = Math.max(1, x(endRatio) - bandX);
          const labelFits = bandW > 64;
          return (
            <g key={`${section.index}-${section.startS}`}>
              {i % 2 === 0 ? (
                <rect
                  x={bandX}
                  y={MARGIN.top}
                  width={bandW}
                  height={PLOT_H}
                  fill="#6E5BFF"
                  opacity={0.07}
                />
              ) : null}
              <line
                x1={bandX}
                x2={bandX}
                y1={MARGIN.top}
                y2={MARGIN.top + PLOT_H}
                stroke="#232B3D"
                strokeDasharray="3 3"
              />
              {labelFits ? (
                <text
                  x={bandX + 4}
                  y={MARGIN.top - 16}
                  fontSize={10}
                  fill="#9AA4B5"
                >
                  {section.title.length > 22
                    ? `${section.title.slice(0, 21)}…`
                    : section.title}
                </text>
              ) : null}
              <text x={bandX + 4} y={MARGIN.top - 4} fontSize={9} fill="#6E7A8F">
                {timeLabel(startRatio)}
              </text>
            </g>
          );
        })}

        {/* Gridlines + y labels */}
        {gridValues.map((value) => (
          <g key={value}>
            <line
              x1={MARGIN.left}
              x2={MARGIN.left + PLOT_W}
              y1={y(value)}
              y2={y(value)}
              stroke="#232B3D"
              strokeWidth={value === 0 ? 1.5 : 0.75}
            />
            <text
              x={MARGIN.left - 6}
              y={y(value) + 3}
              fontSize={10}
              fill="#6E7A8F"
              textAnchor="end"
            >
              {Math.round(value * 100)}%
            </text>
          </g>
        ))}

        {/* X labels: start / middle / end */}
        {[0, 0.5, 1].map((ratio) => (
          <text
            key={ratio}
            x={x(ratio)}
            y={MARGIN.top + PLOT_H + 18}
            fontSize={10}
            fill="#6E7A8F"
            textAnchor={ratio === 0 ? "start" : ratio === 1 ? "end" : "middle"}
          >
            {timeLabel(ratio)}
          </text>
        ))}

        {/* Curve */}
        {points.length > 1 ? (
          <>
            <polygon points={area} fill="#6E5BFF" opacity={0.14} />
            <polyline
              points={line}
              fill="none"
              stroke="#8B7CFF"
              strokeWidth={2}
              strokeLinejoin="round"
              strokeLinecap="round"
            />
          </>
        ) : points.length === 1 ? (
          <circle cx={x(points[0].elapsedRatio)} cy={y(points[0].watchRatio)} r={3.5} fill="#8B7CFF" />
        ) : null}

        {/* Steepest-drop marker */}
        {dropRatio !== null && dropWatch !== null && steepestDrop ? (
          <g>
            <line
              x1={x(dropRatio)}
              x2={x(dropRatio)}
              y1={MARGIN.top}
              y2={MARGIN.top + PLOT_H}
              stroke="#F5B544"
              strokeDasharray="4 3"
              strokeWidth={1.25}
            />
            <circle cx={x(dropRatio)} cy={y(dropWatch)} r={4} fill="#F5B544" />
            <text
              x={Math.min(x(dropRatio) + 6, WIDTH - 150)}
              y={Math.max(y(dropWatch) - 8, 12)}
              fontSize={10.5}
              fill="#F5B544"
            >
              Steepest drop · {steepestDrop.atText ?? timeLabel(dropRatio)}
            </text>
          </g>
        ) : null}

        {/* Hover probe */}
        {hoverPoint ? (
          <g pointerEvents="none">
            <line
              x1={x(hoverPoint.elapsedRatio)}
              x2={x(hoverPoint.elapsedRatio)}
              y1={MARGIN.top}
              y2={MARGIN.top + PLOT_H}
              stroke="#9AA4B5"
              strokeWidth={0.75}
              opacity={0.6}
            />
            <circle
              cx={x(hoverPoint.elapsedRatio)}
              cy={y(hoverPoint.watchRatio)}
              r={4}
              fill="#EDF0F7"
              stroke="#6E5BFF"
              strokeWidth={2}
            />
          </g>
        ) : null}
      </svg>

      {/* Hover readout (text, so it is reachable without a pointer) */}
      <div className="mt-1 flex h-5 items-center gap-2 text-xs text-muted" aria-live="off">
        {hoverPoint ? (
          <span>
            <span className="font-mono text-ink2 tnum">{timeLabel(hoverPoint.elapsedRatio)}</span>
            {" — "}
            <span className="font-mono text-ink2 tnum">
              {Math.round(hoverPoint.watchRatio * 100)}%
            </span>{" "}
            of viewers still watching
            {hoverPoint.watchRatio > 1
              ? " (above 100% — viewers rewatched this part)"
              : ""}
          </span>
        ) : (
          <span>Hover the curve to inspect a moment.</span>
        )}
      </div>
    </div>
  );
}
