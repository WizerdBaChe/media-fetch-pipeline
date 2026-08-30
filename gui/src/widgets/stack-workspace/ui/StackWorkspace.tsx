/**
 * 引用長圖 workspace: the second and last level of the extension menu.
 *
 * One page, three sections, in the order the decisions actually happen --
 * not a wizard. The loop this feature is used in is "look at the image,
 * change one number, run it again", and a wizard makes you walk the whole
 * path to change the last step.
 *
 * It replaces the queue table in the content area rather than opening as a
 * modal: the job takes tens of seconds, the queue keeps running behind it,
 * and a modal would block the app for the whole wait. The header, the
 * capability notice and the status bar all stay where they were.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api } from "@/shared/api/client";
import type { StackJob, StackSource } from "@/shared/api/types";
import { isRunning, useStackStore } from "@/entities/stack-job/model/store";
import { RoiPicker } from "@/features/pick-roi/ui/RoiPicker";
import {
  emptyForm,
  formForVideo,
  formIssues,
  toRequest,
  type CaptionSource,
  type StackFormState,
} from "@/features/run-stack/model/form";
import { presentError } from "@/shared/lib/errors";
import { pickVideoFile, revealPath } from "@/shared/lib/desktop";
import { Button } from "@/shared/ui/Button";

export interface StackWorkspaceProps {
  /**
   * A file or a folder to start from. A queue row knows only where it
   * downloaded to, so resolving "which video, and does it already have
   * captions" is the server's job -- the same ffprobe rule `stack` uses,
   * rather than a guess from the extension.
   */
  initialPath?: string;
  /**
   * A window somebody already chose, arriving from 逐字稿.
   *
   * It carries the caption FILE as well as the times, deliberately: the
   * transcript was read out of that file, so quoting from it costs no second
   * platform request, and the form lands on the soft path already answered
   * rather than on the band picker, which is the wrong fork for a video whose
   * captions are a separate track.
   *
   * Takes precedence over `initialPath` -- both name a video, and this one
   * names the rest of the form too.
   */
  handoff?: StackHandoff;
  /** A previous visit, being resumed. Takes precedence over both of the
   *  above: they say what to START from, and this says what was already
   *  answered. */
  restore?: StackSnapshot;
  /** What a return here would need, kept current in the caller. */
  onSnapshot?: (snapshot: StackSnapshot) => void;
  onClose: () => void;
}

/**
 * The half of this workspace that a return has to bring back.
 *
 * The form, and the list of candidate videos when one is still being chosen.
 * The JOB is not in here and must not be: it lives on the server, its events
 * keep arriving, and a snapshot of a running job would be a stale copy of
 * something the store already holds correctly.
 */
export interface StackSnapshot {
  form: StackFormState;
  choices: StackSource[];
}

export interface StackHandoff {
  target: string;
  subs: string;
  start: string;
  end: string;
}

const SOURCES: ReadonlyArray<{ id: CaptionSource; label: string; hint: string }> = [
  {
    id: "burned",
    label: "字幕燒在畫面上",
    hint: "IG／TikTok 這類短影片常見。要在畫面上框出字幕的位置",
  },
  {
    id: "file",
    label: "有字幕檔或逐字稿",
    hint: "SRT／VTT／純逐字稿。文字由本工具自己排版燒上去",
  },
  {
    id: "url",
    label: "從貼文網址抓字幕",
    hint: "YouTube 這類有獨立字幕軌的來源。抓過一次就會存在影片旁邊",
  },
];

