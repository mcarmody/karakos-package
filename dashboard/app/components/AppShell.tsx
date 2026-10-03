"use client";

import { usePathname } from "next/navigation";
import NavClient from "./NavClient";
import LampShell from "./LampShell";
import TabShelf from "./TabShelf";

export default function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  // /login is pre-auth: nav for a dashboard you haven't signed into yet is
  // meaningless, so it draws full-bleed with no chrome at all.
  if (pathname === "/login") {
    return <>{children}</>;
  }

  // Chat owns its own internal scroll region (header + composer pinned,
  // only the message list scrolls). That only
  // works if `main` itself never scrolls: with `overflow-auto` here too,
  // main's own box could be shorter than pt-safe + chat's height combined,
  // and main would scroll the WHOLE chat page — header and composer
  // included — even though the page's internal flexbox already pins them.
  // `main` becomes a plain, non-scrolling flex column for chat; the chat
  // page fills it and owns safe-area padding itself instead of getting it
  // from the generic pt-safe wrapper below.
  const isChat = pathname?.startsWith("/chat");

  return (
    <div
      data-app-shell
      className="flex flex-col lg:flex-row overflow-hidden"
      // position:fixed, not in-flow height: iOS standalone keeps the LAYOUT
      // viewport at small-viewport size (793 on an 852pt screen) even when
      // the window is genuinely full-bleed — so an in-flow 100lvh shell got
      // clipped at body's 793px box (overflow:hidden), sawing content off.
      // This element being position:fixed escapes BODY's overflow clip
      // (body isn't itself positioned, so the WebKit fixed-descendant-of-
      // overflow-hidden clip bug — see TabShelf.tsx — doesn't apply
      // here). It does NOT mean fixed descendants of THIS element are
      // safe: this div is itself positioned AND overflow:hidden, so any
      // position:fixed child nested inside it gets clipped to ITS box in
      // Safari (WebKit bug 160953). Any fixed overlay under data-app-shell
      // needs a portal to document.body, same as TabShelf. Diagnosed via
      // on-device shellRect readout, 2026-08-19 11:14.
      style={{
        position: "fixed",
        top: 0,
        left: 0,
        right: 0,
        height: "var(--app-height, 100dvh)",
      }}
    >
      {/* Lamplight — three fixed light layers behind everything */}
      <LampShell />
      {/* Desktop persistent sidebar — hidden below lg */}
      <div className="hidden lg:flex lg:flex-shrink-0">
        <NavClient />
      </div>

      {/* Content column: page content + mobile tab shelf. (The hamburger
          drawer lived here for ~45 minutes on 2026-08-19 — reverted with
          the PWA-standalone effort; the shelf is back for mobile-browser
          use, where bottom:0 has always been reliable.) */}
      <div className="flex flex-col flex-1 overflow-hidden min-w-0">
        {/* Gutters 20/28/34px.
            Chat keeps the same gutter but becomes a non-scrolling flex
            column instead of a scrolling block — see isChat comment above. */}
        <main
          className={
            isChat
              ? "flex-1 overflow-hidden flex flex-col min-h-0 p-5 sm:p-7 lg:p-[34px]"
              : "flex-1 overflow-auto p-5 sm:p-7 lg:p-[34px]"
          }
          style={isChat ? undefined : { overscrollBehavior: "contain" }}
        >
          {isChat ? (
            children
          ) : (
            <>
              {/* Notch/status-bar clearance on phone — pages own their own
                  headers now, there's no app-bar left to absorb this. */}
              <div className="pt-safe lg:hidden" aria-hidden />
              {children}
              {/* TabShelf is portaled to document.body and reserves no flex
                  space of its own; this spacer keeps the last bit of
                  scrollable content from rendering underneath it. */}
              <div style={{ height: "calc(70px + env(safe-area-inset-bottom))" }} className="lg:hidden" aria-hidden />
            </>
          )}
        </main>
      </div>
      <TabShelf />
    </div>
  );
}
