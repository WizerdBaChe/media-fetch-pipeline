/**
 * The one place the GUI knows anything about platforms.
 *
 * Audit finding C-1: this table was copy-pasted into `entities/task/ui/TaskRow`
 * and `features/add-urls/ui/ParsePreviewDialog`, which broke §12's claim that
 * "nothing else in the GUI is platform-aware" and turned EXT-1 from one edit
 * into several. Worse, the duplication is invisible to the boundary linter --
 * it is copy-paste, not an import, so nothing could have caught it.
 *
 * Adding a platform now touches: `inputs.py`'s table, the `Platform` union in
 * `shared/api/types.ts`, and this file. Nothing else in the GUI.
 */

import type { Platform } from "@/shared/api/types";

const LABELS: Record<Platform, string> = {
  instagram: "IG",
  threads: "TH",
  youtube: "YT",
  x: "X",
  bilibili: "BL",
  generic: "其他",
};

/**
 * Short badge text for the queue's platform column.
 *
 * Takes a plain string rather than `Platform`: the value arrives from the
 * server, so a backend that learns a new platform before the GUI does must
 * degrade to a dash rather than render `undefined`.
 */
export function platformLabel(platform: string): string {
  return LABELS[platform as Platform] ?? "—";
}
