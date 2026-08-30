/**
 * Which controls a row offers.
 *
 * Rewritten after audit A-1. This used to compute the *target state* for each
 * button — which meant mirroring the server's `LEGAL_TRANSITIONS` here, i.e.
 * two copies of the state machine to keep in step. The server now exposes
 * semantic endpoints (`:start`, `:pause`, …), so this file only decides which
 * *intents* make sense to show, and the server decides what they do.
 *
 * The table below still has to match the server's `ACTIONS` map — but that is
 * a much smaller and more stable thing to agree on than 9×9 transitions, and
 * a test asserts the agreement.
 */

import type { Capabilities, Task, TaskState } from "@/shared/api/types";

export type ActionId = "probe" | "start" | "pause" | "cancel" | "retry";

export interface RowAction {
  id: ActionId;
  label: string;
  primary?: boolean;
  danger?: boolean;
  /** Set when the button must be shown but cannot work yet — never hidden. */
  disabledReason?: string;
}

const NO_PROBE = "取得層尚未實作（M2/M3），無法分析";
const NO_DOWNLOAD = "下載引擎尚未實作（M3）";

/** Mirrors `queue.ACTIONS`: which intents apply in which state. */
export const ACTIONS_BY_STATE: Record<TaskState, ActionId[]> = {
  PARSED: ["probe", "start", "cancel"],
  PROBING: ["cancel"],
  READY: ["probe", "start", "cancel"],
  DOWNLOADING: ["pause", "cancel"],
  PAUSED: ["start", "cancel"],
  EXPIRED: ["probe", "start", "cancel", "retry"],
  COMPLETED: ["probe", "retry"],
  FAILED: ["probe", "cancel", "retry"],
  CANCELLED: ["probe", "retry"],
};

export function rowActions(task: Task, capabilities: Capabilities): RowAction[] {
  const probeBlocked = capabilities.probe ? undefined : NO_PROBE;
  const downloadBlocked = capabilities.download ? undefined : NO_DOWNLOAD;

  switch (task.state) {
    case "PARSED":
      return [
        { id: "probe", label: "分析", primary: true, disabledReason: probeBlocked },
        { id: "cancel", label: "取消" },
      ];
    case "PROBING":
      return [{ id: "cancel", label: "取消", danger: true }];
    case "READY":
      return [
        { id: "start", label: "下載", primary: true, disabledReason: downloadBlocked },
        { id: "cancel", label: "取消" },
      ];
    case "DOWNLOADING":
      return [
        { id: "pause", label: "暫停", primary: true },
        { id: "cancel", label: "取消", danger: true },
      ];
    case "PAUSED":
      return [
        { id: "start", label: "繼續", primary: true, disabledReason: downloadBlocked },
        { id: "cancel", label: "取消" },
      ];
    case "EXPIRED":
      // O-5 ruling: an expired link re-probes rather than asking.
      return [
        { id: "probe", label: "重新分析", primary: true, disabledReason: probeBlocked },
        { id: "cancel", label: "取消" },
      ];
    case "FAILED":
    case "CANCELLED":
      return [{ id: "retry", label: "重試", primary: true, disabledReason: probeBlocked }];
    case "COMPLETED":
      // INV-5: the only way out of COMPLETED is an explicit retry.
      return [{ id: "retry", label: "重新下載", disabledReason: probeBlocked }];
  }
}

/**
 * Whether the global 開始/暫停 buttons have anything to act on.
 *
 * The GUI no longer computes per-row targets for bulk actions — `:startAll`
 * and `:pauseAll` do that server-side and report what they skipped. This is
 * only used to decide whether the button is worth enabling.
 */
export function canStart(task: Task): boolean {
  return ACTIONS_BY_STATE[task.state].includes("start");
}

export function canPause(task: Task): boolean {
  return ACTIONS_BY_STATE[task.state].includes("pause");
}
