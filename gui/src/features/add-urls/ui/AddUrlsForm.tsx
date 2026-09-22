/**
 * Paste-anything input (PSM §5, §6).
 *
 * Flow: parse (pure, free, nothing enters the queue) → decide → either add
 * straight away or open the confirmation dialog. `/v1/input/parse` exists
 * exactly for this: seeing the change report before anything is committed.
 *
 * The paste box is NEVER cleared unless something actually entered the queue —
 * the user's text is the only copy, and clearing it after "沒有找到可用的網址"
 * destroys the thing they would need to fix (§5 error paths).
 */

import { useState } from "react";
import { Button } from "@/shared/ui/Button";
import { useNarrow } from "@/shared/lib/useNarrow";
import type { ParseResult } from "@/shared/api/types";
import { useTaskStore } from "@/entities/task/model/store";
import { globalWriteSubsOf, useConfigStore } from "@/entities/config/model/store";
import { inlineNote, needsConfirmation } from "../model/preview";
import { ParsePreviewDialog } from "./ParsePreviewDialog";
import { SupportedSitesInfo } from "./SupportedSitesInfo";

/**
 * The hint, at two lengths.
 *
 * The box is one row tall by CSS while the element declares two, so a
 * placeholder that wraps has its second line cut off rather than
 * growing the field -- which is what a narrow window did to the long
 * form. Shortening it for narrow windows keeps the detail where there
 * is room for it instead of removing it for everyone.
 */
const HINT_FULL = "貼上網址（可多筆、可含文字、黏在一起也可以）";
const HINT_SHORT = "貼上網址（可多筆）";

const SUBS_LABEL = "一併存字幕";
const SUBS_LABEL_SHORT = "字幕";
const SUBS_HINT =
  "有字幕軌的影片，會在影片旁邊多存一個字幕檔，語言是影片裡實際說的那一種。" +
  "沒有字幕軌的影片照常下載，不會失敗。這裡的勾選只影響這一次加入的網址；" +
  "預設值在設定裡改。";

export function AddUrlsForm() {
  const narrow = useNarrow();
  const placeholder = narrow ? HINT_SHORT : HINT_FULL;
  const preview = useTaskStore((state) => state.preview);
  const add = useTaskStore((state) => state.add);
  const globalWriteSubs = useConfigStore((state) => globalWriteSubsOf(state.config));

  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<ParseResult | null>(null);
  const [note, setNote] = useState("");
  /**
   * The per-paste override, tri-state: `null` is 「沒有覆寫」 and the rows
   * follow the global setting for the rest of their lives, so changing the
   * default later still reaches them.
   *
   * The box therefore SHOWS the global value while the override is null, and
   * an override that lands back on the global value collapses to null again —
   * two identical-looking ticks must not mean two different things.
   */
  const [writeSubs, setWriteSubs] = useState<boolean | null>(null);
  const writeSubsChecked = writeSubs ?? globalWriteSubs;

  const commit = async () => {
    setBusy(true);
    try {
      const report = await add(text, writeSubs);
      if (!report) {
        // The add failed. Keep the dialog and the text exactly as they were:
        // the box is the only copy of what the user typed, and clearing it on
        // failure destroys the thing they would retry with.
        setNote("");
        return;
      }
      setPending(null);
      setText("");
      // The override goes with the text it accompanied. It is a decision about
      // THIS paste, and a tick left standing from three pastes ago is a pin
      // nobody remembers setting — the global setting is where "always" lives.
      setWriteSubs(null);
      // The note set by submit() survives on purpose — it is the record of
      // what was cleaned up, and the next submit clears it.
    } finally {
      setBusy(false);
    }
  };

  const submit = async () => {
    if (!text.trim() || busy) return;
    setBusy(true);
    setNote("");
    try {
      const result = await preview(text);
      if (!result) return; // the failure is already on the status bar
      if (needsConfirmation(result)) {
        setPending(result);
        return;
      }
      setNote(inlineNote(result));
    } finally {
      setBusy(false);
    }
    await commit();
  };

  return (
    <div className="mfp-add">
      <form
        className="mfp-add__row"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <textarea
          className="mfp-add__input"
          value={text}
          rows={2}
          placeholder={placeholder}
          /* The full sentence stays reachable as a tooltip and as the
             accessible name never changes, so narrowing the window costs the
             hint's detail and nothing else. */
          title={HINT_FULL}
          aria-label="貼上網址"
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            // Enter submits; Shift+Enter is a newline, because multi-line
            // paste is the normal case here.
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              void submit();
            }
          }}
        />
        {/* Beside the button that consumes it, not in a menu: this is a
            decision about the paste in the box, and the settings panel already
            holds the "always" version of it. On a narrow window the visible
            word shortens the same way the placeholder does — the accessible
            name stays the full sentence at every width. */}
        <label className="mfp-add__subs" title={SUBS_HINT}>
          <input
            type="checkbox"
            checked={writeSubsChecked}
            aria-label={SUBS_LABEL}
            onChange={(event) => {
              // Back on the global value means no override at all, so a later
              // change to the default still reaches these rows.
              const next = event.target.checked;
              setWriteSubs(next === globalWriteSubs ? null : next);
            }}
          />
          <span aria-hidden="true">{narrow ? SUBS_LABEL_SHORT : SUBS_LABEL}</span>
        </label>
        <Button type="submit" variant="primary" disabled={busy || !text.trim()}>
          {busy ? "處理中…" : "加入佇列"}
        </Button>
        {/* Next to the box it describes, not in a menu: the question is
            "what can I paste HERE", and it is asked while looking at this
            field. Narrowing the window shortens the placeholder, so on a
            narrow window this is the only place the detail still lives. */}
        <SupportedSitesInfo />
      </form>

      {/* A floating hint (see theme.css): out of flow, so showing or hiding
          it still moves nothing, and an empty one no longer holds a blank
          quarter of the header open. Always mounted, never conditionally
          rendered -- a live region that appears together with its text tends
          not to be announced. */}
      <p className="mfp-add__note" data-testid="add-note" role="status">
        {note}
      </p>

      <ParsePreviewDialog
        result={pending}
        busy={busy}
        onConfirm={() => void commit()}
        onCancel={() => setPending(null)}
      />
    </div>
  );
}