export function StackWorkspace({
  initialPath,
  handoff,
  restore,
  onSnapshot,
  onClose,
}: StackWorkspaceProps) {
  const [form, setForm] = useState<StackFormState>(
    () => restore?.form ?? emptyForm(),
  );
  const [command, setCommand] = useState<string[]>([]);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [choices, setChoices] = useState<StackSource[]>(restore?.choices ?? []);
  const [resolving, setResolving] = useState(false);
  /** Whether this mount began as a return. Read by the two effects below,
   *  which would otherwise answer questions the restored form has already
   *  answered -- and answer them differently. */
  const resumed = useRef(restore != null);
  const currentId = useStackStore((state) => state.currentId);
  const job = useStackStore((state) => (currentId ? state.jobs[currentId] : null));

  const patch = useCallback(
    (fields: Partial<StackFormState>) => setForm((prev) => ({ ...prev, ...fields })),
    [],
  );

  const adopt = useCallback((source: StackSource) => {
    setChoices([]);
    setForm(formForVideo(source.path, source.captions[0]));
  }, []);

  /**
   * A hand-off answers the whole first half of the form, so there is nothing
   * to resolve: which video, where the words come from, and which part of it.
   * `stack:sources` would only ask ffprobe a question already answered.
   */
  useEffect(() => {
    if (!handoff || resumed.current) return;
    setChoices([]);
    setForm({
      ...emptyForm(handoff.target),
      captionSource: "file",
      subsFile: handoff.subs,
      start: handoff.start,
      end: handoff.end,
    });
  }, [handoff]);

  /** What a return here would need. Cheap: two references, no copying. */
  useEffect(() => {
    onSnapshot?.({ form, choices });
  }, [onSnapshot, form, choices]);

  /** Resolve whatever the caller opened this with, once. */
  useEffect(() => {
    if (!initialPath || handoff || resumed.current) return;
    let cancelled = false;
    setResolving(true);
    api
      .stackSources(initialPath)
      .then((result) => {
        if (cancelled) return;
        if (result.sources.length === 1) adopt(result.sources[0]!);
        else setChoices(result.sources);
      })
      .catch((error) =>
        !cancelled &&
        setSubmitError(error instanceof ApiError ? error.message : String(error)),
      )
      .finally(() => !cancelled && setResolving(false));
    return () => {
      cancelled = true;
    };
  }, [initialPath, handoff, adopt]);

  const request = useMemo(() => toRequest(form), [form]);
  const issues = useMemo(() => formIssues(form), [form]);

  /**
   * The equivalent command comes from the server, debounced.
   *
   * Building it here would be a second implementation of the same mapping,
   * and the only claim this workspace makes about itself is that it is the
   * CLI with a face on it.
   */
  useEffect(() => {
    if (!form.video.trim()) {
      setCommand([]);
      return;
    }
    const handle = setTimeout(() => {
      api
        .stackCommand(request)
        .then((result) => setCommand(result.command))
        .catch(() => setCommand([]));
    }, 250);
    return () => clearTimeout(handle);
  }, [request, form.video]);

  const busy = isRunning(job);

  const run = async () => {
    setSubmitError(null);
    try {
      const created = await api.startStack(request);
      useStackStore.getState().upsert(created);
      useStackStore.getState().select(created.id);
    } catch (error) {
      setSubmitError(error instanceof ApiError ? error.message : String(error));
    }
  };

  const chooseVideo = async () => {
    const picked = await pickVideoFile();
    if (!picked) return;
    // Through the same resolver as a queue row, so a hand-picked file gets
    // the same "there are captions beside this" treatment.
    try {
      const result = await api.stackSources(picked);
      if (result.sources.length === 1) adopt(result.sources[0]!);
      else setForm(formForVideo(picked));
    } catch {
      setForm(formForVideo(picked));
    }
  };

  return (
    <section className="mfp-stack" aria-labelledby="mfp-stack-title">
      <header className="mfp-stack__header">
        <button type="button" className="mfp-stack__back" onClick={onClose}>
          ← 回到佇列
        </button>
        <h2 id="mfp-stack-title">引用長圖</h2>
      </header>

      <div className="mfp-stack__body">
        <div className="mfp-stack__form">
          {resolving && <p className="mfp-stack__note">讀取影片資訊中…</p>}
          {choices.length > 1 && (
            <fieldset className="mfp-stack__section">
              <legend>這個位置有多支影片</legend>
              <ul className="mfp-stack__choices" data-testid="source-choices">
                {choices.map((source) => (
                  <li key={source.path}>
                    <button type="button" onClick={() => adopt(source)}>
                      {source.path.split(/[\\/]/).pop()}
                      <em>
                        {source.width}×{source.height}・{Math.round(source.duration)} 秒
                        {source.captions.length ? "・已有字幕檔" : ""}
                      </em>
                    </button>
                  </li>
                ))}
              </ul>
            </fieldset>
          )}
          {/* --- source -------------------------------------------------- */}
          <fieldset className="mfp-stack__section">
            <legend>影片</legend>
            <div className="mfp-stack__source">
              <input
                type="text"
                value={form.video}
                placeholder="影片檔的完整路徑"
                aria-label="影片檔路徑"
                onChange={(event) => patch({ video: event.target.value, roi: null })}
              />
              <Button onClick={chooseVideo}>選擇檔案…</Button>
            </div>
          </fieldset>

          {/* --- 1. where the words come from ---------------------------- */}
          <fieldset className="mfp-stack__section">
            <legend>1. 字幕從哪裡來</legend>
            {SOURCES.map((source) => (
              <label key={source.id} className="mfp-stack__choice">
                <input
                  type="radio"
                  name="caption-source"
                  value={source.id}
                  checked={form.captionSource === source.id}
                  onChange={() => patch({ captionSource: source.id })}
                />
                <span>
                  <strong>{source.label}</strong>
                  <em>{source.hint}</em>
                </span>
              </label>
            ))}

            {form.captionSource === "file" && (
              <label className="mfp-stack__field">
                字幕檔路徑
                <input
                  type="text"
                  value={form.subsFile}
                  onChange={(event) => patch({ subsFile: event.target.value })}
                  placeholder="C:\\…\\captions.srt"
                />
              </label>
            )}
            {form.captionSource === "url" && (
              <label className="mfp-stack__field">
                貼文網址
                <input
                  type="text"
                  value={form.subsUrl}
                  onChange={(event) => patch({ subsUrl: event.target.value })}
                  placeholder="https://www.youtube.com/watch?v=…"
                />
              </label>
            )}
          </fieldset>

          {/* --- 2. window and band -------------------------------------- */}
          <fieldset className="mfp-stack__section">
            <legend>2. 範圍</legend>
            <div className="mfp-stack__row">
              <label className="mfp-stack__field">
                從
                <input
                  type="text"
                  value={form.start}
                  onChange={(event) => patch({ start: event.target.value })}
                  placeholder="3:21"
                />
              </label>
              <label className="mfp-stack__field">
                到
                <input
                  type="text"
                  value={form.end}
                  onChange={(event) => patch({ end: event.target.value })}
                  placeholder="4:00"
                />
              </label>
            </div>
            <p className="mfp-stack__note">
              不填就是整支影片。長片建議給範圍——掃描時間跟範圍成正比。
            </p>

            {form.captionSource === "burned" && form.video.trim() && (
              <RoiPicker
                video={form.video.trim()}
                value={form.roi}
                onChange={(roi) => patch({ roi })}
                at={form.previewAt}
                onAtChange={(previewAt) => patch({ previewAt })}
              />
            )}
          </fieldset>

          {/* --- 3. adjustments ------------------------------------------ */}
          <fieldset className="mfp-stack__section">
            <legend>3. 排版</legend>
            <div className="mfp-stack__row">
              <label className="mfp-stack__field">
                字級
                <input
                  type="text"
                  inputMode="numeric"
                  value={form.fontSize}
                  onChange={(event) => patch({ fontSize: event.target.value })}
                  placeholder="26"
                />
              </label>
              <label className="mfp-stack__field">
                每條幾句
                <input
                  type="text"
                  inputMode="numeric"
                  value={form.linesPerStrip}
                  onChange={(event) => patch({ linesPerStrip: event.target.value })}
                  placeholder="2"
                />
              </label>
              <label className="mfp-stack__field">
                條數上限
                <input
                  type="text"
                  inputMode="numeric"
                  value={form.maxStrips}
                  onChange={(event) => patch({ maxStrips: event.target.value })}
                  placeholder="24"
                />
              </label>
            </div>
            <label className="mfp-stack__toggle">
              <input
                type="checkbox"
                checked={form.headFull}
                onChange={(event) => patch({ headFull: event.target.checked })}
              />
              開頭保留整張畫面（預設切在字幕上緣）
            </label>
            <label className="mfp-stack__toggle">
              <input
                type="checkbox"
                checked={form.uniformStrips}
                onChange={(event) => patch({ uniformStrips: event.target.checked })}
              />
              每條等高（預設各自貼齊自己的字）
            </label>
            <p className="mfp-stack__note">
              字級只對「字幕檔／網址」這兩種來源有效——燒死的字幕是原片自己的。
            </p>
          </fieldset>

          {/* --- run ----------------------------------------------------- */}
          <div className="mfp-stack__footer">
          <div className="mfp-stack__actions">
            <button
              type="button"
              className="mfp-stack__run"
              onClick={run}
              disabled={issues.length > 0 || busy}
            >
              {busy ? "產生中…" : "產生長圖"}
            </button>
            {busy && job && (
              <Button
                variant="danger"
                onClick={() => useStackStore.getState().cancel(job.id)}
              >
                停止
              </Button>
            )}
            {issues.length > 0 && (
              <ul className="mfp-stack__issues" data-testid="stack-issues">
                {issues.map((issue) => (
                  <li key={issue}>{issue}</li>
                ))}
              </ul>
            )}
            {submitError && (
              <p className="mfp-stack__error" role="alert">
                {submitError}
              </p>
            )}
          </div>

          {command.length > 0 && (
            <div className="mfp-stack__command">
              <span>等效指令</span>
              <code data-testid="stack-command">{command.join(" ")}</code>
            </div>
          )}
          </div>
        </div>

        <StackResult job={job} />
      </div>
    </section>
  );
}

