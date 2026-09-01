/**
 * Which one-time explanations this person has already been shown.
 *
 * Server state, deliberately. The obvious home is `localStorage`, and it is
 * wrong here: the renderer's storage is per Electron profile and per
 * user-data directory, so anything that resets it -- a portable copy run
 * from a different folder, a cleared profile -- brings every guide back.
 * Somebody who is told how 逐字稿 works on every launch learns to dismiss
 * the dialog without reading it, which is worse than never having shown it.
 *
 * `seen === null` means "not asked yet", and it is not the same as "seen
 * nothing". Treating it as the empty list would flash the first-run dialog
 * on every launch for the fraction of a second before the answer arrives --
 * a UI that appears and vanishes is a UI people learn to ignore.
 *
 * `openNow` is the manual route: every guide is also reachable from a
 * 使用說明 button, because 「我按太快了」 needs an answer that is not
 * 「重灌」.
 */

import { create } from "zustand";
import { api } from "@/shared/api/client";

export interface OnboardingState {
  /** Ids already shown, or null until the server has answered. */
  seen: string[] | null;
  /** A guide the user asked for explicitly, overriding `seen`. */
  openNow: string | null;

  load: () => Promise<void>;
  /** Should `id` appear on its own right now? */
  shouldShow: (id: string) => boolean;
  /** Remember that `id` has been shown. Optimistic: the dialog closes on
   *  the click, not on the round trip, because a modal that lingers for a
   *  network hop reads as a control that did not take. */
  dismiss: (id: string) => void;
  /** Open one on purpose, from a 使用說明 button. */
  open: (id: string) => void;
  close: () => void;
  /** Show everything again. Called from 設定 -- see `/guides:reset`. */
  resetAll: () => Promise<void>;
}

export const useOnboarding = create<OnboardingState>((set, get) => ({
  seen: null,
  openNow: null,

  load: async () => {
    try {
      const guides = await api.guides();
      // Shape-checked, not trusted. An older sidecar that has never heard of
      // `/v1/guides` answers `{}`, and `seen: undefined` is not null -- so
      // `shouldShow` would call `.includes` on undefined and take the whole
      // React tree down with it. That is the defect `resilience.spec.ts`
      // exists for, reintroduced by a new endpoint; it blanked every one of
      // the 39 Playwright specs the first time this ran.
      if (!Array.isArray(guides?.seen)) throw new Error("no guide list");
      set({ seen: guides.seen });
    } catch {
      // Left NULL, which is what makes `shouldShow` answer false for
      // everything. A guide is help, never a gate: a person who has used
      // this app for a month must not be handed the first-run dialog
      // because one request failed, and `seen: []` -- the obvious-looking
      // thing to write here -- means exactly "show them all".
      //
      // The cost is a genuinely new user seeing nothing when the sidecar is
      // still starting, which is why `MainPage` asks again once the session
      // reports the server reachable.
      set({ seen: null });
    }
  },

  shouldShow: (id) => {
    const { seen, openNow } = get();
    if (openNow === id) return true;
    return seen !== null && !seen.includes(id);
  },

  dismiss: (id) => {
    set((state) => ({
      seen: state.seen === null ? [id] : [...new Set([...state.seen, id])],
      openNow: state.openNow === id ? null : state.openNow,
    }));
    // Fire and forget. The worst case for a lost write is being shown one
    // explanation a second time, which is not worth blocking a click on.
    void api.markGuideSeen(id).catch(() => undefined);
  },

  open: (id) => set({ openNow: id }),
  close: () => set({ openNow: null }),

  resetAll: async () => {
    const guides = await api.resetGuides();
    set({ seen: guides.seen, openNow: null });
  },
}));
