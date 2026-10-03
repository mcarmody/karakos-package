"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { usePoll } from "@/lib/hooks";
import { pageAllowed, type PageAllowlist } from "@/lib/permissions";
import { DAILY_ITEMS, ICON_RAIL_ITEMS, NAV_GROUPS } from "./navGroups";
import { navSubtitle } from "@/lib/branding";

/**
 * Desktop/tablet persistent nav.
 *
 * One component, two markups, CSS picks between them at the breakpoint
 * AppShell already had (lg = 1024, the iPad-landscape boundary):
 *   1024-1279  76px icon rail  — daily three + Tasks/Fleet + a More link
 *   >=1280     236px labelled shelf — grouped nav (same grouping as /more)
 *              + an agent status card
 * Below 1024 this renders nothing; TabShelf (lg:hidden) is the phone/iPad-
 * portrait nav.
 */

// Nav header identity + hardware line: the top of the nav names the SYSTEM
// ("Karakos", an optional operator-set subtitle, an uptime/hardware stat below).

interface HealthHost {
  host?: {
    uptime_seconds: number | null;
    temp_c: number | null;
    load1: number | null;
  };
}

function formatUptime(s: number): string {
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  if (d > 0) return `up ${d}d ${h}h`;
  const m = Math.floor((s % 3600) / 60);
  return h > 0 ? `up ${h}h ${m}m` : `up ${m}m`;
}

export default function NavClient() {
  const pathname = usePathname();

  // Confined accounts see only their allowed pages. Middleware is what
  // actually enforces this — trimming the nav is courtesy, so the sidebar
  // isn't a list of links that all bounce back to where you already are.
  // `pages` is "*" for unrestricted accounts, an allowlist otherwise
  const [pages, setPages] = useState<PageAllowlist | null>(null);
  useEffect(() => {
    let live = true;
    fetch("/api/whoami")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (live && d) setPages(d.pages ?? "*"); })
      .catch(() => {});
    return () => { live = false; };
  }, []);

  const restricted = pages !== null && pages !== "*";

  // Hardware line for the header — restricted accounts keep the static
  // title/subtitle but skip the poll (same courtesy-trim rationale as
  // `pages` above).
  const { data: health } = usePoll<HealthHost>(restricted ? "" : "/api/health", 60000);

  function isActive(href: string): boolean {
    return href === "/" ? pathname === "/" : pathname.startsWith(href);
  }

  // Per-account page allowlist.
  const dailyItems = DAILY_ITEMS.filter((item) => pageAllowed(pages, item.href));
  const navGroups = NAV_GROUPS.map((group) => ({
    ...group,
    items: group.items.filter((item) => pageAllowed(pages, item.href)),
  })).filter((group) => group.items.length > 0);
  const railItems = ICON_RAIL_ITEMS.filter((item) => pageAllowed(pages, item.href));

  return (
    <nav className="rail flex w-[76px] xl:w-[236px] flex-shrink-0 flex-col h-full" aria-label="Primary">
      {/* ---- 1024-1279: icon rail — hidden at xl+ ---- */}
      <div className="xl:hidden flex flex-col items-center gap-1.5 h-full py-5">
        <KarakosBadge compact />
        <div style={{ height: 12 }} />
        {railItems.map((item) => (
          <RailIcon key={item.href} href={item.href} label={item.label} active={isActive(item.href)} />
        ))}
        {pageAllowed(pages, "/more") && (
          <div style={{ marginTop: "auto" }}>
            <RailIcon href="/more" label="More" active={isActive("/more")} />
          </div>
        )}
      </div>

      {/* ---- >=1280: labelled shelf, grouped nav + agent card ---- */}
      <div className="hidden xl:flex xl:flex-col h-full">
        <div style={{ padding: "22px 18px 16px" }}>
          <KarakosBadge host={health?.host} />
        </div>
        <div className="flex-1 overflow-y-auto" style={{ padding: "0 12px", display: "flex", flexDirection: "column", gap: 2 }}>
          {dailyItems.length > 0 && <GroupLabel first>daily</GroupLabel>}
          {dailyItems.map((item) => (
            <ShelfRow key={item.href} href={item.href} label={item.label} active={isActive(item.href)} />
          ))}
          {navGroups.map((group) => (
            <div key={group.title}>
              <GroupLabel>{group.title}</GroupLabel>
              {group.items.map((item) => (
                <ShelfRow key={item.href} href={item.href} label={item.label} active={isActive(item.href)} />
              ))}
            </div>
          ))}
        </div>
        <div
          style={{
            padding: "14px 20px 18px",
            borderTop: "1px solid color-mix(in srgb, var(--lit) 55%, transparent)",
            display: "flex",
            alignItems: "center",
            gap: 9,
          }}
        >
          <span aria-hidden style={{ width: 14, height: 14, borderRadius: "50%", background: "var(--lamp)" }} />
          <span style={{ fontSize: 11.5, opacity: 0.55 }}>Karakos</span>
        </div>
      </div>
    </nav>
  );
}

