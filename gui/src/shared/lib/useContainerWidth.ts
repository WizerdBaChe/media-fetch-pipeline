/**
 * How wide the box actually is, rather than how wide the window is.
 *
 * The queue table chose its layout from a media query, which answers about the
 * VIEWPORT, and then drew itself inside `.mfp-main`'s content box. Those two
 * differ by a scrollbar the moment the queue is long enough to need one, so
 * the decision and the drawing were being made with two different rulers --
 * and right above the breakpoint that meant the full column set was chosen for
 * a space it did not fit in, taking the difference out of 來源, which is the
 * absorber and the one column with no width to defend itself.
 *
 * Measuring the container removes the class rather than allowing for it: the
 * width that decides IS the width that gets used. It is also the only version
 * a test can check, since a browser with overlay scrollbars (Playwright's,
 * measured 2026-08-30: viewport 1258, content 1258) cannot reproduce the
 * mismatch at all.
 *
 * A CALLBACK ref, not a `useRef` object. The node it measures appears LATE --
 * the queue renders 「載入中…」 first and the table only after the fetch lands --
 * and an effect keyed on a ref object runs once, against `null`, and never
 * again. That version measured nothing and reported the wide layout forever.
 *
 * `null` until the first observation, so a caller can tell "not measured yet"
 * from "measured and narrow" rather than rendering one layout and swapping it.
 */

import { useCallback, useLayoutEffect, useState } from "react";

export function useContainerWidth(): [(node: HTMLElement | null) => void, number | null] {
  const [node, setNode] = useState<HTMLElement | null>(null);
  const [width, setWidth] = useState<number | null>(null);
  const ref = useCallback((next: HTMLElement | null) => setNode(next), []);

  // LAYOUT effect, so the first measurement lands BEFORE the browser paints.
  // With a plain effect the first frame is drawn from `null` -- the wide
  // layout -- and a narrow window would show ten columns for one frame and
  // then six, which is the flicker the synchronous read below exists to avoid.
  useLayoutEffect(() => {
    if (!node) return;
    // jsdom has no ResizeObserver, and the useful answer where we cannot
    // measure is "unknown" -- the caller falls back to the layout with more
    // information in it, which is what a desktop window shows.
    if (typeof ResizeObserver === "undefined") return;

    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry) setWidth(entry.contentRect.width);
    });
    observer.observe(node);
    // Read once immediately: the observer fires asynchronously, and a table
    // that renders one layout and swaps it a frame later is a visible flicker.
    setWidth(node.getBoundingClientRect().width);
    return () => observer.disconnect();
  }, [node]);

  return [ref, width];
}
