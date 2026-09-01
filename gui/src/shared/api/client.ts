/**
 * Thin `fetch` wrapper over the `/v1` API.
 *
 * Every non-2xx response carries `{errorCode, message, detail}` (PSM §11), so
 * the client raises a typed `ApiError` and the UI can map `errorCode` to a
 * recovery action. A network-level failure gets a synthetic errorCode rather
 * than a bare TypeError -- "無法連線" is a state the user can act on, an
 * unhandled TypeError is not.
 */

import type {
  BriefPackage,
  BriefRequest,
  BriefSaveRequest,
  BriefSaved,
  DoctorReport,
  Guides,
  ToolStatus,
  AddResponse,
  BulkActionResult,
  AppConfig,
  Health,
  ParseResult,
  RemovalResult,
  FrameShot,
  LogsReport,
  StackJob,
  StackRequest,
  StackSource,
  SweepResult,
  Task,
  TaskState,
  CaptionTracks,
  Transcript,
  TranscriptRequest,
  AsrCatalogueEntry,
  AsrInstallMode,
  AsrModel,
  AsrReadiness,
  AsrScanResult,
  TranslateRequest,
  TranslateDocRequest,
  TranslationResult,
  DocumentTranslationResult,
  CorrectionOffer,
  CorrectionWritten,
  GlossaryReport,
  FillerReport,
  RefineOffer,
  RefineStage,
  RefineWritten,
  TidyOffer,
  TidyWritten,
} from "./types";

const BASE = "/v1";

export class ApiError extends Error {
  readonly errorCode: string;
  readonly status: number;
  readonly detail: unknown;

  constructor(errorCode: string, message: string, status: number, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.errorCode = errorCode;
    this.status = status;
    this.detail = detail;
  }
}

