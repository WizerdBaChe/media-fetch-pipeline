import type { TaskState } from "@/shared/api/types";

const LABELS: Record<TaskState, string> = {
  PARSED: "待分析",
  PROBING: "分析中",
  READY: "可下載",
  DOWNLOADING: "下載中",
  PAUSED: "已暫停",
  EXPIRED: "連結過期",
  COMPLETED: "已完成",
  FAILED: "失敗",
  CANCELLED: "已取消",
};

/** Grouping drives colour only; the label is always the precise state. */
const TONE: Record<TaskState, string> = {
  PARSED: "idle",
  PROBING: "busy",
  READY: "idle",
  DOWNLOADING: "busy",
  PAUSED: "idle",
  EXPIRED: "warn",
  COMPLETED: "ok",
  FAILED: "bad",
  CANCELLED: "muted",
};

export function stateLabel(state: TaskState): string {
  return LABELS[state];
}

export function StateBadge({ state }: { state: TaskState }) {
  return (
    <span className={`mfp-badge mfp-badge--${TONE[state]}`} data-state={state}>
      {LABELS[state]}
    </span>
  );
}
