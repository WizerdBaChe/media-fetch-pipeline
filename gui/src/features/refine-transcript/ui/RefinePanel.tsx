/**
 * 整理這份逐字稿：一個面板、兩段可勾、一份成品。
 *
 *     原稿，沒有被動過
 *        └ 勾要做哪幾段 → 「校正並整理…」   問，什麼都還沒寫
 *             └ 校正建議（推測的不預先打勾）
 *               要刪的句子（預設打勾，因為那是你自己的清單在動作）
 *                  └ 「確認並輸出」          寫出一份，原稿完好
 *
 * 這裡取代了兩個各自獨立的面板。它們從來不是互斥的功能，但寫法讓它們
 * 變成互斥：兩個都指著原稿，做完哪一個都會拿到一份缺了另一半的兄弟檔。
 *
 * `idle` 不是等候室。建議不對的時候，關掉它就是正確的結果，而這條路
 * 只要一次點擊。
 *
 * 每一項校正都寫出**為什麼**被提出來——登記過的寫法，或是共同的讀音加上
 * 引擎自己的信心。一份沒有理由的 before/after 清單是無法審的，而無法審
 * 的清單會被整批接受，那正是這個設計要防的事。
 */

import { useEffect, useState } from "react";
import { GlossaryEditor } from "@/entities/glossary/ui/GlossaryEditor";
import { useGlossary } from "@/entities/glossary/model/store";
import {
  describeAction,
  describeCorrections,
  describeProposal,
  describeRemoval,
  describeRemovals,
  highlightOf,
  useRefine,
} from "@/features/refine-transcript/model/store";
import type { RefineStage } from "@/shared/api/types";

interface Props {
  /** 磁碟上的字幕檔。永遠不是媒體檔：整理是逐字稿already在讀者面前之後
   *  才會被要求的事。 */
  source: string;
  /** 開啟檔案位置。測試裡和沒有殼的環境裡不存在，路徑仍然印得出來。 */
  onReveal?: (path: string) => void;
}

/** 寫出來的檔案，用「這是拿來做什麼的」命名，而不是「這是什麼」。
 *  `result` 只有兩段都跑時才會出現；單獨跑一段時沿用原本的鍵。 */
const FILE_LABELS: Record<string, string> = {
  result: "校正並整理後的字幕",
  corrected: "校正後的字幕",
  tidy: "整理後的字幕",
  reading: "純文字（給人讀的）",
  record: "紀錄（可還原回原稿）",
  diff: "改了哪些（可讀）",
};

