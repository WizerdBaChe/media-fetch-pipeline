/**
 * Global 開始/暫停 (§6, §8).
 *
 * After audit A-1 these are one request each: `:startAll` / `:pauseAll` apply
 * the action server-side wherever it applies and report what they skipped. The
 * GUI no longer computes a per-row target, so there is no second copy of the
 * state machine here to drift.
 */

import { Button } from "@/shared/ui/Button";
import { useTaskStore } from "@/entities/task/model/store";
import { useSessionStore } from "@/entities/session/model/store";
import { selectedTasks } from "@/entities/task/model/selectors";
import { canPause, canStart } from "../model/actions";

export function TransferControls() {
  const tasks = useTaskStore((state) => state.tasks);
  const actAll = useTaskStore((state) => state.actAll);
  const capabilities = useSessionStore((state) => state.capabilities);

  const selected = selectedTasks(tasks);
  const startable = selected.filter(canStart);
  const pausable = selected.filter(canPause);

  const blocked = !capabilities.probe && !capabilities.download;

  return (
    <div className="mfp-toolbar__group">
      <Button
        variant="primary"
        disabled={startable.length === 0 || blocked}
        title={blocked ? "取得層與下載引擎尚未實作（M2/M3）" : undefined}
        onClick={() => void actAll("startAll")}
      >
        ▶ 開始下載（{startable.length}）
      </Button>
      <Button disabled={pausable.length === 0} onClick={() => void actAll("pauseAll")}>
        ⏸ 全部暫停
      </Button>
    </div>
  );
}
