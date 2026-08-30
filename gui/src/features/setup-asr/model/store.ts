/**
 * Getting speech recognition ready, as a piece of state rather than a form.
 *
 * The panel this backs has one job: turn 「還不能聽寫」 into 「可以聽寫」. That
 * makes the readiness verdict the state, and every control a way of asking
 * the server to change it -- which is why almost every action here ends by
 * storing the readiness the server returned rather than by re-fetching it.
 * A control that mutated a config and then asked a second question would
 * have a window in which the panel showed the old answer.
 *
 * Deliberately NOT part of `entities/config`. That store owns the settings
 * object and writes it back wholesale; this one asks questions the config
 * cannot answer -- whether a Python can import the engine, whether a folder
 * holds a loadable model, whether the disk allows a junction -- and none of
 * those is a field.
 */

import { create } from "zustand";
import { ApiError, api } from "@/shared/api/client";
import { useBackgroundJobs } from "@/entities/background-job/model/store";
import type {
  AsrCapability,
  AsrCatalogueEntry,
  AsrEngineStatus,
  AsrInstallMode,
  AsrInstallProgress,
  AsrModel,
  AsrReadiness,
  AsrScanResult,
} from "@/shared/api/types";

export interface AsrSetupState {
  readiness: AsrReadiness | null;
  /** Whatever the last request said went wrong, already user-facing. */
  error: string | null;
  /** One flag, because the panel disables its controls as a group: two
   *  overlapping installs into one folder is not a state worth supporting. */
  busy: boolean;
  /** The result of the last 「瀏覽…」, or null when nothing has been picked. */
  scan: AsrScanResult | null;
  /** Which of the scanned models the user is about to install. */
  chosen: AsrModel | null;
  mode: AsrInstallMode;
  install: AsrInstallProgress | null;
  /** What can be fetched, and what is already here. `null` until asked. */
  catalogue: AsrCatalogueEntry[] | null;
  /** The id currently downloading, so one row can show its own progress. */
  downloading: string | null;

  load: () => Promise<void>;
  loadCatalogue: () => Promise<void>;
  download: (id: string) => Promise<boolean>;
  scanFolder: (path: string) => Promise<void>;
  choose: (model: AsrModel | null) => void;
  setMode: (mode: AsrInstallMode) => void;
  clearScan: () => void;
  applyInstallProgress: (progress: AsrInstallProgress) => void;
  installChosen: () => Promise<boolean>;
  select: (name: string) => Promise<void>;
  remove: (name: string) => Promise<void>;
  setEngine: (path: string) => Promise<void>;
  setHome: (path: string) => Promise<void>;
  setDownload: (allowed: boolean) => Promise<void>;
  /** What is done to the sound before recognition: none | level | denoise. */
  setAudio: (mode: string) => Promise<void>;
}

/** Server prose when there is some, the transport's message when there is
 *  not. Never a bare `[object Object]`, which is what an un-narrowed catch
 *  renders for an `ApiError`. */
function messageOf(cause: unknown): string {
  if (cause instanceof ApiError) return cause.message;
  return cause instanceof Error ? cause.message : String(cause);
}

/** 0..1 where the event carries a denominator, null where it does not. */
function ratioOf(progress: AsrInstallProgress): number | null {
  const total = progress.total ?? 0;
  if (total <= 0) return null;
  const done = progress.bytes ?? progress.copied ?? 0;
  return Math.min(1, done / total);
}

