/**
 * Wire types for the `/v1` API (PSM Batch 2 §4.3).
 *
 * Hand-written rather than generated: the Python side is the single source of
 * truth, and a generator would be one more build step to keep alive for a
 * surface this small. The tradeoff is that a backend field rename shows up as
 * a runtime `undefined` here, not a compile error -- which is why the render
 * path treats every optional field as genuinely optional.
 */

export type TaskState =
  | "PARSED"
  | "PROBING"
  | "READY"
  | "DOWNLOADING"
  | "PAUSED"
  | "EXPIRED"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED";

export const TERMINAL_STATES: readonly TaskState[] = ["COMPLETED", "FAILED", "CANCELLED"];

export type Platform =
  | "instagram"
  | "threads"
  | "youtube"
  | "x"
  | "bilibili"
  | "generic";

export interface Variant {
  kind: string;
  url: string;
  width?: number | null;
  height?: number | null;
  ext?: string | null;
  sizeBytes?: number | null;
  requiresMux?: boolean;
}

export interface TaskProgress {
  bytesDone: number;
  bytesTotal: number | null;
  bytesPerSec: number;
  etaSeconds: number | null;
  itemsDone: number;
  itemsTotal: number | null;
  /**
   * Which half of a muxed item is running. Bytes never move during a mux,
   * so a percentage alone reaches 100% and then sits there while ffmpeg
   * works -- the bar says finished and the file does not exist yet.
   */
  phase?: "transferring" | "muxing" | null;
}

export interface Task {
  id: string;
  state: TaskState;
  platform: Platform;
  postId: string;
  canonicalUrl: string;
  sourceUrl: string;
  hintIndex: number | null;
  author: string | null;
  title: string | null;
  /** `null` means "inherit the global policy" (§7.2). */
  policy: string | null;
  /** True once the row was overridden by hand; global changes stop moving it (§7.4). */
  policyPinned: boolean;
  variants: Variant[];
  chosen: Variant | null;
  selected: boolean;
  selectedIndices: number[] | null;
  expiresAt: string | null;
  progress: TaskProgress;
  outputDir: string | null;
  partPath: string | null;
  pathDegradation: "L0" | "L1";
  errorCode: string | null;
  createdAt: string;
  updatedAt: string;
  completedAt: string | null;
}

/**
 * A recognized post this machine currently cannot fetch. Not the same as
 * `unrecognized`: we know exactly what it is, and are declining anyway.
 */
export interface BlockedItem {
  platform: Platform;
  /** Wire code from `capabilities.py`. The label lives in `blocked.ts`. */
  reason: string;
  url: string;
}

export interface ParseReport {
  glued: number;
  extracted: number;
  trackingStripped: string[];
  duplicatesInBatch: number;
  alreadyInQueue: number;
  unrecognized: string[];
  /** Optional so a renderer built against an older server does not crash. */
  blocked?: BlockedItem[];
}

/** One recognized post from `POST /v1/input/parse`. Not yet a queue Task. */
export interface ParsedItem {
  platform: Platform;
  postId: string;
  canonicalUrl: string;
  sourceUrl: string;
  hintIndex: number | null;
}

export interface ParseResult {
  items: ParsedItem[];
  report: ParseReport;
}

export interface AddResponse {
  tasks: Task[];
  report: ParseReport;
}

export interface BulkActionResult {
  action: string;
  changed: number;
  /** Rows the action did not apply to. Not an error. */
  skipped: number;
  tasks: Task[];
}

export interface RemovalResult {
  removed: number;
  cancelledInFlight: number;
  partFilesDeleted: number;
  outputFilesDeleted: number;
  removedIds: string[];
}

/** Broadcast when records leave the queue, so other tabs drop those rows. */
export interface TasksRemoved {
  ids: string[];
}

export interface AppConfig {
  schemaVersion: 1;
  outputRoot: string;
  policy: string;
  /** O-6: drop COMPLETED records older than this many days. `0` = off.
   *  Records only -- never the downloaded files. */
  autoClearDays: number;
  /** How long a day's run log is kept. `0` = off. Error bundles are NOT
   *  covered by it: they are kept until deleted by hand. */
  logRetentionDays: number;
  chrome: { visible: boolean; executablePath: string | null; profileDir: string | null };
  serve: { host: string; port: number };
  /** The server owns more fields than the panel edits (budget, binaries).
   *  They round-trip untouched through PUT /config, so they stay typed as
   *  unknown rather than being narrowed here and silently dropped. */
  [key: string]: unknown;
}

