/**
 * 逐字稿 workspace: the second 延伸工具, and the one that feeds the first.
 *
 * It exists because reading and quoting are the same task done twice. The
 * complaint it answers was 「看著畫面腦子空白」 -- staring at a video with no
 * idea which part to quote -- and the answer is not "here is the text" but
 * "pick the lines and the window comes with them". Everything below is
 * arranged around that hand-off: the list is selectable, the selection shows
 * the window it means, and the button carries both into 引用長圖 along with
 * the caption file already on disk, so quoting what you just read costs no
 * second platform request.
 *
 * Same shape as `StackWorkspace` on purpose -- it replaces the queue table in
 * the content area, the queue keeps running behind it, and going back loses
 * nothing.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api } from "@/shared/api/client";
import type { CaptionTracks, Transcript } from "@/shared/api/types";
import { presentError } from "@/shared/lib/errors";
import { pickVideoFile, revealPath } from "@/shared/lib/desktop";
import {
  clock,
  selectLine,
  selectedText,
  windowFor,
} from "@/features/read-transcript/model/selection";
import {
  describeProgress,
  describeVerdicts,
  useAsrProgress,
} from "@/features/read-transcript/model/progress";
import { useAsrSetup } from "@/features/setup-asr/model/store";
import {
  TARGETS,
  describeTranslate,
  useTranslate,
} from "@/features/translate-transcript/model/store";
import { CorrectionPanel } from "@/features/correct-transcript/ui/CorrectionPanel";
import { TidyPanel } from "@/features/tidy-transcript/ui/TidyPanel";

export interface TranscriptHandoff {
  target: string;
  /** The caption file it was read from, so 引用長圖 re-uses it. */
  subs: string;
  start: string;
  end: string;
}

/**
 * Everything a return to this workspace has to bring back.
 *
 * The transcript itself is in here, not just the path it came from. That is
 * the whole point: re-reading is a platform request on a URL and MINUTES of
 * recognition on an audio file, and 「我按錯了」 must not cost either
 * (user report 2026-08-28).
 */
export interface TranscriptSnapshot {
  target: string;
  subLang: string;
  transcript: Transcript | null;
  tracks: CaptionTracks | null;
  /** Which lines were picked, as an array -- a `Set` does not survive being
   *  held in a parent's state as plainly, and the order does not matter. */
  selected: number[];
  anchor: number | null;
}

export interface TranscriptWorkspaceProps {
  /** A queue row's download, when one sent us here. */
  initialPath?: string;
  /** A previous visit, being resumed. Takes precedence over `initialPath`:
   *  both name what to read, and this one also says it was already read. */
  restore?: TranscriptSnapshot;
  /** What a return here would need. Called as the state changes, so the
   *  caller always holds a current one -- there is no moment at which this
   *  workspace is asked for it, because by then it is unmounting. */
  onSnapshot?: (snapshot: TranscriptSnapshot) => void;
  onClose: () => void;
  onQuote: (handoff: TranscriptHandoff) => void;
  /** Open 設定, where speech recognition is set up.
   *
   *  A prop rather than a store read: this workspace does not own the
   *  settings panel and must not learn to. Optional so the browser-mode and
   *  test renders that have no settings surface still work -- the notice
   *  then explains without offering a button that goes nowhere. */
  onOpenSettings?: () => void;
}

const KIND_LABEL: Record<string, string> = {
  written: "人工字幕",
  automatic: "自動字幕（機器聽寫）",
  beside: "影片旁邊的字幕檔",
  named: "你指定的字幕檔",
  // The one where nobody wrote the words down at all. Named for what it is
  // rather than for what it produces, so the reader keeps knowing that no
  // human checked a line of it.
  recognized: "語音辨識（聽音檔轉出來的）",
};

/**
 * Containers with no picture in them. Mirrors `AUDIO_SUFFIXES` in
 * `src/mfp/asr.py`, and only for this: 引用長圖 stacks video FRAMES, so an
 * mp3 has nothing for it to stack, and offering the button anyway would
 * promise an image the engine cannot make.
 */
const AUDIO_ONLY = new Set([
  ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".oga", ".opus",
  ".wma", ".aiff", ".aif", ".caf", ".amr", ".mka", ".m4b", ".wv",
]);

function isAudioOnly(target: string): boolean {
  if (!target || target.includes("://")) return false;
  const dot = target.lastIndexOf(".");
  return dot > 0 && AUDIO_ONLY.has(target.slice(dot).toLowerCase());
}

