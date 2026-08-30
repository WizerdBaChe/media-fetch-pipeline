/**
 * The four steps the user ruled on, in order and visible.
 *
 *     the original, untouched
 *        └ 「智慧校正…」          ask — nothing is written
 *             └ a diff, per span   review — guesses arrive UNTICKED
 *                  └ fix by hand + enrol
 *                       └ 「確認」   two copies written, original intact
 *
 * The `idle` state is not a waiting room. Closing this without confirming is
 * the correct outcome whenever the suggestions are wrong, and the panel is
 * built so that path costs one click.
 *
 * Every proposal shows WHY it is being offered -- the enrolled spelling, or
 * the shared reading plus the engine's own confidence. A list of before/after
 * pairs with no reasons is unreviewable, and an unreviewable list gets
 * accepted wholesale, which is the failure this design exists to prevent.
 */

import { useEffect, useState } from "react";
import { GlossaryEditor } from "@/entities/glossary/ui/GlossaryEditor";
import { useGlossary } from "@/entities/glossary/model/store";
import {
  describeOffer,
  describeProposal,
  highlightOf,
  useCorrect,
} from "@/features/correct-transcript/model/store";

interface Props {
  /** The caption file on disk. Never a media path: correcting is something
   *  asked for once a transcript is in front of the reader. */
  source: string;
  /** Show a file in its folder. Absent in tests and in any shell without
   *  one, where the paths are still printed. */
  onReveal?: (path: string) => void;
}

