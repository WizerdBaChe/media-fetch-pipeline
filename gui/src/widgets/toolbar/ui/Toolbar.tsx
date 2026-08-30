/**
 * The select-all checkbox used to live here. It moved to the table's own
 * header cell (R7-5): a control that ticks a column belongs at the head of
 * that column, and from the toolbar it could not honour the active tab's
 * filter without looking like it had ticked rows that were not on screen.
 */

import { useTaskStore } from "@/entities/task/model/store";
import { selectedTasks } from "@/entities/task/model/selectors";
import { GlobalPolicySelect } from "@/features/set-policy/ui/GlobalPolicySelect";
import { TransferControls } from "@/features/control-transfer/ui/TransferControls";
import { ClearControls } from "@/features/clear-completed/ui/ClearControls";

export function Toolbar() {
  const tasks = useTaskStore((state) => state.tasks);
  const selected = selectedTasks(tasks);

  return (
    <div className="mfp-toolbar">
      <GlobalPolicySelect />

      {/* The bulk buttons act on the selection, so the count has to be
          visible somewhere now that the checkbox is not here. */}
      <span className="mfp-toolbar__count" data-testid="selected-count">
        已勾選 <span className="mfp-mono">{selected.length}</span>
      </span>

      <TransferControls />
      <div className="mfp-toolbar__spacer" />
      <ClearControls />
    </div>
  );
}
