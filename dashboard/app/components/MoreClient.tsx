"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import pkg from "@/package.json";
import { pageAllowed, type PageAllowlist } from "@/lib/permissions";
import { NAV_GROUPS, type NavGroup } from "./navGroups";

const TONE_STYLE: Record<NavGroup["tone"], React.CSSProperties> = {
  near: {
    background: "var(--slip-near)",
    boxShadow: "var(--sh-near)",
    borderTop: "1px solid var(--lit)",
  },
  far: {
    background: "var(--slip-far)",
    boxShadow: "var(--sh-far)",
  },
};

export default function MoreClient() {
  const [user, setUser] = useState<string | null>(null);
  // Confined accounts see only their allowed pages — same signal
  // NavClient/TabShelf read. The shelf already hides this tab when /more is
  // off the allowlist; this filter covers a direct visit to /more.
  const [pages, setPages] = useState<PageAllowlist | null>(null);

  useEffect(() => {
    let live = true;
    fetch("/api/whoami")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (!live || !d) return;
        setUser(d.user ?? null);
        setPages(d.pages ?? "*");
      })
      .catch(() => { if (live) setPages("*"); });
    return () => { live = false; };
  }, []);

  // Screen 3g: three groups, sourced from the same routes the desktop
  // shelf's NAV_GROUPS points at, trimmed to
  // what this account may reach. Middleware enforces; this is courtesy.
  const groups = NAV_GROUPS.map((group) => ({
    ...group,
    items: group.items.filter((item) => pageAllowed(pages, item.href)),
  })).filter((group) => group.items.length > 0);
  const confined = pages !== null && pages !== "*" && groups.length === 0;

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-baseline justify-between mb-3 flex-shrink-0">
        <h1 className="text-2xl font-semibold" style={{ letterSpacing: "-0.015em", color: "var(--ink)" }}>
          More
        </h1>
        <span style={{ fontSize: 12, opacity: 0.5, color: "var(--ink)" }}>
          {user ? `${user} · ` : ""}v{pkg.version}
        </span>
      </div>

      {confined ? (
        <div
          className="slip-far anim-lift"
          style={{ padding: "14px 16px", fontSize: 14, lineHeight: 1.5 }}
        >
          This account is confined to a limited set of pages.
        </div>
      ) : (
        <div className="flex-1 overflow-auto flex flex-col gap-1">
          {groups.map((group, gi) => (
            <div key={group.title}>
              <div
                style={{
                  fontSize: 10,
                  letterSpacing: ".13em",
                  textTransform: "uppercase",
                  opacity: 0.45,
                  margin: gi === 0 ? "0 0 3px" : "8px 0 3px",
                }}
              >
                {group.title}
              </div>
              <div
                className="anim-lift"
                style={{
                  ...TONE_STYLE[group.tone],
                  animationDelay: `${gi * 80}ms`,
                  borderRadius: "4px 14px 14px 13px",
                  overflow: "hidden",
                }}
              >
                {group.items.map((item, ii) => (
                  <div key={item.href}>
                    {ii > 0 && (
                      <div style={{ height: 1, opacity: 0.1, background: "currentColor" }} aria-hidden />
                    )}
                    <Link
                      href={item.href}
                      className="press"
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                        minHeight: 44,
                        padding: "11px 16px",
                        fontSize: 15.5,
                        color: "var(--ink)",
                        textDecoration: "none",
                      }}
                    >
                      <span>{item.label}</span>
                      {item.detail && (
                        <span style={{ fontSize: 12, opacity: 0.55 }}>{item.detail}</span>
                      )}
                    </Link>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
