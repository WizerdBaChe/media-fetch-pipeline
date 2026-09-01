/**
 * Getting the two外部程式 this product cannot work without, as state.
 *
 * The user-visible problem this backs: a person who has just installed this
 * app pastes a link, presses 開始, and the task fails with a sentence about
 * PATH. Nothing in the app had ever said, before that moment, that two other
 * programs had to be on the machine -- and nothing anywhere could do
 * anything about it. `mfp.toolchain` is the half that can act; this is the
 * half that asks.
 *
 * Shaped like `setup-asr`'s store and for the same reason: the server's
 * answer IS the state, so every action ends by storing what the server
 * returned rather than firing a second question at it. One `installing`
 * name rather than a boolean, because two rows each need to show their own
 * progress and only one of them is moving.
 *
 * Deliberately NOT part of `entities/config`. What is being asked here --
 * does this machine have a working ffmpeg, and which copy is answering --
 * is not a field in the config object, and half the answers change without
 * anything writing to it.
 */

import { create } from "zustand";
import { ApiError, api } from "@/shared/api/client";
import { useBackgroundJobs } from "@/entities/background-job/model/store";
import type { ToolInstallProgress, ToolStatus } from "@/shared/api/types";

/** What each program is FOR, in the words of somebody who has never heard
 *  of it. The name is not an explanation: a panel that lists 「yt-dlp」 and
 *  「ffmpeg」 and stops has told a new user exactly nothing. */
export const TOOL_COPY: Record<string, { label: string; purpose: string; missing: string }> = {
  "yt-dlp": {
    label: "影片下載引擎",
    purpose: "負責讀取貼文、把影片和圖片抓下來。沒有它，任何連結都下載不了。",
    missing: "還沒有這個，所以現在按下載不會有東西。",
  },
  ffmpeg: {
    label: "影音合併工具",
    purpose:
      "YouTube 的高畫質影片和聲音是分開兩軌的，要靠它合成一個檔案。沒有它，只能拿到畫質較差的版本。",
    missing: "還沒有這個，高畫質影片會沒有聲音或被降級。",
  },
  chrome: {
    label: "Chrome 瀏覽器",
    purpose:
      "只有 Instagram 和 Threads 需要它來讀取公開貼文。YouTube、X、Bilibili 都不需要。",
    missing: "沒有 Chrome，Instagram 和 Threads 的貼文讀不到；其他平台不受影響。",
  },
};

/** What the progress frame says, in one line. `resolving` is named as a
 *  phase because ffmpeg's first step is two small requests to find out the
 *  version and the checksum, and a progress bar that sits at 0% for three
 *  seconds with no words is how people conclude a button is broken. */
export function describeProgress(progress: ToolInstallProgress | null): string | null {
  if (progress === null) return null;
  if (progress.phase === "resolving") return `正在向 ${progress.detail ?? "來源"} 詢問最新版本…`;
  if (progress.phase === "installing") return "下載完成，正在安裝…";
  if (progress.phase === "done") return "完成";
  const done = progress.bytes ?? 0;
  const total = progress.total ?? 0;
  if (total <= 0) return `已下載 ${Math.round(done / 1048576)} MB`;
  return `下載中 ${Math.round((done / total) * 100)}%（${Math.round(done / 1048576)} / ${Math.round(total / 1048576)} MB）`;
}

/** 0..1 while there is a denominator, null before the first byte. */
function ratioOf(progress: ToolInstallProgress): number | null {
  const total = progress.total ?? 0;
  if (total <= 0) return null;
  return Math.min(1, (progress.bytes ?? 0) / total);
}

/** Which copy is being run, said out loud.
 *
 *  The row this feeds is the answer to 「我更新了，為什麼還是舊的」, so
 *  「已安裝」 on its own is not enough -- it is exactly the sentence that
 *  makes that question unanswerable. */
export function describeSource(tool: ToolStatus): string {
  if (!tool.installed) return "尚未安裝";
  switch (tool.source) {
    case "managed":
      return "由本程式下載安裝";
    case "path":
      return "使用系統上原本就有的";
    case "configured":
      return "使用設定檔指定的路徑";
    case "system":
      // Chrome, and only Chrome: it is found by reading the registry, not by
      // resolving a path, so it has its own source value. This case was
      // missing for as long as the row existed and fell to the default --
      // which rendered 「已安裝」 for the one tool a user is most likely to
      // have three copies of. `toolchain.SOURCES` is the list; a Python test
      // now reads this function and fails if a value has no case here.
      return "這台電腦上安裝的";
    default:
      // Never a bare 「已安裝」: a status that does not say WHICH copy is
      // the sentence D-151 exists to forbid. An unknown value means this
      // build is older than the sidecar, and saying so is the answer.
      return "已安裝（來源不明）";
  }
}