export const useAsrSetup = create<AsrSetupState>((set, get) => {
  /**
   * Which background-job entry the SSE progress events belong to.
   *
   * A closure rather than a field: it is bookkeeping between two of this
   * store's own methods, not a thing any panel renders, and the progress
   * event carries no id of its own -- the server is answering the one request
   * we have open, and `busy` already says there is only ever one.
   */
  let activeJob: string | null = null;
  /**
   * Run one server call, own the busy flag and the error line, and store
   * whatever readiness came back.
   *
   * Written once because every action below has exactly this shape, and the
   * one thing that must never vary between them is that `busy` is cleared:
   * a panel stuck disabled after a failed call is unrecoverable without a
   * reload.
   */
  const run = async (
    work: () => Promise<AsrReadiness>,
  ): Promise<boolean> => {
    set({ busy: true, error: null });
    try {
      set({ readiness: await work() });
      return true;
    } catch (cause) {
      set({ error: messageOf(cause) });
      return false;
    } finally {
      set({ busy: false, install: null });
    }
  };

  return {
    readiness: null,
    error: null,
    busy: false,
    scan: null,
    chosen: null,
    mode: "copy",
    install: null,
    catalogue: null,
    downloading: null,

    load: async () => {
      await run(() => api.asrReadiness());
    },

    scanFolder: async (path) => {
      set({ busy: true, error: null });
      try {
        const scan = await api.asrScanModels(path);
        const usable = scan.models.filter((model) => model.usable);
        // Pre-selected only when there is exactly one candidate. Choosing
        // the first of several on the user's behalf is how the wrong 3 GB
        // gets copied.
        set({ scan, chosen: usable.length === 1 ? usable[0] : null });
        // A default mode that cannot work is a trap. Linking is the cheapest
        // option and the panel offers it first, so falling back to copy when
        // the disk refuses junctions is the one automatic choice worth making.
        const link = scan.modes.find((entry) => entry.mode === "link");
        if (link && !link.available && get().mode === "link") set({ mode: "copy" });
      } catch (cause) {
        set({ error: messageOf(cause), scan: null, chosen: null });
      } finally {
        set({ busy: false });
      }
    },

    choose: (model) => set({ chosen: model }),
    setMode: (mode) => set({ mode }),
    clearScan: () => set({ scan: null, chosen: null, install: null }),

    // Not cleared on a terminal-looking phase, for the reason the transcript
    // readout is not: the request that produces these is still open, and the
    // action that started it clears the line when it returns.
    applyInstallProgress: (progress) => {
      set({ install: progress });
      // The same event, said twice on purpose: once in the panel that asked
      // for it, once where it survives that panel being closed.
      if (activeJob === null) return;
      useBackgroundJobs.getState().progress(activeJob, {
        detail: describeFetch(progress) ?? describeInstall(progress),
        ratio: ratioOf(progress),
      });
    },

    installChosen: async () => {
      const { chosen, mode } = get();
      if (!chosen) return false;
      set({ busy: true, error: null, install: null });
      const jobs = useBackgroundJobs.getState();
      activeJob = "asr:install";
      jobs.start(activeJob, `加入模型 ${chosen.name}`);
      try {
        const result = await api.asrInstallModel({ path: chosen.path, mode });
        set({ readiness: result.readiness, scan: null, chosen: null });
        jobs.finish("asr:install", { ok: true, detail: "已加入模型資料夾" });
        return true;
      } catch (cause) {
        const message = messageOf(cause);
        set({ error: message });
        jobs.finish("asr:install", { ok: false, detail: message });
        return false;
      } finally {
        activeJob = null;
        set({ busy: false, install: null });
      }
    },

    select: async (name) => {
      await run(() => api.asrSelectModel(name));
    },
    remove: async (name) => {
      await run(() => api.asrRemoveModel(name));
    },
    setEngine: async (path) => {
      await run(() => api.asrSetEngine(path));
    },
    setHome: async (path) => {
      await run(() => api.asrSetHome(path));
    },
    setDownload: async (allowed) => {
      await run(() => api.asrSetDownload(allowed));
    },
    setAudio: async (mode) => {
      await run(() => api.asrSetAudio(mode));
    },
    loadCatalogue: async () => {
      // Not folded into `load`: the readiness answer is needed to render the
      // panel at all, and this one is only needed by the part of it that
      // offers a download. Asking for both at once would make the panel wait
      // on a network call it may not use.
      try {
        set({ catalogue: (await api.asrCatalogue()).models });
      } catch (cause) {
        set({ error: messageOf(cause) });
      }
    },
    download: async (id) => {
      // `downloading` rather than the shared `busy` flag: this one runs for
      // minutes and the panel needs to say WHICH row is moving. `busy` still
      // goes up, so nothing else can be started underneath it.
      set({ busy: true, error: null, downloading: id, install: null });
      const jobs = useBackgroundJobs.getState();
      const named = get().catalogue?.find((entry) => entry.id === id)?.label ?? id;
      activeJob = `asr:download:${id}`;
      jobs.start(activeJob, `下載 ${named}`);
      const job = activeJob;
      try {
        const result = await api.asrDownloadModel(id);
        set({ readiness: result.readiness });
        await get().loadCatalogue();
        jobs.finish(job, { ok: true, detail: "下載完成，已放進模型資料夾" });
        return true;
      } catch (cause) {
        const message = messageOf(cause);
        set({ error: message });
        jobs.finish(job, { ok: false, detail: message });
        return false;
      } finally {
        activeJob = null;
        set({ busy: false, downloading: null, install: null });
      }
    },
  };
});

/**
 * The copy progress as one line, or `null` when there is nothing to say.
 *
 * Pure and exported for the same reason `describeProgress` next door is: it
 * is the part with the arithmetic in it, and arithmetic is what a test can
 * actually pin down. A percentage is only shown when there is a total to
 * divide by -- an invented denominator is the confident wrong number this
 * project keeps catching itself producing.
 */
