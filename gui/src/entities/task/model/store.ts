/**
 * The task queue store (PSM Batch 2 §4.2/§4.3).
 *
 * The server is the source of truth; this store is a cache of it. Every
 * mutation goes to the API first and applies the returned Task — no optimistic
 * local edit. That is deliberate: the state machine is a whitelist on the
 * server, so an optimistic transition could paint a state the server then
 * refuses, and the row would be lying until the next reload.
 */

import { create } from "zustand";
import { ApiError, api } from "@/shared/api/client";
import type { ParseReport, ParseResult, Task, TaskState } from "@/shared/api/types";

export interface TaskStoreState {
  tasks: Record<string, Task>;
  loaded: boolean;
  /** Last failure from any store action, for the status bar to show. */
  lastError: { errorCode: string; message: string } | null;

  load: () => Promise<void>;
  /** Pure preview — nothing enters the queue. Null means the call failed. */
  preview: (text: string) => Promise<ParseResult | null>;
  add: (text: string) => Promise<ParseReport | null>;
  setSelected: (id: string, selected: boolean) => Promise<void>;
  setSelectedAll: (selected: boolean, ids?: string[]) => Promise<void>;
  setPolicy: (id: string, policy: string | null) => Promise<void>;
  /** Semantic action; the server decides the resulting state. */
  act: (id: string, action: string) => Promise<void>;
  actAll: (action: "startAll" | "pauseAll") => Promise<void>;
  transition: (id: string, to: TaskState) => Promise<void>;
  remove: (ids: string[], deleteFiles?: boolean) => Promise<void>;
  clearCompleted: () => Promise<void>;
  clearAll: () => Promise<void>;

  /** Applied from the SSE stream; never triggers a request. */
  upsert: (task: Task) => void;
  /** Applied from the SSE stream when another tab removed records. */
  dropLocal: (ids: string[]) => void;
  clearError: () => void;
}

function index(tasks: Task[]): Record<string, Task> {
  return Object.fromEntries(tasks.map((task) => [task.id, task]));
}

/**
 * Run an API call, recording a failure instead of throwing into React.
 * An unhandled rejection in an event handler is invisible to the user;
 * `lastError` is not.
 */
async function guard<T>(
  set: (partial: Partial<TaskStoreState>) => void,
  action: () => Promise<T>,
): Promise<T | undefined> {
  try {
    const result = await action();
    set({ lastError: null });
    return result;
  } catch (error) {
    if (error instanceof ApiError) {
      set({ lastError: { errorCode: error.errorCode, message: error.message } });
      return undefined;
    }
    throw error;
  }
}

/**
 * Latest-wins coalescing for the select-all gesture.
 *
 * One request per row was the first problem (R5-14). One request per *click*
 * is the second: ten rapid clicks are ten concurrent POSTs, and concurrent
 * requests can be served in any order, so the state the user ends up with is
 * whichever one happened to land last — not the one they last asked for.
 *
 * So: at most one in flight, and only the newest intent is kept while it is.
 * Ten toggles become two requests and the last click is always the last word.
 */
let selectAllInFlight: Promise<void> | null = null;
let selectAllPending: { selected: boolean; ids?: string[] } | null = null;

export const useTaskStore = create<TaskStoreState>()((set, get) => ({
  tasks: {},
  loaded: false,
  lastError: null,

  load: async () => {
    const tasks = await guard(set, () => api.listTasks());
    if (tasks) set({ tasks: index(tasks), loaded: true });
    else set({ loaded: true });
  },

  preview: async (text) => {
    return (await guard(set, () => api.parseInput(text))) ?? null;
  },

  add: async (text) => {
    const response = await guard(set, () => api.addTasks(text));
    // null, not an empty report: "nothing changed" and "the call failed" are
    // different facts, and the caller must not clear the paste box on failure.
    if (!response) return null;

    set({ tasks: { ...get().tasks, ...index(response.tasks) } });
    return response.report;
  },

  setSelected: async (id, selected) => {
    const task = await guard(set, () => api.patchTask(id, { selected }));
    if (task) get().upsert(task);
  },

  setSelectedAll: async (selected, ids) => {
    // One request, not N. See `api.selectMany`: the old fan-out is what made
    // rapid select-all toggling stutter and what starved a second tab's
    // event stream (R5-14 / R5-15, measured 2026-08-16).
    selectAllPending = { selected, ids };
    if (selectAllInFlight) return selectAllInFlight;

    selectAllInFlight = (async () => {
      try {
        while (selectAllPending) {
          const intent = selectAllPending;
          selectAllPending = null;
          const result = await guard(set, () => api.selectMany(intent.selected, intent.ids));
          if (!result) continue;
          const next = { ...get().tasks };
          for (const task of result.tasks) next[task.id] = task;
          set({ tasks: next });
        }
      } finally {
        selectAllInFlight = null;
      }
    })();
    return selectAllInFlight;
  },

  setPolicy: async (id, policy) => {
    const patch = policy === null ? { clearPolicy: true } : { policy };
    const task = await guard(set, () => api.patchTask(id, patch));
    if (task) get().upsert(task);
  },

  transition: async (id, to) => {
    const task = await guard(set, () => api.transition(id, to));
    if (task) get().upsert(task);
  },

  act: async (id, action) => {
    const task = await guard(set, () => api.action(id, action));
    if (task) get().upsert(task);
  },

  actAll: async (action) => {
    // One request, not N: the server applies the action where it applies and
    // reports what it skipped, so a mixed queue is not N round trips and not
    // N identical errors.
    const result = await guard(set, () => api.bulkAction(action));
    if (!result) return;
    const next = { ...get().tasks };
    for (const task of result.tasks) next[task.id] = task;
    set({ tasks: next });
  },

  remove: async (ids, deleteFiles = false) => {
    const result = await guard(set, () => api.remove(ids, deleteFiles));
    // `removedIds`, not the ids we asked for: the server ignores ids it does
    // not have, and dropping a row locally that the server kept would be a
    // lie the next reload silently reverses.
    if (result) get().dropLocal(result.removedIds);
  },

  clearCompleted: async () => {
    const result = await guard(set, () => api.clearCompleted());
    // Was a full re-fetch of the whole queue. The response already says
    // exactly which records went.
    if (result) get().dropLocal(result.removedIds);
  },

  clearAll: async () => {
    const result = await guard(set, () => api.clearAll());
    if (result) set({ tasks: {} });
  },

  upsert: (task) => set({ tasks: { ...get().tasks, [task.id]: task } }),

  dropLocal: (ids) => {
    // Fed from a parsed SSE frame as well as from a response body, so the
    // shape is wire data, not a local invariant.
    if (!Array.isArray(ids)) return;
    const next = { ...get().tasks };
    let touched = false;
    for (const id of ids) {
      if (id in next) {
        delete next[id];
        touched = true;
      }
    }
    // The tab that issued the removal has already applied it; re-setting an
    // unchanged map would repaint the table for nothing.
    if (touched) set({ tasks: next });
  },

  clearError: () => set({ lastError: null }),
}));