export interface ToolsState {
  tools: ToolStatus[] | null;
  /** The name currently being fetched, or null. */
  installing: string | null;
  progress: ToolInstallProgress | null;
  /** Whatever the last request said went wrong, already user-facing. */
  error: string | null;

  /**
   * Re-read every program's state.
   *
   * `keepError` exists because of a defect this store had for exactly one
   * test run: the refresh that follows an install runs whether the install
   * succeeded or not, and on success it clears `error` -- which on FAILURE
   * meant the sentence explaining what went wrong was wiped a microtask
   * after being written. The user pressed 安裝, watched it stop, and was
   * shown nothing at all.
   */
  load: (options?: { keepError?: boolean }) => Promise<void>;
  install: (name: string) => Promise<boolean>;
  remove: (name: string) => Promise<void>;
  applyProgress: (progress: ToolInstallProgress) => void;
}

/**
 * The absent programs this app can fetch ITSELF, in the order they matter.
 *
 * A plain function over the list rather than a method on the store, and the
 * reason is mechanical: a selector that builds an array returns a new
 * reference every render, and `useSyncExternalStore` compares snapshots by
 * identity -- `useTools((s) => s.missing())` is an infinite render loop.
 * Callers select `tools` (a stable reference) and pass it here.
 *
 * Chrome is deliberately excluded. It is missing for a reason no button in
 * this app can fix, and a list that renders 「安裝」 beside every row would
 * be promising something untrue.
 */
export function missingTools(tools: ToolStatus[] | null): ToolStatus[] {
  return (tools ?? []).filter((tool) => !tool.installed && tool.name !== "chrome");
}

function messageOf(cause: unknown): string {
  if (cause instanceof ApiError) return cause.message;
  return cause instanceof Error ? cause.message : String(cause);
}

export const useTools = create<ToolsState>((set, get) => ({
  tools: null,
  installing: null,
  progress: null,
  error: null,

  load: async (options = {}) => {
    try {
      const answer = await api.tools();
      // Shape-checked rather than trusted, for the reason `resilience.spec`
      // was written: an older sidecar that has never heard of `/v1/tools`
      // answers `{}`, and `tools: undefined` passes the `=== null` guard the
      // panel renders behind -- so `.map` runs on undefined and the whole
      // window goes blank. A missing list is an ERROR here, never an empty
      // one: 「沒有缺任何程式」 and 「問不到」 are different answers, and only
      // one of them means the banner should stay away.
      if (!Array.isArray(answer?.tools)) throw new Error("這個版本的本機服務還沒有工具清單。");
      set(options.keepError ? { tools: answer.tools } : { tools: answer.tools, error: null });
    } catch (caught) {
      set({ error: messageOf(caught) });
    }
  },

  applyProgress: (progress) => {
    set({ progress });
    const job = `tool:${progress.tool}`;
    useBackgroundJobs.getState().progress(job, {
      detail: describeProgress(progress),
      ratio: ratioOf(progress),
    });
  },

  install: async (name) => {
    // Reported through `background-job` as well as inline, because the
    // status bar is the one line no screen owns: closing 設定 in the middle
    // of a 106 MB ffmpeg transfer must not make it disappear (the defect
    // `entities/background-job` was created for).
    const jobs = useBackgroundJobs.getState();
    const job = `tool:${name}`;
    const label = TOOL_COPY[name]?.label ?? name;
    jobs.start(job, `安裝${label}`);
    set({ installing: name, error: null, progress: null });
    try {
      const status = await api.installTool(name);
      set((state) => ({
        tools: (state.tools ?? []).map((tool) => (tool.name === name ? status : tool)),
        error: null,
      }));
      jobs.finish(job, { ok: true, detail: `${label}已就緒` });
      return true;
    } catch (caught) {
      const message = messageOf(caught);
      set({ error: message });
      jobs.finish(job, { ok: false, detail: message });
      return false;
    } finally {
      set({ installing: null, progress: null });
      // Re-read rather than trusting the returned row alone: installing
      // ffmpeg also places ffprobe, and a failed install can still have
      // changed what is on disk. The panel must show what IS, not what the
      // last successful call said -- without erasing the reason it is
      // showing it, which is what `keepError` is for.
      void get().load({ keepError: true });
    }
  },

  remove: async (name) => {
    set({ error: null });
    try {
      const status = await api.removeTool(name);
      set((state) => ({
        tools: (state.tools ?? []).map((tool) => (tool.name === name ? status : tool)),
      }));
    } catch (caught) {
      set({ error: messageOf(caught) });
    }
  },
}));
