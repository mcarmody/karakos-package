/**
 * Web push sending — VAPID-signed notifications to every subscribed device.
 *
 * VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY are generated once with
 * `npx web-push generate-vapid-keys` and live in .env.local (gitignored).
 * See README for the full env var list. Same missing-secret warning
 * pattern as SESSION_SECRET in lib/api.ts — fail loud at import time, not
 * silently on first send.
 */

import webpush from "web-push";
import {
  listSubscriptions,
  removeSubscription,
  type PushSubscriptionRow,
} from "@/lib/push-subscriptions";

const VAPID_PUBLIC_KEY = process.env.VAPID_PUBLIC_KEY || "";
const VAPID_PRIVATE_KEY = process.env.VAPID_PRIVATE_KEY || "";
const VAPID_SUBJECT = process.env.VAPID_SUBJECT || "mailto:admin@example.invalid";

if (!VAPID_PUBLIC_KEY || !VAPID_PRIVATE_KEY) {
  console.warn(
    "WARNING: VAPID_PUBLIC_KEY/VAPID_PRIVATE_KEY not set. Web push will not work. " +
    "Generate a keypair with `npx web-push generate-vapid-keys` and add both to .env.local."
  );
} else {
  webpush.setVapidDetails(VAPID_SUBJECT, VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY);
}

export interface PushSendResult {
  sent: number;
  pruned: number;
  failed: number;
}

/**
 * A push subscription is gone for good when the push service returns 404 or
 * 410 — the browser dropped it (uninstalled, cleared data, key rotation).
 * Any other error (network blip, 5xx from the push service) is transient;
 * the row stays and the next send retries it.
 */
function isGone(err: unknown): boolean {
  const status = (err as { statusCode?: number } | undefined)?.statusCode;
  return status === 404 || status === 410;
}

/**
 * Send a notification to every subscribed device. Individual failures never
 * abort the batch — one dead endpoint shouldn't stop the rest of the
 * subscribers from getting notified.
 */
export async function sendPushToAll(
  title: string,
  body: string,
  url?: string
): Promise<PushSendResult> {
  const subscriptions = listSubscriptions();
  const payload = JSON.stringify({ title, body, url: url || "/" });

  const result: PushSendResult = { sent: 0, pruned: 0, failed: 0 };

  await Promise.all(
    subscriptions.map(async (sub: PushSubscriptionRow) => {
      try {
        await webpush.sendNotification(
          {
            endpoint: sub.endpoint,
            keys: { p256dh: sub.p256dh, auth: sub.auth },
          },
          payload
        );
        result.sent += 1;
      } catch (err) {
        if (isGone(err)) {
          removeSubscription(sub.endpoint);
          result.pruned += 1;
        } else {
          result.failed += 1;
          console.error(`push send failed for ${sub.label || sub.endpoint}:`, err);
        }
      }
    })
  );

  return result;
}