/** Caption files, which are READ rather than listened to. Naming them is
 *  what keeps the setup warning off a path that does not need the engine:
 *  a `.srt` works perfectly on a machine with no recognition at all. */
const CAPTION_SUFFIXES = new Set([".srt", ".vtt", ".txt"]);

/**
 * Would this source have to be LISTENED to?
 *
 * A URL goes looking for a caption track first and usually finds one, and a
 * caption file is already text. Only a local media file is guaranteed to
 * reach the engine -- and even then a `.srt` sitting beside it wins, which
 * is why the notice this drives warns rather than blocks.
 */
function mayNeedRecognition(target: string): boolean {
  const trimmed = target.trim();
  if (!trimmed || trimmed.includes("://")) return false;
  const dot = trimmed.lastIndexOf(".");
  if (dot <= 0) return false;
  return !CAPTION_SUFFIXES.has(trimmed.slice(dot).toLowerCase());
}

export function TranscriptWorkspace({
  initialPath,
  restore,
  onSnapshot,
  onClose,
  onQuote,
  onOpenSettings,
}: TranscriptWorkspaceProps) {
  const [target, setTarget] = useState(restore?.target ?? initialPath ?? "");
  const [subLang, setSubLang] = useState(restore?.subLang ?? "orig");
  const [transcript, setTranscript] = useState<Transcript | null>(
    restore?.transcript ?? null,
  );
  const [tracks, setTracks] = useState<CaptionTracks | null>(
    restore?.tracks ?? null,
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Kept beside the message so one failure can be presented differently from
  // the rest: 「還沒設定語音辨識」 is a setup step with a button, and rendering
  // it as red prose beside 「下載失敗」 taught the reader it was a breakage.
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<number>>(
    () => new Set(restore?.selected ?? []),
  );
  const [anchor, setAnchor] = useState<number | null>(restore?.anchor ?? null);
  const [copied, setCopied] = useState(false);

  // Memoized for its IDENTITY, not its cost. `transcript?.lines ?? []` built a
  // fresh array on every render while no transcript was loaded, so `win`'s
  // dependency list changed every time and the useMemo below memoized nothing.
  const lines = useMemo(() => transcript?.lines ?? [], [transcript]);
  const win = useMemo(() => windowFor(lines, selected), [lines, selected]);
  const isUrl = target.includes("://");
  // Progress arrives on the shared event stream, not on this request, so it
  // is read from the store rather than from the promise being awaited.
  const asrProgress = useAsrProgress((state) => state.progress);
  const asrStartedAt = useAsrProgress((state) => state.startedAt);
  const progressLine = loading ? describeProgress(asrProgress, asrStartedAt) : null;
  // The verdicts OUTLIVE the wait: they are about the transcript now on
  // screen, not about how long it took to get. Empty whenever there is
  // nothing the reader has to act on.
  const asrHealth = useAsrProgress((state) => state.health);
  const asrFindings = useAsrProgress((state) => state.findings);
  const verdicts = describeVerdicts(asrFindings, asrHealth);

  // Whether this machine could listen at all, asked ONCE per session and
  // shared with the settings panel. Read here so the answer arrives before
  // the user has waited for a failure -- 「你選的檔案沒有字幕，而這台機器還不能
  // 聽寫」 is worth knowing before pressing the button, not after.
  const readiness = useAsrSetup((state) => state.readiness);
  const loadReadiness = useAsrSetup((state) => state.load);
  useEffect(() => {
    if (readiness === null) void loadReadiness();
  }, [readiness, loadReadiness]);
  const recognition =
    readiness?.capabilities.find((entry) => entry.id === "recognition") ?? null;
  const translation =
    readiness?.capabilities.find((entry) => entry.id === "translation") ?? null;

  // Translation state. A separate feature slice, joined to this one here and
  // only here -- nothing translates as a side effect of reading.
  const translating = useTranslate((state) => state.busy);
  const translateError = useTranslate((state) => state.error);
  const translated = useTranslate((state) => state.result);
  const translateProgress = useTranslate((state) => state.progress);
  // Named apart from this component's own `target`, which is the SOURCE
  // path. Two different meanings of the word met here and one of them had
  // to move.
  const translateTarget = useTranslate((state) => state.target);
  const translateLine = translating ? describeTranslate(translateProgress) : null;

  const clearResult = useCallback(() => {
    setTranscript(null);
    setSelected(new Set());
    setAnchor(null);
  }, []);

  const fail = useCallback((cause: unknown) => {
    if (cause instanceof ApiError) {
      const shown = presentError(cause.errorCode);
      setErrorCode(cause.errorCode);
      setError(shown.hint ? `${shown.label}：${shown.hint}` : shown.label);
      // The setup card below says everything this one would, with buttons.
      // Re-reading readiness makes it say the CURRENT reason rather than
      // whatever it said when the panel opened.
      if (cause.errorCode === "asr_unavailable") void loadReadiness();
    } else {
      setErrorCode(null);
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }, [loadReadiness]);

  const read = useCallback(
    async (refresh = false) => {
      if (!target.trim()) return;
      setLoading(true);
      setError(null);
      setErrorCode(null);
      // Whatever the last run said is not about this one. Cleared here
      // rather than on completion so a failed read cannot leave a stale
      // "聽寫中 80%" sitting under an error message. `begin` rather than
      // `clear` because the previous run's VERDICT has to go too -- a
      // warning about a transcript that is no longer on screen is worse
      // than no warning.
      useAsrProgress.getState().begin();
      try {
        const result = await api.readTranscript({
          target: target.trim(),
          subLang,
          refresh,
        });
        setTranscript(result);
        setSelected(new Set());
        setAnchor(null);
      } catch (cause) {
        clearResult();
        fail(cause);
      } finally {
        setLoading(false);
        useAsrProgress.getState().clear();
      }
    },
    [target, subLang, clearResult, fail],
  );

  /** Which languages exist, so the reader picks instead of guessing. */
  const lookUpTracks = useCallback(async () => {
    if (!isUrl) return;
    setError(null);
    try {
      setTracks(await api.captionTracks(target.trim()));
    } catch (cause) {
      fail(cause);
    }
  }, [isUrl, target, fail]);

  // A row that sent us here already named its file; read it without being
  // asked. Arriving at an empty panel with a filled-in path is a click that
  // teaches nothing.
  //
  // Guarded by a ref rather than by a shortened dependency list: `read`
  // changes identity whenever the language or the box does, and an effect
  // that re-fires on those would re-fetch while somebody was still typing.
  // The ref says "this path has had its one automatic read", which is the
  // actual condition.
  //
  // A RESTORED visit starts with that read already done, so the ref is
  // seeded with the path and the effect skips. Re-reading on the way back
  // would be the exact cost the back button exists to avoid.
  const autoRead = useRef<string | null>(restore ? (initialPath ?? null) : null);
  useEffect(() => {
    if (!initialPath || autoRead.current === initialPath) return;
    autoRead.current = initialPath;
    void read();
  }, [initialPath, read]);

  // What a return here would need, kept current in the caller. An effect
  // rather than a call inside each setter: there are seven places this state
  // changes and one of them would eventually be forgotten.
  useEffect(() => {
    onSnapshot?.({
      target,
      subLang,
      transcript,
      tracks,
      selected: [...selected],
      anchor,
    });
  }, [onSnapshot, target, subLang, transcript, tracks, selected, anchor]);

  const onLineClick = (index: number, extend: boolean) => {
    const next = selectLine(index, anchor, extend);
    setSelected(next.selected);
    setAnchor(next.anchor);
    setCopied(false);
  };

  /** Reveal, and SAY when it was refused. A caption file the user named
   *  themselves can sit outside the output root, and the main process will
   *  not hand the shell a path it cannot prove is contained -- a button that
   *  silently does nothing is worse than one that explains. */
  const reveal = async (path: string) => {
    const refusal = await revealPath(path, "file");
    if (refusal) setError(refusal);
  };

  const copy = async () => {
    const text = selectedText(lines, selected);
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      // Clipboard permission is not something the reader can fix from here,
      // and the text is on screen and selectable anyway.
      setError("這個環境不允許寫入剪貼簿，請直接反白複製");
    }
  };

  /**
   * `named` means the reader pointed this tool straight at a caption file,
   * so there IS no video here -- handing the caption file over as one
   * produces `mfp stack <srt> --subs <srt>`, which is nonsense the workspace
   * would then render as its equivalent command. The window and the caption
   * file are still worth carrying; the video is the one thing left to
   * answer, and 引用長圖 already knows how to ask for it.
   */
  //
  // An audio file joins it for a different reason with the same answer:
  // 引用長圖 stacks video frames, and an mp3 has none. Both cases carry the
  // window and the caption file over and leave the video to be named there.
  const noVideoYet = transcript?.kind === "named" || isAudioOnly(target.trim());

  const quote = () => {
    if (!transcript || !win) return;
    onQuote({
      target: noVideoYet ? "" : target.trim(),
      subs: transcript.source,
      start: win.from,
      end: win.to,
    });
  };

  const languageOptions = tracks
    ? [...tracks.written, ...tracks.automaticOriginal]
    : [];

  return (
    <section className="mfp-tx" aria-label="逐字稿">
      <header className="mfp-tx__head">
        <button type="button" className="mfp-button" onClick={onClose}>
          ← 回到佇列
        </button>
        <h2>逐字稿</h2>
        {transcript?.title && <span className="mfp-tx__title">{transcript.title}</span>}
      </header>

      <fieldset className="mfp-stack__section">
        <legend>要讀哪一段影音</legend>
        <div className="mfp-stack__source">
          <input
            type="text"
            value={target}
            placeholder="貼文網址，或本機的音檔／影片／字幕檔路徑"
            aria-label="影音來源"
            onChange={(event) => {
              setTarget(event.target.value);
              setTracks(null);
              clearResult();
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter") void read();
            }}
          />
          <button
            type="button"
            className="mfp-button"
            onClick={async () => {
              const picked = await pickVideoFile("media");
              if (picked) {
                setTarget(picked);
                setTracks(null);
                clearResult();
              }
            }}
          >
            選檔案…
          </button>
        </div>

        <div className="mfp-tx__lang">
          <label className="mfp-stack__field">
            字幕語言
            {languageOptions.length ? (
              <select
                className="mfp-select"
                value={subLang}
                onChange={(event) => setSubLang(event.target.value)}
              >
                <option value="orig">原始語言（自動判斷）</option>
                {languageOptions.map((code) => (
                  <option key={code} value={code}>
                    {code}
                  </option>
                ))}
              </select>
            ) : (
              <input
                type="text"
                value={subLang}
                onChange={(event) => setSubLang(event.target.value)}
              />
            )}
          </label>
          {isUrl && (
            <button type="button" className="mfp-button" onClick={() => void lookUpTracks()}>
              看有哪些字幕
            </button>
          )}
          {tracks && (
            <span className="mfp-tx__tracks">
              人工 {tracks.written.length ? tracks.written.join("、") : "無"}
              ｜自動 {tracks.automaticCount} 種
              {tracks.spokenLanguage ? `｜口說 ${tracks.spokenLanguage}` : ""}
            </span>
          )}
        </div>

        <div className="mfp-tx__actions">
          <button
            type="button"
            className="mfp-button mfp-button--primary"
            disabled={!target.trim() || loading}
            onClick={() => void read()}
            title={
              isUrl
                ? "先找字幕軌；沒有字幕才聽音訊"
                : "沒有字幕檔的音檔或影片，會直接聽音訊轉成文字"
            }
          >
            {loading ? "讀取中…" : "讀取逐字稿"}
          </button>
          {transcript && (
            <button
              type="button"
              className="mfp-button"
              disabled={loading}
              title="忽略已存的字幕檔，重新抓一次（本機音檔會重新聽寫）"
              onClick={() => void read(true)}
            >
              重新抓取
            </button>
          )}
        </div>

        {/* Only while a read is in flight, and only once the engine has said
            something. Recognition takes minutes where reading a caption
            track takes a second, and a wait with no readout is
            indistinguishable from a hang. */}
        {progressLine && (
          <p className="mfp-tx__progress" role="status" data-testid="asr-progress">
            {progressLine}
          </p>
        )}

        {/* What can be said about the transcript now on screen: findings
            first (properties of the file), then the engine's own report on
            the parts findings cannot see. Stays with the transcript rather
            than vanishing with the spinner, because it describes the file
            and not the wait. A long recording can come back complete,
            plausible-looking and partly wrong, and this is the only place
            that says so. */}
        {verdicts.length > 0 && (
          <ul className="mfp-tx__health" role="status" data-testid="asr-health">
            {verdicts.map((verdict, index) => (
              <li key={index} data-severity={verdict.severity}>
                {verdict.text}
              </li>
            ))}
          </ul>
        )}

        {/* The setup card. Shown BEFORE a failure when the source is one
            that would have to be listened to and this machine cannot, and
            INSTEAD of the red line when a run has already failed for that
            reason. Both cases are the same situation and deserve the same
            two sentences plus a button -- what they must not be is a wall of
            prose naming a config key. */}
        {recognition !== null &&
          !recognition.ready &&
          (errorCode === "asr_unavailable" || mayNeedRecognition(target)) && (
            <div className="mfp-tx__setup" role="status" data-testid="asr-setup-notice">
              {/* 「聽寫」 is the VERB for this capability everywhere -- the
                  panel's verdicts, this card, the settings table's label is
                  the noun 「語音辨識」. It used to also be called 用「聽的」
                  here, which made a third name for one thing. */}
              <strong>這個檔案沒有現成字幕，只能靠聽寫，但{recognition.headline}</strong>
              <p>{recognition.detail}</p>
              <p className="mfp-tx__setup-why">
                有現成字幕檔的影片不受影響；只有把聲音轉成文字才需要設定這個。
              </p>
              {onOpenSettings && (
                <button
                  type="button"
                  className="mfp-button mfp-button--primary"
                  onClick={onOpenSettings}
                >
                  去設定語音辨識
                </button>
              )}
            </div>
          )}

        {error && errorCode !== "asr_unavailable" && (
          <p className="mfp-tx__error" role="alert">
            {error}
          </p>
        )}
      </fieldset>

      {transcript && (
        <fieldset className="mfp-stack__section mfp-tx__result">
          <legend>
            {transcript.lineCount} 行
            ｜{KIND_LABEL[transcript.kind] ?? transcript.kind}
            {transcript.language ? `（${transcript.language}）` : ""}
          </legend>

          <p className="mfp-tx__hint">
            點一行選取，按住 Shift 再點可以選一段。選好之後可以直接做成引用長圖。
            {/* 「位置」 means the FOLDER. The button used to launch the .srt
                itself in whatever the OS thinks owns it, which is a
                different verb than its name (UAT 2026-08-28). */}
            <button
              type="button"
              className="mfp-tx__reveal"
              onClick={() => void reveal(transcript.source)}
              title={transcript.source}
            >
              開啟檔案位置
            </button>
            {transcript.kind === "recognized" && (
              <span className="mfp-asr__muted">
                {" "}這次分析有自己的資料夾，裡面分成 <code>字幕檔</code>
                （<code>.srt</code>，給播放器和引用長圖）和 <code>文字檔</code>
                （<code>.txt</code>，純文字給人讀）；校正和翻譯的結果也會放進去。
              </span>
            )}
          </p>

          {/* Translation. Offered only once there IS a transcript, and only
              as an explicit action -- nothing here translates as a side
              effect of reading (user ruling 2026-08-28). When the capability
              is not set up the row says so in one line instead of hiding,
              because a feature nobody can see is one nobody asks for. */}
          <div className="mfp-tx__translate" data-testid="transcript-translate">
            {translation?.ready ? (
              <>
                <span className="mfp-asr__part-name">翻譯成</span>
                <select
                  className="mfp-select"
                  value={translateTarget}
                  disabled={translating}
                  onChange={(event) =>
                    useTranslate.getState().setTarget(event.target.value)
                  }
                  aria-label="翻譯的目標語言"
                >
                  {TARGETS.map((option) => (
                    <option key={option.code} value={option.code}>
                      {option.label}
                    </option>
                  ))}
                </select>
                {/* Said BEFORE the click, not after: the limit belongs to the
                    choice being made. Only for Chinese targets, because that
                    is where it was measured -- fra/deu/jpn came back whole
                    (see translate.clauses_of). */}
                {translateTarget.startsWith("zho") && (
                  <span className="mfp-asr__muted" data-testid="translate-zh-note">
                    中文譯文偶爾會漏掉句子裡的一個子句，重要內容請對照原文。
                  </span>
                )}
                <button
                  type="button"
                  className="mfp-button"
                  disabled={translating}
                  data-testid="translate-run"
                  onClick={() =>
                    void useTranslate
                      .getState()
                      .run(transcript.source, transcript.language ?? undefined)
                  }
                >
                  {translating ? "翻譯中…" : "翻譯這份逐字稿"}
                </button>
                {translateLine && (
                  <span className="mfp-asr__progress" role="status">
                    {translateLine}
                  </span>
                )}
                {translated && (
                  <span className="mfp-asr__muted">
                    {/* 「已翻好」是完成度斷言，而中文譯文可能掉子句——量測見
                        translate.clauses_of。改成不宣稱品質的說法，並把程式
                        真的算得出來的疑慮講出來。 */}
                    翻好 {translated.lineCount} 行
                    <button
                      type="button"
                      className="mfp-tx__reveal"
                      onClick={() => void reveal(translated.source)}
                      title={translated.source}
                    >
                      開啟翻譯檔位置
                    </button>
                  </span>
                )}
                {/* `?? []` because the sidecar is built and shipped separately
                    from this renderer: a release/ built before this field
                    existed must not white-screen the whole workspace over a
                    line whose only job is to add a caveat. */}
                {translated && (translated.suspectLines ?? []).length > 0 && (
                  <span className="mfp-tx__warn" role="status" data-testid="translate-suspect">
                    其中 {translated.suspectLines.length} 行可能少了一個子句
                    （第 {translated.suspectLines.slice(0, 3).join("、")} 句
                    {translated.suspectLines.length > 3 ? " 等" : ""}），
                    建議對照原文再用。
                  </span>
                )}
                {translateError && (
                  <span className="mfp-tx__error" role="alert">
                    {translateError}
                  </span>
                )}
              </>
            ) : translation === null ? (
              // Readiness has not come back yet, so NOTHING is known about
              // translation. This used to fall through to the branch below
              // and assert 「翻譯功能還沒設定。」 -- a statement of fact made
              // during the one moment there is no fact to state, and a
              // 「去設定」 button offered for a problem that may not exist.
              <span className="mfp-asr__muted" data-testid="translate-unknown">
                正在確認翻譯功能…
              </span>
            ) : (
              <span className="mfp-asr__muted" data-testid="translate-unavailable">
                {translation.detail}
                {onOpenSettings && (
                  <button type="button" className="mfp-tx__reveal" onClick={onOpenSettings}>
                    去設定
                  </button>
                )}
              </span>
            )}
          </div>

          {/* Term correction, under the transcript and under translation.
              Third in the row for the same reason translation is second: it
              is something asked for once a transcript is in front of the
              reader, and nothing here corrects as a side effect of
              transcribing. Unlike translation it needs no model and no
              engine, so it is offered unconditionally -- an empty glossary
              produces no suggestions and says so. */}
          <CorrectionPanel source={transcript.source} onReveal={(path) => void reveal(path)} />

          {/* Fourth, and last for a reason: it is the only one that DELETES.
              A reader who has read, translated and corrected is the one in a
              position to decide that the 嗯 can go -- and this is offered
              unconditionally, like correction and unlike translation, because
              it needs no engine and no model. An empty filler list removes
              nothing and says so. */}
          <TidyPanel source={transcript.source} onReveal={(path) => void reveal(path)} />

          <ol className="mfp-tx__lines" data-testid="transcript-lines">
            {lines.map((line, index) => (
              <li key={`${line.at}-${index}`}>
                <button
                  type="button"
                  className="mfp-tx__line"
                  aria-pressed={selected.has(index)}
                  data-selected={selected.has(index) ? "true" : undefined}
                  onClick={(event) => onLineClick(index, event.shiftKey)}
                >
                  <span className="mfp-tx__at mfp-mono">{clock(line.at)}</span>
                  <span className="mfp-tx__text">{line.text}</span>
                </button>
              </li>
            ))}
          </ol>

          {/* Sticky, for the same reason 產生長圖 is: this is what the reader
              came to do, and it must not sit below a transcript that can be
              a thousand lines long. */}
          <div className="mfp-tx__footer">
            {win ? (
              <>
                <span className="mfp-tx__window mfp-mono" data-testid="transcript-window">
                  {win.from} – {win.to}（{win.span} 秒，{selected.size} 行）
                </span>
                <button type="button" className="mfp-button" onClick={() => void copy()}>
                  {copied ? "已複製" : "複製選取"}
                </button>
                <button
                  type="button"
                  className="mfp-button mfp-button--primary"
                  onClick={quote}
                  data-testid="quote-selection"
                  title={
                    noVideoYet
                      ? "這是一個字幕檔，接下來還要選影片"
                      : "帶著這段時間範圍和字幕檔去做長圖"
                  }
                >
                  用這段做引用長圖{noVideoYet ? "…" : ""}
                </button>
              </>
            ) : (
              <span className="mfp-tx__window">還沒有選任何一行</span>
            )}
          </div>
        </fieldset>
      )}
    </section>
  );
}
