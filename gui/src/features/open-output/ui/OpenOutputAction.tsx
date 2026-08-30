/**
 * 開啟檔案位置 / 複製路徑 — the capability G4 could not provide from a
 * browser (G6 §6).
 *
 * One component, two behaviours, chosen by feature detection:
 *
 *   desktop  → 開啟檔案位置, asks the main process to reveal the output
 *   browser  → 複製路徑, the honest capability a web page actually has
 *
 * Keeping both here rather than leaving 複製路徑 behind in `RowActions`
 * means the browser build never loses the fallback, and the two are
 * impossible to drift apart: they are the same button.
 *
 * **Both branches answer.** 複製路徑 used to be a bare
 * `void navigator.clipboard?.writeText(dir)` — no confirmation, and a
 * rejected clipboard (an insecure context is enough) looked exactly like a
 * successful copy. So the two buttons in this one file disagreed about
 * whether the user is owed an answer, while the comment below already said
 * a silent button is indistinguishable from a broken one.
 */

import { useEffect, useRef, useState } from "react";
import { Button } from "@/shared/ui/Button";
import { desktop, type DesktopApi } from "@/shared/lib/desktop";
import type { Task } from "@/shared/api/types";

type Flash = { kind: "ok" | "error"; text: string } | null;

/** How long a success sits there. Long enough to be read on a glance away
 *  from the button, short enough not to be mistaken for row state. */
const OK_VISIBLE_MS = 1600;

export function OpenOutputAction({ task }: { task: Task }) {
  const [flash, setFlash] = useState<Flash>(null);
  const timer = useRef<number | null>(null);
  const api = desktop();
  const dir = task.outputDir;

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );

  if (task.state !== "COMPLETED" || !dir) return null;

  const clearTimer = () => {
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = null;
  };

  /** Success is transient; a failure is not. An error the user has not dealt
   *  with must not time itself out of the way. */
  const succeed = (text: string) => {
    clearTimer();
    setFlash({ kind: "ok", text });
    timer.current = window.setTimeout(() => setFlash(null), OK_VISIBLE_MS);
  };
  const fail = (text: string) => {
    clearTimer();
    setFlash({ kind: "error", text });
  };

  const copy = async () => {
    try {
      // Not `clipboard?.` — an absent clipboard is a failure the user needs
      // to hear about, and optional chaining would swallow it into the
      // success path. `navigator.clipboard` is undefined outside a secure
      // context, which is a real way to serve this page.
      await navigator.clipboard.writeText(dir);
      succeed("已複製");
    } catch {
      fail("無法複製路徑，請手動選取。");
    }
  };

  // Takes the bridge as an argument rather than closing over `api`: the
  // caller sits inside the branch where TypeScript has already proven it is
  // there, so no non-null assertion is needed to say what is already known.
  const reveal = async (bridge: DesktopApi) => {
    clearTimer();
    setFlash(null);
    const result = await bridge.showInFolder(dir);
    // A refusal must be visible. The containment check lives in the main
    // process (§6.1) and can legitimately say no; a button that silently
    // does nothing is indistinguishable from a broken one.
    if (!result.ok) fail(result.error ?? "無法開啟檔案位置。");
  };

  return (
    <>
      {api ? (
        <Button variant="ghost" onClick={() => void reveal(api)} title={dir}>
          開啟檔案位置
        </Button>
      ) : (
        <Button variant="ghost" onClick={() => void copy()} title={dir}>
          複製路徑
        </Button>
      )}
      {flash && (
        <span
          className={flash.kind === "ok" ? "mfp-row__ok" : "mfp-row__error"}
          // `status` rather than `alert` for a success: a confirmation that
          // interrupts a screen reader mid-sentence is worse than no
          // confirmation. A refusal does interrupt, deliberately.
          role={flash.kind === "ok" ? "status" : "alert"}
          title={flash.text}
        >
          {flash.kind === "ok" ? "✓" : "⚠"} {flash.text}
        </span>
      )}
    </>
  );
}
