import type { ReactNode } from "react";
import { Button } from "@/shared/ui/Button";
import { useTaskStore } from "@/entities/task/model/store";
import { useSessionStore } from "@/entities/session/model/store";
import type { Task } from "@/shared/api/types";
import { rowActions } from "../model/actions";

interface RowActionsProps {
  task: Task;
  /**
   * 開啟資料夾 / 複製路徑, supplied by the caller.
   *
   * A slot rather than an import: that button belongs to the `open-output`
   * feature, and FSD forbids one feature slice importing a sibling (PSM
   * §3.1, enforced by `boundaries/element-types`). The widget that composes
   * both is `widgets/queue-table`, one layer up, which is allowed to.
   */
  outputAction?: ReactNode;
  /**
   * 延伸工具, supplied by the caller for the same reason as `outputAction` --
   * it is a widget, and a feature may not reach up a layer to import one.
   *
   * It belongs in the output zone rather than beside the state verbs: it acts
   * on the artifact the download produced, not on the transfer, and it is
   * enabled by exactly the condition 開啟檔案位置 is.
   */
  extensionAction?: ReactNode;
}

export function RowActions({ task, outputAction, extensionAction }: RowActionsProps) {
  const act = useTaskStore((state) => state.act);
  const remove = useTaskStore((state) => state.remove);
  const capabilities = useSessionStore((state) => state.capabilities);

  /**
   * Two zones, split by how STABLE the contents are (UAT item 5-6).
   *
   * These are two different kinds of verb. State actions belong to the state
   * machine: they change with every row and there are at most two of them.
   * Output actions belong to the artifact: they barely change at all. Flat in
   * one row, the two varied together -- 「重新下載」 being two characters wider
   * than 「下載」 pushed everything after it sideways, so no button was in the
   * same place twice down the page and the eye had no landing point. That is
   * also why 開啟資料夾 was hard to find: it was one of five identical ghost
   * buttons at a position that moved.
   *
   * The fix needs no fixed pixel width and so cannot clip. State actions are
   * left-aligned, output actions right-aligned, and the slack between them is
   * empty space rather than content. 移除 is last on every row, so it lands on
   * the same x; the reveal sits immediately left of it, so it does too.
   */
  return (
    <div className="mfp-row__buttons">
      <span className="mfp-row__buttons-state">
        {/* A state verb is never a GHOST, and the reason is the column's left
            edge rather than emphasis.

            A ghost paints neither border nor background, so the eye lands on
            its label -- 10px of padding and 1px of border further right than
            the box. Every state except COMPLETED leads with a `primary`
            button, which does paint, so its box starts at the cell's padding
            edge where 「操作」 is. COMPLETED leads with 「重新下載」, which is
            neither primary nor danger; it was the one row in four whose ink
            began 11px right of its own header (measured 2026-08-30, reported
            as 操作 not lining up).

            No header position can be right for both. So the leading control
            paints on every row, and the column's left edge stops depending on
            what state the row happens to be in. 移除 in the output zone stays
            a real ghost: it is right-aligned, and nothing reads a header
            against it. */}
        {rowActions(task, capabilities).map((action) => (
          <Button
            key={action.id}
            variant={action.primary ? "primary" : action.danger ? "danger" : "default"}
            disabled={Boolean(action.disabledReason)}
            title={action.disabledReason}
            onClick={() => void act(task.id, action.id)}
          >
            {action.label}
          </Button>
        ))}
      </span>

      <span className="mfp-row__buttons-output">
        {extensionAction}
        {outputAction}

        <Button
          variant="ghost"
          onClick={() => void remove([task.id], false)}
          title="只移除紀錄，不刪除已完成的檔案"
        >
          移除
        </Button>
      </span>
    </div>
  );
}
