"use client";

/**
 * The /chat header's session picker. Replaces the
 * plain native <select> (which can't carry a per-row live/idle dot or a
 * host label, and renders as an unstyled OS sheet on iOS regardless of any
 * CSS here) with a real popover on desktop and a bottom sheet on phone —
 * design/LAMPLIGHT.md's shell contract (press feedback, cross-fade rise,
 * lamp-lit surfaces).
 */

import { useEffect, useRef, useState } from "react";
import { useMediaQuery } from "@/lib/hooks";
import type { SessionOption } from "@/lib/sessionRoster";

interface SessionPickerProps {
  options: SessionOption[];
  selected: string;
  onSelect: (name: string) => void;
  titleSize: number;
  maxWidth: string;
}

function LiveDot({ live }: { live: boolean }) {
  return (
    <span
      aria-hidden
      style={{
        width: 7,
        height: 7,
        borderRadius: "50%",
        flex: "none",
        background: live ? "var(--ok, #86a85f)" : "var(--ink)",
        opacity: live ? 1 : 0.35,
      }}
    />
  );
}

function SessionRow({
  option,
  active,
  onClick,
}: {
  option: SessionOption;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="press"
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        width: "100%",
        textAlign: "left",
        padding: "10px 12px",
        borderRadius: 10,
        border: "none",
        background: active ? "var(--slip-near, var(--elevated))" : "transparent",
        color: "var(--ink)",
        cursor: "pointer",
      }}
    >
      <LiveDot live={option.live} />
      <span style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            fontSize: 14.5,
            fontWeight: active ? 600 : 500,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {option.displayName}
        </div>
        {/* Some shards' `note` field (used as `definition`) is a full
            engineering paragraph, not a one-line label — a single-line
            ellipsis chopped it mid-word, but letting it wrap freely blew
            the row out to 6+ lines and broke the popover's layout. A
            2-line clamp is the middle ground: readable, bounded height. */}
        <div
          style={{
            fontSize: 11.5,
            opacity: 0.55,
            lineHeight: 1.35,
            display: "-webkit-box",
            WebkitLineClamp: 2,
            WebkitBoxOrient: "vertical",
            overflow: "hidden",
          }}
        >
          {option.host}
          {option.definition ? ` · ${option.definition}` : ""}
        </div>
      </span>
    </button>
  );
}

export default function SessionPicker({ options, selected, onSelect, titleSize, maxWidth }: SessionPickerProps) {
  const [open, setOpen] = useState(false);
  const isPhone = useMediaQuery("(max-width: 767px)");
  const rootRef = useRef<HTMLDivElement>(null);
  const selectedOption = options.find((o) => o.name === selected);

  // Click-outside / Escape close for the desktop popover. The phone sheet
  // closes on backdrop click instead (handled inline below).
  useEffect(() => {
    if (!open || isPhone) return;
    const onDocClick = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, isPhone]);

  return (
    <div ref={rootRef} style={{ position: "relative", display: "flex", alignItems: "center" }}>
      <button
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="press"
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          background: "none",
          border: "none",
          color: "var(--ink)",
          fontSize: titleSize,
          fontWeight: 600,
          letterSpacing: "-0.015em",
          maxWidth,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
          cursor: "pointer",
          padding: 0,
        }}
      >
        <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
          {selectedOption?.displayName || selected || "Choose a session…"}
        </span>
        <span aria-hidden style={{ fontSize: 13, opacity: 0.6 }}>▾</span>
      </button>

      {open && !isPhone && (
        <div
          role="listbox"
          style={{
            position: "absolute",
            top: "calc(100% + 8px)",
            left: 0,
            zIndex: 40,
            width: 340,
            maxHeight: 360,
            overflowY: "auto",
            padding: 6,
            borderRadius: 14,
            border: "1px solid var(--border)",
            background: "var(--elevated)",
            boxShadow: "6px 10px 28px rgba(0,0,0,.35)",
          }}
        >
          {options.length === 0 ? (
            <div style={{ padding: 14, fontSize: 13, opacity: 0.6 }}>No sessions available.</div>
          ) : (
            options.map((o) => (
              <SessionRow
                key={o.name}
                option={o}
                active={o.name === selected}
                onClick={() => {
                  onSelect(o.name);
                  setOpen(false);
                }}
              />
            ))
          )}
        </div>
      )}

      {open && isPhone && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 50,
            display: "flex",
            flexDirection: "column",
            justifyContent: "flex-end",
          }}
        >
          <div
            aria-hidden
            onClick={() => setOpen(false)}
            style={{ position: "absolute", inset: 0, background: "rgba(0,0,0,.55)" }}
          />
          <div
            role="listbox"
            style={{
              position: "relative",
              maxHeight: "70vh",
              overflowY: "auto",
              borderTopLeftRadius: 18,
              borderTopRightRadius: 18,
              padding: "14px 10px calc(18px + env(safe-area-inset-bottom, 0px))",
              background: "var(--elevated)",
              borderTop: "1px solid var(--border)",
            }}
          >
            <div
              aria-hidden
              style={{
                width: 36,
                height: 4,
                borderRadius: 2,
                background: "var(--ink)",
                opacity: 0.25,
                margin: "0 auto 12px",
              }}
            />
            <div style={{ fontSize: 12, opacity: 0.55, padding: "0 10px 8px", textTransform: "uppercase", letterSpacing: "0.06em" }}>
              Choose a session
            </div>
            {options.length === 0 ? (
              <div style={{ padding: 14, fontSize: 13, opacity: 0.6 }}>No sessions available.</div>
            ) : (
              options.map((o) => (
                <SessionRow
                  key={o.name}
                  option={o}
                  active={o.name === selected}
                  onClick={() => {
                    onSelect(o.name);
                    setOpen(false);
                  }}
                />
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
}
