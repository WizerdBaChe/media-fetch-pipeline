/**
 * 文件翻譯: the third 延伸工具, and the only one that never touches a video.
 *
 * It is a view of its own rather than a panel inside 逐字稿, and that is the
 * one real design question this feature had. Everything in the transcript
 * workspace acts on the transcript ON SCREEN -- translate it, correct it,
 * tidy it -- and this verb takes a `.txt`/`.md` the user has somewhere else
 * entirely. Putting it there would have made 「翻譯」 mean two different
 * things one above the other, with the second one silently ignoring the
 * transcript the reader is looking at. 延伸工具 is the menu whose stated job
 * is being where later tools land, so this is where it landed.
 *
 * Same shape as its two neighbours: it replaces the queue table in the
 * content area, the queue keeps running behind it, and going back loses
 * nothing.
 */

import { useEffect } from "react";
import { pickDocumentFile, revealPath } from "@/shared/lib/desktop";
import { GuideButton } from "@/entities/onboarding/ui/FeatureGuide";
import { CLAUSE_LOSS_NOTE, LANGUAGES, mayLoseAClause } from "@/shared/lib/languages";
import { useAsrSetup } from "@/features/setup-asr/model/store";
import {
  canRun,
  describeDocProgress,
  describeDocResult,
  useTranslateDoc,
} from "@/features/translate-document/model/store";

export interface DocumentWorkspaceProps {
  onClose: () => void;
  /** Open 設定, where translation is set up. A prop rather than a store
   *  read, for the reason 逐字稿 takes one: which panels exist is the page's
   *  business, not this widget's. */
  onOpenSettings?: () => void;
}

