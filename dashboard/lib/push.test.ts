import { beforeEach, describe, expect, it, vi } from "vitest";

const sendNotification = vi.fn();
const setVapidDetails = vi.fn();

vi.mock("web-push", () => ({
  default: {
    setVapidDetails: (...args: unknown[]) => setVapidDetails(...args),
    sendNotification: (...args: unknown[]) => sendNotification(...args),
  },
}));

const listSubscriptions = vi.fn();
const removeSubscription = vi.fn();

vi.mock("@/lib/push-subscriptions", () => ({
  listSubscriptions: (...args: unknown[]) => listSubscriptions(...args),
  removeSubscription: (...args: unknown[]) => removeSubscription(...args),
}));

beforeEach(() => {
  vi.clearAllMocks();
  process.env.VAPID_PUBLIC_KEY = "test-public-key";
  process.env.VAPID_PRIVATE_KEY = "test-private-key";
});

const sub = (endpoint: string) => ({
  id: 1,
  endpoint,
  p256dh: "p256dh",
  auth: "auth",
  label: "test device",
  created_at: "2026-01-01",
});

describe("sendPushToAll", () => {
  it("sends to every subscription and reports counts", async () => {
    listSubscriptions.mockReturnValue([sub("https://a"), sub("https://b")]);
    sendNotification.mockResolvedValue(undefined);

    const { sendPushToAll } = await import("./push");
    const result = await sendPushToAll("Title", "Body", "/somewhere");

    expect(sendNotification).toHaveBeenCalledTimes(2);
    expect(result).toEqual({ sent: 2, pruned: 0, failed: 0 });
    expect(removeSubscription).not.toHaveBeenCalled();
  });

  it("prunes subscriptions that 410 Gone", async () => {
    listSubscriptions.mockReturnValue([sub("https://dead"), sub("https://alive")]);
    sendNotification.mockImplementation((target: { endpoint: string }) => {
      if (target.endpoint === "https://dead") {
        const err = Object.assign(new Error("gone"), { statusCode: 410 });
        return Promise.reject(err);
      }
      return Promise.resolve();
    });

    const { sendPushToAll } = await import("./push");
    const result = await sendPushToAll("Title", "Body");

    expect(result).toEqual({ sent: 1, pruned: 1, failed: 0 });
    expect(removeSubscription).toHaveBeenCalledWith("https://dead");
    expect(removeSubscription).toHaveBeenCalledTimes(1);
  });

  it("prunes on 404 the same as 410", async () => {
    listSubscriptions.mockReturnValue([sub("https://missing")]);
    sendNotification.mockRejectedValue(
      Object.assign(new Error("not found"), { statusCode: 404 })
    );

    const { sendPushToAll } = await import("./push");
    const result = await sendPushToAll("Title", "Body");

    expect(result).toEqual({ sent: 0, pruned: 1, failed: 0 });
  });

  it("counts transient errors as failed without pruning", async () => {
    listSubscriptions.mockReturnValue([sub("https://flaky")]);
    sendNotification.mockRejectedValue(
      Object.assign(new Error("server error"), { statusCode: 500 })
    );

    const { sendPushToAll } = await import("./push");
    const result = await sendPushToAll("Title", "Body");

    expect(result).toEqual({ sent: 0, pruned: 0, failed: 1 });
    expect(removeSubscription).not.toHaveBeenCalled();
  });

  it("one dead subscription doesn't stop the rest of the batch", async () => {
    listSubscriptions.mockReturnValue([
      sub("https://one"),
      sub("https://dead"),
      sub("https://three"),
    ]);
    sendNotification.mockImplementation((target: { endpoint: string }) =>
      target.endpoint === "https://dead"
        ? Promise.reject(Object.assign(new Error("gone"), { statusCode: 410 }))
        : Promise.resolve()
    );

    const { sendPushToAll } = await import("./push");
    const result = await sendPushToAll("Title", "Body");

    expect(result).toEqual({ sent: 2, pruned: 1, failed: 0 });
  });
});
