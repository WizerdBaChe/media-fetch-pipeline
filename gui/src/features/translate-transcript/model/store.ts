/**
 * Translating a transcript that already exists.
 *
 * Its own feature slice, not a mode of `read-transcript`, and that is the
 * user's ruling rather than an organisational preference (2026-08-28):
 * translating is a second thing a person asks for once they have a
 * transcript in front of them, and nothing may translate as a side effect of
 * transcribing. The two slices never import each other; the workspace above
 * them is what puts a button from one next to a result from the other.
 *
 * The progress store is singular for the same reason `useAsrProgress` is:
 * this is a request in flight, not a job with an id. One is running or none
 * is, and the panel that started it is the one waiting.
 */

import { create } from "zustand";
import { ApiError, api } from "@/shared/api/client";
import type { MtProgress, TranslationResult } from "@/shared/api/types";
import { LANGUAGES } from "@/shared/lib/languages";

/** What the workspace offers.
 *
 * The list itself moved to `shared/lib/languages` when 文件翻譯 became the
 * second feature that needs it -- two slices in one layer may not import
 * each other, and a copy per feature is a copy that goes stale. The name
 * stays here because this is what the transcript workspace calls it. */
export const TARGETS = LANGUAGES;

export interface TranslateState {
  busy: boolean;
  error: string | null;
  progress: MtProgress | null;
  result: TranslationResult | null;
  target: string;

  setTarget: (target: string) => void;
  applyProgress: (progress: MtProgress) => void;
  clear: () => void;
  run: (source: string, sourceLanguage?: string) => Promise<TranslationResult | null>;
}

export const useTranslate = create<TranslateState>((set, get) => ({
  busy: false,
  error: null,
  progress: null,
  result: null,
  target: "eng_Latn",

  setTarget: (target) => set({ target }),
  applyProgress: (progress) => set({ progress }),
  clear: () => set({ error: null, progress: null, result: null }),

  run: async (source, sourceLanguage) => {
    set({ busy: true, error: null, progress: null, result: null });
    try {
      const result = await api.translate({
        source,
        target: get().target,
        ...(sourceLanguage ? { sourceLanguage } : {}),
      });
      set({ result });
      return result;
    } catch (cause) {
      set({
        error:
          cause instanceof ApiError
            ? cause.message
            : cause instanceof Error
              ? cause.message
              : String(cause),
      });
      return null;
    } finally {
      // Cleared here rather than on the terminal frame: the request is what
      // ends, and a readout left on 「翻譯中 100%」 under an error message is
      // the stale-progress bug this project has fixed twice already.
      set({ busy: false, progress: null });
    }
  },
}));

/**
 * The progress line, or `null` when there is nothing to say.
 *
 * Pure, and counted in LINES. Translation has no notion of the audio's
 * length, so seconds would be an invented unit -- and a percentage is only
 * shown when there is a total to divide by.
 */
export function describeTranslate(progress: MtProgress | null): string | null {
  if (!progress || progress.phase === "loaded") {
    return progress ? "翻譯模型載入中…" : null;
  }
  if (progress.phase !== "line") return null;
  const total = progress.total ?? 0;
  const done = progress.done ?? 0;
  if (total <= 0) return "翻譯中…";
  return `翻譯中 ${Math.round((done / total) * 100)}%（${done}/${total} 行）`;
}
