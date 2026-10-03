"use client";

import { useEffect, useState } from "react";
import { hasPushApis, isIOS, isStandalone } from "@/lib/push-support";

type Status =
  | "checking"
  | "unsupported"
  | "ios-not-installed"
  | "off"
  | "on";

/** applicationServerKey wants a Uint8Array, VAPID public keys ship base64url. */
function urlBase64ToUint8Array(base64Url: string): Uint8Array<ArrayBuffer> {
  const padding = "=".repeat((4 - (base64Url.length % 4)) % 4);
  const base64 = (base64Url + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  // Explicit ArrayBuffer so TS 5.7+ types this as BufferSource-compatible.
  const bytes = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

interface PushSettingsProps {
  /**
   * "Send test notification" hits /api/push/test, which fires at every
   * subscribed device — there's no per-device targeting.
   * Fine for the full Settings page; hidden for confined accounts
   * so they can't repeatedly buzz every subscribed phone.
   */
  allowTest?: boolean;
}

export default function PushSettings({ allowTest = true }: PushSettingsProps) {
  const [status, setStatus] = useState<Status>("checking");
  const [busy, setBusy] = useState(false);
  const [testResult, setTestResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      if (!hasPushApis()) {
        setStatus("unsupported");
        return;
      }
      if (isIOS() && !isStandalone()) {
        setStatus("ios-not-installed");
        return;
      }
      try {
        const reg = await navigator.serviceWorker.ready;
        const sub = await reg.pushManager.getSubscription();
        setStatus(sub ? "on" : "off");
      } catch {
        setStatus("off");
      }
    })();
  }, []);

  const enable = async () => {
    setBusy(true);
    setError(null);
    try {
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        setError("Notification permission was not granted");
        setStatus("off");
        return;
      }
      const keyRes = await fetch("/api/push/vapid-public-key");
      const { publicKey } = await keyRes.json();
      if (!publicKey) {
        setError("Server has no VAPID public key configured");
        return;
      }
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(publicKey),
      });
      const json = sub.toJSON();
      const res = await fetch("/api/push/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ endpoint: json.endpoint, keys: json.keys }),
      });
      if (!res.ok) throw new Error(`subscribe failed: HTTP ${res.status}`);
      setStatus("on");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to enable notifications");
      setStatus("off");
    } finally {
      setBusy(false);
    }
  };

  const disable = async () => {
    setBusy(true);
    setError(null);
    try {
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.getSubscription();
      if (sub) {
        await fetch("/api/push/unsubscribe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ endpoint: sub.endpoint }),
        });
        await sub.unsubscribe();
      }
      setStatus("off");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to disable notifications");
    } finally {
      setBusy(false);
    }
  };

  const sendTest = async () => {
    setBusy(true);
    setTestResult(null);
    setError(null);
    try {
      const res = await fetch("/api/push/test", { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      setTestResult(
        `Sent ${data.sent}, pruned ${data.pruned}, failed ${data.failed}`
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "Test send failed");
    } finally {
      setBusy(false);
    }
  };

  if (status === "checking") {
    return (
      <p className="text-sm" style={{ color: "var(--text-muted)" }}>
        Checking notification support...
      </p>
    );
  }

  if (status === "unsupported") {
    return (
      <p className="text-sm" style={{ color: "var(--text-muted)" }}>
        This browser doesn&apos;t support push notifications.
      </p>
    );
  }

  if (status === "ios-not-installed") {
    return (
      <p className="text-sm" style={{ color: "var(--text-secondary)" }}>
        Add Karakos to your Home Screen first (Share → Add to Home Screen),
        then open it from there to enable notifications. iOS Safari doesn&apos;t
        allow push notifications in the browser tab.
      </p>
    );
  }

  return (
    <div className="py-2">
      <div className="flex items-center justify-between py-2">
        <div>
          <p className="text-sm font-medium" style={{ color: "var(--text-primary)" }}>
            Notifications on this device
          </p>
          <p className="text-xs mt-0.5" style={{ color: "var(--text-muted)" }}>
            {status === "on" ? "Enabled" : "Disabled"}
          </p>
        </div>
        <button
          onClick={status === "on" ? disable : enable}
          disabled={busy}
          className="text-xs px-3 py-1.5 rounded"
          style={{
            background: status === "on" ? "transparent" : "#3b82f6",
            border: status === "on" ? "1px solid var(--border-subtle)" : "1px solid #3b82f6",
            color: status === "on" ? "var(--text-secondary)" : "#ffffff",
            cursor: busy ? "default" : "pointer",
            opacity: busy ? 0.6 : 1,
            minHeight: 44,
          }}
        >
          {busy ? "..." : status === "on" ? "Turn off" : "Turn on"}
        </button>
      </div>

      {status === "on" && allowTest && (
        <div className="flex items-center gap-2 py-1">
          <button
            onClick={sendTest}
            disabled={busy}
            className="text-xs px-3 py-1.5 rounded"
            style={{
              background: "transparent",
              border: "1px solid var(--border-subtle)",
              color: "var(--text-secondary)",
              cursor: busy ? "default" : "pointer",
              opacity: busy ? 0.6 : 1,
              minHeight: 44,
            }}
          >
            Send test notification
          </button>
          {testResult && (
            <span className="text-xs" style={{ color: "var(--text-muted)" }}>
              {testResult}
            </span>
          )}
        </div>
      )}

      {error && (
        <p className="text-xs mt-1" style={{ color: "#ef4444" }}>
          {error}
        </p>
      )}
    </div>
  );
}
