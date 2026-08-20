/**
 * Lamplight hour buckets — a time-of-day theme that recolors the whole
 * dashboard through CSS custom properties (see globals.css) instead of a
 * flat light/dark toggle.
 *
 * The same boundaries are duplicated in the pre-paint inline script in
 * app/layout.tsx — that script must be self-contained to run before
 * hydration. If you change a boundary here, change it there too.
 */

export type HourBucket =
  | "dawn" | "morning" | "day" | "afternoon"
  | "golden" | "dusk" | "night" | "late";

export const HOUR_BUCKETS: HourBucket[] = [
  "dawn", "morning", "day", "afternoon", "golden", "dusk", "night", "late",
];

/** Minutes-since-midnight → bucket. v1 fixed windows; sunset-aware later. */
export function bucketForMinutes(m: number): HourBucket {
  if (m >= 300 && m < 450) return "dawn";       // 05:00–07:30
  if (m >= 450 && m < 690) return "morning";    // 07:30–11:30
  if (m >= 690 && m < 930) return "day";        // 11:30–15:30
  if (m >= 930 && m < 1080) return "afternoon"; // 15:30–18:00
  if (m >= 1080 && m < 1155) return "golden";   // 18:00–19:15
  if (m >= 1155 && m < 1290) return "dusk";     // 19:15–21:30
  if (m >= 1290 || m < 90) return "night";      // 21:30–01:30
  return "late";                                // 01:30–05:00
}

/**
 * Resolve the bucket to show right now, honouring pins:
 * - localStorage["karakos-hour"] pins a bucket outright
 * - legacy "karakos-theme": light pins "day", dark pins "night"
 */
export function currentBucket(now: Date = new Date()): HourBucket {
  try {
    const pin = localStorage.getItem("karakos-hour");
    if (pin && (HOUR_BUCKETS as string[]).includes(pin)) return pin as HourBucket;
    const theme = localStorage.getItem("karakos-theme");
    if (theme === "light") return "day";
    if (theme === "dark") return "night";
  } catch {
    /* SSR or storage blocked — fall through to the clock */
  }
  return bucketForMinutes(now.getHours() * 60 + now.getMinutes());
}

/** Stamp the bucket on <html> if it changed. Returns the applied bucket. */
export function applyHour(now: Date = new Date()): HourBucket {
  const b = currentBucket(now);
  const el = document.documentElement;
  if (el.getAttribute("data-hour") !== b) el.setAttribute("data-hour", b);
  return b;
}
