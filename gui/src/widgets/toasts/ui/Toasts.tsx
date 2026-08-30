/**
 * Transient confirmations, out of the way.
 *
 * A notice used to be a notice: everything the server said landed in the same
 * full-width bar under the header, in the same shape as "the local service is
 * unreachable". So 「已移除 3 筆紀錄」 — news about something the user had just
 * done deliberately — arrived looking like a fault. The banner is the right
 * shape for a condition that persists and may need acting on; it is the wrong
 * shape for a receipt.
 *
 * The split is by TONE, decided in `presentNotice`, not by code: `info` is a
 * receipt and gets a toast, anything else keeps the bar. Keying it on level
 * rather than on a list of codes means a notice added later cannot end up as
 * a toast that fades before an error is read.
 *
 * `aria-live="polite"`, not `alert`: this is the accessible equivalent of the
 * same judgement — a receipt waits for a gap in the conversation, a fault
 * interrupts.
 */

import { useEffect } from "react";
import { useSessionStore, noticeKey } from "@/entities/session/model/store";
import { presentNotice } from "@/shared/lib/notices";
import { Button } from "@/shared/ui/Button";

/** Long enough to read a short sentence, short enough not to pile up. */
export const TOAST_MS = 4500;

function Toast({ text, onDone }: { text: string; onDone: () => void }) {
  useEffect(() => {
    const timer = window.setTimeout(onDone, TOAST_MS);
    return () => window.clearTimeout(timer);
  }, [onDone]);

  return (
    <div className="mfp-toast">
      <span className="mfp-toast__text">{text}</span>
      <Button variant="ghost" onClick={onDone} aria-label="關閉提示">
        ✕
      </Button>
    </div>
  );
}

export function Toasts() {
  const notices = useSessionStore((state) => state.notices);
  const dismissNotice = useSessionStore((state) => state.dismissNotice);

  const toasts = notices
    .map((notice) => ({ notice, presented: presentNotice(notice) }))
    .filter((entry) => entry.presented.tone === "toast");

  if (toasts.length === 0) return null;

  return (
    <div className="mfp-toasts" role="status" aria-live="polite">
      {toasts.map(({ notice, presented }) => {
        const key = noticeKey(notice);
        return (
          <Toast
            key={key}
            text={presented.text}
            // Expiry goes through the same dismissal as the ✕, so it reaches
            // the other tab too. One person, one receipt, however many
            // windows are open on it.
            onDone={() => dismissNotice(key)}
          />
        );
      })}
    </div>
  );
}