/**
 * One external program, and everything a stuck person needs to know about it.
 *
 * `source` is the field this type exists for. 「已安裝」 alone cannot answer
 * 「我明明更新了，為什麼還是舊版」: yt-dlp can be present three ways at once --
 * set in the config file, installed by this program into its own tools
 * folder, or sitting on PATH -- and only one of them is being run.
 */
export interface ToolStatus {
  name: string;
  installed: boolean;
  /** Which copy is answering. `toolchain.SOURCES` is the authoritative list
   *  — "configured" | "managed" | "path" | "system" — and null means nothing
   *  answered. Rendered by `describeSource`, which a Python test checks
   *  against that tuple. */
  source: string | null;
  path: string | null;
  version: string | null;
  /** False for Chrome: we can link to it and must not install it. */
  manageable: boolean;
  /** Roughly how big the transfer is, so a warning can precede it. */
  approxBytes: number | null;
  homepage: string | null;
  /** Where the managed copy came from, and when. Null unless `source` is
   *  "managed" -- provenance for a file this program put on the disk. */
  installedFrom: string | null;
  installedAt: string | null;
}

/** A managed install, reporting itself. Same channel discipline as
 *  `AsrInstallProgress`: the promise resolves at the END, this arrives
 *  throughout, and a 106 MB transfer with neither would look like a hang. */
export interface ToolInstallProgress {
  tool: string;
  /** "resolving" | "downloading" | "installing" | "done". */
  phase: string;
  bytes?: number;
  total?: number;
  detail?: string;
}

/** Which one-time explanations this person has already been shown. Server
 *  state, not browser state -- see `GuidesConfig` for why. */
export interface Guides {
  seen: string[];
}

/**
 * What this build can actually do. Both flags are false while M2/M3
 * acquisition is frozen; the UI reads them so a task that cannot advance is
 * explained rather than left looking wedged.
 */
export interface Capabilities {
  probe: boolean;
  download: boolean;
}

export interface Health {
  ok: boolean;
  apiVersion: number;
  capabilities: Capabilities;
  /** True when the queue file was unreadable and was rebuilt empty (§4.5). */
  queueRebuilt?: boolean;
}

export interface Notice {
  level: "info" | "warn" | "error";
  code: string;
  /**
   * Prose, where the sender is the only one who knows the sentence.
   *
   * Optional on purpose. The SERVER must not fill this in: it is English by
   * contract, so a sentence written there reaches the user in the wrong
   * language -- which is exactly what happened, a red banner that suddenly
   * spoke English after a bulk removal. Server-sent notices carry a `code`
   * and the fields below, and `presentNotice` writes the sentence, the same
   * way `presentError` already does for a row.
   */
  message?: string;
  /** Free-form extra from the sender, appended after the presented text. */
  detail?: string;
  taskId?: string;
  /** `records_removed` only. */
  removed?: number;
  cancelledInFlight?: number;
  /**
   * `formats_excluded` only: reason -> how many rows the source offered that
   * were never quality options (storyboard sheets, audio-only streams,
   * fragmented renditions this build cannot move).
   */
  excluded?: Record<string, number>;
}

export interface QueueStats {
  active: number;
  queued: number;
  done: number;
  failed: number;
  aggregateBytesPerSec: number;
  budgetRemainingThisHour: number | null;
}

export interface BudgetWait {
  platform: string;
  resumesInMs: number;
  reason: string;
}

/** The `{errorCode, message, detail}` body every non-2xx response carries. */
export interface ApiErrorBody {
  errorCode: string;
  message: string;
  detail?: unknown;
}

/** One dependency row from `GET /v1/doctor`. */
export interface DoctorCheck {
  name: string;
  found: boolean;
  path: string | null;
  version: string | null;
  ok: boolean;
  errorCode: string | null;
  detail: string | null;
  required: boolean;
  versionStatus: string | null;
}

export interface DoctorReport {
  ok: boolean;
  checks: DoctorCheck[];
  exitCode: number;
}

/* --- 延伸工具：引用長圖 (`/v1/stack`) ------------------------------------ */