export function DocumentWorkspace({ onClose, onOpenSettings }: DocumentWorkspaceProps) {
  const source = useTranslateDoc((state) => state.source);
  const from = useTranslateDoc((state) => state.from);
  const to = useTranslateDoc((state) => state.to);
  const busy = useTranslateDoc((state) => state.busy);
  const error = useTranslateDoc((state) => state.error);
  const result = useTranslateDoc((state) => state.result);
  const progress = useTranslateDoc((state) => state.progress);

  // Whether this machine can translate at all, asked once per session and
  // shared with the settings panel. Read here so the answer is on screen
  // before the user has picked a file and waited for a refusal.
  const readiness = useAsrSetup((state) => state.readiness);
  const loadReadiness = useAsrSetup((state) => state.load);
  useEffect(() => {
    if (readiness === null) void loadReadiness();
  }, [readiness, loadReadiness]);
  const translation =
    readiness?.capabilities.find((entry) => entry.id === "translation") ?? null;

  const progressLine = busy ? describeDocProgress(progress) : null;
  const summary = describeDocResult(result);
  const ready = canRun({ source, from, busy });

  /** Reveal, and SAY when it was refused -- a button that silently does
   *  nothing is worse than one that explains. */
  const reveal = async (path: string) => {
    const refusal = await revealPath(path, "file");
    if (refusal) useTranslateDoc.setState({ error: refusal });
  };

  return (
    <section className="mfp-tx" aria-label="文件翻譯">
      <header className="mfp-tx__head">
        <button type="button" className="mfp-button" onClick={onClose}>
          ← 回到佇列
        </button>
        <h2>文件翻譯</h2>
        <GuideButton id="translatedoc" className="mfp-tx__guide" />
      </header>

      <fieldset className="mfp-stack__section">
        <legend>要翻哪一份文件</legend>
        <div className="mfp-stack__source">
          <input
            type="text"
            value={source}
            placeholder="本機的 .txt／.md／.markdown 檔案路徑"
            aria-label="文件來源"
            onChange={(event) => useTranslateDoc.getState().setSource(event.target.value)}
          />
          <button
            type="button"
            className="mfp-button"
            data-testid="doc-pick"
            onClick={async () => {
              const picked = await pickDocumentFile();
              if (picked) useTranslateDoc.getState().setSource(picked);
            }}
          >
            選檔案…
          </button>
        </div>
        <p className="mfp-tx__hint">
          {/* Said before the click rather than as a refusal after it. A `.srt`
              has timings and this verb would throw them away, so it is sent
              to the verb that keeps them instead. */}
          程式碼、表格、front matter、行內程式碼和連結網址都不會被送去翻譯，
          原樣保留。字幕檔（<code>.srt</code>／<code>.vtt</code>）請用
          「逐字稿」裡的翻譯，那裡會保留時間軸。
        </p>
      </fieldset>

      <fieldset className="mfp-stack__section">
        <legend>語言</legend>
        <div className="mfp-tx__lang">
          <label className="mfp-stack__field">
            原文是什麼語言
            <select
              className="mfp-select"
              value={from}
              disabled={busy}
              data-testid="doc-from"
              onChange={(event) => useTranslateDoc.getState().setFrom(event.target.value)}
            >
              {/* No default, and the empty option stays in the list rather
                  than disappearing once something is chosen. A document
                  carries no language in its name -- unlike a transcript this
                  project wrote -- and a wrong source language produces
                  fluent output that is not a translation of anything. */}
              <option value="">請選擇…</option>
              {LANGUAGES.map((option) => (
                <option key={option.code} value={option.code}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>

          <label className="mfp-stack__field">
            翻成
            <select
              className="mfp-select"
              value={to}
              disabled={busy}
              data-testid="doc-to"
              onChange={(event) => useTranslateDoc.getState().setTo(event.target.value)}
            >
              {LANGUAGES.map((option) => (
                <option key={option.code} value={option.code}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        </div>

        {/* The same caution 逐字稿 shows, from the same measurement and the
            same constant -- it is a property of the model and the target
            language, not of which panel asked. */}
        {mayLoseAClause(to) && (
          <p className="mfp-asr__muted" data-testid="doc-zh-note">
            {CLAUSE_LOSS_NOTE}
          </p>
        )}
      </fieldset>

      <fieldset className="mfp-stack__section">
        <legend>翻譯</legend>

        {translation !== null && !translation.ready ? (
          <div className="mfp-tx__setup" role="status" data-testid="doc-unavailable">
            <strong>{translation.headline}</strong>
            <p>{translation.detail}</p>
            {onOpenSettings && (
              <button
                type="button"
                className="mfp-button mfp-button--primary"
                onClick={onOpenSettings}
              >
                去設定翻譯模型
              </button>
            )}
          </div>
        ) : (
          <div className="mfp-tx__actions">
            <button
              type="button"
              className="mfp-button mfp-button--primary"
              disabled={!ready}
              data-testid="doc-run"
              title={
                !source.trim()
                  ? "先選一份文件"
                  : !from
                    ? "先說原文是什麼語言"
                    : "翻譯這份文件，結果會放進一個新的分析資料夾"
              }
              onClick={() => void useTranslateDoc.getState().run()}
            >
              {busy ? "翻譯中…" : "翻譯這份文件"}
            </button>
            {progressLine && (
              <span className="mfp-asr__progress" role="status" data-testid="doc-progress">
                {progressLine}
              </span>
            )}
          </div>
        )}

        {result && (
          <div className="mfp-fix__written" data-testid="doc-result">
            <p>{summary}</p>
            {/* 「翻好 N 句」 rather than 「已翻好」: the second is a
                completeness claim this build cannot vouch for (D-133). */}
            <ul className="mfp-fix__files">
              <li>
                <button
                  type="button"
                  className="mfp-tx__reveal"
                  title={result.source}
                  onClick={() => void reveal(result.source)}
                >
                  翻好的文件
                </button>
              </li>
              {result.record && (
                <li>
                  <button
                    type="button"
                    className="mfp-tx__reveal"
                    title={result.record}
                    onClick={() => void reveal(result.record!)}
                  >
                    翻譯紀錄（從哪來、用哪個模型）
                  </button>
                </li>
              )}
            </ul>
            {(result.suspectLines ?? []).length > 0 && (
              <p className="mfp-tx__warn" role="status" data-testid="doc-suspect">
                其中 {result.suspectLines.length} 句可能少了一個子句
                （第 {result.suspectLines.slice(0, 3).join("、")} 句
                {result.suspectLines.length > 3 ? " 等" : ""}），
                建議對照原文再用。
              </p>
            )}
          </div>
        )}

        {error && (
          <p className="mfp-tx__error" role="alert" data-testid="doc-error">
            {error}
          </p>
        )}
      </fieldset>
    </section>
  );
}
