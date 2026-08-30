/**
 * Translating a DOCUMENT -- the desktop half of `mfp translate-doc`.
 *
 * Its own slice rather than a mode of `translate-transcript`, mirroring the
 * two verbs underneath it (D-131): what they share is the engine, and what
 * differs is what a line MEANS. Sharing a store would have made the source
 * language optional here, which is the one thing it must not be.
 *
 *     idle ──run──▶ busy ──▶ done
 *                     └────▶ failed
 *
 * `from` is held with an empty default and no guess. A transcript this
 * project wrote carries its language in its filename; a document carries
 * nothing, and a wrong source language produces fluent output that is not a
 * translation of anything -- so the panel asks and the button stays
 * disabled until it has been answered.
 */

import { create } from "zustand";
import { ApiError, api } from "@/shared/api/client";
import type { DocumentTranslationResult, MtProgress } from "@/shared/api/types";

export interface TranslateDocState {
  busy: boolean;
  error: string | null;
  progress: MtProgress | null;
  result: DocumentTranslationResult | null;
  /** The document being translated. Empty until one is picked or typed. */
  source: string;
  /** FLORES code. Empty means "not answered yet", which is a state, not a
   *  default -- see the module note. */
  from: string;
  to: string;

  setSource: (source: string) => void;
  setFrom: (from: string) => void;
  setTo: (to: string) => void;
  applyProgress: (progress: MtProgress) => void;
  clear: () => void;
  run: () => Promise<DocumentTranslationResult | null>;
}

function reason(cause: unknown): string {
  if (cause instanceof ApiError) return cause.message;
  if (cause instanceof Error) return cause.message;
  return String(cause);
}

export const useTranslateDoc = create<TranslateDocState>((set, get) => ({
  busy: false,
  error: null,
  progress: null,
  result: null,
  source: "",
  from: "",
  to: "zho_Hant",

  // A new document is a new question: whatever the last run produced is not
  // about this one, and a result left on screen under a different filename
  // is the stale-result bug this project has fixed twice on other panels.
  setSource: (source) => set({ source, result: null, error: null }),
  setFrom: (from) => set({ from }),
  setTo: (to) => set({ to }),
  applyProgress: (progress) => set({ progress }),
  clear: () => set({ error: null, progress: null, result: null }),

  run: async () => {
    const { source, from, to } = get();
    // Both refusals are the server's to explain; this only stops a request
    // that cannot carry the question. `canRun` is what the button reads, so
    // in practice a user never reaches either.
    if (!source.trim() || !from) return null;
    set({ busy: true, error: null, progress: null, result: null });
    try {
      const result = await api.translateDocument({
        source: source.trim(),
        target: to,
        sourceLanguage: from,
      });
      set({ result });
      return result;
    } catch (cause) {
      set({ error: reason(cause) });
      return null;
    } finally {
      // Cleared with the request, not on the last progress frame: a readout
      // left at 「翻譯中 100%」 under an error message says the run finished.
      set({ busy: false, progress: null });
    }
  },
}));

/** Is there enough on screen to ask the question at all? Pure, so the panel
 *  and its tests agree about when the button is live. */
export function canRun(state: {
  source: string;
  from: string;
  busy: boolean;
}): boolean {
  return Boolean(state.source.trim()) && Boolean(state.from) && !state.busy;
}

/**
 * The progress line, or `null` when there is nothing to say.
 *
 * Counted in SENTENCES here and in lines on the transcript side, because
 * that is what each verb actually sends -- one unit per surface, named on
 * the surface, rather than one word covering two different countings.
 */
export function describeDocProgress(progress: MtProgress | null): string | null {
  if (!progress || progress.phase === "loaded") {
    return progress ? "翻譯模型載入中…" : null;
  }
  if (progress.phase !== "line") return null;
  const total = progress.total ?? 0;
  const done = progress.done ?? 0;
  if (total <= 0) return "翻譯中…";
  return `翻譯中 ${Math.round((done / total) * 100)}%（${done}/${total} 句）`;
}

/**
 * What the result says about the document's structure, or null.
 *
 * `verbatimBlocks` is the number worth reading: it counts what was held OUT
 * of the request -- code, tables, front matter, link targets -- so a run
 * reporting none on a document full of code did not parse it as Markdown.
 */
export function describeDocResult(
  result: DocumentTranslationResult | null,
): string | null {
  if (!result) return null;
  return (
    `翻好 ${result.sentences} 句，${result.blocks} 個段落，` +
    `其中 ${result.verbatimBlocks} 個原樣保留（程式碼、表格這類不會被翻譯）。`
  );
}
