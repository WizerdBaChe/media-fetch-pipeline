/**
 * The whitelist, as one piece of state for the whole window.
 *
 * An entity rather than part of `features/correct-transcript`, and the
 * reason is the bug that created this file: the glossary is now edited in
 * two places -- 設定 has the full editor, and 逐字稿 opens the same editor in
 * a dialog -- and a copy of the list per surface is a copy that goes stale.
 * Adding a term in 設定 while the correction panel held its own array meant
 * the panel kept saying 「詞庫是空的」 until it was remounted.
 *
 * Every mutation stores the WHOLE list the server returned rather than
 * patching a local copy, for the same reason `setup-asr` stores readiness
 * that way: the server is what decides, and a client that merges deltas is a
 * second place the list can be wrong about itself.
 */

import { create } from "zustand";
import { ApiError, api } from "@/shared/api/client";
import type { GlossaryEntry, GlossaryReport } from "@/shared/api/types";

export interface GlossaryDraft {
  /** The term as it was BEFORE this edit. Empty means a new entry. */
  original?: string;
  term: string;
  aliases: string[];
  note?: string;
}

export interface GlossaryState {
  report: GlossaryReport | null;
  busy: boolean;
  /** Already user-facing. `null` whenever the last call succeeded. */
  error: string | null;

  load: () => Promise<void>;
  save: (draft: GlossaryDraft) => Promise<boolean>;
  remove: (term: string) => Promise<boolean>;
  /** The one-alias shorthand, used by the correction panel's 「記起來」. */
  enrol: (term: string, alias?: string) => Promise<boolean>;
  clearError: () => void;
}

function reason(cause: unknown): string {
  if (cause instanceof ApiError) return cause.message;
  if (cause instanceof Error) return cause.message;
  return String(cause);
}

export const useGlossary = create<GlossaryState>((set) => {
  /** One call, owning `busy` and `error`, storing whatever came back.
   *  Written once because every mutation below has exactly this shape and
   *  the one thing that must never vary is that `busy` is cleared. */
  const run = async (work: () => Promise<GlossaryReport>): Promise<boolean> => {
    set({ busy: true, error: null });
    try {
      set({ report: await work() });
      return true;
    } catch (cause) {
      set({ error: reason(cause) });
      return false;
    } finally {
      set({ busy: false });
    }
  };

  return {
    report: null,
    busy: false,
    error: null,

    load: async () => {
      await run(() => api.glossary());
    },
    save: (draft) =>
      run(() =>
        api.saveTerm({
          original: draft.original ?? "",
          term: draft.term.trim(),
          aliases: draft.aliases.map((alias) => alias.trim()).filter(Boolean),
          note: draft.note ?? "",
        }),
      ),
    remove: (term) => run(() => api.removeTerm(term)),
    enrol: (term, alias = "") => run(() => api.enrolTerm(term.trim(), alias.trim())),
    clearError: () => set({ error: null }),
  };
});

/** The entries, or an empty list while nothing has been loaded. A caller
 *  that has to distinguish 「還沒問」 from 「空的」 reads `report` itself --
 *  and the correction panel does, because those two say different things to
 *  a reader looking at a transcript with no suggestions. */
export function entriesOf(report: GlossaryReport | null): GlossaryEntry[] {
  return report?.entries ?? [];
}

/** How one entry reads in a single line. Pure, so the sentence the panel
 *  shows and the sentence a test asserts are the same one. */
export function describeEntry(entry: GlossaryEntry): string {
  if (!entry.aliases.length) return "還沒有記錄過錯字，只會比對讀音";
  return `也見過：${entry.aliases.join("、")}`;
}

/** Wrong forms are typed as one line. Splitting on both commas keeps a
 *  reader who types the full-width one from creating a single alias with a
 *  comma inside it, which would never match anything. */
export function parseAliases(raw: string): string[] {
  return raw
    .split(/[,，、]/)
    .map((part) => part.trim())
    .filter(Boolean);
}

export function joinAliases(aliases: string[]): string {
  return aliases.join("、");
}