/**
 * A stack job's states. Deliberately NOT `TaskState`: a transfer's PROBING
 * and DOWNLOADING describe bytes moving, and borrowing them for "scan a
 * band" would put a word on screen that means something else everywhere
 * else in this app.
 */
export type StackJobState =
  | "QUEUED"
  | "PREPARING"
  | "RUNNING"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED";

export const STACK_TERMINAL_STATES: readonly StackJobState[] = [
  "COMPLETED",
  "FAILED",
  "CANCELLED",
];

/** One run's parameters, named after the CLI flags they become. */
export interface StackRequest {
  video: string;
  subs?: string | null;
  roi?: string | null;
  start?: string | null;
  end?: string | null;
  offset?: string | null;
  subLang?: string;
  maxStrips?: number | null;
  preview?: boolean;
  settings?: Record<string, string>;
  out?: string | null;
}

export interface StackJob {
  id: string;
  state: StackJobState;
  request: StackRequest;
  /** The equivalent `mfp stack ...` line, built server-side. */
  command: string[];
  /** The last thing the run said. Text, not a percentage: the phases have
   *  no common unit and a bar that invents one lies. */
  message: string | null;
  output: string | null;
  preview: string | null;
  source: string | null;
  strips: number | null;
  cuesFound: number | null;
  truncated: number | null;
  width: number | null;
  height: number | null;
  errorCode: string | null;
  errorDetail: string | null;
  /** Folder under `logs/errors/`, when this one left evidence. */
  errorBundle: string | null;
  createdAt: string;
  finishedAt: string | null;
}

/**
 * One frame, and the numbers a drag needs to become a pixel row.
 *
 * `frameHeight` is the video's own height -- what `--roi` is expressed in --
 * while `imageHeight` is what the browser lays out. Both are sent so the
 * picker converts without knowing how the server scaled anything.
 */
export interface FrameShot {
  frameWidth: number;
  frameHeight: number;
  imageWidth: number;
  imageHeight: number;
  duration: number;
  at: number;
  image: string;
}

/** One stackable video, and the captions already beside it. */
export interface StackSource {
  path: string;
  width: number;
  height: number;
  duration: number;
  captions: string[];
}

export interface LogsReport {
  logDir: string;
  errorDir: string;
  files: number;
  fileBytes: number;
  bundles: number;
  bundleBytes: number;
  retentionDays: number;
  lines: Record<string, unknown>[];
}

export interface SweepResult {
  files: number;
  bundles: number;
  bytesFreed: number;
  keptToday: boolean;
}

/* --- 延伸工具：逐字稿 (`/v1/transcript`) --------------------------------- */

/**
 * One read's parameters. `target` takes all three shapes the CLI takes -- a
 * post URL, a downloaded video whose captions sit beside it, or a caption
 * file -- so the GUI never has to decide which it is holding.
 */
export interface TranscriptRequest {
  target: string;
  subLang?: string;
  start?: string | null;
  end?: string | null;
  refresh?: boolean;
  /** "auto" | "always" | "never" -- when the AUDIO may be transcribed
   *  instead of a caption track read. "auto" is the default and means "only
   *  when no caption track exists", which for an mp3 is always. */
  recognize?: string;
  /** Spoken language for recognition; "auto" detects it from the audio. */
  asrLanguage?: string;
}

/**
 * How far along a transcription is.
 *
 * Reading a caption track is a second's work and needs none of this.
 * LISTENING to an hour of audio is minutes, and a request that says nothing
 * for minutes is indistinguishable from a hung server -- so the engine's
 * progress is forwarded onto the same event stream everything else uses.
 *
 * `phase` walks `decoded` → `loaded` → `language` → `segment`; only
 * `segment` carries `at`, and only `at` against `duration` is a fraction.
 */
export interface AsrProgress {
  phase: string;
  at?: number;
  duration?: number;
  lines?: number;
  language?: string;
  /** `phase: "retry"` only: which rung of the recognition ladder is starting,
   *  and the sentence explaining why the first attempt was not kept. */
  rung?: number;
  reason?: string;
}

