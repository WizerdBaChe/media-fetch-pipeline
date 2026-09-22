/**
 * The three things a dialog owes a keyboard.
 *
 * UX walkthrough F10: 「確認要加入的項目」 and the shared confirm box opened
 * with `document.activeElement` still on the textarea behind them, ignored
 * Escape, and left focus wherever it happened to be when they closed. So a
 * person who had just pasted links and pressed Enter got a dialog they had
 * to Tab into, and Enter again re-submitted the box underneath.
 *
 * Both behaviours already existed one layer up -- `FeatureGuide` closes on
 * Escape, `SettingsOverlay` closes on Escape and hands focus back -- which is
 * why this is a hook rather than a fourth copy. `SettingsOverlay` does NOT
 * use it, and that is deliberate: it makes the surface behind `inert`, so by
 * the time its effect runs the browser has already blurred the trigger and
 * there is nothing left to remember; its page owns the restore instead.
 *
 * What this does not do is trap Tab inside the dialog. A modal that cannot
 * be tabbed out of needs every focusable element inside it to be reachable
 * in a cycle, and getting that half-right is worse than not doing it: the
 * two dialogs here have two buttons and a scrolling list, and Escape plus a
 * focused button answers what the finding was about. Stated rather than
 * silently omitted, because the next reader will ask.
 */

import { useEffect, useRef } from "react";

export interface DialogHandles<S extends HTMLElement, F extends HTMLElement> {
  /** The dialog surface. Focused when nothing more specific is named. */
  surface: React.RefObject<S | null>;
  /** The control focus should land on: the safe one, never the destructive
   *  one. Both dialogs here render 取消 first, so it is also the first. */
  initial: React.RefObject<F | null>;
}

export function useDialog<
  S extends HTMLElement = HTMLDivElement,
  F extends HTMLElement = HTMLButtonElement,
>(onCancel: () => void): DialogHandles<S, F> {
  const surface = useRef<S | null>(null);
  const initial = useRef<F | null>(null);
  // A ref, so a re-render with a new closure does not re-run the effect and
  // move focus a second time while someone is reading.
  const cancel = useRef(onCancel);
  cancel.current = onCancel;

  useEffect(() => {
    const trigger = document.activeElement as HTMLElement | null;
    (initial.current ?? surface.current)?.focus();

    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      // Stopped here so one Escape closes one thing: this dialog can be open
      // over a workspace that also listens for it.
      event.stopPropagation();
      cancel.current();
    };
    document.addEventListener("keydown", onKey);

    return () => {
      document.removeEventListener("keydown", onKey);
      // Back to whatever opened it -- and only if that control is still on
      // the page, since confirming can be what removes it.
      if (trigger && document.contains(trigger)) trigger.focus();
    };
  }, []);

  return { surface, initial };
}
