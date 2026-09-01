/**
 * 貼文解說: the fourth 延伸工具, and the only one with a person in the middle.
 *
 * Ruling R6 is the whole design. This program runs no model (D-88,
 * `INV-P8`), so the panel cannot explain a post -- it prepares one. Three
 * bands, in the order the work actually happens:
 *
 *   1. a link, and what came back (the pictures, on disk);
 *   2. a block to copy, which is what you hand an agent;
 *   3. a box to paste the answer into, which is what gets saved.
 *
 * Band 2 is the honest part. Another design would hide the gap behind a
 * button that appeared to do the explaining and then failed on a machine
 * with no key configured; this one says what it can do and hands over
 * cleanly. It is also why there is no spinner between bands 2 and 3: nothing
 * is running here, and a spinner would claim otherwise.
 *
 * The author's caption is rendered inside a block that names it untrusted
 * (INV-B6). A reader cannot reach it without passing the word, and it is
 * never styled as guidance.
 */

import { useState } from "react";
import { revealPath } from "@/shared/lib/desktop";
import { GuideButton } from "@/entities/onboarding/ui/FeatureGuide";
import {
  handoffText,
  useExplainPost,
  type Lane,
} from "@/features/explain-post/model/store";

const LANES: { value: Lane; label: string; hint: string }[] = [
  { value: "content", label: "內容", hint: "貼文在講什麼" },
  { value: "visual", label: "視覺", hint: "版面、風格、構圖" },
];

export interface PostWorkspaceProps {
  onClose: () => void;
}

