"use client";

import { useEffect } from "react";
import { applyHour } from "../lib/hour";

/**
 * The lamp: three fixed layers behind all content.
 * (a) source — soft-gradient circle top-right, 7s opacity flicker (opacity-
 * only animation — animating transform/filter on a layer this size is a
 * real performance cost on phones)
 * (b) cone — clipped wash falling down-left
 * (c) floor — bottom vignette
 * Also owns the minute tick that keeps data-hour current after the
 * pre-paint script (app/layout.tsx) has done the first stamp.
 */
export default function LampShell() {
  useEffect(() => {
    applyHour();
    const id = setInterval(() => applyHour(), 60_000);
    return () => clearInterval(id);
  }, []);

  const layer: React.CSSProperties = {
    position: "fixed",
    pointerEvents: "none",
    zIndex: -1,
  };

  return (
    <>
      <div
        aria-hidden
        style={{
          ...layer,
          right: -90,
          top: -150,
          width: 330,
          height: 330,
          borderRadius: "50%",
          background: "var(--lamp)",
          animation: "flicker 7s ease-in-out infinite",
          willChange: "opacity",
        }}
      />
      <div
        aria-hidden
        style={{
          ...layer,
          right: -40,
          top: -60,
          width: 520,
          height: 760,
          clipPath: "polygon(58% 0%,100% 0%,100% 74%,6% 100%,0% 62%)",
          background: "var(--cone)",
        }}
      />
      <div
        aria-hidden
        style={{
          ...layer,
          left: 0,
          right: 0,
          bottom: 0,
          height: 340,
          background: "var(--floor)",
        }}
      />
    </>
  );
}
