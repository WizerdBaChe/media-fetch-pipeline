/**
 * Whether the window is narrow enough that a long hint would wrap.
 *
 * `matchMedia` rather than a resize listener: the browser already knows the
 * answer and only tells us when it changes, so there is no per-frame work and
 * no debounce to get wrong.
 *
 * Absence is treated as "not narrow". jsdom has no `matchMedia`, and the
 * useful default when we cannot tell is the layout with more information in
 * it — a test that never opted into the narrow case should see the same thing
 * a desktop window sees.
 */

import { useEffect, useState } from "react";

export const NARROW_QUERY = "(max-width: 900px)";

export function useNarrow(query: string = NARROW_QUERY): boolean {
  const [narrow, setNarrow] = useState(() => matches(query));

  useEffect(() => {
    const list = mediaList(query);
    if (!list) return;
    const onChange = (event: MediaQueryListEvent) => setNarrow(event.matches);
    // Re-read on subscribe: the width can have changed between the first
    // render and this effect, and a hint that is wrong until the next resize
    // is the bug this hook exists to avoid.
    setNarrow(list.matches);
    list.addEventListener("change", onChange);
    return () => list.removeEventListener("change", onChange);
  }, [query]);

  return narrow;
}

function mediaList(query: string): MediaQueryList | null {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return null;
  return window.matchMedia(query);
}

function matches(query: string): boolean {
  return mediaList(query)?.matches ?? false;
}