/** Raised when the server is unreachable -- almost always "mfp serve is not running". */
export const SERVER_UNREACHABLE = "server_unreachable";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: {
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...init?.headers,
      },
    });
  } catch (cause) {
    throw new ApiError(
      SERVER_UNREACHABLE,
      "無法連線到本機服務，請確認 `mfp serve` 正在執行",
      0,
      cause,
    );
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  const payload = text ? safeJson(text) : null;

  if (!response.ok) {
    const body = (payload ?? {}) as Partial<{ errorCode: string; message: string; detail: unknown }>;
    throw new ApiError(
      body.errorCode ?? "http_error",
      body.message ?? `HTTP ${response.status}`,
      response.status,
      body.detail,
    );
  }

  return payload as T;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

export const api = {
  health: () => request<Health>("/health"),

  /** Pure preview: no network, no budget cost, nothing enters the queue. */
  parseInput: (text: string) =>
    request<ParseResult>("/input/parse", { method: "POST", body: JSON.stringify({ text }) }),

  getConfig: () => request<AppConfig>("/config"),
  /** Read-only: the panel renders whatever the server reports, never a
   *  cached guess. A stale dependency row is worse than no row. */
  getDoctor: () => request<DoctorReport>("/doctor"),
  putConfig: (config: AppConfig) =>
    request<AppConfig>("/config", { method: "PUT", body: JSON.stringify(config) }),

  listTasks: (state?: TaskState) =>
    request<Task[]>(state ? `/queue?state=${encodeURIComponent(state)}` : "/queue"),

  /** Parse + enqueue in one call. The response carries the INV-7 change report. */
  addTasks: (text: string) =>
    request<AddResponse>("/queue", { method: "POST", body: JSON.stringify({ text }) }),

  patchTask: (
    id: string,
    patch: {
      policy?: string;
      clearPolicy?: boolean;
      selected?: boolean;
      selectedIndices?: number[] | null;
    },
  ) =>
    request<Task>(`/queue/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),

  /**
   * Tick or untick many rows in ONE request.
   *
   * The per-row PATCH above is still right for a single checkbox. It is
   * wrong for "select all": a browser allows six connections per origin over
   * HTTP/1.1 and the event stream holds one for the life of the tab, so N
   * PATCHes do not merely cost N round trips — they starve everything else
   * on the origin, including a second tab's stream (measured 2026-08-16).
   *
   * Omitting `ids` means every row in the queue; the table passes the rows
   * of the current tab, because a select-all in a filtered view that reached
   * hidden rows would be lying.
   */
  selectMany: (selected: boolean, ids?: string[]) =>
    request<BulkActionResult>("/queue:select", {
      method: "POST",
      body: JSON.stringify(ids ? { ids, selected } : { selected }),
    }),

  /** Semantic action (PSM §4.1). The server decides the target state. */
  action: (id: string, action: string) =>
    request<Task>(`/queue/${encodeURIComponent(id)}:${action}`, { method: "POST" }),

  bulkAction: (action: "startAll" | "pauseAll", ids?: string[]) =>
    request<BulkActionResult>(`/queue:${action}`, {
      method: "POST",
      body: JSON.stringify({ ids: ids ?? [] }),
    }),

  /** Primitive escape hatch; ordinary callers should use `action`. */
  transition: (id: string, to: TaskState, errorCode?: string) =>
    request<Task>(`/queue/${encodeURIComponent(id)}:transition`, {
      method: "POST",
      body: JSON.stringify({ to, errorCode: errorCode ?? null }),
    }),

  remove: (ids: string[], deleteFiles = false) =>
    request<RemovalResult>("/queue:remove", {
      method: "POST",
      body: JSON.stringify({ ids, deleteFiles }),
    }),

  clearCompleted: () => request<RemovalResult>("/queue:clearCompleted", { method: "POST" }),

  clearAll: () => request<RemovalResult>("/queue:clearAll", { method: "POST" }),

  sweep: (maxAgeDays: number) =>
    request<RemovalResult>("/queue:sweep", {
      method: "POST",
      body: JSON.stringify({ maxAgeDays }),
    }),
  /* --- 延伸工具：引用長圖 --------------------------------------------- */

  /** What this request would run, without running it. Asked of the server
   *  rather than built here: one renderer means the line the workspace
   *  shows cannot drift from the one that executes. */
  stackCommand: (payload: StackRequest) =>
    request<{ command: string[] }>("/stack:command", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  /** Which videos are here, and what captions sit beside them. A queue
   *  row knows only its output folder, and a post can hold more than one. */
  stackSources: (path: string) =>
    request<{ sources: StackSource[] }>("/stack:sources", {
      method: "POST",
      body: JSON.stringify({ path }),
    }),

  /** One frame, for aiming the subtitle band at something visible. */
  stackFrame: (video: string, at: string, maxWidth?: number) =>
    request<FrameShot>("/stack:frame", {
      method: "POST",
      body: JSON.stringify({ video, at, ...(maxWidth ? { maxWidth } : {}) }),
    }),

  startStack: (payload: StackRequest) =>
    request<StackJob>("/stack", { method: "POST", body: JSON.stringify(payload) }),

  listStackJobs: () => request<StackJob[]>("/stack"),

  getStackJob: (id: string) => request<StackJob>(`/stack/${encodeURIComponent(id)}`),

  cancelStackJob: (id: string) =>
    request<StackJob>(`/stack/${encodeURIComponent(id)}:cancel`, { method: "POST" }),

  /** The produced image, by job id. There is no route that reads back a
   *  path the caller names, and that is deliberate. */
  stackImageUrl: (id: string, kind: "result" | "preview" = "result") =>
    `/v1/stack/${encodeURIComponent(id)}/image?kind=${kind}`,

  /* --- 延伸工具：貼文解說 ------------------------------------------------ */

  /** Fetch a post's pictures and say where an explanation would go.
   *
   *  Costs platform budget unless the post is already on disk, in which case
   *  `reused` is true and it cost nothing -- so calling it again is safe.
   *
   *  This does NOT explain anything. `mfp` runs no model (D-88); the reader
   *  of the returned images is an agent somewhere else, and the desktop's job
   *  is to hand over the pictures and the destination. */
  brief: (payload: BriefRequest) =>
    request<BriefPackage>("/brief", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  /** Append an explanation to a post's analysis file.
   *
   *  Appending, never rewriting (INV-B4): the file is a substrate for later
   *  work, and something already summarised cannot be re-summarised in a new
   *  direction. `post` must be the `post.postDir` value `brief` reported --
   *  the server refuses anything outside an analysis store. */
  saveBrief: (payload: BriefSaveRequest) =>
    request<BriefSaved>("/brief:save", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  /* --- 延伸工具：逐字稿 ------------------------------------------------- */

  /** Read a video's captions as lines -- or, when there are none and the
   *  source is a media file, LISTEN to it.
   *
   *  Still not a job. Reading captions is one metadata read and one small
   *  download; recognition is minutes, which is what `asr` progress events
   *  are for. Job states would be a second vocabulary for a request that
   *  either returns a transcript or an error, and nothing in between that a
   *  caller can act on. */
  readTranscript: (payload: TranscriptRequest) =>
    request<Transcript>("/transcript", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  /** Which caption tracks exist, before committing to one. Costs a metadata
   *  read and downloads nothing, so the workspace can offer a language
   *  instead of letting the reader discover by failure that the original
   *  could not be determined. */
  captionTracks: (url: string) =>
    request<CaptionTracks>("/transcript:tracks", {
      method: "POST",
      body: JSON.stringify({ url }),
    }),

  /** Translate a transcript that already exists.
   *
   *  Takes a caption FILE, never a URL or a media path: translating is a
   *  second thing a person asks for once they have a transcript in front of
   *  them, and there is deliberately no route from an mp3 to here. */
  translate: (payload: TranslateRequest) =>
    request<TranslationResult>("/translate", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  /** Translate a DOCUMENT -- `.txt`, `.md`, `.markdown`.
   *
   *  A different call rather than a flag on `translate`, mirroring the two
   *  verbs underneath: `translate` reads one line as one utterance, which is
   *  right for captions and wrong for prose (D-131). `sourceLanguage` is
   *  required in practice here, and the refusal for its absence comes from
   *  the server so there is one sentence explaining it, not two. */
  translateDocument: (payload: TranslateDocRequest) =>
    request<DocumentTranslationResult>("/translate:doc", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  /** What COULD be corrected in a transcript. Writes nothing.
   *
   *  Separate from `applyCorrections` because the user is between the two
   *  calls. That is the whole shape of the feature rather than an API
   *  style: the version that decided and wrote in one step is the one that
   *  damaged correct text on every real firing (D-115). */
  proposeCorrections: (source: string, exactOnly = false) =>
    request<CorrectionOffer>("/correct:propose", {
      method: "POST",
      body: JSON.stringify({ source, exactOnly }),
    }),

  /** Write the corrected copy and the record of what changed.
   *
   *  `accepted` is a list of INDICES into the offer, never replacements. A
   *  call that could name its own before/after pair would let the renderer
   *  write anything at all, and the point of the glossary is that it is the
   *  only source of what may be written. */
  applyCorrections: (source: string, accepted?: number[], exactOnly = false) =>
    request<CorrectionWritten>("/correct:apply", {
      method: "POST",
      body: JSON.stringify({ source, exactOnly, ...(accepted ? { accepted } : {}) }),
    }),

  /** What a reading copy WOULD leave out. Writes nothing.
   *
   *  Two calls with the user between them, like the corrector -- and it
   *  matters more here, because this one DELETES. */
  proposeTidy: (source: string) =>
    request<TidyOffer>("/tidy:propose", {
      method: "POST",
      body: JSON.stringify({ source }),
    }),

  /** Write the reading copy. The original is not among the files.
   *
   *  `accepted` is a list of INDICES into the offer and can only NARROW it.
   *  The server recomputes what is eligible, so a renderer cannot ask for a
   *  cue the filler list does not cover. */
  applyTidy: (source: string, accepted?: number[]) =>
    request<TidyWritten>("/tidy:apply", {
      method: "POST",
      body: JSON.stringify({ source, ...(accepted ? { accepted } : {}) }),
    }),

  /** What the ticked stages WOULD do, in one call. Writes nothing.
   *
   *  One endpoint for one stage or both, so 「只做校正」 is a plan of length
   *  one rather than a different feature with its own code path. The order
   *  in `stages` is discarded: the server runs `refine.ORDER`, which is what
   *  makes the output of 校正＋整理 one artifact with one name instead of
   *  two spellings of the same content. */
  proposeRefine: (source: string, stages: RefineStage[], exactOnly = false) =>
    request<RefineOffer>("/refine:propose", {
      method: "POST",
      body: JSON.stringify({ source, stages, exactOnly }),
    }),

  /** Write the one set the ticked stages produce. The original is not among
   *  the files.
   *
   *  Both accepted lists are INDICES into the offer and can only NARROW it.
   *  The server recomputes the whole plan, so a renderer can neither ask for
   *  a substitution the glossary does not hold nor a deletion the filler
   *  list does not cover. */
  applyRefine: (
    source: string,
    stages: RefineStage[],
    acceptedCorrections?: number[],
    acceptedRemovals?: number[],
    exactOnly = false,
  ) =>
    request<RefineWritten>("/refine:apply", {
      method: "POST",
      body: JSON.stringify({
        source,
        stages,
        exactOnly,
        ...(acceptedCorrections ? { acceptedCorrections } : {}),
        ...(acceptedRemovals ? { acceptedRemovals } : {}),
      }),
    }),

  /** The filler whitelist. Empty is the normal state on a new machine, and
   *  an empty one removes nothing. */
  fillers: () => request<FillerReport>("/fillers"),

  /** Add terms, and/or the offered common set.
   *
   *  `common` is a flag rather than a default because nothing may ever be
   *  removed by a term the user did not put on their own list. */
  addFillers: (terms: string[], common = false) =>
    request<FillerReport>("/fillers:add", {
      method: "POST",
      body: JSON.stringify({ terms, common }),
    }),

  /** Drop terms. Nothing already written changes. */
  removeFillers: (terms: string[]) =>
    request<FillerReport>("/fillers:remove", {
      method: "POST",
      body: JSON.stringify({ terms }),
    }),

  /** The whitelist. Empty is the normal state on a new machine. */
  glossary: () => request<GlossaryReport>("/glossary"),

  /** Declare a term, and optionally the wrong form just seen.
   *
   *  The alias is what makes this worth doing: a correction made by hand
   *  once is matched exactly the next time, so the feature gets better from
   *  use rather than from a bigger model. */
  enrolTerm: (term: string, alias = "") =>
    request<GlossaryReport>("/glossary:enrol", {
      method: "POST",
      body: JSON.stringify({ term, alias }),
    }),

  /** Rewrite one entry, or add one in full.
   *
   *  `original` names the entry being replaced -- the term as it was BEFORE
   *  the edit, so the correct spelling itself can be corrected. Empty adds.
   *  A glossary that could only be appended to made its first typo
   *  permanent, and this list decides what the corrector may write. */
  saveTerm: (entry: {
    original?: string;
    term: string;
    aliases: string[];
    note?: string;
  }) =>
    request<GlossaryReport>("/glossary:update", {
      method: "POST",
      body: JSON.stringify({
        original: entry.original ?? "",
        term: entry.term,
        aliases: entry.aliases,
        note: entry.note ?? "",
      }),
    }),

  /** Forget a term. Transcripts already corrected with it keep their
   *  corrections -- this only stops it being proposed again. */
  removeTerm: (term: string) =>
    request<GlossaryReport>("/glossary:remove", {
      method: "POST",
      body: JSON.stringify({ term }),
    }),

  /* --- 語音辨識的準備 --------------------------------------------------- */

  /** Can this machine transcribe, and if not, what is left to do.
   *
   *  One call, not four. The panel used to be assembled from `getDoctor`
   *  plus `getConfig` plus the renderer's own idea of what a model folder
   *  is -- which is how the settings panel ended up showing a row labelled
   *  `asr` with a path in it and no way to act on either. */
  asrReadiness: () => request<AsrReadiness>("/asr/readiness"),

  /** What models are in a folder the user just picked. Costs a directory
   *  walk and a few header reads; loads nothing. */
  asrScanModels: (path: string) =>
    request<AsrScanResult>("/asr/models:scan", {
      method: "POST",
      body: JSON.stringify({ path }),
    }),

  /** Bring one in. Minutes for a copy, instant for a link -- progress
   *  arrives on the event stream, not on this promise. */
  asrInstallModel: (payload: { path: string; mode: AsrInstallMode; name?: string }) =>
    request<{ model: AsrModel; readiness: AsrReadiness }>("/asr/models:install", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  asrSelectModel: (name: string) =>
    request<AsrReadiness>("/asr/models:select", {
      method: "POST",
      body: JSON.stringify({ name }),
    }),

  /** Only ever removes a shortcut; the server refuses anything else, so a
   *  real 3 GB folder can never be deleted by a click in here. */
  asrRemoveModel: (name: string) =>
    request<AsrReadiness>("/asr/models:remove", {
      method: "POST",
      body: JSON.stringify({ name }),
    }),

  /** Point at a Python. Validated before it is saved, so the answer to
   *  "did that work" arrives with the action instead of at the next
   *  transcription. */
  asrSetEngine: (path: string) =>
    request<AsrReadiness>("/asr/engine", {
      method: "POST",
      body: JSON.stringify({ path }),
    }),

  asrSetHome: (path: string) =>
    request<AsrReadiness>("/asr/home", {
      method: "POST",
      body: JSON.stringify({ path }),
    }),

  asrSetDownload: (allowed: boolean) =>
    request<AsrReadiness>("/asr/download", {
      method: "POST",
      body: JSON.stringify({ name: allowed ? "on" : "off" }),
    }),

  asrCatalogue: () => request<{ models: AsrCatalogueEntry[] }>("/asr/catalogue"),

  asrDownloadModel: (name: string) =>
    request<{ model: AsrModel; readiness: AsrReadiness }>("/asr/models:download", {
      method: "POST",
      body: JSON.stringify({ name }),
    }),

  asrSetAudio: (mode: string) =>
    request<AsrReadiness>("/asr/audio", {
      method: "POST",
      body: JSON.stringify({ name: mode }),
    }),

  /* --- 外部程式（yt-dlp / ffmpeg / Chrome） ----------------------------- */

  /** Every external program, with which copy is actually in use. Runs each
   *  one to read its version, so it is a few hundred milliseconds, not
   *  instant -- called when a panel opens, never on a timer. */
  tools: () => request<{ tools: ToolStatus[] }>("/tools"),

  /** Fetch, verify and wire up one program. Minutes for ffmpeg; progress
   *  arrives on the event stream, and this promise resolves once it can
   *  run. Only ever called from a button. */
  installTool: (name: string) =>
    request<ToolStatus>("/tools:install", {
      method: "POST",
      body: JSON.stringify({ name }),
    }),

  /** Drop the copy this program installed. A copy on PATH is untouched. */
  removeTool: (name: string) =>
    request<ToolStatus>("/tools:remove", {
      method: "POST",
      body: JSON.stringify({ name }),
    }),

  /* --- 一次性說明 ------------------------------------------------------- */

  guides: () => request<Guides>("/guides"),

  /** Append, server-side. Not a `PUT /config`: two guides dismissed in the
   *  same second would each send the config they read before the other's
   *  write, and the second would silently drop the first. */
  markGuideSeen: (id: string) =>
    request<Guides>("/guides:seen", {
      method: "POST",
      body: JSON.stringify({ id }),
    }),

  resetGuides: () => request<Guides>("/guides:reset", { method: "POST" }),

  /* --- 紀錄 ------------------------------------------------------------ */

  readLogs: (limit = 200) => request<LogsReport>(`/logs?limit=${limit}`),
  clearLogs: () => request<SweepResult>("/logs:clear", { method: "POST" }),
  clearErrorBundles: () => request<SweepResult>("/logs:clearErrors", { method: "POST" }),
};

export type Api = typeof api;
