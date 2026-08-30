/**
 * One step back, and exactly one.
 *
 * The complaint (2026-08-28): going from 逐字稿 to 引用長圖 left no way back.
 * The only exit was 回到佇列, which threw away a transcript that had cost
 * minutes of recognition -- so "I picked the wrong lines" meant choosing the
 * audio file again and re-reading it.
 *
 * A single slot rather than a stack, on the user's ruling: 「只要保留一個上一
 * 次動作快照，否則無限增生很麻煩」. That is also the honest shape. Every entry
 * in a deep stack would carry a whole workspace's state, most of them stale,
 * and a reader who pressed 上一步 four times would arrive somewhere nobody
 * could predict. One slot is a promise that can be kept: the screen you were
 * looking at before this one, whole.
 *
 * Generic over the view type on purpose -- this file may not know what a
 * 逐字稿 is, and `pages/main` may not have to re-derive what "back" means.
 */

export interface Step<V> {
  /** The view to return to, INCLUDING whatever its workspace was holding.
   *  A view without that is a re-entry, not a return. */
  view: V;
  /** What the button calls it: 「回到上一步（逐字稿）」. */
  label: string;
}

export interface History<V> {
  view: V;
  /** The one slot. `null` means there is nothing to go back to, and the
   *  control must not be on screen -- a disabled button that never enables
   *  teaches people it is broken. */
  previous: Step<V> | null;
}

export function start<V>(view: V): History<V> {
  return { view, previous: null };
}

/**
 * Move to `next`, remembering `leaving`.
 *
 * `leaving` is passed in rather than taken from `state.view` because the
 * caller is the only one who can capture what its workspace currently holds
 * -- the state that makes the return a return.
 */
export function go<V>(
  _state: History<V>,
  next: V,
  leaving: Step<V>,
): History<V> {
  // The old slot is DISCARDED rather than pushed. That is the single-slot
  // rule in one line, and the reason this takes the state it does not read:
  // it is the reducer for this transition, and a caller that had to know
  // the slot is dropped would be the second place the rule lives.
  return { view: next, previous: leaving };
}

/**
 * Return to the remembered view, and forget it.
 *
 * Forgetting is what makes the slot a slot. Keeping it would turn 上一步
 * into a toggle between two screens, which is a different promise than the
 * button makes, and swapping it for the view being left would grow the
 * stack this deliberately does not have.
 */
export function back<V>(state: History<V>): History<V> {
  return state.previous === null
    ? state
    : { view: state.previous.view, previous: null };
}

/**
 * Replace the current view without touching the slot.
 *
 * For a move that is not a step: re-rendering the same screen with a
 * different argument. Nothing uses it to navigate.
 */
export function replace<V>(state: History<V>, view: V): History<V> {
  return { ...state, view };
}
