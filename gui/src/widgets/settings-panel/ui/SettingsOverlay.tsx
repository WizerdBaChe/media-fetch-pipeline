/**
 * 設定 as a MODE, not as a block wedged into the page.
 *
 * What this replaces, measured 2026-08-30: the panel rendered as a sibling
 * above `<main>`, so opening it squeezed `.mfp-main` from 598.8px to 0 while
 * the tabs and the toolbar stayed exactly where they were. The tabs still
 * worked -- they changed a filter on a table with no height -- so clicking one
 * did nothing a person could see, which is 「點了也沒反應」 word for word. Worse,
 * 「清除已完成」 and 「一鍵刪除紀錄」 stayed enabled the whole time: the controls
 * that destroy records were live while the user was editing the settings that
 * govern them.
 *
 * Why a modal layer rather than a fourth view (the 2026-08-30 ruling, and the
 * evidence behind it):
 *
 *   - D-80 already drew this line. 「工具」 was chosen over 「擴充功能」 because
 *     tools 「DO something to a video, which separates them from 設定」.
 *   - 設定 opens from INSIDE 逐字稿 and 文件翻譯. A thing reachable from within
 *     a sibling is not that sibling's sibling; it is a layer above.
 *   - Each tool takes a subject and produces a work product, which is why each
 *     carries a `restore` snapshot. 設定 has neither.
 *   - A tool's exit is a DESTINATION (回到佇列). This one's is a RESUMPTION --
 *     back to wherever you were, queue or workspace.
 *
 * So the state stays a boolean orthogonal to `view`, which is what it always
 * was; only the drawing changes. The surface behind is kept MOUNTED and made
 * `inert`: unmounting it would take a transcript that cost minutes to produce
 * down with it, and those snapshots are accepted behaviour that a layout fix
 * has no business overwriting.
 *
 * The status bar is deliberately NOT covered. It is the only line no view owns
 * and it is where a 3 GB download reports itself; hiding it behind 設定 would
 * recreate, one layer up, the exact defect `entities/background-job` exists to
 * end.
 */

import { useEffect, useRef } from "react";
import { SettingsPanel, type SettingsCategory } from "./SettingsPanel";

export function SettingsOverlay({
  onClose,
  initial,
}: {
  onClose: () => void;
  initial?: SettingsCategory;
}) {
  const surface = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    surface.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeRef.current();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  // Putting focus BACK is not done here, and the reason is worth writing down:
  // the surface behind becomes `inert` in the same commit that mounts this, so
  // by the time an effect here runs, the browser has already blurred whatever
  // opened 設定 and `document.activeElement` is `<body>`. There is nothing left
  // to remember. `MainPage` owns the control that opens this and restores focus
  // to it after the commit that removes `inert`.

  return (
    <div
      className="mfp-settings-mode"
      role="dialog"
      aria-modal="true"
      aria-label="設定"
      data-testid="settings-mode"
      tabIndex={-1}
      ref={surface}
    >
      <div className="mfp-settings-mode__body">
        <SettingsPanel onClose={onClose} initial={initial} />
      </div>
    </div>
  );
}
