/**
 * Term correction over a transcript that already exists.
 *
 * The state machine is the feature. The user's ruling (2026-08-28) was that
 * the original comes first, a button ASKS whether to correct, the change is
 * shown as a diff, the reader may then fix things by hand and enrol what they
 * fixed, and only a confirmation writes anything:
 *
 *     idle ──ask──▶ offered ──confirm──▶ written
 *       ▲              │  ▲                  │
 *       └────discard───┘  └──enrol (re-ask)──┘
 *
 * `offered` is not a preview of a change that is going to happen. It is the
 * whole decision, and `idle` is a perfectly good place to end up: D-115 built
 * the version with no `offered` state at all, and every firing on real data
 * silently damaged correct text.
 *
 * Its own slice rather than a mode of `read-transcript`, on the same reasoning
 * that separated translation: correcting is a third thing a person asks for
 * once a transcript is in front of them, and nothing may correct as a side
 * effect of transcribing.
 */

import { create } from "zustand";
import { ApiError, api } from "@/shared/api/client";
import { useGlossary } from "@/entities/glossary/model/store";
import type {
  CorrectionOffer,
  CorrectionProposal,
  CorrectionWritten,
} from "@/shared/api/types";

export type CorrectPhase = "idle" | "offered" | "written";

export interface CorrectState {
  phase: CorrectPhase;
  busy: boolean;
  error: string | null;
  source: string | null;
  offer: CorrectionOffer | null;
  /** Indices into `offer.proposals` the reader has kept. Everything starts
   *  accepted for tier `exact` and REJECTED for tier `phonetic`: one was
   *  declared by the user, the other is this module's guess, and a guess
   *  should not arrive pre-ticked. */
  accepted: Set<number>;
  written: CorrectionWritten | null;

  ask: (source: string) => Promise<void>;
  toggle: (index: number) => void;
  setAll: (on: boolean) => void;
  confirm: () => Promise<CorrectionWritten | null>;
  enrol: (term: string, alias?: string) => Promise<void>;
  /** Recompute the offer against the glossary as it stands now.
   *
   *  Every path that changes the glossary ends here, and that is the whole
   *  point of changing it: leaving the old list on screen after editing a
   *  term is how a reader concludes the edit did nothing. */
  reask: () => Promise<void>;
  discard: () => void;
}

function reason(cause: unknown): string {
  if (cause instanceof ApiError) return cause.message;
  if (cause instanceof Error) return cause.message;
  return String(cause);
}

/** Which proposals start ticked. Exact hits are the user's own past
 *  corrections coming back; phonetic hits are inference and start off. */
export function defaultAccepted(proposals: CorrectionProposal[]): Set<number> {
  const chosen = new Set<number>();
  proposals.forEach((proposal, index) => {
    if (proposal.tier === "exact") chosen.add(index);
  });
  return chosen;
}

export const useCorrect = create<CorrectState>((set, get) => ({
  phase: "idle",
  busy: false,
  error: null,
  source: null,
  offer: null,
  accepted: new Set<number>(),
  written: null,

  ask: async (source) => {
    set({ busy: true, error: null, written: null, source });
    try {
      const offer = await api.proposeCorrections(source);
      set({
        offer,
        accepted: defaultAccepted(offer.proposals),
        phase: "offered",
      });
    } catch (cause) {
      set({ error: reason(cause), phase: "idle", offer: null });
    } finally {
      set({ busy: false });
    }
  },

  toggle: (index) => {
    const next = new Set(get().accepted);
    if (next.has(index)) next.delete(index);
    else next.add(index);
    set({ accepted: next });
  },

  setAll: (on) => {
    const offer = get().offer;
    if (!offer) return;
    set({
      accepted: on
        ? new Set(offer.proposals.map((_, index) => index))
        : new Set<number>(),
    });
  },

  confirm: async () => {
    const { source, accepted } = get();
    if (!source) return null;
    set({ busy: true, error: null });
    try {
      const written = await api.applyCorrections(
        source,
        [...accepted].sort((a, b) => a - b),
      );
      set({ written, phase: "written" });
      return written;
    } catch (cause) {
      set({ error: reason(cause) });
      return null;
    } finally {
      set({ busy: false });
    }
  },

  enrol: async (term, alias = "") => {
    if (!term.trim()) return;
    set({ busy: true, error: null });
    try {
      // Through the glossary entity, which owns the list. This used to keep
      // its own copy, and the copy went stale the moment the same list
      // became editable in 設定 as well.
      const ok = await useGlossary.getState().enrol(term, alias);
      if (!ok) {
        set({ error: useGlossary.getState().error });
        return;
      }
      await get().reask();
    } finally {
      set({ busy: false });
    }
  },

  reask: async () => {
    const source = get().source;
    if (!source || get().phase === "idle") return;
    set({ busy: true, error: null });
    try {
      const offer = await api.proposeCorrections(source);
      set({ offer, accepted: defaultAccepted(offer.proposals), phase: "offered" });
    } catch (cause) {
      set({ error: reason(cause) });
    } finally {
      set({ busy: false });
    }
  },

  discard: () =>
    set({ phase: "idle", offer: null, accepted: new Set<number>(), written: null,
          error: null }),
}));