function KarakosBadge({
  compact,
  host,
}: {
  compact?: boolean;
  host?: HealthHost["host"];
}) {
  if (compact) {
    return (
      <div
        aria-label="Karakos"
        style={{
          width: 46,
          height: 46,
          borderRadius: 14,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontSize: 18,
          fontWeight: 700,
          background: "var(--accent)",
          color: "var(--on-accent)",
          boxShadow: "0 0 20px color-mix(in srgb, var(--accent) 30%, transparent)",
        }}
      >
        K
      </div>
    );
  }
  const subtitle = navSubtitle();
  const bits: string[] = [];
  if (host?.uptime_seconds != null) bits.push(formatUptime(host.uptime_seconds));
  if (host?.temp_c != null) bits.push(`${host.temp_c}°C`);
  if (host?.load1 != null) bits.push(`load ${host.load1}`);
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "4px 2px" }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 15, fontWeight: 600 }}>Karakos</div>
        {subtitle && <div style={{ fontSize: 11, opacity: 0.5 }}>{subtitle}</div>}
        {bits.length > 0 && (
          <div style={{ fontSize: 10, opacity: 0.4, marginTop: 2, fontVariantNumeric: "tabular-nums" }}>
            {bits.join(" · ")}
          </div>
        )}
      </div>
    </div>
  );
}

function GroupLabel({ children, first }: { children: React.ReactNode; first?: boolean }) {
  return (
    <div
      style={{
        fontSize: 9.5,
        letterSpacing: ".14em",
        textTransform: "uppercase",
        opacity: 0.4,
        padding: first ? "8px 10px 6px" : "14px 10px 6px",
      }}
    >
      {children}
    </div>
  );
}

function ShelfRow({ href, label, active }: { href: string; label: string; active: boolean }) {
  return (
    <Link
      href={href}
      className="press"
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 11,
        minHeight: 44,
        padding: "9px 12px",
        borderRadius: 10,
        fontSize: 13.5,
        fontWeight: active ? 600 : 400,
        color: "var(--ink)",
        textDecoration: "none",
        opacity: active ? 1 : 0.62,
        background: active ? "var(--slip-near)" : "transparent",
        borderTop: active ? "1px solid var(--lit)" : "1px solid transparent",
      }}
    >
      {label}
    </Link>
  );
}

function RailIcon({ href, label, active }: { href: string; label: string; active: boolean }) {
  return (
    <Link
      href={href}
      className="press"
      aria-label={label}
      title={label}
      style={{
        width: 52,
        height: 52,
        borderRadius: 14,
        flex: "none",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 4,
        textDecoration: "none",
        color: "var(--ink)",
        opacity: active ? 1 : 0.5,
        background: active ? "var(--slip-near)" : "transparent",
        borderTop: active ? "1px solid var(--lit)" : "1px solid transparent",
      }}
    >
      <span aria-hidden style={{ width: 18, height: 2, borderRadius: 2, background: active ? "var(--accent)" : "transparent" }} />
      <span style={{ fontSize: 9.5 }}>{label}</span>
    </Link>
  );
}
