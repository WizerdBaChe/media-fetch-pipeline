/**
 * The glossary, editable. One component, rendered in two places.
 *
 * 設定 shows it inline; 逐字稿 opens the SAME component in a dialog, which is
 * what the user asked for (2026-08-28) and also what keeps the two surfaces
 * from drifting -- there is one editor, and 「設定裡能改的」 and 「校正時能改
 * 的」 are the same sentence about the same list.
 *
 * Three properties this list has to have, because of what it IS:
 *
 *   * it decides what the corrector is ALLOWED to write, so a typo in it
 *     becomes a wrong word in every transcript corrected afterwards. That is
 *     why the entry that could only be appended to was a defect, not a
 *     missing convenience.
 *   * a delete confirms IN THE ROW rather than in a dialog. This component
 *     is itself inside a dialog half the time, and a modal over a modal is
 *     a trap the Escape key cannot reason about.
 *   * nothing is saved as you type. An input that writes on every keystroke
 *     would enrol 基, 基板, 基板上 while somebody was still typing.
 */

import { useEffect, useState } from "react";
import type { GlossaryEntry } from "@/shared/api/types";
import {
  describeEntry,
  joinAliases,
  parseAliases,
  useGlossary,
} from "@/entities/glossary/model/store";

interface Props {
  /** Show a file in its folder. Absent in a browser tab, where the path is
   *  still printed so it can be copied. */
  onReveal?: (path: string) => void;
  /** Called after any change lands, so a caller showing suggestions from
   *  this list can recompute them. The correction panel does; 設定 does not,
   *  because nothing there depends on the list. */
  onChanged?: () => void;
}

export function GlossaryEditor({ onReveal, onChanged }: Props) {
  const report = useGlossary((state) => state.report);
  const busy = useGlossary((state) => state.busy);
  const error = useGlossary((state) => state.error);

  useEffect(() => {
    if (report === null) void useGlossary.getState().load();
  }, [report]);

  const [term, setTerm] = useState("");
  const [aliases, setAliases] = useState("");
  const [note, setNote] = useState("");

  const add = async () => {
    if (!term.trim()) return;
    const ok = await useGlossary.getState().save({
      term,
      aliases: parseAliases(aliases),
      note,
    });
    if (!ok) return;
    setTerm("");
    setAliases("");
    setNote("");
    onChanged?.();
  };

  const entries = report?.entries ?? [];

  return (
    <div className="mfp-gloss" data-testid="glossary-editor">
      <p className="mfp-asr__muted">
        校正只會寫出這裡登記過的詞。空的詞庫不會產生任何建議，這是設計上的保證，
        不是還沒調好。
      </p>

      {report && (
        <p className="mfp-gloss__where">
          <code title={report.path}>{report.path}</code>
          {onReveal && (
            <button
              type="button"
              className="mfp-tx__reveal"
              onClick={() => onReveal(report.path)}
            >
              開啟位置
            </button>
          )}
          {!report.phoneticKeys && (
            <span className="mfp-asr__muted">
              （讀音比對目前無法使用，只會比對完全相同的寫法）
            </span>
          )}
        </p>
      )}

      {report === null ? (
        <p className="mfp-asr__muted" data-testid="glossary-loading">
          讀取詞庫中…
        </p>
      ) : entries.length === 0 ? (
        <p className="mfp-asr__muted" data-testid="glossary-empty">
          詞庫是空的。把常被聽錯的專有名詞加進來，下次校正就會認得。
        </p>
      ) : (
        <ul className="mfp-gloss__list" data-testid="glossary-list">
          {/* Column names, because a row that already has values shows no
              placeholders and 「基板 | 機板 | 備註」 does not say which box is
              which. Presentational: the inputs carry their own labels. */}
          <li className="mfp-gloss__head" aria-hidden="true">
            <span>正確的寫法</span>
            <span>看到的錯字</span>
            <span>備註</span>
          </li>
          {entries.map((entry) => (
            <Row
              key={entry.term}
              entry={entry}
              busy={busy}
              onChanged={onChanged}
            />
          ))}
        </ul>
      )}

      <div className="mfp-gloss__add">
        <span className="mfp-asr__part-name">加入新的術語</span>
        <input
          className="mfp-fix__input"
          value={term}
          placeholder="正確的寫法"
          aria-label="新術語的正確寫法"
          disabled={busy}
          onChange={(event) => setTerm(event.target.value)}
        />
        <input
          className="mfp-fix__input"
          value={aliases}
          placeholder="錯字，多個用、隔開"
          aria-label="新術語看到的錯字"
          disabled={busy}
          onChange={(event) => setAliases(event.target.value)}
        />
        <input
          className="mfp-fix__input"
          value={note}
          placeholder="備註（可留空）"
          aria-label="新術語的備註"
          disabled={busy}
          onChange={(event) => setNote(event.target.value)}
        />
        <button
          type="button"
          className="mfp-button"
          disabled={busy || !term.trim()}
          data-testid="glossary-add"
          onClick={() => void add()}
        >
          加入
        </button>
      </div>

      {error && (
        <p className="mfp-tx__error" role="alert" data-testid="glossary-error">
          {error}
        </p>
      )}
    </div>
  );
}