/**
 * What the engine noticed about its OWN output, arriving as a final
 * `phase: "health"` progress record.
 *
 * It travels on the progress stream rather than in the response for a
 * reason: the server forwards `on_progress` to the event stream and drops
 * the engine's human-readable lines entirely, so a warning printed for the
 * CLI's stderr is invisible here. The people most likely to hand this a
 * two-hour recording are the ones using the window, not the terminal.
 *
 * Every field is optional. An older engine build reports none of them, and
 * a transcript is the deliverable either way -- this only decides what can
 * be SAID about it.
 */
/**
 * One determinable fact about the transcript now on screen, from
 * `mfp.quality`, arriving as a final `phase: "findings"` progress record.
 *
 * Distinct from `AsrHealth` on purpose. Health is the ENGINE's report on
 * itself and can only be believed; a finding is a property of the file that
 * anyone holding it can re-derive. When the two disagree, this is the
 * evidence.
 */
export interface TranscriptFinding {
  /** Machine-stable: `simplified-script`, `repeated-line`, `stops-early`,
   *  `sparse-text`, `presentation-punctuation`, `backwards-cue`,
   *  `thin-coverage`, `language-drift`, `contested-language`,
   *  `instruction-capture`. Unknown codes still render — the sentence comes
   *  from the server, so a new check needs no change here. */
  code: string;
  /** `warn` — look at this before using the transcript. `note` — true and
   *  worth knowing, not a problem by itself. */
  severity: "warn" | "note" | string;
  /** A finished Traditional Chinese sentence, written by the layer that
   *  knows the fact. Same rule `AsrReadiness` follows. */
  detail: string;
  /** Seconds into the transcript, when the finding has a place. */
  at?: number | null;
  evidence?: Record<string, unknown>;
}

/**
 * One passage of a recording, and the language it was decoded in.
 *
 * A recording is not required to be in one language, and until the engine
 * mapped them it decided once for the whole file — which on a meeting that
 * switches partway meant the second half was invented rather than heard.
 * The plan is what the product can now SAY about that, and it arrives on the
 * `phase: "language"` progress record before any segment does.
 */
export interface LanguageStretch {
  language: string;
  /** Seconds from the start of the recording. */
  start: number;
  end: number;
  /** How many of this passage's own 30-second windows voted for its
   *  language. Below 0.8 the two languages were alternating faster than a
   *  window can follow, and those minutes are worth less than the rest. */
  agreement?: number;
  windows?: number;
}

export interface AsrHealth {
  segments?: number;
  peakMemoryMB?: number | null;
  /** Windows the engine had to retry at a higher temperature. */
  temperatureFallbacks?: number;
  /** ...far enough that it threw its running context away. Each one is a
   *  seam where the transcript's continuity restarts. */
  contextDrops?: number;
  lowConfidence?: number;
  longestRepeatRun?: number;
  repeatedText?: string | null;
  /** Whisper's known long-audio failure: the same line until the audio ends. */
  degenerated?: boolean;
}

/* --- 語音辨識的準備狀態 ------------------------------------------------ */

/**
 * One model folder, already judged by the server.
 *
 * Nothing here is re-derived on this side, and that is the point. Whether a
 * folder is a usable model is decided by reading its bytes, which only the
 * server can do; a renderer that re-implemented any part of that would
 * eventually disagree with the process that actually loads the thing.
 * `summary` and `notes` arrive as finished Traditional Chinese sentences for
 * the same reason.
 */
/** What a model is FOR. The field the whole two-capability display hangs
 *  off, and the one thing a person has to see at a glance about a folder
 *  they downloaded. Read from the model's own bytes by the server. */
export type ModelKind = "recognition" | "translation" | "unknown";

export interface AsrModel {
  name: string;
  path: string;
  state:
    | "ready"
    | "unknown_kind"
    | "needs_conversion"
    | "incomplete"
    | "not_a_model"
    | "unreadable";
  /** Complete and loadable AS ITS KIND. Never enough on its own -- `kind` is
   *  the other half of every question about a model. */
  usable: boolean;
  kind: ModelKind;
  /** `kind` in the words shown on screen, carried rather than mapped here,
   *  so the CLI and this panel cannot drift into two vocabularies. */
  kindLabel: string;
  /** Which tokenizer file a translation model carries. */
  tokenizer: string | null;
  /** The pip package needed to read it, or null when the engine already has
   *  everything. The difference between "download a model" and "download a
   *  model AND install a package". */
  tokenizerPackage: string | null;
  /** FLORES-200 codes a translation model names. Empty when it did not say. */
  languageCodes: string[];
  summary: string;
  notes: string[];
  sizeBytes: number;
  layout: "plain" | "hub" | null;
  spec: string | null;
  specRevision: number | null;
  binaryVersion: number | null;
  melBins: number | null;
  languages: number | null;
  suggestedName: string | null;
  /** A junction rather than the model itself. The one thing this panel may
   *  remove, because removing it deletes nothing. */
  isLink: boolean;
}

