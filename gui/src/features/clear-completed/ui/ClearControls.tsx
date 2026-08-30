/**
 * The three manual removal entry points (O-6).
 *
 * All three remove **records only**; completed media files are never touched
 * (INV-6). The split that matters: 清除已完成 can only hit terminal rows, so it
 * needs no confirmation, while 刪除勾選 and 一鍵刪除紀錄 can hit live work and
 * must say how many transfers they are about to cancel (INV-8).
 */

import { useState } from "react";
import { Button } from "@/shared/ui/Button";
import { ConfirmDialog } from "@/shared/ui/ConfirmDialog";
import { useTaskStore } from "@/entities/task/model/store";
import { allTasks, countInFlight, selectedTasks } from "@/entities/task/model/selectors";

type Pending = { kind: "selected"; ids: string[]; inFlight: number } | { kind: "all"; total: number; inFlight: number } | null;

export function ClearControls() {
  const tasks = useTaskStore((state) => state.tasks);
  const remove = useTaskStore((state) => state.remove);
  const clearCompleted = useTaskStore((state) => state.clearCompleted);
  const clearAll = useTaskStore((state) => state.clearAll);
  const [pending, setPending] = useState<Pending>(null);

  const all = allTasks(tasks);
  const selected = selectedTasks(tasks);
  const completedCount = all.filter((task) => task.state === "COMPLETED").length;

  const askRemoveSelected = () => {
    const inFlight = countInFlight(selected);
    const ids = selected.map((task) => task.id);
    if (inFlight === 0) {
      // Purely terminal rows: no live work dies, so no confirmation (O-6).
      void remove(ids, false);
      return;
    }
    setPending({ kind: "selected", ids, inFlight });
  };

  const confirm = () => {
    if (!pending) return;
    if (pending.kind === "selected") void remove(pending.ids, false);
    else void clearAll();
    setPending(null);
  };

  return (
    <div className="mfp-toolbar__group">
      <Button disabled={completedCount === 0} onClick={() => void clearCompleted()}>
        清除已完成（{completedCount}）
      </Button>
      <Button disabled={selected.length === 0} onClick={askRemoveSelected}>
        刪除勾選（{selected.length}）
      </Button>
      <Button
        variant="danger"
        disabled={all.length === 0}
        onClick={() =>
          setPending({ kind: "all", total: all.length, inFlight: countInFlight(all) })
        }
      >
        一鍵刪除紀錄
      </Button>

      <ConfirmDialog
        open={pending !== null}
        danger
        title={pending?.kind === "all" ? "刪除全部紀錄？" : "刪除勾選的紀錄？"}
        confirmLabel="刪除紀錄"
        onCancel={() => setPending(null)}
        onConfirm={confirm}
        body={
          pending && (
            <>
              <p>
                將移除{" "}
                <strong>
                  {pending.kind === "all" ? pending.total : pending.ids.length}
                </strong>{" "}
                筆紀錄。
              </p>
              {pending.inFlight > 0 && (
                <p className="mfp-modal__warning">
                  其中 <strong>{pending.inFlight}</strong> 筆仍在進行中，會先被取消。
                </p>
              )}
              <p className="mfp-modal__note">
                只刪除紀錄與未完成的暫存檔；<strong>已完成的媒體檔案不會被刪除</strong>。
              </p>
            </>
          )
        }
      />
    </div>
  );
}