export function CorrectionPanel({ source, onReveal }: Props) {
  const phase = useCorrect((state) => state.phase);
  const busy = useCorrect((state) => state.busy);
  const error = useCorrect((state) => state.error);
  const offer = useCorrect((state) => state.offer);
  const accepted = useCorrect((state) => state.accepted);
  const written = useCorrect((state) => state.written);
  const glossary = useGlossary((state) => state.report);

  const [term, setTerm] = useState("");
  const [wrong, setWrong] = useState("");
  /** The full editor, over this panel. Not a second copy of it -- the same
   *  component 設定 renders, so 「在這裡改」 and 「去設定改」 cannot become two
   *  behaviours that have to be kept in step. */
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    if (useGlossary.getState().report === null) void useGlossary.getState().load();
  }, []);

  // A transcript swapped underneath a live offer would leave the reader
  // reviewing changes to a file they are no longer looking at.
  useEffect(() => {
    if (useCorrect.getState().source !== source) useCorrect.getState().discard();
  }, [source]);

  const summary = describeOffer(offer);

  return (
    <div className="mfp-fix" data-testid="correction-panel" data-phase={phase}>
      {phase === "idle" && (
        <div className="mfp-fix__row">
          <button
            type="button"
            className="mfp-button"
            disabled={busy}
            data-testid="correct-ask"
            onClick={() => void useCorrect.getState().ask(source)}
          >
            {busy ? "檢查中…" : "智慧校正…"}
          </button>
          <span className="mfp-asr__muted">
            只會先列出建議，原檔不會被改動。
            {glossary && glossary.entries.length === 0 && (
              <> 目前詞庫是空的。</>
            )}
            {glossary && glossary.entries.length > 0 && (
              <> 詞庫裡有 {glossary.entries.length} 個術語。</>
            )}
          </span>
          <button
            type="button"
            className="mfp-tx__reveal"
            data-testid="glossary-open"
            onClick={() => setEditing(true)}
          >
            管理詞庫…
          </button>
        </div>
      )}

      {phase === "offered" && offer && (
        <div className="mfp-fix__offer">
          <p className="mfp-fix__summary" data-testid="correct-summary">
            {summary}
          </p>

          {offer.proposals.length > 0 && (
            <>
              <div className="mfp-fix__row">
                <button
                  type="button"
                  className="mfp-tx__reveal"
                  onClick={() => useCorrect.getState().setAll(true)}
                >
                  全選
                </button>
                <button
                  type="button"
                  className="mfp-tx__reveal"
                  onClick={() => useCorrect.getState().setAll(false)}
                >
                  全不選
                </button>
              </div>

              {/* The corrected sentence, with the change marked -- not a
                  before/after pair. Two nearly-identical Chinese sentences
                  side by side are unreadable at exactly the moment it
                  matters, and what the reader is judging is the sentence
                  they would end up with (user ruling 2026-08-28). */}
              <ul className="mfp-fix__list" data-testid="correct-list">
                {offer.proposals.map((proposal, index) => {
                  const shown = highlightOf(offer, index);
                  return (
                    <li key={`${proposal.cue}-${proposal.start}-${index}`}>
                      <label className="mfp-fix__item" data-tier={proposal.tier}>
                        <input
                          type="checkbox"
                          checked={accepted.has(index)}
                          disabled={busy}
                          onChange={() => useCorrect.getState().toggle(index)}
                          aria-label={`第 ${proposal.cue + 1} 句：${proposal.was} 改成 ${proposal.now}`}
                        />
                        <span className="mfp-fix__where mfp-mono">
                          第 {proposal.cue + 1} 句
                        </span>
                        {shown ? (
                          <span
                            className="mfp-fix__line"
                            data-testid={`correct-preview-${index}`}
                          >
                            {shown.head}
                            <mark className="mfp-fix__mark">{shown.mark}</mark>
                            {shown.tail}
                          </span>
                        ) : (
                          // Only when the file no longer reads as the
                          // proposal says. Marking characters by an offset
                          // that no longer lines up would highlight the
                          // wrong word, which is worse than showing a pair.
                          <span className="mfp-fix__line">
                            <span className="mfp-fix__was">{proposal.was}</span>
                            <span className="mfp-fix__arrow">→</span>
                            <span className="mfp-fix__now">{proposal.now}</span>
                          </span>
                        )}
                        <span className="mfp-asr__muted">
                          原本是「{proposal.was}」，{describeProposal(proposal)}
                          {proposal.key ? `（${proposal.key}）` : ""}
                        </span>
                      </label>
                    </li>
                  );
                })}
              </ul>
            </>
          )}

          {/* Enrolling is the only thing here that makes the next run better,
              so it is offered whether or not this run found anything -- a
              transcript with a wrong term and an empty glossary produces no
              suggestions at all, and that is exactly when a reader most needs
              somewhere to put the correction they just spotted. */}
          <div className="mfp-fix__enrol">
            <span className="mfp-asr__part-name">加入詞庫</span>
            <input
              className="mfp-fix__input"
              value={term}
              placeholder="正確的寫法"
              disabled={busy}
              aria-label="正確的寫法"
              onChange={(event) => setTerm(event.target.value)}
            />
            <input
              className="mfp-fix__input"
              value={wrong}
              placeholder="看到的錯字（可留空）"
              disabled={busy}
              aria-label="看到的錯字"
              onChange={(event) => setWrong(event.target.value)}
            />
            <button
              type="button"
              className="mfp-button"
              disabled={busy || !term.trim()}
              data-testid="correct-enrol"
              onClick={() => {
                void useCorrect.getState().enrol(term, wrong);
                setTerm("");
                setWrong("");
              }}
            >
              記起來
            </button>
            {/* Typed a term wrong, or want it gone again? The editor is one
                click away rather than a trip to 設定 and back -- the moment
                a reader notices a bad entry is while they are looking at
                what it produced. */}
            <button
              type="button"
              className="mfp-tx__reveal"
              data-testid="glossary-open-offered"
              onClick={() => setEditing(true)}
            >
              管理詞庫…
            </button>
          </div>

          <div className="mfp-fix__row">
            <button
              type="button"
              className="mfp-button mfp-button--primary"
              disabled={busy}
              data-testid="correct-confirm"
              onClick={() => void useCorrect.getState().confirm()}
            >
              {busy ? "寫入中…" : `確認並輸出（${accepted.size} 處）`}
            </button>
            <button
              type="button"
              className="mfp-button"
              disabled={busy}
              data-testid="correct-discard"
              onClick={() => useCorrect.getState().discard()}
            >
              不要校正
            </button>
          </div>
        </div>
      )}

      {phase === "written" && written && (
        <div className="mfp-fix__written" data-testid="correct-written">
          <p>
            套用了 {written.applied} 處。
            <strong> 原檔沒有被更動。</strong>
          </p>
          <ul className="mfp-fix__files">
            {Object.entries(written.written).map(([label, path]) => (
              <li key={label}>
                <span className="mfp-asr__part-name">{FILE_LABELS[label] ?? label}</span>
                <code title={path}>{path}</code>
                {onReveal && (
                  <button
                    type="button"
                    className="mfp-tx__reveal"
                    onClick={() => onReveal(path)}
                  >
                    開啟位置
                  </button>
                )}
              </li>
            ))}
          </ul>
          <button
            type="button"
            className="mfp-button"
            onClick={() => useCorrect.getState().discard()}
          >
            完成
          </button>
        </div>
      )}

      {error && (
        <span className="mfp-tx__error" role="alert" data-testid="correct-error">
          {error}
        </span>
      )}

      {editing && (
        <GlossaryDialog
          onClose={() => {
            setEditing(false);
            // What was edited changes what is being offered. Recomputing on
            // close rather than on every keystroke inside the dialog: the
            // list underneath must not shuffle while somebody is typing.
            void useCorrect.getState().reask();
          }}
          onReveal={onReveal}
        />
      )}
    </div>
  );
}

/**
 * The editor, over this panel.
 *
 * A real modal rather than a popover: editing the whitelist is a decision
 * with consequences for every future transcript, and the list of suggestions
 * underneath is about to change because of it. Escape and a click on the
 * backdrop both close it, because a mouse user will try one and a keyboard
 * user has only the other.
 */
function GlossaryDialog({
  onClose,
  onReveal,
}: {
  onClose: () => void;
  onReveal?: (path: string) => void;
}) {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <div className="mfp-modal__backdrop" onClick={onClose} role="presentation">
      <div
        className="mfp-modal mfp-modal--wide"
        role="dialog"
        aria-modal="true"
        aria-label="校正詞庫"
        data-testid="glossary-dialog"
        onClick={(event) => event.stopPropagation()}
      >
        <h2 className="mfp-modal__title">校正詞庫</h2>
        <div className="mfp-modal__body">
          <GlossaryEditor onReveal={onReveal} />
        </div>
        <div className="mfp-modal__actions">
          <button type="button" className="mfp-button" onClick={onClose}>
            關閉
          </button>
        </div>
      </div>
    </div>
  );
}

/** The four files, named for what they are FOR rather than what they are. */
const FILE_LABELS: Record<string, string> = {
  corrected: "校正後的字幕",
  reading: "校正後的純文字",
  record: "校正紀錄",
  diff: "改了哪些（可讀）",
};