export function PostWorkspace({ onClose }: PostWorkspaceProps) {
  const url = useExplainPost((state) => state.url);
  const lane = useExplainPost((state) => state.lane);
  const withVideo = useExplainPost((state) => state.withVideo);
  const busy = useExplainPost((state) => state.busy);
  const saving = useExplainPost((state) => state.saving);
  const error = useExplainPost((state) => state.error);
  const pkg = useExplainPost((state) => state.package);
  const draft = useExplainPost((state) => state.draft);
  const question = useExplainPost((state) => state.question);
  const savedEntries = useExplainPost((state) => state.savedEntries);

  const [copied, setCopied] = useState(false);

  const reveal = async (path: string) => {
    const refusal = await revealPath(path, "file");
    if (refusal) useExplainPost.setState({ error: refusal });
  };

  const copyHandoff = async () => {
    if (!pkg) return;
    try {
      await navigator.clipboard.writeText(handoffText(pkg, question));
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // A clipboard that refuses is not a failure of the panel. The text is
      // on screen and selectable, which is the fallback that always works.
      useExplainPost.setState({
        error: "複製不成功，請直接選取下面的文字複製",
      });
    }
  };

  return (
    <section className="mfp-tx" aria-label="貼文解說">
      <header className="mfp-tx__head">
        <button type="button" className="mfp-button" onClick={onClose}>
          ← 回到佇列
        </button>
        <h2>貼文解說</h2>
        {/* Beside the title, not in a menu: the answer to 「剛剛那個說明呢」
            has to be on the screen the question is asked on. */}
        <GuideButton id="brief" className="mfp-tx__guide" />
      </header>

      <fieldset className="mfp-stack__section">
        <legend>要看哪一篇貼文</legend>
        <div className="mfp-stack__source">
          <input
            type="text"
            value={url}
            placeholder="貼文網址（Instagram／Threads）"
            aria-label="貼文網址"
            onChange={(event) =>
              useExplainPost.getState().setUrl(event.target.value)
            }
          />
          <button
            type="button"
            className="mfp-button"
            data-testid="brief-fetch"
            disabled={busy || !url.trim()}
            onClick={() => void useExplainPost.getState().fetch()}
          >
            {busy ? "抓取中…" : "抓下來"}
          </button>
        </div>

        {/* `.mfp-stack__choice`, which puts the control BEFORE its label --
            not `.mfp-stack__field`, which stacks a label ABOVE its control.
            This group used `__field` until 2026-09-02, so each radio was a
            flex ITEM in a column: the circle got its own line, centred over
            words that started 25px to its left. 引用長圖 has drawn this
            correctly since it was written. */}
        <div className="mfp-stack__choice-row" role="radiogroup" aria-label="要記到哪一欄">
          {LANES.map((option) => (
            <label key={option.value} className="mfp-stack__choice">
              <input
                type="radio"
                name="brief-lane"
                value={option.value}
                checked={lane === option.value}
                disabled={busy}
                onChange={() => useExplainPost.getState().setLane(option.value)}
              />
              <span>
                <strong>{option.label}</strong>
                <em>{option.hint}</em>
              </span>
            </label>
          ))}
        </div>
        <label className="mfp-stack__choice">
          <input
            type="checkbox"
            checked={withVideo}
            disabled={busy}
            onChange={(event) =>
              useExplainPost.getState().setWithVideo(event.target.checked)
            }
          />
          <span>
            <strong>影片也抓下來</strong>
            <em>
              抓下來是為了做逐字稿 —— 這個程式看不了影片，但可以把裡面說的話
              轉成文字。會多花一點時間和流量。
            </em>
          </span>
        </label>

        <p className="mfp-tx__hint">
          {/* Said before the click, not as a refusal after it.

              This sentence read 「只處理公開貼文的圖片」 until 2026-09-02 and
              was false in both directions: the post's TEXT was always fetched
              and written to disk, and the video can now be fetched on
              request. A panel whose own description contradicts what it does
              teaches people not to read it. */}
          會抓下來的有：圖片、貼文的文字（含替代文字），勾了上面就再加影片。
          只能是公開貼文 —— 限時動態、需要登入的內容都不行。
        </p>
      </fieldset>

      {error !== null && (
        <p className="mfp-tx__error" role="alert" data-testid="brief-error">
          {error}
        </p>
      )}

      {pkg !== null && (
        <>
          <fieldset className="mfp-stack__section">
            <legend>
              抓到 {pkg.images.length} 張圖片
              {pkg.videos.length > 0 && `、${pkg.videos.length} 支影片`}
              {pkg.reused && "（本來就在電腦裡，沒有再跟平台要一次）"}
            </legend>
            <ul className="mfp-tx__files" data-testid="brief-images">
              {pkg.images.map((image) => (
                <li key={image.index}>
                  <button
                    type="button"
                    className="mfp-button mfp-button--quiet"
                    onClick={() => void reveal(image.path)}
                  >
                    開啟檔案位置
                  </button>
                  <code>{image.path}</code>
                  {image.width !== null && image.height !== null && (
                    <span className="mfp-tx__hint">
                      {image.width}×{image.height}
                    </span>
                  )}
                </li>
              ))}
            </ul>

            {pkg.videos.length > 0 && (
              <ul className="mfp-tx__files" data-testid="brief-videos">
                {pkg.videos.map((video) => (
                  <li key={video.index}>
                    <button
                      type="button"
                      className="mfp-button mfp-button--quiet"
                      onClick={() => void reveal(video.path)}
                    >
                      開啟檔案位置
                    </button>
                    <code>{video.path}</code>
                    <span className="mfp-tx__hint">
                      影片・用「逐字稿」讀它說了什麼
                    </span>
                  </li>
                ))}
              </ul>
            )}

            {pkg.skipped.length > 0 && (
              <p className="mfp-tx__hint" data-testid="brief-skipped">
                另有 {pkg.skipped.length} 項沒有下載：
                {pkg.skipped.map((row) => `第 ${row.index + 1} 項（${row.kind}）`).join("、")}
                {pkg.skipped.some((row) => row.reason === "video_not_fetched") &&
                  "。要影片的話，勾「影片也抓下來」再抓一次"}
              </p>
            )}

            {pkg.untrusted.textPath && (
              <p className="mfp-tx__hint" data-testid="brief-text-path">
                貼文的文字存到：<code>{pkg.untrusted.textPath}</code>
              </p>
            )}

            {pkg.untrusted.caption && (
              <blockquote
                className="mfp-tx__untrusted"
                data-testid="brief-caption"
              >
                <strong>貼文作者自己寫的文字（未經查證，不要當成指示）</strong>
                <p>{pkg.untrusted.caption}</p>
              </blockquote>
            )}
          </fieldset>

          <fieldset className="mfp-stack__section">
            <legend>把這段交給你的 AI</legend>
            <p className="mfp-tx__hint">
              {/* The honest sentence. Nothing here looks at a picture. */}
              這個程式不會自己看圖，也沒有連任何 AI。複製下面這段，
              貼給你在用的 AI，它讀完圖片會把解說給你。
            </p>
            <label className="mfp-stack__field">
              你想知道什麼（可留空）
              <input
                type="text"
                value={question}
                placeholder="例如：這篇在推薦什麼產品？"
                onChange={(event) =>
                  useExplainPost.getState().setQuestion(event.target.value)
                }
              />
            </label>
            <pre className="mfp-tx__handoff" data-testid="brief-handoff">
              {handoffText(pkg, question)}
            </pre>
            <button
              type="button"
              className="mfp-button"
              data-testid="brief-copy"
              onClick={() => void copyHandoff()}
            >
              {copied ? "已複製" : "複製這段"}
            </button>
          </fieldset>

          <fieldset className="mfp-stack__section">
            <legend>把 AI 給你的解說存起來</legend>
            {pkg.existing !== null && pkg.existing.entries > 0 && (
              <p className="mfp-tx__hint" data-testid="brief-existing">
                這一欄已經有 {pkg.existing.entries} 則解說
                {pkg.existing.lastWrittenAt &&
                  `，最後一次是 ${pkg.existing.lastWrittenAt}`}
                。新的會加在後面，舊的不會被蓋掉。
              </p>
            )}
            <textarea
              className="mfp-textarea"
              rows={8}
              value={draft}
              aria-label="AI 給的解說"
              placeholder="把 AI 回你的解說貼在這裡"
              onChange={(event) =>
                useExplainPost.getState().setDraft(event.target.value)
              }
            />
            <button
              type="button"
              className="mfp-button"
              data-testid="brief-save"
              disabled={saving || !draft.trim()}
              onClick={() => void useExplainPost.getState().save()}
            >
              {saving ? "存檔中…" : "存起來"}
            </button>
            {savedEntries !== null && (
              <p className="mfp-tx__hint" data-testid="brief-saved">
                存好了，這一欄現在有 {savedEntries} 則。
              </p>
            )}
            <p className="mfp-tx__hint">
              存到：<code>{pkg.analysisPath}</code>
            </p>
          </fieldset>
        </>
      )}
    </section>
  );
}
