"use client";

/**
 * Shared Lamplight primitives for the operator screens (Fleet, Agents,
 * Costs, Memory): semantic colors, alpha helpers, slips, tiles and bars.
 */

import React from "react";

/** Minimum touch target . */
export const TOUCH = 44;
/** iOS zooms the page when an input's font-size is under 16px. */
export const INPUT_FONT = 17;

/**
 * Translucent variant of a token color for borders/fills. color-mix works
 * against CSS custom properties (several of them hour-driven); the old
 * string-suffix-alpha trick (`${hex}66`) only works on literal hex.
 */
export function alpha(color: string, pct: number): string {
  return `color-mix(in srgb, ${color} ${pct}%, transparent)`;
}

// Semantic status colors — the design language remap, thresholds unchanged.
export const OK = "var(--ok)"; // was #22c55e — ok / done / bought / healthy
export const WARN = "var(--warn)"; // was #f59e0b — low / stale / ahead / processing
export const ERR = "var(--err)"; // was #ef4444 — over / blocked / failed
export const REVIEW = "var(--review)"; // was #a855f7 — in-review / proposed
export const INFO = "var(--info)"; // was #3b82f6 — ready / recipe / info

/** Small uppercase section label, e.g. "audits · active", "agent spend". */
export function SectionLabel({
  children,
  style,
}: {
  children: React.ReactNode;
  style?: React.CSSProperties;
}) {
  return (
    <div
      className="text-[10px] uppercase tracking-wide font-medium"
      style={{ color: "var(--text-muted)", letterSpacing: "0.11em", ...style }}
    >
      {children}
    </div>
  );
}

/** Status dot — solid, or breathing for "processing"/"stale but alive". */
export function StatusDot({
  color,
  breathe,
  halo,
  haloDelay,
  size = 8,
}: {
  color: string;
  breathe?: boolean;
  /** Pulsing box-shadow ring (globals.css `.anim-halo`, keyframe `halo`) —
   * "good"/"warn" nominal-state dots (masthead pill, Fleet rows), not
   * "actively working" (that's `breathe`). currentColor-based, so `color`
   * also sets the ring's tint (OK green vs WARN amber = haloAmber). */
  halo?: boolean;
  /** Stagger multiple halo dots in a list so they don't all pulse in sync
   *. */
  haloDelay?: number;
  size?: number;
}) {
  return (
    <span
      className={halo ? "anim-halo" : undefined}
      style={{
        width: size,
        height: size,
        borderRadius: 99,
        flexShrink: 0,
        background: color,
        color: halo ? color : undefined,
        animation: breathe ? "breathe 2.2s ease-in-out infinite" : undefined,
        animationDelay: halo && haloDelay ? `${haloDelay}s` : undefined,
      }}
    />
  );
}

/** Small coloured pill — status/priority/kind tags. Outlined by default. */
export function Tag({
  label,
  color,
  filled,
}: {
  label: string;
  color: string;
  filled?: boolean;
}) {
  return (
    <span
      className="text-[9.5px] uppercase tracking-wide font-semibold rounded px-1.5 py-0.5 whitespace-nowrap"
      style={{
        letterSpacing: "0.06em",
        border: `1px solid ${filled ? color : alpha(color, 45)}`,
        backgroundColor: filled ? color : alpha(color, 12),
        // Filled semantic pills (ok/warn/err) are all warm-hued like --accent
        // in every hour palette, so --on-accent's calibrated contrast reads
        // fine against them too — avoids inventing a second dark/light pair.
        color: filled ? "var(--on-accent)" : color,
      }}
    >
      {label}
    </span>
  );
}

/** Filled action pill — Approve/Reject on task cards, semantic-colored. */
export function FilledPill({
  label,
  onClick,
  color,
  disabled,
  title,
}: {
  label: string;
  onClick: (e: React.MouseEvent) => void;
  color: string;
  disabled?: boolean;
  title?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className="press text-[11px] font-semibold rounded-lg"
      style={{
        minHeight: TOUCH,
        padding: "0 12px",
        border: "none",
        backgroundColor: color,
        color: "var(--on-accent)",
        fontFamily: "inherit",
        touchAction: "manipulation",
        opacity: disabled ? 0.55 : 1,
      }}
    >
      {label}
    </button>
  );
}

