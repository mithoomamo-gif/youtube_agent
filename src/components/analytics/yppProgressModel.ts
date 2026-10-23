/**
 * YPP (YouTube Partner Program) progress — pure data-shaping logic.
 *
 * This module is deliberately free of React, fetching and formatting
 * side effects so the rules can be unit-tested in isolation (the repo
 * has no frontend test runner yet; the functions here are pure and can
 * be exercised directly, e.g. `node` type-stripping or a future vitest
 * suite). The component (`YppProgress.tsx`) only renders what this
 * module decides.
 *
 * HARD ROADMAP RULE (MASTER_ROADMAP §3.11 Channel Health / §3.15,
 * line 366): "The app never estimates qualification from ordinary watch
 * time if Analytics qualification data is unavailable."
 *
 * How that rule is enforced HERE, structurally:
 *
 * - The ONLY input that can fill a watch-hours qualification bar is
 *   {@link YppProgressData.qualifiedWatchHours} — the figure reported
 *   by YouTube Analytics qualification data.
 * - {@link YppProgressData.ordinaryWatchHours} (all watch time) is a
 *   SEPARATE field. It is carried through to the view model untouched
 *   so the UI can display it on its own line, explicitly labelled as
 *   NOT the qualification figure. No code path in this module copies
 *   it into a bar's `current`, `pct` or `remaining`.
 * - When `qualifiedWatchHours` is null, the bar model is
 *   `available: false` with `pct: null` — there is no number to
 *   render, so the component physically cannot show a filled bar or
 *   a percentage for qualification.
 *
 * Thresholds (explicitly labelled on the component):
 * - Current application bar (full programme): 1,000 subscribers +
 *   4,000 qualified public watch hours in the last 12 months.
 * - Upcoming bar: from 1 Feb 2027, NEW channels need 1,000 subscribers
 *   + 8,000 watch hours. Applications received by 31 Jan 2027 23:59 UTC
 *   are judged on the old 4,000-hour bar (user-confirmed decision,
 *   2026-10-23). The 8,000-hour bar is displayed separately and
 *   visually distinct — it never replaces the current bar.
 */

// ── Threshold constants (single source of truth for the component) ───────

export const YPP_SUBSCRIBER_TARGET = 1_000;

/** Current application bar: qualified public watch hours, last 12 months. */
export const YPP_CURRENT_WATCH_HOURS_TARGET = 4_000;

/** Upcoming bar for new channels, effective 1 Feb 2027. */
export const YPP_UPCOMING_WATCH_HOURS_TARGET = 8_000;

export const YPP_UPCOMING_EFFECTIVE_LABEL = "1 Feb 2027";

/** Applications received by this instant are judged on the 4,000-hour bar. */
export const YPP_CURRENT_BAR_DEADLINE_LABEL = "31 Jan 2027, 23:59 UTC";

// ── Input (what the analytics read model hands the component) ────────────

/**
 * One channel's YPP figures, as shaped by `src/api/queries/ypp.ts`
 * from the analytics team's read model. Every field is nullable: a
 * null means "the backend did not report this" — never "zero".
 */
export interface YppProgressData {
  /** Current subscriber count, or null when not reported. */
  subscriberCount: number | null;
  /**
   * QUALIFIED public watch hours (last 12 months) from YouTube
   * Analytics qualification data — the only figure allowed to fill a
   * qualification bar. Null = qualification data unavailable.
   */
  qualifiedWatchHours: number | null;
  /**
   * Ordinary (all) watch hours, for display ONLY. Clearly labelled in
   * the UI as not the qualification figure; never used for progress.
   */
  ordinaryWatchHours: number | null;
  /** ISO-8601 instant the figures are current as of, or null. */
  asOf: string | null;
}

export interface YppProgressInput {
  /** False when there is no channel or it is disconnected/needs reauth. */
  channelConnected: boolean;
  /** A fetch is in flight and no data has arrived yet. */
  loading?: boolean;
  /** The fetch failed (network / 5xx). A 404 is NOT a failure — the
   *  query layer maps it to `data: null` ("no analytics yet"). */
  loadFailed?: boolean;
  data: YppProgressData | null;
}

// ── View model ─────────────────────────────────────────────────────────────

/**
 * The mutually exclusive top-level states the component can render:
 *
 * - `not_connected` — no connected channel (nothing else is shown).
 * - `loading`       — first fetch in flight.
 * - `load_error`    — fetch failed; no numbers shown.
 * - `no_analytics`  — channel connected, but the backend has no
 *                     analytics figures for it yet (or no snapshot row
 *                     with any figure at all).
 * - `ready`         — at least one figure is present; per-bar
 *                     availability (notably qualification) is decided
 *                     per bar via {@link YppBarModel.available}.
 */
export type YppComponentState =
  | "not_connected"
  | "loading"
  | "load_error"
  | "no_analytics"
  | "ready";

export type YppBarKind =
  | "subscribers"
  | "qualified_hours_current"
  | "qualified_hours_upcoming";

