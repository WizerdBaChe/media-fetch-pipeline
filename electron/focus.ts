/**
 * Bring the existing window to the front (UAT §5-13).
 *
 * `focus()` alone is not enough on Windows, and the second launch proved it:
 * the taskbar icon highlighted and the window stayed exactly where it was.
 * A process that is not already in the foreground may not take the
 * foreground -- Windows downgrades the request to a taskbar flash. That is
 * not an Electron bug and no amount of calling `focus()` harder fixes it.
 *
 `moveTop()` alone is not the way out either, and that was MEASURED rather
 * than assumed: with `moveTop` shipped, a second launch still failed to
 * reach the foreground within 25 s -- on the packaged shell AND on the
 * unpacked one, which also killed the theory that the portable target's
 * 5-second self-extraction was losing the foreground privilege. Its contract
 * says "regardless of focus", and that is true of the Z-ORDER; it does not
 * make the window active, and Windows restricts a background process from
 * placing a window above the foreground one in the normal band.
 *
 * The topmost band is not restricted the same way. Entering it, raising, and
 * leaving it is the way through: the window ends at the top of the normal
 * band, and does not stay pinned over everything else afterwards.
 *
 * `app.focus({ steal: true })` is NOT the way out: `steal` is declared
 * `@platform darwin` and does nothing here.
 *
 * Order matters, and each step is undoing a different way of not being
 * visible: a minimized window has no z-order position to move, and a hidden
 * one is not in the stack at all. `focus()` still comes last, because
 * z-order is where the window is drawn and focus is where the keyboard goes;
 * a window in front that does not take typing is only half raised.
 */

/** The slice of `BrowserWindow` this needs -- narrow so a test can fake it. */
export interface FocusableWindow {
  isMinimized(): boolean;
  isVisible(): boolean;
  restore(): void;
  show(): void;
  moveTop(): void;
  focus(): void;
  setAlwaysOnTop(flag: boolean): void;
}

export function bringToFront(window: FocusableWindow): void {
  if (window.isMinimized()) window.restore();
  if (!window.isVisible()) window.show();

  // Into the topmost band, raise, then back out. The flag is what gets past
  // the restriction; leaving it on would pin the window over every other
  // application, which is a different bug and a worse one.
  window.setAlwaysOnTop(true);
  window.moveTop();
  window.focus();
  window.setAlwaysOnTop(false);
}
