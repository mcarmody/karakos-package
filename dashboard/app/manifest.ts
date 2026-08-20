import type { MetadataRoute } from "next";
import { MANIFEST_BACKGROUND_COLOR, MANIFEST_THEME_COLOR } from "./lib/theme-tokens";

/**
 * Next 15 metadata route — served at /manifest.webmanifest.
 *
 * scope + start_url are both "/": the dashboard is served from the domain
 * root (see README/nginx setup), not a subpath, so a narrower scope would
 * silently exclude routes from the installed-app navigation.
 *
 * Icons are placeholder glyph tiles (public/icons/) — swap the PNGs there
 * and the colors in app/lib/theme-tokens.ts when real branding lands;
 * nothing else needs to change.
 */
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "Karakos",
    short_name: "Karakos",
    description: "Agent system monitoring and control",
    start_url: "/",
    scope: "/",
    id: "/",
    display: "standalone",
    background_color: MANIFEST_BACKGROUND_COLOR,
    theme_color: MANIFEST_THEME_COLOR,
    icons: [
      {
        src: "/icons/icon-192.png",
        sizes: "192x192",
        type: "image/png",
        purpose: "any",
      },
      {
        src: "/icons/icon-512.png",
        sizes: "512x512",
        type: "image/png",
        purpose: "any",
      },
      {
        src: "/icons/icon-maskable-512.png",
        sizes: "512x512",
        type: "image/png",
        purpose: "maskable",
      },
    ],
  };
}
