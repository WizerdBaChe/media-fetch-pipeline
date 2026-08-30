/**
 * One queue row. Presentational only — every action arrives as a slot, so this
 * entity never imports a feature (PSM §3.1 layering).
 */

import type { ReactNode } from "react";
import type { Task } from "@/shared/api/types";
import { ProgressBar } from "@/shared/ui/ProgressBar";
import { formatEta, formatPercent, formatSpeed } from "@/shared/lib/format";
import { presentError } from "@/shared/lib/errors";
import { platformLabel } from "@/shared/lib/platform";
import { columnProps, type QueueColumnId } from "../model/columns";
import { progressRatio } from "../model/selectors";
import { StateBadge } from "./StateBadge";

interface TaskRowProps {
  task: Task;
  /**
   * The columns the header drew, by id. Passed in rather than recomputed here:
   * two components deciding independently which columns exist is how a header
   * ends up over a value that is not its own, which is the whole of D-83.
   *
   * Optional so the many tests that render one row on its own keep working;
   * absent means every column, which is what a wide window shows.
   */
  shown?: ReadonlySet<QueueColumnId>;
  /** Resolved from the row's own policy or the global one. */
  effectivePolicy: string;
  onToggleSelected: (selected: boolean) => void;
  policyControl?: ReactNode;
  actions?: ReactNode;
  /** Live countdown for a budget-throttled row (§6). */
  waitingMs?: number | null;
}

export function TaskRow({
  task,
  shown,
  effectivePolicy,
  onToggleSelected,
  policyControl,
  actions,
  waitingMs,
}: TaskRowProps) {
  const draws = (id: QueueColumnId) => shown === undefined || shown.has(id);
  const ratio = progressRatio(task);
  const isActive = task.state === "DOWNLOADING" || task.state === "PROBING";
  const failed = task.state === "FAILED" || task.state === "EXPIRED";
  const error = failed ? presentError(task.errorCode) : null;

  const itemsTotal = task.progress.itemsTotal;
  const subject = task.author ?? task.title ?? task.postId;

  /**
   * A bar only where progress is a real quantity. A task that never started
   * has none, and an unknown-length bar is drawn INDETERMINATE -- so a failed
   * row with no bytes rendered as a full red bar sweeping back and forth,
   * which reads as "busy" on a row that will never move again. It showed up
   * the moment the error text stopped occupying this cell (2026-08-16).
   */
  const showBar = ratio !== null || isActive || task.state === "COMPLETED";

  return (
    <tr className="mfp-row" data-state={task.state} data-testid={`task-row-${task.id}`}>
      <td {...columnProps("check")} className="mfp-row__check">
        <input
          type="checkbox"
          checked={task.selected}
          onChange={(event) => onToggleSelected(event.target.checked)}
          aria-label={`選取 ${subject}`}
        />
      </td>

      {draws("platform") && (
        <td {...columnProps("platform")} className="mfp-row__platform">
          {platformLabel(task.platform)}
        </td>
      )}

      {/* The author truncates; the note must not. Before this was a flex row
          the whole cell was one ellipsis context, so the note was silently
          clipped off the end and the row looked like it had no note at all
          (R5-7, 2026-08-16).
          The flex is on an inner span rather than on the cell: `display:flex`
          on a `<td>` stops it BEING a table cell, so it dropped out of the
          row's vertical alignment and this column's text sat 18px above both
          its neighbours (measured 2026-08-23). */}
      <td {...columnProps("subject")} className="mfp-row__subject">
        <span className="mfp-row__subject-inner">
          <span className="mfp-row__author" title={task.canonicalUrl}>
            {subject}
          </span>
          {task.pathDegradation === "L1" && (
            <span className="mfp-row__note" title="路徑過長，資料夾名稱已改為雜湊值">
              資料夾名稱已縮短
            </span>
          )}
        </span>
      </td>

      {/* 項目 and 畫質 were one cell holding three kinds of thing: a count,
          a control and a status label. Every row's three were a different
          width, so nothing lined up vertically and the column read as if it
          slid left and right down the page (UAT item 5-6). One column, one
          kind of thing -- the count is a number and right-aligns like the
          other numbers; the control keeps its own column. */}
      {draws("items") && (
        <td {...columnProps("items")} className="mfp-row__items mfp-mono">
          {itemsTotal ? `${itemsTotal} 項` : "—"}
        </td>
      )}

      <td {...columnProps("quality")} className="mfp-row__quality mfp-mono">
        {policyControl}
        {task.policyPinned && <span className="mfp-row__pinned">已自訂</span>}
        {!task.policyPinned && <span className="mfp-row__inherited">{effectivePolicy}</span>}
      </td>

      {/* 狀態 and 進度 are separate columns because they were separate header
          cells all along — but the bar used to sit under 狀態 and the number
          under 進度, so every header label read as if it were one column out
          of step with what was beneath it (R7-5). */}
      <td {...columnProps("state")} className="mfp-row__state">
        {waitingMs != null ? (
          <span className="mfp-row__waiting" data-testid="budget-wait">
            ⏳ 等待配額 {formatEta(waitingMs / 1000)}
          </span>
        ) : error ? (
          <span className="mfp-row__error" title={error.hint}>
            ⚠ {error.label}
          </span>
        ) : (
          <StateBadge state={task.state} />
        )}
      </td>

      {/* When 速度 and 剩餘 stand down, their numbers move here rather than
          disappearing: a transfer that is going slowly is exactly when someone
          looks, and a narrow window is no reason to stop answering. */}
      <td
        {...columnProps("progress")}
        className="mfp-row__progress"
        title={
          draws("speed") || !isActive
            ? undefined
            : `速度 ${formatSpeed(task.progress.bytesPerSec)}・剩餘 ${formatEta(task.progress.etaSeconds)}`
        }
      >
        {showBar ? (
          <div className="mfp-progress-cell">
            <ProgressBar
              ratio={task.state === "COMPLETED" ? 1 : ratio}
              state={
                task.state === "COMPLETED"
                  ? "done"
                  : task.state === "FAILED"
                    ? "failed"
                    : isActive
                      ? "active"
                      : "paused"
              }
              label={`${subject} 進度`}
            />
            <span className="mfp-row__percent mfp-mono">
              {/* A muxed item is two transfers plus an ffmpeg pass. The
                  bytes are all in by then, so the percentage freezes at
                  100% while work continues -- saying so is the difference
                  between "finished" and "nearly finished". */}
              {task.progress.phase === "muxing"
                ? "合併中"
                : formatPercent(task.progress.bytesDone, task.progress.bytesTotal)}
            </span>
          </div>
        ) : (
          <span className="mfp-row__percent mfp-mono">—</span>
        )}
      </td>

      {draws("speed") && (
        <td {...columnProps("speed")} className="mfp-row__speed mfp-mono">
          {isActive ? formatSpeed(task.progress.bytesPerSec) : "—"}
        </td>
      )}
      {draws("eta") && (
        <td {...columnProps("eta")} className="mfp-row__eta mfp-mono">
          {isActive ? formatEta(task.progress.etaSeconds) : "—"}
        </td>
      )}

      <td {...columnProps("actions")} className="mfp-row__actions">
        {actions}
      </td>
    </tr>
  );
}