/** One remaining setup step. `action` names a button, never a command. */
export interface AsrSetupStep {
  text: string;
  /** `pick-model` browses for a folder that is not here yet;
   *  `choose-installed` picks among the ones that already are. The
   *  distinction is the whole of C7: they used to be one name, so a step
   *  reading 「選一個已經在資料夾裡的辨識模型」 rendered a button that opened
   *  a browse-for-a-new-folder dialog. `open-home` was in this union and
   *  emitted by no branch on the server. */
  action: "pick-engine" | "pick-model" | "choose-installed" | "guide" | null;
}

export interface AsrEngineStatus {
  present: boolean;
  path: string | null;
  version: string | null;
  source: "environment" | "settings" | "beside-the-app" | null;
  problem: string | null;
}

export interface AsrHomeStatus {
  path: string;
  exists: boolean;
  writable: boolean;
  freeBytes: number | null;
  configured: boolean;
}

/** `model_unusable` used to be here and no branch produced it: a folder that
 *  will not load is a `AsrModel` with `usable: false`, which the model list
 *  says, not a state of the capability. */
export type AsrCapabilityState = "ready" | "no_engine" | "engine_broken" | "no_model";

/**
 * One thing this machine can or cannot do, and what is missing for it.
 *
 * The type that answers the actual complaint: somebody who does not know
 * what a model IS still has to be able to look at a list and see which of
 * the things they wanted works, which does not, and what KIND of file the
 * broken one is waiting for. Nothing here requires knowing the word
 * "CTranslate2".
 */
export interface AsrCapability {
  id: "recognition" | "translation";
  label: string;
  /** One clause: 把聲音變成文字. */
  what: string;
  ready: boolean;
  state: AsrCapabilityState;
  headline: string;
  detail: string;
  steps: AsrSetupStep[];
  active: AsrModel | null;
  configured: string;
  /** The kind of model this needs, and its screen label. The pair is what
   *  lets the panel say 「還缺一個翻譯模型」 rather than 「還缺一個模型」. */
  needsKind: ModelKind;
  needsLabel: string;
  /** How many usable models of that kind are in the folder. Zero means "go
   *  and get one"; more than zero means "pick one". */
  available: number;
  optional: boolean;
}

/** The whole setup question, answered once by the server. */
export interface AsrReadiness {
  /** RECOGNITION only. Translation is opt-in, and a machine with no
   *  translation model is not "not ready" -- it has not opted in. */
  ready: boolean;
  capabilities: AsrCapability[];
  engine: AsrEngineStatus;
  home: AsrHomeStatus;
  /** Everything in the folder, of either kind, usable or not. */
  models: AsrModel[];
  allowDownload: boolean;
  /** `none` | `level` | `denoise` — see the option list in the setup panel. */
  audio?: string;
  canLink: boolean;
  linkDetail: string;
}

/** Lines translated so far. Counted in LINES, never in seconds: translation
 *  has no notion of the audio's length, and a bar in the wrong unit is worse
 *  than one in a coarse unit. */
export interface MtProgress {
  phase: string;
  done?: number;
  total?: number;
}

export interface TranslateRequest {
  source: string;
  target: string;
  sourceLanguage?: string;
}

export interface TranslationResult {
  source: string;
  sourceLanguage: string;
  targetLanguage: string;
  lineCount: number;
  engine: Record<string, unknown>;
  /** 引擎疑似把哪幾行切短了（1 起算）。多半是空的。
   *
   * 這個欄位存在，是因為它本來不存在：翻譯路徑早就算得出來，CLI 也會印，
   * 只有看不到 stderr 的這一端從來沒被告知。 */
  suspectLines: number[];
}

/* --- 文件翻譯 (`/v1/translate:doc`) --------------------------------------- */