/**
 * One term. Edited in place, saved on a button, deleted after a confirmation
 * that lives in this row.
 *
 * The draft is local so a half-typed edit is never in the store, and it is
 * re-seeded from the entry whenever the server's copy changes -- otherwise a
 * failed save would leave the row showing text that is not on disk while the
 * error line said it had not been written.
 */
function Row({
  entry,
  busy,
  onChanged,
}: {
  entry: GlossaryEntry;
  busy: boolean;
  onChanged?: () => void;
}) {
  const [term, setTerm] = useState(entry.term);
  const [aliases, setAliases] = useState(joinAliases(entry.aliases));
  const [note, setNote] = useState(entry.note);
  const [confirming, setConfirming] = useState(false);

  useEffect(() => {
    setTerm(entry.term);
    setAliases(joinAliases(entry.aliases));
    setNote(entry.note);
  }, [entry]);

  const dirty =
    term !== entry.term ||
    aliases !== joinAliases(entry.aliases) ||
    note !== entry.note;

  const save = async () => {
    if (!term.trim()) return;
    const ok = await useGlossary.getState().save({
      original: entry.term,
      term,
      aliases: parseAliases(aliases),
      note,
    });
    if (ok) onChanged?.();
  };

  const remove = async () => {
    setConfirming(false);
    const ok = await useGlossary.getState().remove(entry.term);
    if (ok) onChanged?.();
  };

  return (
    <li className="mfp-gloss__row" data-term={entry.term}>
      <input
        className="mfp-fix__input"
        value={term}
        disabled={busy}
        aria-label={`${entry.term} 的正確寫法`}
        onChange={(event) => setTerm(event.target.value)}
      />
      <input
        className="mfp-fix__input"
        value={aliases}
        placeholder="看到的錯字，多個用、隔開"
        disabled={busy}
        aria-label={`${entry.term} 看到的錯字`}
        onChange={(event) => setAliases(event.target.value)}
      />
      <input
        className="mfp-fix__input"
        value={note}
        placeholder="備註"
        disabled={busy}
        aria-label={`${entry.term} 的備註`}
        onChange={(event) => setNote(event.target.value)}
      />

      {confirming ? (
        <span className="mfp-gloss__confirm" role="alert">
          刪掉「{entry.term}」？已經校正過的檔案不會變。
          <button
            type="button"
            className="mfp-button mfp-button--danger"
            disabled={busy}
            data-testid={`glossary-remove-yes-${entry.term}`}
            onClick={() => void remove()}
          >
            刪除
          </button>
          <button
            type="button"
            className="mfp-button"
            onClick={() => setConfirming(false)}
          >
            取消
          </button>
        </span>
      ) : (
        <>
          <button
            type="button"
            className="mfp-button"
            disabled={busy || !dirty || !term.trim()}
            data-testid={`glossary-save-${entry.term}`}
            onClick={() => void save()}
          >
            {dirty ? "儲存" : "已儲存"}
          </button>
          <button
            type="button"
            className="mfp-tx__reveal"
            disabled={busy}
            data-testid={`glossary-remove-${entry.term}`}
            onClick={() => setConfirming(true)}
          >
            刪除
          </button>
        </>
      )}

      {!dirty && !confirming && (
        <span className="mfp-asr__muted">{describeEntry(entry)}</span>
      )}
    </li>
  );
}