/**
 * One proposal as the sentence it PRODUCES, split for highlighting.
 *
 * The user's ruling (2026-08-28): show the corrected text with the change
 * marked, not a before/after pair. A pair of nearly-identical Chinese
 * sentences side by side is unreadable at exactly the moment it matters --
 * which is the same reason `correct.diff` on the server names the span
 * instead of printing two lines.
 *
 * Only THIS proposal is applied. A cue with two suggestions in it gets two
 * rows, and each row shows what its own tick would do; folding the other
 * one in would ask the reader to approve a sentence no single checkbox
 * produces.
 *
 * Returns `null` when the text no longer reads as the proposal says it
 * does. That should not happen -- the server recomputes proposals against
 * the file on every call -- and if it ever does, a caller that rendered the
 * highlight anyway would be marking the wrong characters. The panel falls
 * back to the plain pair.
 */
export function highlightOf(
  offer: CorrectionOffer | null,
  index: number,
): { head: string; mark: string; tail: string } | null {
  const proposal = offer?.proposals[index];
  if (!proposal) return null;
  const text = offer?.cues[proposal.cue]?.text;
  if (typeof text !== "string") return null;
  if (text.slice(proposal.start, proposal.end) !== proposal.was) return null;
  return {
    head: text.slice(0, proposal.start),
    mark: proposal.now,
    tail: text.slice(proposal.end),
  };
}

/**
 * The one-line summary above the list, or null when there is nothing to say.
 *
 * Pure. Two facts it must carry and a naive count would not: how many of the
 * offers are GUESSES, and whether phonetic matching was even available --
 * "no suggestions" means something different when the phonetic tier is
 * switched off, and a reader who is not told that will conclude the
 * transcript is clean.
 */
export function describeOffer(offer: CorrectionOffer | null): string | null {
  if (!offer) return null;
  if (offer.glossaryEntries === 0) {
    return "詞庫是空的，所以不會有任何建議。先把常錯的術語加進去。";
  }
  if (offer.proposals.length === 0) {
    return offer.phoneticKeys
      ? "沒有找到需要校正的術語。"
      : "沒有找到登記過的錯字（讀音比對目前無法使用，只比對了完全相同的寫法）。";
  }
  const exact = offer.proposals.filter((p) => p.tier === "exact").length;
  const guessed = offer.proposals.length - exact;
  if (guessed === 0) return `${exact} 處是你登記過的寫法。`;
  if (exact === 0) return `${guessed} 處讀音相同，是推測的，請逐項確認。`;
  return `${exact} 處是登記過的寫法，${guessed} 處是讀音推測的，請逐項確認。`;
}

/** How one proposal reads in the list. The confidence is shown only when it
 *  was measured -- an absent number must not render as 0%. */
export function describeProposal(proposal: CorrectionProposal): string {
  const how = proposal.tier === "exact" ? "登記過的寫法" : "讀音相同";
  const sure =
    proposal.confidence === null || proposal.confidence === undefined
      ? ""
      : `，引擎信心 ${Math.round(proposal.confidence * 100)}%`;
  return `${how}${sure}`;
}
