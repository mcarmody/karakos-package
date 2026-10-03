"use client";

/**
 * Push-support detection, shared by every surface that needs to know
 * whether *this* browser/context can receive web push.
 *
 * Extracted from app/components/PushSettings.tsx (Settings → Notifications)
 * so any opt-in prompt agrees with it
 * instead of re-deriving its own answer. Both call sites care about the
 * same iOS wrinkle: Safari refuses push notifications from a plain browser
 * tab — the page has to be added to the Home Screen (standalone display)
 * first.
 */

export function isIOS(): boolean {
  return /iPad|iPhone|iPod/.test(navigator.userAgent) && !("MSStream" in window);
}

export function isStandalone(): boolean {
  return (
    window.matchMedia("(display-mode: standalone)").matches ||
    // iOS Safari's own non-standard flag — matchMedia doesn't cover it there.
    (navigator as Navigator & { standalone?: boolean }).standalone === true
  );
}

/** The raw browser APIs web push needs, independent of the iOS wrinkle. */
export function hasPushApis(): boolean {
  return "serviceWorker" in navigator && "PushManager" in window;
}

/**
 * True when this browser/context can actually receive web push right now —
 * the APIs exist, and if it's iOS Safari, the app is running standalone
 * (added to the Home Screen). A browser tab on iOS gets nothing: no push
 * API call there will ever succeed, so callers should show no UI at all
 * rather than a button that can't work.
 */
export function isPushCapable(): boolean {
  return hasPushApis() && !(isIOS() && !isStandalone());
}
