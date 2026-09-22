/**
 * Work that outlives the screen which started it.
 *
 * The defect this exists for, measured 2026-08-30: 「下載延伸資源」 starts a
 * transfer of about 3 GB, its progress is rendered inside `AsrSetupPanel`, and
 * closing 設定 unmounts that panel. The request keeps running -- the store is
 * module-level and the progress events arrive over SSE into `useLiveUpdates`,
 * which is mounted on `App` and never goes away -- so the STATE was never the
 * problem. There was simply nowhere left on screen that could say so, and a
 * 3 GB transfer nobody can see is indistinguishable from a hang.
 *
 * So a job is recorded HERE, at a layer no view owns, and the status bar reads
 * it. Opening a tool, going back to the queue, opening and closing 設定 --
 * none of them can touch it. Only quitting the app can, which is the promise
 * this store is making and the reason a finished job waits to be dismissed
 * rather than clearing itself: a transfer that ended while the user was
 * somewhere else must still be there when they come back.
 *
 * Deliberately an entity and deliberately generic. `features/setup-tools`
 * writes to it (a feature may reach down to an entity) and `widgets/status-bar`
 * reads it, so the two never meet. Nothing here knows what is being installed.
 */

import { create } from "zustand";

export type JobPhase = "running" | "done" | "failed";

export interface BackgroundJob {
  /** Stable per piece of work, so a second event finds the same entry. */
  id: string;
  /** What is happening, in the user's words. 「安裝影片下載引擎」 */
  label: string;
  /** The line underneath: bytes, percentage, or why it failed. */
  detail: string | null;
  phase: JobPhase;
  /** 0..1 only where there is a real denominator. An invented one is the
   *  confident wrong number this project keeps catching itself producing. */
  ratio: number | null;
  startedAt: number;
  endedAt: number | null;
}

export interface BackgroundJobState {
  /** Oldest first, so a list reads in the order things were started. */
  jobs: BackgroundJob[];
  start: (id: string, label: string) => void;
  progress: (id: string, patch: { detail?: string | null; ratio?: number | null }) => void;
  finish: (id: string, outcome: { ok: boolean; detail: string }) => void;
  dismiss: (id: string) => void;
  clearFinished: () => void;
}

const now = () => Date.now();

export const useBackgroundJobs = create<BackgroundJobState>((set) => ({
  jobs: [],

  start: (id, label) =>
    set((state) => {
      const fresh: BackgroundJob = {
        id,
        label,
        detail: null,
        phase: "running",
        ratio: null,
        startedAt: now(),
        endedAt: null,
      };
      // Restart in place rather than appending a second entry: the same
      // download tried twice is one thing that happened twice, and two rows
      // for it would make the count say something untrue.
      const at = state.jobs.findIndex((job) => job.id === id);
      if (at === -1) return { jobs: [...state.jobs, fresh] };
      const jobs = [...state.jobs];
      jobs[at] = fresh;
      return { jobs };
    }),

  progress: (id, patch) =>
    set((state) => ({
      jobs: state.jobs.map((job) =>
        // Only while it is running. A late event arriving after the request
        // returned would otherwise reopen a finished job's progress line.
        job.id === id && job.phase === "running"
          ? {
              ...job,
              detail: patch.detail === undefined ? job.detail : patch.detail,
              ratio: patch.ratio === undefined ? job.ratio : patch.ratio,
            }
          : job,
      ),
    })),

  finish: (id, outcome) =>
    set((state) => ({
      jobs: state.jobs.map((job) =>
        job.id === id
          ? {
              ...job,
              phase: outcome.ok ? "done" : "failed",
              detail: outcome.detail,
              // A finished transfer is finished, whatever the last event said.
              ratio: outcome.ok ? 1 : job.ratio,
              endedAt: now(),
            }
          : job,
      ),
    })),

  dismiss: (id) => set((state) => ({ jobs: state.jobs.filter((job) => job.id !== id) })),

  // Running jobs are never swept: 「清除已完成」 means what it says, and a
  // control that quietly cancelled a 3 GB transfer would be the worst button
  // in this application.
  clearFinished: () => set((state) => ({ jobs: state.jobs.filter((job) => job.phase === "running") })),
}));

/** How many are still going, which is what the status bar leads with. */
export function runningJobs(jobs: BackgroundJob[]): BackgroundJob[] {
  return jobs.filter((job) => job.phase === "running");
}
