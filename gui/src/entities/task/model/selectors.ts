/**
 * Derivations over the task map. Pure functions taking the map, so they are
 * testable without a store and cheap to reuse from any layer above.
 */

import type { Task, TaskState } from "@/shared/api/types";

export type TabId = "all" | "downloading" | "pending" | "completed" | "failed";

/** Which task states each state tab shows (PSM §6). */
export const TAB_STATES: Record<Exclude<TabId, "all">, readonly TaskState[]> = {
  downloading: ["DOWNLOADING", "PROBING"],
  pending: ["PARSED", "READY", "PAUSED", "EXPIRED"],
  completed: ["COMPLETED"],
  failed: ["FAILED", "CANCELLED"],
};

export function allTasks(tasks: Record<string, Task>): Task[] {
  return Object.values(tasks).sort((a, b) => a.createdAt.localeCompare(b.createdAt));
}

export function tasksForTab(tasks: Record<string, Task>, tab: TabId): Task[] {
  const ordered = allTasks(tasks);
  if (tab === "all") return ordered;
  const states = TAB_STATES[tab];
  return ordered.filter((task) => states.includes(task.state));
}

export function countsByTab(tasks: Record<string, Task>): Record<TabId, number> {
  const ordered = allTasks(tasks);
  return {
    all: ordered.length,
    downloading: ordered.filter((t) => TAB_STATES.downloading.includes(t.state)).length,
    pending: ordered.filter((t) => TAB_STATES.pending.includes(t.state)).length,
    completed: ordered.filter((t) => TAB_STATES.completed.includes(t.state)).length,
    failed: ordered.filter((t) => TAB_STATES.failed.includes(t.state)).length,
  };
}

export function selectedTasks(tasks: Record<string, Task>): Task[] {
  return allTasks(tasks).filter((task) => task.selected);
}

const NON_TERMINAL: readonly TaskState[] = [
  "PARSED",
  "PROBING",
  "READY",
  "DOWNLOADING",
  "PAUSED",
  "EXPIRED",
];

/**
 * How many of `candidates` are still live. O-6 requires the confirm dialog to
 * say this number out loud before a removal cancels work in flight.
 */
export function countInFlight(candidates: Task[]): number {
  return candidates.filter((task) => NON_TERMINAL.includes(task.state)).length;
}

/** The effective policy for a row: its own override, else the global one. */
export function effectivePolicy(task: Task, globalPolicy: string): string {
  return task.policy ?? globalPolicy;
}

export function aggregateBytesPerSec(tasks: Record<string, Task>): number {
  return allTasks(tasks)
    .filter((task) => task.state === "DOWNLOADING")
    .reduce((sum, task) => sum + (task.progress?.bytesPerSec ?? 0), 0);
}

/** 0–1, or null when the total is not known yet (indeterminate bar). */
export function progressRatio(task: Task): number | null {
  const { bytesDone, bytesTotal, itemsDone, itemsTotal } = task.progress;
  if (bytesTotal && bytesTotal > 0) return bytesDone / bytesTotal;
  if (itemsTotal && itemsTotal > 0) return itemsDone / itemsTotal;
  return null;
}