/** The right-hand half: what the run is doing, or what it produced. */
function StackResult({ job }: { job: StackJob | null | undefined }) {
  if (!job) {
    return (
      <aside className="mfp-stack__result mfp-stack__result--empty">
        <p>還沒有產生過。左邊填完按「產生長圖」。</p>
      </aside>
    );
  }

  if (isRunning(job)) {
    return (
      <aside className="mfp-stack__result" aria-live="polite">
        <p className="mfp-stack__state" data-state={job.state}>
          {job.state === "PREPARING" ? "準備中" : "產生中"}
        </p>
        {/* Text, not a bar: the phases have no common unit and a percentage
            here would be invented. The queue table learned the same thing
            about muxing. */}
        <p className="mfp-stack__message">{job.message ?? "…"}</p>
      </aside>
    );
  }

  if (job.state === "FAILED") {
    const presented = presentError(job.errorCode);
    return (
      <aside className="mfp-stack__result mfp-stack__result--failed" role="alert">
        <h3>{presented.label}</h3>
        <p>{job.errorDetail}</p>
        {presented.hint && <p className="mfp-stack__hint">{presented.hint}</p>}
        {job.errorBundle && (
          <Button onClick={() => revealPath(job.errorBundle!, "errors")}>
            開啟這次的錯誤資料
          </Button>
        )}
      </aside>
    );
  }

  if (job.state === "CANCELLED") {
    return (
      <aside className="mfp-stack__result">
        <p>已停止。沒有產生圖檔。</p>
      </aside>
    );
  }

  return (
    <aside className="mfp-stack__result mfp-stack__result--done">
      <p className="mfp-stack__summary">
        {job.strips} 條・{job.width}×{job.height}
        {job.truncated ? `（另有 ${job.truncated} 條超過上限沒有放進去）` : ""}
      </p>
      <img
        className="mfp-stack__image"
        src={api.stackImageUrl(job.id)}
        alt="產生的引用長圖"
      />
      {job.output && (
        <Button onClick={() => revealPath(job.output!, "file")}>開啟檔案位置</Button>
      )}
    </aside>
  );
}
