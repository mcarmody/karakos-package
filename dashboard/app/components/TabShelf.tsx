"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { usePathname } from "next/navigation";
import { pageAllowed, type PageAllowlist } from "@/lib/permissions";

const TABS = [
  { href: "/chat", label: "Chat" },
  { href: "/", label: "Ops Board" },
  { href: "/more", label: "More" },
];

/**
 * Mobile bottom tab shelf.
 *
 * Replaces the old off-canvas drawer: Chat / Ops Board / More live
 * here on every phone route. Desktop keeps NavClient's sidebar
 * instead — this component is `lg:hidden`.
 */
export default function TabShelf() {
  const pathname = usePathname();

  // Confined accounts see only their allowed tabs — same signal NavClient
  // reads for the sidebar, fetched independently since the shelf lives
  // outside it. `pages` is "*" for unrestricted accounts.
  const [pages, setPages] = useState<PageAllowlist | null>(null);
  useEffect(() => {
    let live = true;
    fetch("/api/whoami")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (live && d) setPages(d.pages ?? "*"); })
      .catch(() => {});
    return () => { live = false; };
  }, []);

  // Portal target must exist before we render to document.body (SSR has
  // no document at all, and mounting straight to document.body during the
  // very first client render would double-render vs. the server markup).
  const [mounted, setMounted] = useState(false);
  useEffect(() => { setMounted(true); }, []);

  // The per-account allowlist. "/more" is the shelf's own catch-all, not a nav item.
  const tabs = TABS.filter((t) => pageAllowed(pages, t.href));

  if (!mounted) return null;

  return createPortal(
    <nav
      aria-label="Primary"
      className="shelf lg:hidden flex"
      style={{
        // Root cause of the day-long "shelf invisible / floating / cut
        // off" saga: this component is `position: fixed`, but it was a
        // DESCENDANT of `data-app-shell` (AppShell.tsx), which is ALSO
        // `position: fixed` and has `overflow: hidden`. That combination
        // is a documented Safari/WebKit bug (WebKit bug 160953): a fixed
        // descendant of an overflow:hidden positioned ancestor gets
        // clipped to that ancestor's box instead of escaping to the true
        // viewport, contrary to spec. Every --app-height / dvh-vs-lvh /
        // manual pixel-offset fix tried today (see git log) was chasing
        // that clip, not the real bug — none of them addressed it because
        // the ancestor box was always the thing doing the clipping.
        // Portaling straight to document.body removes this element from
        // that ancestor's DOM subtree entirely, so the clip can't apply.
        //
        // bottom is 0, unconditionally. The standalone-mode offset hacks
        // (-30px, then calc(100dvh - 100lvh)) are gone with the PWA
        // itself (mobile-browser use only). A fixed element's containing block
        // IS the layout viewport — in a browser tab that's exactly the
        // visible area, so bottom:0 is simply correct here.
        position: "fixed",
        left: 0,
        right: 0,
        bottom: 0,
        zIndex: 20,
        paddingTop: 10,
        // 0 in a normal browser tab with the toolbar up; covers the home
        // indicator when Safari minimizes its chrome on scroll.
        paddingBottom: "calc(4px + env(safe-area-inset-bottom))",
        paddingLeft: 10,
        paddingRight: 10,
      }}
    >
      {tabs.map(({ href, label }) => {
        const active = href === "/" ? pathname === "/" : pathname?.startsWith(href);
        return (
          <Link
            key={href}
            href={href}
            className="press"
            style={{
              flex: 1,
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              justifyContent: "center",
              gap: 6,
              minHeight: 44,
              color: "var(--ink)",
              textDecoration: "none",
            }}
          >
            <span
              aria-hidden
              style={{
                width: 26,
                height: 2,
                borderRadius: 2,
                background: active ? "var(--accent)" : "transparent",
              }}
            />
            <span style={{ fontSize: 11.5, opacity: active ? 1 : 0.42 }}>{label}</span>
          </Link>
        );
      })}
    </nav>,
    document.body
  );
}
