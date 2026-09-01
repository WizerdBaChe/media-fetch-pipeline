import { useConfigStore, globalPolicyOf } from "@/entities/config/model/store";
import { useSessionStore } from "@/entities/session/model/store";
import { useTaskStore } from "@/entities/task/model/store";
import { effectivePolicy, tasksForTab, type TabId } from "@/entities/task/model/selectors";

import {
  FULL_LAYOUT_MIN,
  columnProps,
  visibleColumns,
} from "@/entities/task/model/columns";
import { useContainerWidth } from "@/shared/lib/useContainerWidth";
import { TaskRow } from "@/entities/task/ui/TaskRow";
import { RowActions } from "@/features/control-transfer/ui/RowActions";
import { OpenOutputAction } from "@/features/open-output/ui/OpenOutputAction";
import { ExtensionMenu } from "@/features/pick-extension/ui/ExtensionMenu";
import { RowPolicySelect } from "@/features/set-policy/ui/RowPolicySelect";

export interface QueueTableProps {
  tab: TabId;
  /** Opens a延伸工具 on this row's download. Passed down rather than
   *  imported by the row: an entity may not reach for a feature, and the
   *  page is the layer that knows what opening a tool means. */
  onUseExtension?: (id: string, path?: string) => void;
}

export function QueueTable({ tab, onUseExtension }: QueueTableProps) {
  const tasks = useTaskStore((state) => state.tasks);
  const loaded = useTaskStore((state) => state.loaded);
  const setSelected = useTaskStore((state) => state.setSelected);
  const setSelectedAll = useTaskStore((state) => state.setSelectedAll);
  const config = useConfigStore((state) => state.config);
  const budgetWaits = useSessionStore((state) => state.budgetWaits);

  /**
   * Below `FULL_LAYOUT_MIN` the ten columns cannot all be drawn without
   * starving 來源, so four of them stand down.
   *
   * Measured on the table's OWN container, not on the window: those two differ
   * by a scrollbar the moment the queue is long enough to need one, and
   * deciding with one ruler while drawing with the other is what took the
   * difference out of 來源. `null` means not measured yet (no ResizeObserver,
   * i.e. jsdom), and the useful default there is the layout with more
   * information in it.
   */
  const [fit, space] = useContainerWidth();
  const narrow = space !== null && space < FULL_LAYOUT_MIN;
  const columns = visibleColumns(narrow);
  // By id, not by object identity: a narrowed column is a COPY of its entry.
  const shown = new Set(columns.map((column) => column.id));

  const rows = tasksForTab(tasks, tab);
  const policy = globalPolicyOf(config);
  const rowIds = rows.map((task) => task.id);
  const selectedCount = rows.filter((task) => task.selected).length;
  const allSelected = rows.length > 0 && selectedCount === rows.length;
  const someSelected = selectedCount > 0 && !allSelected;

  if (!loaded) {
    return <p className="mfp-empty">載入中…</p>;
  }

  if (rows.length === 0) {
    return (
      <p className="mfp-empty">
        {Object.keys(tasks).length === 0
          ? "佇列是空的。把網址貼到上面的欄位，可以一次多筆。"
          : "這個分頁沒有項目。"}
      </p>
    );
  }

  return (
    // The measured box, and deliberately not the table itself: in the full
    // layout a too-narrow container makes the TABLE wider than the space it
    // sits in, so measuring the table would feed its own overflow back into
    // the decision. A plain block at 100% is the space, whatever the table
    // then does with it.
    <div className="mfp-table-fit" ref={fit}>
    <table className="mfp-table" data-layout={narrow ? "narrow" : "full"}>
      {/* Widths and alignment both come out of one column list, so the header
          and the body cannot disagree and a changing number cannot make its
          column breathe (R7-2, R7-5). Which columns are IN that list is the
          same question at a narrow window, which is why hiding one is a matter
          of what this list contains rather than of a CSS rule somewhere else. */}
      <colgroup>
        {columns.map((column) => (
          <col
            key={column.id}
            data-col={column.id}
            style={column.width === null ? undefined : { width: column.width }}
          />
        ))}
      </colgroup>
      <thead>
        <tr>
          {columns.map((column) =>
            column.id === "check" ? (
              <th key={column.id} scope="col" {...columnProps("check")} className="mfp-table__check">
                {/* Lives here rather than in the toolbar: a select-all belongs
                    at the head of the column it ticks. It covers the rows of
                    THIS tab only — one in a filtered view that reached rows
                    the user cannot see would be lying about what it did. */}
                <input
                  type="checkbox"
                  checked={allSelected}
                  ref={(node) => {
                    // Partial selection reads as indeterminate rather than
                    // unchecked, so the header never claims "nothing is
                    // selected" when three are.
                    if (node) node.indeterminate = someSelected;
                  }}
                  onChange={(event) => void setSelectedAll(event.target.checked, rowIds)}
                  aria-label="選取這個分頁的所有項目"
                  title="全選（只影響目前分頁）"
                />
              </th>
            ) : (
              <th key={column.id} scope="col" {...columnProps(column.id)}>
                {column.label}
              </th>
            ),
          )}
        </tr>
      </thead>
      <tbody>
        {rows.map((task) => (
          <TaskRow
            key={task.id}
            task={task}
            shown={shown}
            effectivePolicy={effectivePolicy(task, policy)}
            waitingMs={budgetWaits[task.id] ?? null}
            onToggleSelected={(selected) => void setSelected(task.id, selected)}
            policyControl={<RowPolicySelect task={task} globalPolicy={policy} />}
            // Composed here rather than inside RowActions: two feature
            // slices may not import each other, and a widget is the layer
            // allowed to put them side by side (PSM §3.1).
            //
            // Both go in as SLOTS rather than as siblings of RowActions. As a
            // sibling the extension button was a second block in the cell, so
            // it wrapped onto its own line and every row stood 60px tall
            // against a 34px --row-height (measured 2026-08-23); inside, it
            // joins the flex row the other buttons already share.
            actions={
              <RowActions
                task={task}
                outputAction={<OpenOutputAction task={task} />}
                extensionAction={
                  onUseExtension && (
                    <ExtensionMenu
                      compact
                      label="延伸"
                      // Shown on every row, enabled only where there is
                      // something to work on. A button that appears and
                      // disappears teaches nobody where it lives, and a
                      // disabled one that says why teaches both.
                      disabledReason={
                        task.state !== "COMPLETED"
                          ? "下載完成後才能使用"
                          : !task.outputDir
                            ? "這個項目沒有可用的檔案"
                            : undefined
                      }
                      // A row hands a tool the folder it downloaded into.
                      // 文件翻譯 takes a .txt/.md the user names and 貼文解說
                      // takes a post URL, so both are offered from the header
                      // and disabled here WITH THE REASON, rather than opened
                      // on something they would refuse. Disabled and not
                      // hidden: a control that appears and disappears teaches
                      // nobody where it lives (D-80).
                      unavailable={{
                        translatedoc: "文件翻譯要選一份 .txt／.md 文件，請從上方的「延伸工具」開啟",
                        brief: "貼文解說要貼一個貼文網址，請從上方的「延伸工具」開啟",
                      }}
                      onPick={(id) => onUseExtension(id, task.outputDir ?? undefined)}
                    />
                  )
                }
              />
            }
          />
        ))}
      </tbody>
    </table>
    </div>
  );
}
