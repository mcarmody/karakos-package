import type { Metadata, Viewport } from "next";
import type { CSSProperties } from "react";
import localFont from "next/font/local";
import { fontStack } from "./_fonts/stack";
// Token contract loads ABOVE globals.css — see app/theme.css. Placeholder
// values only; this changes nothing visually today.
import "./theme.css";
import "./globals.css";
import AppShell from "./components/AppShell";

const sourceSans = localFont({
  src: [
    { path: "./_fonts/source-sans-3/source-sans-3-normal-ac057a5593.woff2", weight: "200 900", style: "normal" },
  ],
  variable: "--font-sans",
  display: "swap",
});

// The root font renders live data (names, chat, memory text), so it keeps
// every subset Google serves for it. Each non-latin subset is its own file
// with a unicode-range, fetched only when such a character is on the page;
// fontStack() puts them between Source Sans and its fallback.
const sourceSansCyrillicExt = localFont({
  src: [
    { path: "./_fonts/source-sans-3/source-sans-3-normal-cyrillic-ext-ce21e07f81.woff2", weight: "200 900", style: "normal" },
  ],
  display: "swap",
  preload: false,
  adjustFontFallback: false,
  declarations: [{ prop: "unicode-range", value: "U+0460-052F, U+1C80-1C8A, U+20B4, U+2DE0-2DFF, U+A640-A69F, U+FE2E-FE2F" }],
});
const sourceSansCyrillic = localFont({
  src: [
    { path: "./_fonts/source-sans-3/source-sans-3-normal-cyrillic-44aa5fb37c.woff2", weight: "200 900", style: "normal" },
  ],
  display: "swap",
  preload: false,
  adjustFontFallback: false,
  declarations: [{ prop: "unicode-range", value: "U+0301, U+0400-045F, U+0490-0491, U+04B0-04B1, U+2116" }],
});
const sourceSansGreekExt = localFont({
  src: [
    { path: "./_fonts/source-sans-3/source-sans-3-normal-greek-ext-cd19f948c2.woff2", weight: "200 900", style: "normal" },
  ],
  display: "swap",
  preload: false,
  adjustFontFallback: false,
  declarations: [{ prop: "unicode-range", value: "U+1F00-1FFF" }],
});
const sourceSansGreek = localFont({
  src: [
    { path: "./_fonts/source-sans-3/source-sans-3-normal-greek-5045881eda.woff2", weight: "200 900", style: "normal" },
  ],
  display: "swap",
  preload: false,
  adjustFontFallback: false,
  declarations: [{ prop: "unicode-range", value: "U+0370-0377, U+037A-037F, U+0384-038A, U+038C, U+038E-03A1, U+03A3-03FF" }],
});
const sourceSansVietnamese = localFont({
  src: [
    { path: "./_fonts/source-sans-3/source-sans-3-normal-vietnamese-7a9ba93945.woff2", weight: "200 900", style: "normal" },
  ],
  display: "swap",
  preload: false,
  adjustFontFallback: false,
  declarations: [{ prop: "unicode-range", value: "U+0102-0103, U+0110-0111, U+0128-0129, U+0168-0169, U+01A0-01A1, U+01AF-01B0, U+0300-0301, U+0303-0304, U+0308-0309, U+0323, U+0329, U+1EA0-1EF9, U+20AB" }],
});
const sourceSansLatinExt = localFont({
  src: [
    { path: "./_fonts/source-sans-3/source-sans-3-normal-latin-ext-ed3571ea9f.woff2", weight: "200 900", style: "normal" },
  ],
  display: "swap",
  preload: false,
  adjustFontFallback: false,
  declarations: [{ prop: "unicode-range", value: "U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, U+2113, U+2C60-2C7F, U+A720-A7FF" }],
});

