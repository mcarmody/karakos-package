/**
 * Colors shared between app/manifest.ts (PWA manifest install/splash
 * chrome) and the viewport theme-color meta tag in app/layout.tsx. A
 * metadata route can't read globals.css at request time, so these are
 * kept in sync here by hand instead of duplicated as raw hex.
 *
 * Value is Lamplight's dusk ground (--bg in globals.css's
 * [data-hour="dusk"] block) — the manifest's install/splash chrome
 * matches the app's own default palette.
 */
export const MANIFEST_BACKGROUND_COLOR = "#241c17"; // Lamplight dusk ground
export const MANIFEST_THEME_COLOR = "#241c17"; // Lamplight dusk ground
