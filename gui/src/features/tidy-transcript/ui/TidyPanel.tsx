/**
 * The 整理版 control, under the corrector and built to the same shape.
 *
 * It shows before it writes, for the reason the corrector does and one more:
 * this deletes. The list on screen is the decision, not a preview of one, so
 * `idle` -- the reader looks and closes it -- is a perfectly good outcome.
 *
 * Offered removals start ticked. That is the opposite of the corrector's
 * phonetic tier and it is not an inconsistency: a removal is only ever
 * offered for a cue that is NOTHING BUT terms the user put on their own
 * list, so it is their rule firing rather than this module guessing.
 */

import { useEffect, useState } from "react";
import {
  describeRemoval,
  describeTidyOffer,
  useTidy,
} from "@/features/tidy-transcript/model/store";

/** The three files `write_pair` produces, in the order a reader wants them:
 *  the subtitle copy, the same thing as prose, then the record. */
const WRITTEN_FILES: ReadonlyArray<readonly [string, string]> = [
  ["tidy", "整理後的字幕"],
  ["reading", "整理後的純文字"],
  ["record", "刪掉哪些（可還原）"],
];

export function TidyPanel({
  source,
  onReveal,
}: {
  source: string;
  onReveal?: (path: string) => void;
}) {
  const phase = useTidy((state) => state.phase);
  const busy = useTidy((state) => state.busy);
  const error = useTidy((state) => state.error);
  const offer = useTidy((state) => state.offer);
  const accepted = useTidy((state) => state.accepted);
  const written = useTidy((state) => state.written);
  const fillers = useTidy((state) => state.fillers);
  const [managing, setManaging] = useState(false);
  const [draft, setDraft] = useState("");

  useEffect(() => {
    if (useTidy.getState().fillers === null) void useTidy.getState().loadFillers();
  }, []);

  // A transcript swapped underneath a live offer would leave the reader
  // deciding about cues in a file they are no longer looking at.
  useEffect(() => {
    if (useTidy.getState().source !== source) useTidy.getState().discard();
  }, [source]);

  const summary = describeTidyOffer(offer);
  const empty = fillers !== null && fillers.terms.length === 0;

  // Rendered in both phases rather than only in `idle` (D-80: a control that
  // appears and disappears teaches nobody where it lives). It matters here
  // beyond tidiness -- editing the list RE-ASKS, and looking at an offer is
  // exactly when a reader realises a term is missing from it.
  const manageButton = (
    <button
      type="button"
      className="mfp-tx__reveal"
      data-testid="fillers-open"
      onClick={() => setManaging(true)}
    >
      管理語助詞…
    </button>
  );

  return (
    <div className="mfp-fix" data-testid="tidy-panel" data-phase={phase}>
      {phase === "idle" && (
        <div className="mfp-fix__row">
          <button
            type="button"
            className="mfp-button"
            disabled={busy}
            data-testid="tidy-ask"
            onClick={() => void useTidy.getState().ask(source)}
          >
            {busy ? "檢查中…" : "整理版（去掉語助詞）…"}
          </button>
          <span className="mfp-asr__muted">
            只會先列出要刪的句子，原檔不會被改動。
            {empty && <> 目前語助詞清單是空的。</>}
            {fillers && fillers.terms.length > 0 && (
              <> 清單裡有 {fillers.terms.length} 個詞。</>
            )}
          </span>
          {manageButton}
        </div>
      )}

      {phase === "offered" && offer && (
        <div className="mfp-fix__offer">
          <p className="mfp-fix__summary" data-testid="tidy-summary">
            {summary}
          </p>

          {offer.removals.length > 0 && (
            <>
              <div className="mfp-fix__row">
                <button
                  type="button"
                  className="mfp-tx__reveal"
                  onClick={() => useTidy.getState().setAll(true)}
                >
                  全選
                </button>
                <button
                  type="button"
                  className="mfp-tx__reveal"
                  onClick={() => useTidy.getState().setAll(false)}
                >
                  全不選
                </button>
              </div>

              <ul className="mfp-fix__list" data-testid="tidy-list">
                {offer.removals.map((removal, index) => (
                  <li key={`${removal.cue}-${index}`} className="mfp-fix__item">
                    <label>
                      <input
                        type="checkbox"
                        checked={accepted.has(index)}
                        onChange={() => useTidy.getState().toggle(index)}
                        aria-label={describeRemoval(removal)}
                      />
                      <span className="mfp-fix__was">{describeRemoval(removal)}</span>
                    </label>
                  </li>
                ))}
              </ul>
            </>
          )}

          <div className="mfp-fix__row">
            <button
              type="button"
              className="mfp-button"
              disabled={busy || accepted.size === 0}
              data-testid="tidy-confirm"
              onClick={() => void useTidy.getState().confirm()}
            >
              {busy ? "寫入中…" : `確認並輸出（${accepted.size} 句）`}
            </button>
            <button
              type="button"
              className="mfp-tx__reveal"
              data-testid="tidy-discard"
              onClick={() => useTidy.getState().discard()}
            >
              先不要
            </button>
            {manageButton}
          </div>
        </div>
      )}

      {phase === "written" && written && (
        <div className="mfp-fix__written" data-testid="tidy-written">
          <p>
            整理版已輸出，刪掉 {written.removed} 句。
            {/* Said here rather than left implied: it is the reassurance the
                whole flow rests on, and the reader has just deleted things. */}
            <span className="mfp-asr__muted"> 原稿沒有動到。</span>
          </p>
          <ul className="mfp-fix__files">
            {WRITTEN_FILES.map(([key, label]) => {
              const path = written.written[key];
              return path ? (
                <li key={key}>
                  <button
                    type="button"
                    className="mfp-tx__reveal"
                    title={path}
                    onClick={() => onReveal?.(path)}
                  >
                    {label}
                  </button>
                </li>
              ) : null;
            })}
          </ul>
          <button
            type="button"
            className="mfp-tx__reveal"
            onClick={() => useTidy.getState().discard()}
          >
            完成
          </button>
        </div>
      )}

      {error && (
        <p className="mfp-tx__error" role="alert" data-testid="tidy-error">
          {error}
        </p>
      )}

      {managing && (
        <div className="mfp-fix__enrol" data-testid="fillers-editor">
          <p className="mfp-fix__summary">語助詞清單</p>
          <p className="mfp-asr__muted">
            只有整句都是清單裡的詞，那一句才會被刪。句子中間的「好」「那個」
            永遠不會被碰到。
          </p>
          <div className="mfp-fix__row">
            <input
              className="mfp-fix__input"
              value={draft}
              placeholder="要加入的語助詞"
              aria-label="要加入的語助詞"
              onChange={(event) => setDraft(event.target.value)}
            />
            <button
              type="button"
              className="mfp-button"
              disabled={busy || !draft.trim()}
              data-testid="filler-add"
              onClick={() => {
                void useTidy.getState().addFiller(draft);
                setDraft("");
              }}
            >
              加入
            </button>
            <button
              type="button"
              className="mfp-tx__reveal"
              disabled={busy}
              data-testid="filler-add-common"
              onClick={() => void useTidy.getState().addCommon()}
            >
              加入常見的
            </button>
          </div>
          <ul className="mfp-fix__list" data-testid="filler-terms">
            {(fillers?.terms ?? []).map((term) => (
              <li key={term} className="mfp-fix__item">
                <span className="mfp-fix__was">{term}</span>
                <button
                  type="button"
                  className="mfp-tx__reveal"
                  aria-label={`移除 ${term}`}
                  onClick={() => void useTidy.getState().removeFiller(term)}
                >
                  移除
                </button>
              </li>
            ))}
          </ul>
          <button
            type="button"
            className="mfp-tx__reveal"
            onClick={() => setManaging(false)}
          >
            關閉
          </button>
        </div>
      )}
    </div>
  );
}
