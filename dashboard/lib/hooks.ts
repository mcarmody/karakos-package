"use client";

import { useEffect, useLayoutEffect, useState, useCallback } from "react";

const POLL_CACHE_PREFIX = "karakos-poll:";

/** Best-effort sessionStorage read — private browsing, disabled storage, or
 * a quota error all just mean "no cached value," never a thrown error. */
function readPollCache<T>(url: string): T | null {
  try {
    if (typeof window === "undefined") return null;
    const raw = window.sessionStorage.getItem(POLL_CACHE_PREFIX + url);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}

/** Best-effort sessionStorage write — same posture as the read above. */
function writePollCache<T>(url: string, data: T): void {
  try {
    if (typeof window === "undefined") return;
    window.sessionStorage.setItem(POLL_CACHE_PREFIX + url, JSON.stringify(data));
  } catch {
    // quota exceeded, storage disabled, private mode — silently drop
  }
}

/**
 * Poll an endpoint at a given interval. A falsy url disables the poll —
 * callers gate per-user sections on it (e.g. Settings only polls
 * /api/health for the unrestricted account).
 *
 * Hydrates its initial state from sessionStorage (keyed per endpoint) so a
 * reload or re-navigation paints the last real data immediately instead of
 * a blank/loading flash, then silently revalidates via the normal poll.
 * sessionStorage, not localStorage — per-tab, so a stale week-old tab never
 * resurfaces as "current" data.
 *
 * The cache is read in a layout effect, NOT in the useState initializer.
 * The server has no sessionStorage, so it always renders the empty state;
 * a client whose first render already held cached data rendered different
 * markup from the server's HTML, which is React hydration error #418. That
 * fired on every page from the second load in a tab onward, because
 * NavClient polls /api/health on every page. A layout effect runs after
 * hydration but before the browser paints, so the cached data still shows
 * on the first frame: no blank flash, and no mismatch.
 */
export function usePoll<T>(url: string, intervalMs: number = 10000) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useLayoutEffect(() => {
    const cached = readPollCache<T>(url);
    if (cached !== null) {
      setData(cached);
      setLoading(false);
    }
  }, [url]);

  const fetchData = useCallback(async () => {
    if (!url) return;
    try {
      const res = await fetch(url);
      if (!res.ok) {
        if (res.status === 401) {
          window.location.href = "/login";
          return;
        }
        throw new Error(`HTTP ${res.status}`);
      }
      const json = await res.json();
      setData(json);
      setError(null);
      writePollCache(url, json);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  }, [url]);

  // Pause while the tab is hidden (Karakos review 2026-09-23 #29: the kiosk
  // moved 2.96 GB because every background tab kept polling), and catch up
  // with one immediate fetch when it becomes visible again.
  useEffect(() => {
    const hidden = () => typeof document !== "undefined" && document.hidden;
    const tick = () => {
      if (!hidden()) fetchData();
    };
    fetchData();
    const id = setInterval(tick, intervalMs);
    const onVisible = () => {
      if (!hidden()) fetchData();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [fetchData, intervalMs]);

  return { data, error, loading, refetch: fetchData };
}

/**
 * Track a CSS media query in JS, for the handful of places layout choice
 * isn't just a style flip — e.g.
 * deciding whether two panels are mounted side by side or as one.
 * Starts `false` (matches server-rendered markup) and syncs after mount;
 * expect one extra render on desktop viewports, same trade the existing
 * whoami fetches already make.
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(false);

  useEffect(() => {
    const mql = window.matchMedia(query);
    setMatches(mql.matches);
    const handler = (e: MediaQueryListEvent) => setMatches(e.matches);
    mql.addEventListener("change", handler);
    return () => mql.removeEventListener("change", handler);
  }, [query]);

  return matches;
}
