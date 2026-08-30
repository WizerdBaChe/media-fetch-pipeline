/**
 * The reading copy: a transcript with its filler cues left out.
 *
 * Same state machine as `correct-transcript`, and for a sharper reason. That
 * one substitutes and can be wrong about a word; this one DELETES whole
 * cues, so the state where the user is looking at the list and nothing has
 * happened yet is not a preview -- it is the decision.
 *
 *     idle ──ask──▶ offered ──confirm──▶ written
 *       ▲              │
 *       └────discard───┘
 *
 * Everything starts TICKED here, unlike the corrector's phonetic tier. The
 * difference is where the candidates come from: a filler removal is only
 * ever offered for a cue that is nothing but terms the user themselves put
 * on the list, so an offer is the user's own rule firing, not this module
 * guessing. What they are checking is whether the rule was right about that
 * cue -- and 41 of 41 were, on the transcript this was measured against.
 */

import { create } from "zustand";
import { ApiError, api } from "@/shared/api/client";
import type { FillerReport, TidyOffer, TidyWritten } from "@/shared/api/types";

export type TidyPhase = "idle" | "offered" | "written";

export interface TidyState {
  phase: TidyPhase;
  busy: boolean;
  error: string | null;
  source: string | null;
  offer: TidyOffer | null;
  /** Indices into `offer.removals` the reader still wants gone. */
  accepted: Set<number>;
  written: TidyWritten | null;
  fillers: FillerReport | null;

  loadFillers: () => Promise<void>;
  addCommon: () => Promise<void>;
  addFiller: (term: string) => Promise<void>;
  removeFiller: (term: string) => Promise<void>;
  ask: (source: string) => Promise<void>;
  toggle: (index: number) => void;
  setAll: (on: boolean) => void;
  confirm: () => Promise<TidyWritten | null>;
  discard: () => void;
}

function reason(cause: unknown): string {
  if (cause instanceof ApiError) return cause.message;
  if (cause instanceof Error) return cause.message;
  return String(cause);
}

export const useTidy = create<TidyState>((set, get) => ({
  phase: "idle",
  busy: false,
  error: null,
  source: null,
  offer: null,
  accepted: new Set<number>(),
  written: null,
  fillers: null,

  loadFillers: async () => {
    try {
      set({ fillers: await api.fillers() });
    } catch (cause) {
      set({ error: reason(cause) });
    }
  },

  addCommon: async () => {
    set({ busy: true, error: null });
    try {
      const fillers = await api.addFillers([], true);
      set({ fillers, busy: false });
      // The list decides what can be offered, so an offer on screen is stale
      // the moment it changes. Leaving it there is how a reader concludes
      // that adding terms did nothing.
      if (get().phase === "offered" && get().source) await get().ask(get().source!);
    } catch (cause) {
      set({ busy: false, error: reason(cause) });
    }
  },

  addFiller: async (term: string) => {
    const cleaned = term.trim();
    if (!cleaned) return;
    set({ busy: true, error: null });
    try {
      const fillers = await api.addFillers([cleaned]);
      set({ fillers, busy: false });
      if (get().phase === "offered" && get().source) await get().ask(get().source!);
    } catch (cause) {
      set({ busy: false, error: reason(cause) });
    }
  },

  removeFiller: async (term: string) => {
    set({ busy: true, error: null });
    try {
      const fillers = await api.removeFillers([term]);
      set({ fillers, busy: false });
      if (get().phase === "offered" && get().source) await get().ask(get().source!);
    } catch (cause) {
      set({ busy: false, error: reason(cause) });
    }
  },

  ask: async (source: string) => {
    set({ busy: true, error: null, source });
    try {
      const offer = await api.proposeTidy(source);
      set({
        offer,
        accepted: new Set(offer.removals.map((_, index) => index)),
        phase: "offered",
        written: null,
        busy: false,
      });
    } catch (cause) {
      set({ busy: false, error: reason(cause), phase: "idle" });
    }
  },

  toggle: (index: number) => {
    const next = new Set(get().accepted);
    if (next.has(index)) next.delete(index);
    else next.add(index);
    set({ accepted: next });
  },

  setAll: (on: boolean) => {
    const offer = get().offer;
    set({
      accepted: on && offer
        ? new Set(offer.removals.map((_, index) => index))
        : new Set<number>(),
    });
  },

  confirm: async () => {
    const { source, offer, accepted } = get();
    if (!source || !offer) return null;
    set({ busy: true, error: null });
    try {
      const written = await api.applyTidy(source, [...accepted].sort((a, b) => a - b));
      set({ written, phase: "written", busy: false });
      return written;
    } catch (cause) {
      set({ busy: false, error: reason(cause) });
      return null;
    }
  },

  discard: () =>
    set({ phase: "idle", offer: null, accepted: new Set<number>(), written: null,
          error: null }),
}));

/**
 * The one-line summary above the list, or null when there is nothing to say.
 *
 * Pure. The empty-list case is its own sentence rather than a count of zero:
 * "nothing to remove" and "you have not told me what to remove" are
 * different states, and rendering them the same makes an unconfigured
 * feature look like a broken one.
 */
export function describeTidyOffer(offer: TidyOffer | null): string | null {
  if (!offer) return null;
  if (offer.fillerTerms === 0) {
    return "語助詞清單是空的，所以不會刪掉任何東西。先加幾個常見的。";
  }
  if (offer.removals.length === 0) {
    return "這份逐字稿裡沒有整句都是語助詞的句子。";
  }
  const { removed, cues, kept } = offer.summary;
  return `${cues} 句裡有 ${removed} 句整句都是語助詞，整理後剩 ${kept} 句。`;
}

/** How one offered removal reads in the list. 1-based, as every other
 *  surface in this product numbers cues. */
export function describeRemoval(removal: { cue: number; text: string }): string {
  return `第 ${removal.cue + 1} 句：${removal.text}`;
}
