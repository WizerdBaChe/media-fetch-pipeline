import { POLICY_OPTIONS } from "@/entities/config/model/store";
import { useTaskStore } from "@/entities/task/model/store";
import type { Task } from "@/shared/api/types";

const INHERIT = "__inherit__";

/**
 * Per-row quality override (§7.4). Choosing a value pins the row so a later
 * global change cannot silently erase a deliberate edit; "跟隨全域" unpins it.
 */
export function RowPolicySelect({ task, globalPolicy }: { task: Task; globalPolicy: string }) {
  const setPolicy = useTaskStore((state) => state.setPolicy);

  return (
    <select
      className="mfp-select mfp-select--inline"
      aria-label={`${task.postId} 畫質`}
      value={task.policy ?? INHERIT}
      onChange={(event) => {
        const value = event.target.value;
        void setPolicy(task.id, value === INHERIT ? null : value);
      }}
    >
      <option value={INHERIT}>
        跟隨全域（
        {POLICY_OPTIONS.find((option) => option.value === globalPolicy)?.label ?? globalPolicy}）
      </option>
      {POLICY_OPTIONS.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  );
}
