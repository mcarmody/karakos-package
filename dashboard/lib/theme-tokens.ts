/**
 * Placeholder color values shared between app/theme.css (custom properties,
 * consumed by components at render time) and app/manifest.ts (PWA manifest
 * colors — a metadata route can't read a stylesheet at request time, so the
 * two are kept in sync by hand here instead of duplicated as raw hex).
 *
 * Values are Lamplight's dusk ground (#241c17), so the
 * manifest's install/splash chrome matches the app's own palette.
 */
export const MANIFEST_BACKGROUND_COLOR = "#241c17"; // Lamplight dusk ground
export const MANIFEST_THEME_COLOR = "#241c17"; // Lamplight dusk ground
