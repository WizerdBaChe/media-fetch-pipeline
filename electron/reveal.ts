/**
 * Which file a reveal should point at (UAT supplement 1).
 *
 * Its own module for the same reason `containment.ts` is: `main.ts`
 * claims the single-instance lock and wires Electron at import time, so
 * nothing in it can be reached from a test.
 */

import { readdirSync, statSync } from "node:fs";
import path from "node:path";

/**
 * The file a reveal should land on, or null.
 *
 * `shell.showItemInFolder` on a DIRECTORY opens that directory's parent and
 * selects the directory -- which is one level further from the files than
 * the user was already standing. A post downloads into its own folder, so
 * the useful reveal is "open that folder with something in it selected",
 * and that needs a file name the caller does not have.
 *
 * `.part` is skipped: a half-finished transfer is not the output. Sorted,
 * so the same folder reveals the same file every time rather than whatever
 * order the filesystem happens to answer in.
 */
export function firstFileIn(directory: string): string | null {
  let entries;
  try {
    entries = readdirSync(directory, { withFileTypes: true });
  } catch {
    // Not a directory, or unreadable. Both mean "no file to point at" --
    // the caller falls back to revealing the target itself.
    return null;
  }
  const names = entries
    .filter((entry) => entry.isFile() && !entry.name.endsWith(".part"))
    .map((entry) => entry.name)
    .sort();
  const [first] = names;
  return first ? path.join(directory, first) : null;
}

/**
 * `select` means `shell.showItemInFolder` -- open the containing folder with
 * this item highlighted. `open` means `shell.openPath` -- hand the thing
 * itself to whatever the OS thinks owns it.
 */
export type RevealPlan =
  | { action: "select"; target: string }
  | { action: "open"; target: string };

/**
 * What "開啟位置" should actually do for one resolved path.
 *
 * The two cases were conflated and one of them was wrong (UAT 2026-08-28).
 * `firstFileIn` returns null both for "an empty folder" and for "the target
 * is a FILE", and the caller treated both as "nothing to point at, open the
 * target". Opening a folder is what the reader asked for; opening a `.srt`
 * launches Notepad, which is a different verb than the button's name.
 *
 * So the file case is decided here instead:
 *
 *   - a FILE      -> select it. This is exactly what `showItemInFolder` is
 *                    for, and the "one level further out" objection below
 *                    does not apply -- the parent of a file IS its folder.
 *   - a DIRECTORY -> select a file inside it, because
 *                    `showItemInFolder` on a directory opens the
 *                    directory's PARENT, which is one level further from
 *                    the files than the reader was already standing.
 *   - an EMPTY directory, or anything unreadable -> open it, which is the
 *                    only remaining way to land the reader in the right
 *                    place.
 */
export function planReveal(resolved: string): RevealPlan {
  let stats;
  try {
    stats = statSync(resolved);
  } catch {
    // Gone between the containment check and here, or unreadable. Let the
    // shell produce the error rather than inventing one.
    return { action: "open", target: resolved };
  }
  if (stats.isFile()) return { action: "select", target: resolved };
  const inside = firstFileIn(resolved);
  return inside
    ? { action: "select", target: inside }
    : { action: "open", target: resolved };
}

