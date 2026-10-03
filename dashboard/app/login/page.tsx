"use client";

import { useState } from "react";
import { alpha, ERR, fieldStyle, INPUT_FONT } from "@/app/components/lamplight-ui";

/**
 * Leave the login page by a full document load, not router.push().
 *

 * Symptom: "logging in doesn't work on submit, I have to submit and THEN
 * reload." The session cookie is set by the API
 * response, but router.push() is a client-side transition that can be served
 * from the App Router's prefetched RSC payload for the destination — a payload
 * fetched while there was still no session, so it is the /login redirect.
 * Reloading worked because a real request finally reached middleware carrying
 * the new cookie. A hard navigation makes that the only path.
 */
function goHome(home: unknown) {
  window.location.assign(typeof home === "string" ? home : "/");
}

export default function LoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      const res = await fetch("/api/auth", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });

      if (res.ok) {
        // The auth route returns where this account belongs. Confined accounts
        // go straight to their home page instead of bouncing off one they are
        // not allowed to see.
        const body = await res.json().catch(() => ({}));
        goHome(body?.home);
      } else {
        const data = await res.json();
        setError(data.error || "Login failed");
      }
    } catch (err) {
      setError("Network error");
    } finally {
      setLoading(false);
    }
  };

  const hairline: React.CSSProperties = {
    flex: 1,
    height: 1,
    background: alpha("var(--ink)", 16),
  };

  return (
    <div
      className="min-h-screen flex items-center justify-center relative overflow-hidden"
      style={{ background: "var(--bg)" }}
    >
      {/* Centered lamp glow — login's own variant of the corner LampShell,
          repositioned/resized for a full-bleed centered composition. */}
      <div
        aria-hidden
        style={{
          position: "fixed",
          pointerEvents: "none",
          zIndex: -1,
          left: "50%",
          top: -120,
          width: 420,
          height: 340,
          transform: "translateX(-50%)",
          borderRadius: "50%",
          animation: "flicker 7s ease-in-out infinite",
          background: "var(--lamp)",
        }}
      />
      <div
        aria-hidden
        style={{
          position: "fixed",
          pointerEvents: "none",
          zIndex: -1,
          left: 0,
          right: 0,
          bottom: 0,
          height: 380,
          background: "var(--floor)",
        }}
      />

      <div
        className="anim-lift relative"
        style={{ width: "100%", maxWidth: 360, padding: "0 34px" }}
      >
        {/* K mark */}
        <div
          style={{
            width: 44,
            height: 44,
            borderRadius: 13,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 19,
            fontWeight: 700,
            background: "var(--accent)",
            color: "var(--on-accent)",
            boxShadow: `0 0 30px ${alpha("var(--accent)", 35)}`,
          }}
        >
          K
        </div>
        <div
          style={{
            fontSize: 27,
            fontWeight: 600,
            letterSpacing: "-0.025em",
            marginTop: 22,
            color: "var(--text-primary)",
          }}
        >
          Karakos
        </div>
        <div style={{ fontSize: 14.5, opacity: 0.55, marginTop: 7, lineHeight: 1.45 }}>
          Sign in to talk to your agents.
        </div>

        <form
          onSubmit={handleSubmit}
          style={{
            marginTop: 30,
            display: "flex",
            flexDirection: "column",
            gap: 10,
          }}
        >
          <div>
            <label htmlFor="username" className="sr-only">
              Username
            </label>
            {/* name + autoComplete are what make iOS Keychain and Android
                autofill offer to save and re-fill this. Without them the
                managers see an unlabelled form and stay silent, which is why
                the login had to be typed by hand on a phone every time. */}
            <input
              id="username"
              name="username"
              type="text"
              autoComplete="username"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="Username"
              required
              className="press w-full"
              style={{
                ...fieldStyle,
                height: 52,
                minHeight: 52,
                borderRadius: 14,
                fontSize: INPUT_FONT,
                padding: "0 16px",
              }}
            />
          </div>
          <div>
            <label htmlFor="password" className="sr-only">
              Password
            </label>
            <input
              id="password"
              name="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Password"
              required
              className="press w-full"
              style={{
                ...fieldStyle,
                height: 52,
                minHeight: 52,
                borderRadius: 14,
                fontSize: INPUT_FONT,
                padding: "0 16px",
              }}
            />
          </div>
          {error && (
            <p style={{ color: ERR, fontSize: 13 }}>{error}</p>
          )}
          <button
            type="submit"
            disabled={loading}
            className="press w-full"
            style={{
              height: 52,
              borderRadius: 99,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 15.5,
              fontWeight: 500,
              background: "transparent",
              color: "var(--text-primary)",
              border: `1px solid ${alpha("var(--ink)", 22)}`,
              fontFamily: "inherit",
              touchAction: "manipulation",
              opacity: loading ? 0.6 : 0.9,
            }}
          >
            {loading ? "Signing in…" : "Sign in"}
          </button>
        </form>

      </div>
    </div>
  );
}