/** Ghost/outlined button — Reset session, secondary actions. */
export function GhostPill({
  label,
  onClick,
  color = "var(--text-primary)",
  disabled,
}: {
  label: string;
  onClick: (e: React.MouseEvent) => void;
  color?: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="press text-[11.5px] font-medium rounded-lg"
      style={{
        minHeight: TOUCH,
        padding: "0 12px",
        border: `1px solid ${alpha(color, 50)}`,
        backgroundColor: "transparent",
        color,
        fontFamily: "inherit",
        touchAction: "manipulation",
        opacity: disabled ? 0.55 : 1,
      }}
    >
      {label}
    </button>
  );
}

/** A stat tile — the fleet and cost rows, host metrics. */
export function StatTile({
  label,
  value,
  tone,
}: {
  label: string;
  value: React.ReactNode;
  /** "warn" gets the --warn top edge + colored number (an attention tile). */
  tone?: "warn" | "err" | "ok" | "neutral";
}) {
  const toneColor = tone === "warn" ? "var(--warn)" : tone === "err" ? "var(--err)" : tone === "ok" ? "var(--ok)" : undefined;
  return (
    <div
      className={toneColor ? "lit" : undefined}
      style={{
        flex: 1,
        minWidth: 82,
        borderRadius: 12,
        padding: "11px 13px",
        background: toneColor ? "var(--slip-near)" : "var(--slip-far)",
        boxShadow: toneColor ? "var(--sh-near)" : "var(--sh-far)",
        ...(toneColor ? { borderTop: `1px solid ${alpha(toneColor, 45)}` } : {}),
      }}
    >
      <SectionLabel style={{ opacity: 0.55, color: toneColor || undefined }}>{label}</SectionLabel>
      <div className="text-xl font-semibold mt-0.5" style={{ color: toneColor || "var(--text-primary)" }}>
        {value}
      </div>
    </div>
  );
}

/** Near slip — a lit card that catches the lamp. Use for the primary/first item. */
export const slipNear: React.CSSProperties = {
  background: "var(--slip-near)",
  boxShadow: "var(--sh-near)",
  borderRadius: "4px 14px 14px 13px",
};

/** Far slip — sits in the falloff, no lit edge. */
export const slipFar: React.CSSProperties = {
  background: "var(--slip-far)",
  boxShadow: "var(--sh-far)",
  borderRadius: "4px 14px 14px 13px",
};

/** Input/select styling. */
export const fieldStyle: React.CSSProperties = {
  backgroundColor: "var(--bg-elevated)",
  border: "1px solid var(--border)",
  color: "var(--text-primary)",
  fontSize: INPUT_FONT,
  fontFamily: "inherit",
  outline: "none",
  minHeight: TOUCH,
  minWidth: 0,
  borderRadius: 8,
  padding: "0 10px",
};

export function elapsedMs(ms: number): string {
  const s = Math.floor(ms / 1000);
  if (s < 0) return "0s";
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h${m % 60}m`;
  return `${Math.floor(h / 24)}d${h % 24}h`;
}

/** amber >5m, red >10m — the audit tool-elapsed thresholds, unchanged. */
export function toolElapsedColor(ms: number): string {
  if (ms > 10 * 60 * 1000) return "var(--err)";
  if (ms > 5 * 60 * 1000) return "var(--warn)";
  return "var(--text-secondary)";
}

/**
 * The notched progress bar — agent spend (Costs) and the
 * host bars on the ops board use this. The notch
 * marks where spend should be today; omit `pctExpected` for a plain bar
 * (Costs' "today" tile has no pace to mark against — a single day isn't a
 * pace).
 */
export function NotchBar({
  pctUsed,
  pctExpected,
  fillColor,
  tall,
}: {
  pctUsed: number;
  pctExpected?: number;
  /** undefined = track only, no fill (an untracked/unspent category). */
  fillColor?: string;
  tall?: boolean;
}) {
  const trackH = tall ? 10 : 7;
  const notchH = tall ? 14 : 11;
  return (
    <div
      className="relative w-full"
      style={{ height: trackH, borderRadius: 999, backgroundColor: "var(--bg-elevated)" }}
    >
      {fillColor && (
        <div
          className="vital-fill"
          style={{
            position: "absolute",
            top: 0,
            left: 0,
            height: "100%",
            width: `${Math.min(pctUsed, 100)}%`,
            borderRadius: 999,
            backgroundColor: fillColor,
          }}
        />
      )}
      {pctExpected !== undefined && (
        <div
          className="absolute"
          style={{
            top: -(notchH - trackH) / 2,
            left: `${Math.min(pctExpected, 100)}%`,
            width: 2,
            height: notchH,
            borderRadius: 2,
            backgroundColor: alpha("var(--text-primary)", 75),
          }}
        />
      )}
    </div>
  );
}