/** 一份文件的翻譯請求。
 *
 * `sourceLanguage` 在這裡是必填的：逐字稿的檔名帶著語言，文件沒有，猜錯會
 * 得到一份讀起來很順、但不是任何東西的翻譯。型別上仍是選填，因為拒絕這件事
 * 由伺服器負責講清楚——前端再寫一次同一句話，就有兩份會走鐘的說法。 */
export interface TranslateDocRequest {
  source: string;
  target: string;
  sourceLanguage?: string;
}

/** 文件翻譯的結果。`TranslationResult` 的欄位全都在，外加這個動詞自己數得出
 *  來的結構數字，以及寫在譯文旁邊的紀錄檔。 */
export interface DocumentTranslationResult extends TranslationResult {
  /** 譯文旁邊的 `<stem>.<flores>.translation.json`：這份譯文是從哪來的、用
   *  哪個模型、切了幾個子句、哪幾行可能被截短。舊版 sidecar 沒有這個欄位，
   *  所以讀的時候要當它可能不存在。 */
  record?: string | null;
  /** 文件被切成幾個區塊，其中幾個是原封不動保留的（程式碼、表格、front
   *  matter…）。一份滿是程式碼的文件卻回報 0，代表它沒有被當成 Markdown 解析。 */
  blocks: number;
  verbatimBlocks: number;
  sentences: number;
}

/* --- 整理版：語助詞刪減 (`/v1/tidy`, `/v1/fillers`) ----------------------- */

/** 一句「整句都是語助詞」的句子，以及把它放回去所需要的一切。 */
export interface TidyRemoval {
  /** 0 起算。畫面上顯示「第 N 句」時才加一。 */
  cue: number;
  text: string;
  start: number;
  end: number;
}

export interface TidySummary {
  cues: number;
  removed: number;
  kept: number;
  removedChars: number;
  totalChars: number;
  share: number;
}

/** 整理版**會**刪掉什麼。什麼都還沒寫。 */
export interface TidyOffer {
  source: string;
  cues: TranscriptCue[];
  removals: TidyRemoval[];
  summary: TidySummary;
  /** 清單上有幾個詞。0 代表不可能刪掉任何東西——不說清楚的話，
   *  「沒有東西可刪」看起來會像壞掉而不是還沒設定。 */
  fillerTerms: number;
}

export interface TidyWritten {
  /** 沒有被寫到的那個檔。會出現在回應裡，是因為「你的原稿還在」正是
   *  整個流程立足的那句話。 */
  original: string;
  written: Record<string, string>;
  removed: number;
}

export interface FillerReport {
  path: string;
  terms: string[];
}

/* --- 後補正：術語校正 (`/v1/correct`, `/v1/glossary`) --------------------- */

/** One substitution the corrector is OFFERING. Nothing here has happened.
 *
 *  `tier` is the difference between "you told me this spelling was wrong"
 *  and "this merely sounds like a term you declared". They are shown
 *  differently on purpose: the second one is a guess, and a reader who
 *  cannot tell them apart cannot review the list. */
export interface CorrectionProposal {
  /** Index into the `cues` array of the same response. */
  cue: number;
  /** Character offsets within that cue's text. */
  start: number;
  end: number;
  was: string;
  now: string;
  term: string;
  /** `exact` — a spelling the user enrolled. `phonetic` — same reading. */
  tier: "exact" | "phonetic" | string;
  /** The reading that matched, for a phonetic hit. Shown, because
   *  「機板 和 基板 都是 ji ban」 IS the argument for the substitution. */
  key?: string;
  /** The engine's own probability over the characters being replaced, or
   *  null when the transcript predates word-level evidence. Null is not
   *  zero: it means nobody measured, not that the engine was unsure. */
  confidence: number | null;
}

export interface TranscriptCue {
  start: number;
  end: number;
  text: string;
}

export interface CorrectionOffer {
  source: string;
  /** The transcript as it stands — the "before" side, so nothing has to
   *  read the file twice. */
  cues: TranscriptCue[];
  proposals: CorrectionProposal[];
  /** The same list as prose, built on the server so the CLI and the GUI
   *  cannot drift into two descriptions of one change. */
  diff: string;
  /** False when the phonetic tier is unavailable. Exact matches still work;
   *  a UI that stays silent about this looks broken instead of degraded. */
  phoneticKeys: boolean;
  glossaryEntries: number;
}

