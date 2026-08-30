/**
 * A question mark that opens a small panel of explanatory text.
 *
 * Not a tooltip: the content is a paragraph or a list the user reads at
 * their own pace, so it must survive the pointer leaving, be reachable from
 * the keyboard, and be dismissible without a mouse. A hover-only tooltip
 * would put the answer to "what can I paste here?" somewhere a keyboard user
 * cannot reach at all.
 *
 * It is NOT a modal. It does not trap focus or dim the page -- it is
 * supplementary, and blocking the app to read a help note would be a worse
 * trade than the one it saves.
 */

import { useEffect, useId, useRef, useState, type ReactNode } from "react";

interface InfoPopoverProps {
  /** Accessible name for the trigger, e.g. "支援哪些網站". */
  label: string;
  children: ReactNode;
  /** Test hook for the panel; the trigger derives its own from it. */
  testId?: string;
}

export function InfoPopover({ label, children, testId }: InfoPopoverProps) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const rootRef = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!open) return;

    // Both dismissals are required, and for different users: Escape is the
    // only one a keyboard user has, and an outside click is the only one a
    // mouse user will think to try.
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    function onPointerDown(event: MouseEvent) {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    }

    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("mousedown", onPointerDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("mousedown", onPointerDown);
    };
  }, [open]);

  return (
    <span className="mfp-info" ref={rootRef}>
      <button
        type="button"
        className="mfp-info__trigger"
        aria-label={label}
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        data-testid={testId ? `${testId}-trigger` : undefined}
        onClick={() => setOpen((value) => !value)}
      >
        ?
      </button>
      {open && (
        <div className="mfp-info__panel" id={panelId} role="dialog" aria-label={label} data-testid={testId}>
          {children}
        </div>
      )}
    </span>
  );
}