export function describeInstall(progress: AsrInstallProgress | null): string | null {
  if (!progress) return null;
  if (progress.phase === "moved") return "搬移完成";
  if (progress.phase !== "copy") return null;
  const total = progress.total ?? 0;
  const copied = progress.copied ?? 0;
  if (total <= 0) return "複製中…";
  return `複製中 ${Math.round((copied / total) * 100)}%（${humanBytes(copied)} / ${humanBytes(total)}）`;
}

/**
 * A DOWNLOAD's progress, as one line, or `null`.
 *
 * Separate from `describeInstall` because the two are different verbs with
 * different failure feelings: a copy is bounded by the disk and a download is
 * bounded by somebody else's server, so 「下載中 4%」 sitting still for a
 * minute is ordinary where 「複製中 4%」 is not.
 *
 * The phases come from `asr/fetch_model.py`: `sized` announces the total
 * before a byte moves, `fetching` carries the running count.
 */
export function describeFetch(progress: AsrInstallProgress | null): string | null {
  if (!progress) return null;
  if (progress.phase === "sized") {
    return progress.bytes ? `準備下載 ${humanBytes(progress.bytes)}…` : "準備下載…";
  }
  if (progress.phase !== "fetching") return null;
  const total = progress.total ?? 0;
  const done = progress.bytes ?? 0;
  // No percentage without a denominator. An invented one is the confident
  // wrong number this project keeps catching itself producing.
  if (total <= 0) return `下載中（${humanBytes(done)}）`;
  return `下載中 ${Math.round((done / total) * 100)}%（${humanBytes(done)} / ${humanBytes(total)}）`;
}

/**
 * Three tones, and the reason there are three rather than two.
 *
 * `ready` and `blocked` are the obvious pair. `off` is the one this surface
 * kept getting wrong: a capability nobody opted into is not a fault, and
 * painting it the same alarm colour as a broken one taught readers that
 * orange means nothing in particular.
 */
export type PartTone = "ready" | "blocked" | "off";

/**
 * The engine as THREE states, because it has three.
 *
 * `engine.present` is `path != null && problem == null`, so rendering it as
 * a two-way switch filed a CONFIGURED BUT BROKEN interpreter under
 * 「尚未設定」 -- in the same row as 「在這裡設定的」, one band above its own
 * path in 技術細節. That is the contradiction the audit calls C1, and it is
 * invisible to a test whose fixture sets `path: null`.
 *
 * A path with a problem and no path with a problem are also different
 * things: the first is a choice that failed, the second is a shell variable
 * pointing at nothing. Both say the problem; only one of them is 「尚未設定」.
 */
export function describeEngine(engine: AsrEngineStatus): {
  tone: PartTone;
  text: string;
  problem: string | null;
} {
  if (engine.path === null) {
    return engine.problem === null
      ? { tone: "off", text: "尚未設定", problem: null }
      : { tone: "blocked", text: "指定的位置有問題", problem: engine.problem };
  }
  if (engine.problem !== null) {
    return { tone: "blocked", text: "設定了，但現在不能用", problem: engine.problem };
  }
  return {
    tone: "ready",
    text: engine.version ? `已就緒（faster-whisper ${engine.version}）` : "已就緒",
    problem: null,
  };
}

/**
 * Whether a not-ready capability is a FAULT or an offer nobody took up.
 *
 * `optional` was added to the contract for exactly this, defaulted to `true`
 * for both capabilities, set by neither builder and read by nothing -- so
 * 「還沒有翻譯功能」 rendered in the same alarm orange as a recognition
 * engine that will not import.
 */
export function capabilityTone(capability: AsrCapability): PartTone {
  if (capability.ready) return "ready";
  return capability.optional ? "off" : "blocked";
}

/** The verdict word for a tone. Short by design: the WHY belongs to the
 *  capability's own card, and printing the same sentence in the card and in
 *  the table is what made one state look like two disagreeing ones (C4). */
export function toneWord(tone: PartTone): string {
  if (tone === "ready") return "可以用";
  return tone === "blocked" ? "還不能用" : "沒有開通";
}

/** Sizes a person reads. Mirrors `asr_models.human_bytes` on the server;
 *  duplicated because a progress line arrives as raw bytes on the event
 *  stream and there is nothing to ask. */
export function humanBytes(size: number): string {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = size;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return index <= 1 ? `${Math.round(value)} ${units[index]}` : `${value.toFixed(1)} ${units[index]}`;
}