export interface CorrectionWritten {
  /** The file that was NOT written to. Named in the response because "your
   *  original is intact" is the promise this whole flow rests on. */
  original: string;
  written: Record<string, string>;
  applied: number;
}

/* --- 一次做完：校正＋整理 (`/v1/transcript/refine`) ----------------------- */

/** 管線上的一段。順序不在這裡決定——送什麼順序上去都一樣，
 *  伺服器一律照 `refine.ORDER`（先校正、後整理）跑。 */
export type RefineStage = "correct" | "tidy";

/** 勾起來的那幾段**會**做什麼。什麼都還沒寫。 */
export interface RefineOffer {
  source: string;
  /** 實際會跑的順序，不一定是送上去的順序。 */
  stages: RefineStage[];
  /** 原稿。 */
  cues: TranscriptCue[];
  /** 校正之後的樣子——下面那些 `removals` 是對著它算的，也是讀者
   *  真正在決定的東西。沒有勾校正時就等於 `cues`。 */
  corrected: TranscriptCue[];
  proposals: CorrectionProposal[];
  diff: string;
  removals: TidyRemoval[];
  /** 沒有勾整理時是空物件。 */
  summary: Partial<TidySummary>;
  phoneticKeys: boolean;
  glossaryEntries: number;
  fillerTerms: number;
}

export interface RefineWritten {
  /** 沒有被寫到的那個檔。「你的原稿還在」是整個流程立足的那句話。 */
  original: string;
  stages: RefineStage[];
  written: Record<string, string>;
  corrected: number;
  removed: number;
}

export interface GlossaryEntry {
  term: string;
  aliases: string[];
  note: string;
}

export interface GlossaryReport {
  path: string;
  entries: GlossaryEntry[];
  phoneticKeys: boolean;
}

export type AsrInstallMode = "copy" | "move" | "link";

/** One of the three ways a model can be brought in, with its consequence
 *  and whether the disk actually allows it. `available` is measured on the
 *  server by making a junction and removing it -- a greyed-out option that
 *  guessed would be wrong in both directions. */
export interface AsrInstallModeOption {
  mode: AsrInstallMode;
  label: string;
  detail: string;
  available: boolean;
  reason: string | null;
  warnings: string[];
}

export interface AsrScanResult {
  models: AsrModel[];
  /** The verdict on the folder that was picked, so an empty `models` can be
   *  explained in that folder's own terms instead of silently. */
  diagnosis: AsrModel;
  modes: AsrInstallModeOption[];
}

/**
 * Bytes moved so far, on the shared event stream. Same shape of problem as
 * `AsrProgress`: minutes of work that must not look like a hang.
 *
 * Two producers, one channel. `asr_models.install_model` copies from disk and
 * reports `copied`; `asr/fetch_model.py` downloads and reports `bytes`. They
 * are kept as separate names rather than merged, because 「複製中 40%」 and
 * 「下載中 40%」 are different promises to the reader and `describeInstall` /
 * `describeFetch` must not be able to render each other's frames.
 */
export interface AsrInstallProgress {
  phase: string;
  /** `install_model`: bytes copied. */
  copied?: number;
  /** `fetch_model.py`: bytes downloaded. */
  bytes?: number;
  total?: number;
  file?: string;
  /** `fetch_model.py`, `phase: "sized"`: how many files the repo holds. */
  files?: number;
}

/**
 * One line, and when it was said.
 *
 * No end time, because the sources carry none: a caption cue has one, but on
 * the rolling-caption path several cues share a line and the line's own end
 * is not any of theirs. The next line's `at` is the end.
 */
export interface TranscriptLine {
  at: number;
  text: string;
}

export interface Transcript {
  /** The caption file this was read out of. Handed straight to
   *  `stack --subs` when the reader picks lines to quote, so reading and
   *  then quoting costs one platform request rather than two. */
  source: string;
  /** "written" | "automatic" | "beside" | "named" | "recognized" -- a machine
   *  transcription and a human one are not the same evidence, and the reader
   *  should not have to guess which they are looking at. `recognized` is the
   *  strongest form of that warning: nobody wrote those words down at all,
   *  they were heard. */
  kind: string;
  language: string | null;
  title: string | null;
  lineCount: number;
  lines: TranscriptLine[];
}

