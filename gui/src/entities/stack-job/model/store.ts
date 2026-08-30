/**
 * Stack jobs: what is running, what just finished, and which one is on show.
 *
 * A separate store from `task`, because they are separate things. A queue
 * row is a transfer with bytes and a resumable `.part`; a stack job is a
 * short piece of local work whose product is an image somebody has to LOOK
 * at. Merging them would mean every consumer of the queue learning to skip
 * rows that are not transfers.
 *
 * The server is the only place a job exists. This store holds what the
 * stream and the fetches have said, and never invents a state of its own --
 * an optimistic RUNNING that the server never confirms is how a stop button
 * ends up unable to stop anything.
 */

import { create } from "zustand";
import { ApiError, api } from "@/shared/api/client";
import { STACK_TERMINAL_STATES, type StackJob } from "@/shared/api/types";

export interface StackStoreState {
  jobs: Record<string, StackJob>;
  order: string[];
  /** The job the workspace is showing, if any. */
  currentId: string | null;
  lastError: string | null;

  load: () => Promise<void>;
  upsert: (job: StackJob) => void;
  select: (id: string | null) => void;
  cancel: (id: string) => Promise<void>;
  clearError: () => void;
}

export function isRunning(job: StackJob | null | undefined): boolean {
  return !!job && !STACK_TERMINAL_STATES.includes(job.state);
}

/** Newest first: the workspace shows what just happened, not what happened
 *  first. */
export function jobList(state: StackStoreState): StackJob[] {
  return state.order
    .map((id) => state.jobs[id])
    .filter((job): job is StackJob => Boolean(job));
}

export const useStackStore = create<StackStoreState>()((set, get) => ({
  jobs: {},
  order: [],
  currentId: null,
  lastError: null,

  load: async () => {
    try {
      const jobs = await api.listStackJobs();
      set({
        jobs: Object.fromEntries(jobs.map((job) => [job.id, job])),
        // The server returns them oldest first; the newest is the one
        // somebody is waiting for.
        order: jobs.map((job) => job.id).reverse(),
        lastError: null,
      });
    } catch (error) {
      set({ lastError: error instanceof ApiError ? error.message : String(error) });
    }
  },

  upsert: (job) =>
    set((state) => ({
      jobs: { ...state.jobs, [job.id]: job },
      order: state.order.includes(job.id) ? state.order : [job.id, ...state.order],
    })),

  select: (id) => set({ currentId: id }),

  cancel: async (id) => {
    try {
      // The response is the job as the server now sees it; the stream will
      // say the same thing a moment later, and agreeing twice is cheaper
      // than guessing once.
      get().upsert(await api.cancelStackJob(id));
    } catch (error) {
      set({ lastError: error instanceof ApiError ? error.message : String(error) });
    }
  },

  clearError: () => set({ lastError: null }),
}));