export const metadata: Metadata = {
  title: "Karakos",
  description: "Agent system — monitoring, chat, observability",
  manifest: "/manifest.webmanifest",
  appleWebApp: {
    capable: true,
    statusBarStyle: "black-translucent",
    title: "Karakos",
  },
  icons: {
    apple: "/icons/apple-touch-icon.png",
  },
  other: {
    // Next's appleWebApp.capable emits title + status-bar-style but NOT
    // this tag — and iOS ignores apple-mobile-web-app-status-bar-style
    // unless the legacy capable tag is present. Without it the installed
    // app letterboxes below the status bar (window 793 on an 852pt
    // screen, safe-area-bottom 0) instead of going full-bleed — the
    // bottom-strip bug, diagnosed on-device 2026-08-19 10:47.
    "apple-mobile-web-app-capable": "yes",
  },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: "#241c17",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" data-hour="dusk" suppressHydrationWarning>
      <head>
        {/* Pre-paint hour stamp — prevents a flash of the wrong palette.
            Boundaries duplicate app/lib/hour.ts; keep them in sync. */}
        <script
          dangerouslySetInnerHTML={{
            __html: `
              (function() {
                try {
                  // One-time migration (2026-08-20): any stored light/dark
                  // pin predates Lamplight's time-of-day palette going live
                  // today, so it silently pinned day/night forever and the
                  // scheme never changed with the clock. Hand control back
                  // to the clock once; re-pinning via the toggle still works
                  // and is left alone from then on.
                  if (!localStorage.getItem('karakos-lamplight-v1')) {
                    var t0 = localStorage.getItem('karakos-theme');
                    if (t0 === 'light' || t0 === 'dark') {
                      localStorage.setItem('karakos-theme', 'system');
                    }
                    localStorage.setItem('karakos-lamplight-v1', '1');
                  }
                  var b = null;
                  var pin = localStorage.getItem('karakos-hour');
                  var B = ['dawn','morning','day','afternoon','golden','dusk','night','late'];
                  if (pin && B.indexOf(pin) !== -1) b = pin;
                  if (!b) {
                    var t = localStorage.getItem('karakos-theme');
                    if (t === 'light') b = 'day';
                    else if (t === 'dark') b = 'night';
                  }
                  if (!b) {
                    var d = new Date();
                    var m = d.getHours() * 60 + d.getMinutes();
                    b = (m >= 300 && m < 450) ? 'dawn'
                      : (m >= 450 && m < 690) ? 'morning'
                      : (m >= 690 && m < 930) ? 'day'
                      : (m >= 930 && m < 1080) ? 'afternoon'
                      : (m >= 1080 && m < 1155) ? 'golden'
                      : (m >= 1155 && m < 1290) ? 'dusk'
                      : (m >= 1290 || m < 90) ? 'night'
                      : 'late';
                  }
                  document.documentElement.setAttribute('data-hour', b);
                } catch(e) {}
              })();
            `,
          }}
        />
        {/* The iOS standalone bottom-gap fix lives in globals.css: a
            (display-mode: standalone) media rule flips --app-height from
            100dvh to 100lvh. Diagnosed on-device 2026-08-19 10:33 — in
            standalone, innerHeight/visualViewport/dvh/svh ALL report the
            small viewport (793 on an 852pt screen); a JS measurement
            script lived here briefly and was wrong for that reason. lvh
            is the only unit that reports the true screen height. */}
      </head>
      <body className={sourceSans.variable} style={{ "--font-sans": fontStack(sourceSans, sourceSansCyrillicExt, sourceSansCyrillic, sourceSansGreekExt, sourceSansGreek, sourceSansVietnamese, sourceSansLatinExt) } as CSSProperties}>
        <AppShell>{children}</AppShell>
        {/* Registers the service worker post-load, out of the critical path.
            PRODUCTION ONLY, deliberately. sw.js is cache-first for
            /_next/static/, which is right in production because those paths are
            content-hashed — a changed chunk is a changed URL. Under `next dev`
            they are NOT hashed: the same URL serves different bytes after every
            edit, so cache-first pins the browser to stale code indefinitely,
            across full navigations, while the dev server compiles correctly and
            curl sees the new bytes. That cost 25 minutes on 2026-08-21 and is
            close to undiagnosable from inside, because every signal you would
            reach for says the edit landed. The dev branch below actively
            unregisters and clears, so a profile already poisoned by an earlier
            session heals itself on the next load.
            Consequence, accepted: navigator.serviceWorker.ready never resolves
            without a registration, so the push toggles (app/page.tsx:420,
            PushSettings.tsx) sit spinning in dev. Dev push has never worked
            anyway — it needs a real VAPID subscription. */}
        <script
          dangerouslySetInnerHTML={{
            __html:
              process.env.NODE_ENV === "production"
                ? `
              if ('serviceWorker' in navigator) {
                window.addEventListener('load', function() {
                  navigator.serviceWorker.register('/sw.js').catch(function() {});
                });
              }
            `
                : `
              if ('serviceWorker' in navigator) {
                navigator.serviceWorker.getRegistrations().then(function(rs) {
                  rs.forEach(function(r) { r.unregister(); });
                }).catch(function() {});
                if (window.caches && caches.keys) {
                  caches.keys().then(function(ks) {
                    ks.forEach(function(k) { caches.delete(k); });
                  }).catch(function() {});
                }
              }
            `,
          }}
        />
      </body>
    </html>
  );
}