export interface CaptionTracks {
  title: string | null;
  spokenLanguage: string | null;
  written: string[];
  /** Separate from the rest because the rest is a hundred machine
   *  translations and this is the one that is actually the video. */
  automaticOriginal: string[];
  automaticCount: number;
}

/**
 * One model this product will fetch for you.
 *
 * A short curated list rather than a search over the Hub: a wrong repo id
 * costs a multi-gigabyte download that ends in 「這不是可以用的模型」, so every
 * entry was checked against the Hub for existence, files and size rather
 * than written from memory.
 */
export interface AsrCatalogueEntry {
  id: string;
  repo: string;
  kind: "recognition" | "translation";
  label: string;
  /** One sentence saying what this one is FOR, in the reader's terms. */
  detail: string;
  /** Display figure, so a size can be shown before anything is committed to. */
  bytes: number;
  recommended: boolean;
  installed: boolean;
}


/* --- 延伸工具：貼文解說 (`/v1/brief`) ------------------------------------ */

/** A picture the post carries, already on disk.
 *
 *  `width`/`height` describe the FILE and are null when nothing could
 *  resolve them -- the file is fine, the metadata is not. */
export interface BriefImage {
  index: number;
  path: string;
  bytes: number;
  width: number | null;
  height: number | null;
  altText: string | null;
}

/** An item of the post that is NOT a picture, and why.
 *
 *  Every item is in `images` or here, which is how a reader knows a video
 *  was in the post at all. */
export interface BriefSkipped {
  index: number;
  kind: string;
  reason: string;
  detail: string | null;
}

/** The author's own words.
 *
 *  Its own block, and the SHAPE is the warning (INV-B6): nobody reaches a
 *  caption without passing through the word `untrusted`. Whoever renders
 *  this must present it as a quotation from a stranger and must never treat
 *  it as an instruction. */
export interface BriefUntrusted {
  caption: string | null;
  altText: Record<string, string | null>;
  /** The file beside the images holding everything above, with a first line
   *  that says whose words they are. `null` when the post had no text.
   *
   *  In here rather than beside `analysisPath`, deliberately: a path is a way
   *  of reaching the author's words, and INV-B6 is about there being exactly
   *  one door with a sign on it. */
  textPath: string | null;
}

/** A video that WAS transferred, because the caller asked for it.
 *
 *  Its own list rather than a row in `images`: an agent iterating `images`
 *  reads them, and nothing can read a video. This is the file you run
 *  逐字稿 over. */
export interface BriefVideo {
  index: number;
  path: string;
  bytes: number;
  width: number | null;
  height: number | null;
}

export interface BriefPost {
  platform: string;
  url: string;
  id: string;
  author: string | null;
  timestamp: string | null;
  /** The analysis run. Pass it back verbatim to save an explanation. */
  postDir: string;
}

export interface BriefExisting {
  entries: number;
  lastWrittenAt: string | null;
}

export interface BriefBudget {
  platform: string;
  requestsUsed: number;
  requestsRemaining: number;
  nextAllowedAt?: string | null;
}

/** What `/v1/brief` returns.
 *
 *  There is no field here holding prose ABOUT the pictures, and there will
 *  not be: this tool runs no model (D-88, `INV-P8`), and the explanation is
 *  written by whoever looked. */
export interface BriefPackage {
  schemaVersion: number;
  lane: string;
  post: BriefPost;
  untrusted: BriefUntrusted;
  images: BriefImage[];
  /** Empty unless `withVideo` was asked for; then every video is here and
   *  none is in `skipped`. */
  videos: BriefVideo[];
  skipped: BriefSkipped[];
  /** Where an explanation would be written. Nothing is written until save. */
  analysisPath: string;
  existing: BriefExisting | null;
  budget: BriefBudget;
  /** True means the post was already on disk and no platform request was
   *  made -- asking again is free. */
  reused: boolean;
  degraded: boolean;
  degradedReason: string | null;
}

export interface BriefRequest {
  url: string;
  lane?: string;
  refresh?: boolean;
  /** Fetch the post's video(s) too, so 逐字稿 has a file to run over.
   *  Costs bandwidth and is off unless asked for. */
  withVideo?: boolean;
}

export interface BriefSaveRequest {
  post: string;
  body: string;
  lane?: string;
  question?: string;
}

export interface BriefSaved {
  analysisPath: string;
  entries: number;
}
