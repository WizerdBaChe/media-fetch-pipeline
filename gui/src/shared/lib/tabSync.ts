/**
 * Cross-tab echo for viewer-side acknowledgements (UAT §5-5).
 *
 * Closing an error banner in one tab left it open in the other, because
 * dismissal was pure local state. It is not server state and should not
 * become server state: "I have read this" is a fact about a person, not
 * about the queue, and a round trip to record it would be a round trip to
 * record something the server has no use for.
 *
 * `BroadcastChannel` is the right size for that — same origin, same browser,
 * no server involved. **Its boundary is honest and worth stating: the
 * Electron window and a Chrome tab are different browsers, so an
 * acknowledgement does not travel between them.** Making that case work
 * would mean the server holding notice state, which is the design this
 * comment just argued against.
 */

const CHANNEL_NAME = "mfp:tab-sync";

export type TabMessage = { type: "notice-dismissed"; key: string };

/** Absent in a non-secure context and in some test environments. Feature
 *  detection, not a try/catch, so the absence is visible at the call site. */
function channel(): BroadcastChannel | null {
  if (typeof BroadcastChannel === "undefined") return null;
  return new BroadcastChannel(CHANNEL_NAME);
}

export function publish(message: TabMessage): void {
  const bus = channel();
  if (!bus) return;
  bus.postMessage(message);
  // Opened per call rather than held open: this fires on a click, not in a
  // loop, and a long-lived channel in a module scope is one more thing that
  // has to be torn down in every test that touches it.
  bus.close();
}

/** Returns the unsubscribe. A no-op when the browser has no channel. */
export function subscribe(handler: (message: TabMessage) => void): () => void {
  const bus = channel();
  if (!bus) return () => undefined;
  const listener = (event: MessageEvent) => handler(event.data as TabMessage);
  bus.addEventListener("message", listener);
  return () => {
    bus.removeEventListener("message", listener);
    bus.close();
  };
}