export function RefinePanel({ source, onReveal }: Props) {
  const phase = useRefine((state) => state.phase);
  const busy = useRefine((state) => state.busy);
  const error = useRefine((state) => state.error);
  const stages = useRefine((state) => state.stages);
  const offer = useRefine((state) => state.offer);
  const acceptedCorrections = useRefine((state) => state.acceptedCorrections);
  const acceptedRemovals = useRefine((state) => state.acceptedRemovals);
  const written = useRefine((state) => state.written);
  const fillers = useRefine((state) => state.fillers);
  const glossary = useGlossary((state) => state.report);

  const [term, setTerm] = useState("");
  const [wrong, setWrong] = useState("");
  const [draft, setDraft] = useState("");
  /** 完整的編輯器，蓋在這個面板上。不是它的第二份拷貝——就是 設定 用的
   *  那個元件，所以「在這裡改」跟「去設定改」不會變成兩種行為。 */
  const [editing, setEditing] = useState(false);
  const [managing, setManaging] = useState(false);

  useEffect(() => {
    if (useGlossary.getState().report === null) void useGlossary.getState().load();
    if (useRefine.getState().fillers === null) void useRefine.getState().loadFillers();
  }, []);

  // 逐字稿在還有活的建議時被抽換掉，會讓讀者在替一個他們已經不在看的檔案
  // 做決定。
  useEffect(() => {
    if (useRefine.getState().source !== source) useRefine.getState().discard();
  }, [source]);

  const correcting = stages.has("correct");
  const tidying = stages.has("tidy");
  const nothingTicked = stages.size === 0;

  /** 兩段的勾選。在 `idle` 和 `offered` 都畫得出來，理由跟 D-80 一樣：
   *  會出現又會消失的控制項教不會任何人它住在哪裡。這裡還有第二個理由
   *  ——改勾選會重問，而看著建議的當下正是讀者發現「我其實只想要另一半」
   *  的時候。 */
  const stagePicker = (
    <fieldset className="mfp-fix__stages" data-testid="refine-stages">
      <legend className="mfp-asr__part-name">要做哪幾段</legend>
      {(
        [
          ["correct", "校正術語", "只會改詞庫裡登記過的詞"],
          ["tidy", "去掉語助詞", "只刪整句都是語助詞的句子"],
        ] as ReadonlyArray<readonly [RefineStage, string, string]>
      ).map(([stage, label, hint]) => (
        <label key={stage} className="mfp-fix__stage" data-stage={stage}>
          <input
            type="checkbox"
            checked={stages.has(stage)}
            disabled={busy}
            data-testid={`refine-stage-${stage}`}
            onChange={(event) =>
              void useRefine.getState().setStage(stage, event.target.checked)
            }
          />
          <span>{label}</span>
          <span className="mfp-asr__muted">{hint}</span>
        </label>
      ))}
      {/* 兩段都做時才說得上順序，而順序不是使用者的選擇：先校正再整理，
          否則刪掉的紀錄會用一個使用者手上沒有的檔案在編號。 */}
      {correcting && tidying && (
        <span className="mfp-asr__muted" data-testid="refine-order">
          會先校正再整理，輸出一份。
        </span>
      )}
    </fieldset>
  );

  return (
    <div className="mfp-fix" data-testid="refine-panel" data-phase={phase}>
      {phase === "idle" && (
        <>
          {stagePicker}
          <div className="mfp-fix__row">
            <button
              type="button"
              className="mfp-button"
              disabled={busy || nothingTicked}
              data-testid="refine-ask"
              onClick={() => void useRefine.getState().ask(source)}
            >
              {busy ? "檢查中…" : describeAction(stages)}
            </button>
            <span className="mfp-asr__muted">
              只會先列出來，原檔不會被改動。
              {correcting && glossary && (
                <>
                  {" "}
                  {glossary.entries.length === 0
                    ? "目前詞庫是空的。"
                    : `詞庫裡有 ${glossary.entries.length} 個術語。`}
                </>
              )}
              {tidying && fillers && (
                <>
                  {" "}
                  {fillers.terms.length === 0
                    ? "目前語助詞清單是空的。"
                    : `語助詞清單裡有 ${fillers.terms.length} 個詞。`}
                </>
              )}
            </span>
            {correcting && (
              <button
                type="button"
                className="mfp-tx__reveal"
                data-testid="glossary-open"
                onClick={() => setEditing(true)}
              >
                管理詞庫…
              </button>
            )}
            {tidying && (
              <button
                type="button"
                className="mfp-tx__reveal"
                data-testid="fillers-open"
                onClick={() => setManaging(true)}
              >
                管理語助詞…
              </button>
            )}
          </div>
        </>
      )}

      {phase === "offered" && offer && (
        <div className="mfp-fix__offer">
          {stagePicker}

          {correcting && (
            <section data-testid="refine-correct-section">
              <p className="mfp-fix__summary" data-testid="correct-summary">
                {describeCorrections(offer)}
              </p>

              {offer.proposals.length > 0 && (
                <>
                  <div className="mfp-fix__row">
                    <button
                      type="button"
                      className="mfp-tx__reveal"
                      onClick={() => useRefine.getState().setAllCorrections(true)}
                    >
                      全選
                    </button>
                    <button
                      type="button"
                      className="mfp-tx__reveal"
                      onClick={() => useRefine.getState().setAllCorrections(false)}
                    >
                      全不選
                    </button>
                  </div>

                  {/* 改好之後的句子，標出改動處——不是 before/after 兩行。
                      讀者在判斷的是他們最後會拿到的那句話。 */}
                  <ul className="mfp-fix__list" data-testid="correct-list">
                    {offer.proposals.map((proposal, index) => {
                      const shown = highlightOf(offer, index);
                      return (
                        <li key={`${proposal.cue}-${proposal.start}-${index}`}>
                          <label className="mfp-fix__item" data-tier={proposal.tier}>
                            <input
                              type="checkbox"
                              checked={acceptedCorrections.has(index)}
                              disabled={busy}
                              onChange={() =>
                                useRefine.getState().toggleCorrection(index)
                              }
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
                              // 只有在檔案已經不是建議說的那樣時才走這裡。
                              // 用對不上的偏移量去標記，會標到錯的字。
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

              {/* 登記是這裡唯一會讓「下一次」變好的動作，所以不管這一次有
                  沒有找到東西都提供——一份有錯字而詞庫是空的稿子完全不會
                  產生建議，而那正是讀者最需要有地方放他剛發現的更正的時候。 */}
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
                    void useRefine.getState().enrol(term, wrong);
                    setTerm("");
                    setWrong("");
                  }}
                >
                  記起來
                </button>
                <button
                  type="button"
                  className="mfp-tx__reveal"
                  data-testid="glossary-open-offered"
                  onClick={() => setEditing(true)}
                >
                  管理詞庫…
                </button>
              </div>
            </section>
          )}

          {tidying && (
            <section data-testid="refine-tidy-section">
              <p className="mfp-fix__summary" data-testid="tidy-summary">
                {describeRemovals(offer)}
              </p>

              {offer.removals.length > 0 && (
                <>
                  <div className="mfp-fix__row">
                    <button
                      type="button"
                      className="mfp-tx__reveal"
                      onClick={() => useRefine.getState().setAllRemovals(true)}
                    >
                      全選
                    </button>
                    <button
                      type="button"
                      className="mfp-tx__reveal"
                      onClick={() => useRefine.getState().setAllRemovals(false)}
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
                            checked={acceptedRemovals.has(index)}
                            disabled={busy}
                            onChange={() => useRefine.getState().toggleRemoval(index)}
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
                  className="mfp-tx__reveal"
                  data-testid="fillers-open-offered"
                  onClick={() => setManaging(true)}
                >
                  管理語助詞…
                </button>
              </div>
            </section>
          )}

          <div className="mfp-fix__row">
            <button
              type="button"
              className="mfp-button mfp-button--primary"
              disabled={busy || nothingTicked}
              data-testid="refine-confirm"
              onClick={() => void useRefine.getState().confirm()}
            >
              {busy ? "寫入中…" : `確認並輸出（${describeChosen(
                correcting ? acceptedCorrections.size : null,
                tidying ? acceptedRemovals.size : null,
              )}）`}
            </button>
            <button
              type="button"
              className="mfp-button"
              disabled={busy}
              data-testid="refine-discard"
              onClick={() => useRefine.getState().discard()}
            >
              先不要
            </button>
          </div>
        </div>
      )}

      {phase === "written" && written && (
        <div className="mfp-fix__written" data-testid="refine-written">
          <p>
            {describeWritten(written.stages, written.corrected, written.removed)}
            {/* 講出來而不是留給人猜：讀者剛剛刪掉了東西，而這是整個流程
                立足的那句話。 */}
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
            onClick={() => useRefine.getState().discard()}
          >
            完成
          </button>
        </div>
      )}

      {nothingTicked && phase !== "written" && (
        <p className="mfp-asr__muted" data-testid="refine-nothing-ticked">
          兩段都沒有勾，所以沒有事情可以做。
        </p>
      )}

      {error && (
        <span className="mfp-tx__error" role="alert" data-testid="refine-error">
          {error}
        </span>
      )}

      {editing && (
        <GlossaryDialog
          onClose={() => {
            setEditing(false);
            // 編輯過的東西會改變現在提供的建議。在關閉時重算而不是每次
            // 按鍵都重算：下面那份清單不可以在有人正在打字時亂跳。
            void useRefine.getState().reask();
          }}
          onReveal={onReveal}
        />
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
                void useRefine.getState().addFiller(draft);
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
              onClick={() => void useRefine.getState().addCommon()}
            >
              加入常見的
            </button>
          </div>
          <ul className="mfp-fix__list" data-testid="filler-terms">
            {(fillers?.terms ?? []).map((item) => (
              <li key={item} className="mfp-fix__item">
                <span className="mfp-fix__was">{item}</span>
                <button
                  type="button"
                  className="mfp-tx__reveal"
                  aria-label={`移除 ${item}`}
                  onClick={() => void useRefine.getState().removeFiller(item)}
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

/** 按鈕上的數字。只講有勾的那幾段——一個關掉的段落報 0，讀起來像是
 *  它做了而且什麼都沒做。 */
function describeChosen(corrections: number | null, removals: number | null): string {
  const parts: string[] = [];
  if (corrections !== null) parts.push(`校正 ${corrections} 處`);
  if (removals !== null) parts.push(`刪 ${removals} 句`);
  return parts.join("、") || "沒有選任何一段";
}

/** 寫完之後那句話。做了哪幾段就講哪幾段。 */
function describeWritten(
  stages: string[],
  corrected: number,
  removed: number,
): string {
  const parts: string[] = [];
  if (stages.includes("correct")) parts.push(`套用了 ${corrected} 處校正`);
  if (stages.includes("tidy")) parts.push(`刪掉 ${removed} 句語助詞`);
  return `${parts.join("，")}。`;
}

/**
 * 詞庫編輯器，蓋在這個面板上。
 *
 * 用真的 modal 而不是 popover：編輯白名單是一個對未來每一份逐字稿都有
 * 後果的決定，而且下面那份建議清單正要因此改變。Escape 和點背景都關得掉，
 * 因為滑鼠使用者會試後者，而鍵盤使用者只有前者。
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
