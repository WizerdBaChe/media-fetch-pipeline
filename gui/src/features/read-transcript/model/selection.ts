/**
 * Turning "these lines" into "this window".
 *
 * This is the whole point of the transcript tool, and it is pure so that it
 * can be got right without a browser. The user's complaint was 「看著畫面腦
 * 子空白」 -- staring at a video with no idea which part to quote. Reading is
 * only half the answer; the other half is that what they picked has to become
 * `--from`/`--to` without them converting anything.
 *
 * Two rules decide everything below:
 *
 * 1. **A line's end is the next line's start.** The sources carry no end per
 *    line (see `TranscriptLine`), so the last selected line's end has to be
 *    borrowed from the line after it -- which is why these functions need the
 *    WHOLE list, not just the selection.
 *
 * 2. **The window is rounded outward, never inward.** `--from` is truncated
 *    down and `--to` rounded up, because a window one second short silently
 *    drops the line the user picked, and a window one second long costs a
 *    frame nobody minds.
 */

import type { TranscriptLine } from "@/shared/api/types";

/** What the last line of a transcript gets for an end, having no successor. */
export const TAIL_SECONDS = 3;

export interface Window {
  from: string;
  to: string;
  /** Seconds, for showing how long the quote will be. */
  span: number;
}

/**
 * `h:mm:ss` past an hour, `m:ss` under it.
 *
 * Deliberately the same shape `mfp transcript` prints and `--from` parses:
 * the string this produces is meant to survive being copied into a terminal.
 */
export function clock(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = h ? String(m).padStart(2, "0") : String(m);
  return h ? `${h}:${mm}:${String(s).padStart(2, "0")}` : `${mm}:${String(s).padStart(2, "0")}`;
}

/** Where the line at `index` stops: the next one's start, or a tail. */
export function endOf(lines: readonly TranscriptLine[], index: number): number {
  const next = lines[index + 1];
  return next ? next.at : (lines[index]?.at ?? 0) + TAIL_SECONDS;
}

/**
 * The window covering every selected line.
 *
 * There is no non-contiguous case to handle, and that is a decision rather
 * than an omission: one run of `stack` produces one image from one window, so
 * two separate passages could never both be honoured. `selectLine` can only
 * produce a run, so the UI cannot express a quote the engine cannot make.
 */
export function windowFor(
  lines: readonly TranscriptLine[],
  selected: ReadonlySet<number>,
): Window | null {
  const picked = [...selected].filter((i) => i >= 0 && i < lines.length).sort((a, b) => a - b);
  if (!picked.length) return null;

  const first = picked[0]!;
  const last = picked[picked.length - 1]!;
  const from = Math.floor(lines[first]!.at);
  const to = Math.ceil(endOf(lines, last));
  return { from: clock(from), to: clock(to), span: to - from };
}

/**
 * Click and shift-click, as every list of things has worked since 1984.
 *
 * A plain click replaces the selection with one line and moves the anchor;
 * shift-click selects the run between the anchor and here, which is the shape
 * a quote actually has. Returns the new selection AND the new anchor, because
 * a shift-click must not move the anchor -- otherwise extending twice walks
 * the range instead of growing it.
 */
export function selectLine(
  index: number,
  anchor: number | null,
  extend: boolean,
): { selected: Set<number>; anchor: number } {
  if (!extend || anchor === null) {
    return { selected: new Set([index]), anchor: index };
  }
  const [lo, hi] = anchor <= index ? [anchor, index] : [index, anchor];
  const selected = new Set<number>();
  for (let i = lo; i <= hi; i += 1) selected.add(i);
  return { selected, anchor };
}

/** The selected lines as text, for the clipboard. */
export function selectedText(
  lines: readonly TranscriptLine[],
  selected: ReadonlySet<number>,
): string {
  return [...selected]
    .sort((a, b) => a - b)
    .map((i) => lines[i]?.text ?? "")
    .filter(Boolean)
    .join("\n");
}
