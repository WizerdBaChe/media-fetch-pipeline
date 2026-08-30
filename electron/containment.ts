/**
 * Path containment for `openPath` / `showInFolder` (G6 §6.1).
 *
 * **This is the one real security rule in this milestone.** The renderer
 * hands us a path; `shell.openPath` on an arbitrary path *executes* it if it
 * is executable. So the path must be proven to sit inside the configured
 * `outputRoot` before it goes anywhere near the shell -- and proven HERE, in
 * the main process. A check in the renderer is not a check: the renderer is
 * the thing we are checking.
 *
 * The algorithm is `naming.py`'s `_ensure_within_root`, ported deliberately
 * rather than reinvented, including the case that was fixed in batch 1:
 *
 *   - resolve both sides (`realpath`), so `..` and symlinks cannot smuggle
 *     the target out of the root after the string comparison;
 *   - compare against a **separator-terminated** root, so `D:\out-of-scope`
 *     does not count as being inside `D:\out`;
 *   - build that prefix the way Python's `os.path.join(root, "")` does, NOT
 *     by concatenating a separator -- a bare drive root already ends in one,
 *     and `"D:\\" + "\\"` would reject every legitimate path under an output
 *     root set to a bare drive.
 *
 * Node's `path.join(root, "")` does NOT behave like Python's: it normalises
 * the trailing separator away. That difference is exactly the bug the batch 1
 * fix was about, so the terminator is appended conditionally below.
 */

import { realpathSync } from "node:fs";
import path from "node:path";

export type ContainmentVerdict =
  | { ok: true; resolved: string }
  | { ok: false; reason: "not_absolute" | "missing" | "outside_root" };

/**
 * Canonical form of `p`. `realpathSync.native` resolves symlinks, junctions
 * and 8.3 short names and returns the on-disk casing; when the path does not
 * exist it throws, and the caller decides what that means (for the target it
 * is a refusal; for the root it degrades to a lexical resolve, matching
 * Python's non-strict `os.path.realpath`).
 */
function canonical(p: string): string | null {
  try {
    return realpathSync.native(p);
  } catch {
    return null;
  }
}

/** Python's `os.path.join(root, "")`: terminate with a separator unless the
 *  string already ends in one (a bare drive root does). */
function withTrailingSeparator(root: string): string {
  return root.endsWith(path.sep) ? root : root + path.sep;
}

/**
 * Decide whether `candidate` may be handed to the shell.
 *
 * `candidate` must exist: `openPath` on something that is not there can only
 * fail anyway, and requiring existence is what lets `realpath` do its job
 * instead of a lexical comparison a symlink could walk around.
 */
export function containedInRoot(candidate: string, outputRoot: string): ContainmentVerdict {
  if (typeof candidate !== "string" || candidate.length === 0 || !path.isAbsolute(candidate)) {
    return { ok: false, reason: "not_absolute" };
  }

  const realCandidate = canonical(candidate);
  if (realCandidate === null) return { ok: false, reason: "missing" };

  // A root that does not exist yet still gives a usable lexical answer, which
  // is what Python's non-strict realpath would produce too.
  const realRoot = canonical(outputRoot) ?? path.resolve(outputRoot);

  // Windows filesystems are case-insensitive, so the comparison must be too;
  // `realpathSync.native` normalises casing on both sides, and lowercasing is
  // the belt for the lexical-fallback braces.
  const a = realCandidate.toLowerCase();
  const b = realRoot.toLowerCase();

  if (a === b) return { ok: true, resolved: realCandidate };
  if (a.startsWith(withTrailingSeparator(b))) return { ok: true, resolved: realCandidate };
  return { ok: false, reason: "outside_root" };
}
