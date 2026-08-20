"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { usePathname } from "next/navigation";

const TABS = [
  { href: "/", label: "Home" },
  { href: "/agents", label: "Agents" },
  { href: "/chat", label: "Chat" },
  { href: "/conversations", label: "Convos" },
  { href: "/system", label: "System" },
  { href: "/settings", label: "Settings" },
];

/**
 * Mobile bottom tab shelf. Mirrors the sidebar nav in layout.tsx (which is
 * `hidden` below the `lg` breakpoint) so phone/tablet users get the same
 * routes as a fixed bottom bar instead of a squeezed 208px sidebar.
 *
 * This package is single-account (see app/api/auth/route.ts — one
 * DASHBOARD_USER/DASHBOARD_PASSWORD pair, no per-user page allowlists), so
 * unlike the household dashboard this was ported from, every tab renders
 * unconditionally — there's no per-account permission check to gate on.
 */
export default function TabShelf() {
  const pathname = usePathname();

  // Portal target must exist before we render to document.body (SSR has no
  // document at all, and mounting straight to document.body during the
  // very first client render would double-render vs. the server markup).
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    setMounted(true);
  }, []);

  if (!mounted) return null;

  return createPortal(
    <nav
      aria-label="Primary"
      className="shelf lg:hidden flex"
      style={{
        // position:fixed with bottom:0 unconditionally -- this is a plain
        // mobile-browser tab bar, not a PWA-standalone shell, so there's
        // no safe-area/viewport offset hack to reconcile. Portaled to
        // document.body (rather than rendered in place) so a future
        // positioned+overflow:hidden ancestor higher in the tree can't
        // clip it -- a fixed descendant of an overflow:hidden positioned
        // ancestor gets clipped to that ancestor's box in Safari/WebKit
        // (WebKit bug 160953), which is exactly the kind of "shelf is
        // invisible on iOS" bug that's easy to reintroduce by nesting.
        position: "fixed",
        left: 0,
        right: 0,
        bottom: 0,
        zIndex: 20,
        paddingTop: 10,
        paddingBottom: "calc(4px + env(safe-area-inset-bottom))",
        paddingLeft: 10,
        paddingRight: 10,
      }}
    >
      {TABS.map(({ href, label }) => {
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
                width: 22,
                height: 2,
                borderRadius: 2,
                background: active ? "var(--accent)" : "transparent",
              }}
            />
            <span style={{ fontSize: 10.5, opacity: active ? 1 : 0.42 }}>{label}</span>
          </Link>
        );
      })}
    </nav>,
    document.body
  );
}