export interface YppBarModel {
  kind: YppBarKind;
  /** Short metric name, e.g. "Subscribers". */
  label: string;
  /** The explicit target wording shown on the component. */
  targetLabel: string;
  /**
   * True only when a real reported figure exists. When false,
   * `current`/`pct`/`remaining` are all null and the component must
   * render its unavailable treatment — never a 0% bar.
   */
  available: boolean;
  current: number | null;
  target: number;
  /** 0–100, or null when unavailable. Derived ONLY from `current`. */
  pct: number | null;
  remaining: number | null;
  /** True only when an available figure meets/exceeds the target. */
  met: boolean;
}

export interface YppViewModel {
  state: YppComponentState;
  /** Mirrors `data.qualifiedWatchHours != null` in the ready state. */
  qualificationDataAvailable: boolean;
  subscribersBar: YppBarModel;
  /** The current application bar: qualified hours toward 4,000. */
  currentHoursBar: YppBarModel;
  /** The upcoming bar: qualified hours toward 8,000 (from 1 Feb 2027). */
  upcomingHoursBar: YppBarModel;
  /** Display-only ordinary watch hours (never a qualification input). */
  ordinaryWatchHours: number | null;
  /** Both current thresholds met on REPORTED figures. Meeting the
   *  thresholds is not acceptance — YouTube still reviews the channel. */
  currentThresholdsMet: boolean;
  asOf: string | null;
}

// ── Shaping ────────────────────────────────────────────────────────────────

/** A reported figure is usable only if it is a finite, non-negative number. */
function cleanFigure(value: number | null | undefined): number | null {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    return null;
  }
  return value;
}

function buildBar(
  kind: YppBarKind,
  label: string,
  targetLabel: string,
  reported: number | null,
  target: number,
): YppBarModel {
  const current = cleanFigure(reported);
  if (current === null) {
    return {
      kind,
      label,
      targetLabel,
      available: false,
      current: null,
      target,
      pct: null,
      remaining: null,
      met: false,
    };
  }
  return {
    kind,
    label,
    targetLabel,
    available: true,
    current,
    target,
    pct: Math.min(100, (current / target) * 100),
    remaining: Math.max(0, target - current),
    met: current >= target,
  };
}

/**
 * Shape raw input into the component's view model.
 *
 * State precedence: not_connected > loading > load_error > no_analytics
 * > ready. A payload whose every figure is null is `no_analytics`, not
 * `ready` — there is nothing honest to draw from it. Stale data shown
 * alongside a failed refresh stays `ready` (the fetch layer keeps the
 * last good data); `loadFailed` only wins when there is no data at all.
 */
export function shapeYppProgress(input: YppProgressInput): YppViewModel {
  const data = input.data;

  const subscribersBar = buildBar(
    "subscribers",
    "Subscribers",
    `Target: ${YPP_SUBSCRIBER_TARGET.toLocaleString("en-US")} subscribers`,
    data?.subscriberCount ?? null,
    YPP_SUBSCRIBER_TARGET,
  );

  // QUALIFICATION RULE: both watch-hours bars are built exclusively
  // from `qualifiedWatchHours`. `ordinaryWatchHours` is never passed
  // to buildBar — see the module header.
  const qualified = data ? cleanFigure(data.qualifiedWatchHours) : null;

  const currentHoursBar = buildBar(
    "qualified_hours_current",
    "Qualified public watch hours (last 12 months)",
    `Target: ${YPP_CURRENT_WATCH_HOURS_TARGET.toLocaleString("en-US")} qualified watch hours — current application bar`,
    qualified,
    YPP_CURRENT_WATCH_HOURS_TARGET,
  );

  const upcomingHoursBar = buildBar(
    "qualified_hours_upcoming",
    "Qualified public watch hours (last 12 months)",
    `Upcoming target from ${YPP_UPCOMING_EFFECTIVE_LABEL}: ${YPP_UPCOMING_WATCH_HOURS_TARGET.toLocaleString("en-US")} qualified watch hours for new channels`,
    qualified,
    YPP_UPCOMING_WATCH_HOURS_TARGET,
  );

  const hasAnyFigure =
    data !== null &&
    (cleanFigure(data.subscriberCount) !== null ||
      qualified !== null ||
      cleanFigure(data.ordinaryWatchHours) !== null);

  let state: YppComponentState;
  if (!input.channelConnected) {
    state = "not_connected";
  } else if (input.loading && data === null) {
    state = "loading";
  } else if (input.loadFailed && data === null) {
    state = "load_error";
  } else if (!hasAnyFigure) {
    state = "no_analytics";
  } else {
    state = "ready";
  }

  return {
    state,
    qualificationDataAvailable: qualified !== null,
    subscribersBar,
    currentHoursBar,
    upcomingHoursBar,
    ordinaryWatchHours: data ? cleanFigure(data.ordinaryWatchHours) : null,
    currentThresholdsMet: subscribersBar.met && currentHoursBar.met,
    asOf: data?.asOf ?? null,
  };
}

/** Locale-stable thousands formatting for figures (e.g. 1,234). */
export function formatFigure(value: number): string {
  return Math.round(value).toLocaleString("en-US");
}

/** Watch hours with one decimal when fractional (e.g. 3,912.5). */
export function formatHours(value: number): string {
  const rounded = Math.round(value * 10) / 10;
  return Number.isInteger(rounded)
    ? rounded.toLocaleString("en-US")
    : rounded.toLocaleString("en-US", {
        minimumFractionDigits: 1,
        maximumFractionDigits: 1,
      });
}
