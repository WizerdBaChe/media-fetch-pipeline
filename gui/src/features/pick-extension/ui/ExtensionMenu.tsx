/**
 * 延伸工具: the first of the two levels, and the place later tools land.
 *
 * The name is a decision, not a default. 「擴充功能」 reads as something
 * installed from outside and collides with 「擴充設定」 -- settings FOR
 * extensions -- which is a different idea again. 「工具」 says these are
 * things that DO something to a source the user names, which separates them
 * from 設定 without further explanation.
 *
 * That sentence said 「to a video」 until 2026-09-01 and was already false:
 * 貼文解說 takes a link to a page of photographs, not a video. Corrected in
 * the same commit that registered the fourth tool -- 逐字稿 among them, since
 * removed 2026-09-16 (D-162) -- because a classification whose stated reason
 * contradicts its own registry teaches the next reader the wrong tier, which
 * is how a verb ends up in the wrong menu and then in the wrong module.
 *
 * Two doors, one menu. From the header it asks for a source; from a queue
 * row it already has one. Everything below the menu is the tool's own.
 *
 * A FEATURE, not a widget. It composes nothing -- no entity, no store, no
 * request -- it is one action a person takes plus the list of tools they can
 * take it on, which is what `features/` is for. Sitting in `widgets/` is
 * what made `QueueTable` import it sideways, and a widget importing a widget
 * is the one thing FSD's layering actually forbids. From here both callers
 * reach DOWN a layer and the import graph stays a tree.
 */

import { useEffect, useId, useRef, useState } from "react";

export interface ExtensionDef {
  id: string;
  label: string;
  hint: string;
}

export const EXTENSIONS: readonly ExtensionDef[] = [
  {
    id: "quotestack",
    label: "引用長圖",
    hint: "把影片與字幕疊成一張可以直接貼出去的長圖",
  },
  {
    id: "brief",
    label: "貼文解說",
    hint: "把貼文的圖片抓下來，交給你的 AI 看，再把解說存回同一個資料夾",
  },
];

export interface ExtensionMenuProps {
  /** What the button says. The row version is an icon-sized label. */
  label?: string;
  compact?: boolean;
  /** Why this menu can do nothing here, when it can do nothing. */
  disabledReason?: string;
  /**
   * `id -> why this ONE tool cannot be used here`. Rendered disabled with
   * the reason as its title rather than hidden: a control that appears and
   * disappears teaches nobody where it lives (D-80).
   *
   * Per instance rather than a field on `ExtensionDef`, because
   * applicability is a fact about the DOOR and not about the tool -- 貼文解說
   * takes a post URL the user names, so it is offered from the header and
   * not from a queue row, which only ever knows about a downloaded video. A
   * field on the definition could not say something different in the two
   * places it is read.
   */
  unavailable?: Readonly<Record<string, string>>;
  onPick: (id: string) => void;
}

export function ExtensionMenu({
  label = "延伸工具",
  compact = false,
  disabledReason,
  unavailable,
  onPick,
}: ExtensionMenuProps) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement | null>(null);
  // One id per instance, and the menu items derive theirs from it. A
  // disabled control's reason lived only in `title`, which a keyboard, a
  // touch screen and a screen reader all miss -- and the reason is the whole
  // difference between 「還不能用」 and 「壞了」 (UX walkthrough F6). `useId`
  // rather than a counter because twelve of these render at once, one per
  // queue row, and two of them sharing an id would describe the wrong button.
  const reasonId = useId();

  // Close on an outside click or Escape. A popover that only closes by
  // choosing something is a popover people learn to avoid opening.
  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="mfp-ext" ref={root}>
      <button
        type="button"
        className={compact ? "mfp-ext__button mfp-ext__button--compact" : "mfp-ext__button"}
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={Boolean(disabledReason)}
        title={disabledReason ?? "對這支影片使用延伸工具"}
        aria-describedby={disabledReason ? reasonId : undefined}
        onClick={() => setOpen((was) => !was)}
      >
        {label}
      </button>
      {/* Off-screen rather than visible: this button sits in a queue row
          whose columns are measured, and a reason printed in the cell would
          move every control in it. The `title` stays for the mouse. */}
      {disabledReason && (
        <span id={reasonId} className="mfp-sr-only">
          {disabledReason}
        </span>
      )}

      {open && (
        <ul className="mfp-ext__menu" role="menu" data-testid="extension-menu">
          {EXTENSIONS.map((tool) => (
            <li key={tool.id} role="none">
              <button
                type="button"
                role="menuitem"
                disabled={Boolean(unavailable?.[tool.id])}
                title={unavailable?.[tool.id]}
                aria-describedby={
                  unavailable?.[tool.id] ? `${reasonId}-${tool.id}` : undefined
                }
                onClick={() => {
                  setOpen(false);
                  onPick(tool.id);
                }}
              >
                <strong>{tool.label}</strong>
                {/* VISIBLE here, unlike the trigger: a menu row has space
                    under its name, and a reader who cannot hover is the
                    reader this menu is hardest for. BESIDE the tool's own
                    hint, not instead of it -- 停用不隱藏 (D-80) exists so
                    people learn what lives here, and a disabled row that
                    stops saying what the tool does teaches nothing. */}
                <em>
                  {tool.hint}
                  {unavailable?.[tool.id] && (
                    <span className="mfp-ext__why" id={`${reasonId}-${tool.id}`}>
                      {unavailable[tool.id]}
                    </span>
                  )}
                </em>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
