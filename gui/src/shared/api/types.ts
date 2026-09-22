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
  /** `null` means "inherit the global 一併存字幕 setting" -- the same
   *  three-state shape `policy` uses. `false` is a deliberate "not for this
   *  row", which is why it cannot double as "inherit". */
  writeSubs: boolean | null;
  /** True once the row was overridden by hand; see `policyPinned`. */
  writeSubsPinned: boolean;
  variants: Variant[];
  chosen: Variant | null;
  selected: boolean;
  selectedIndices: number[] | null;
  expiresAt: string | null;
  progress: TaskProgress;
  outputDir: string | null;
  partPath: string | null;
  pathDegradation: "L0" | "L1";
  /** True only when captions were asked for, no track was chosen, and the
   *  automatic list came back empty twice from a platform that answers a
   *  refusal the same way as "none" (Q2, D-155's shape once more) -- so
   *  "no captions" and "could not ask" are indistinguishable. Absent on an
   *  older sidecar, which must read as "nothing to say" (P-72), never as
   *  a silent `true` or `false`. */
  captionsUnconfirmed?: boolean;
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
  /** Save the platform's own caption track beside the media, in the language
   *  the video was SPOKEN in. The default behind every queue row. There is no
   *  language setting beside it on purpose: naming one asks the platform to
   *  TRANSLATE, which has to be deliberate rather than stored (D-156/P-49). */
  writeSubs: boolean;
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

/** A managed install, reporting itself. The promise resolves at the END,
 *  this arrives throughout, and a 106 MB transfer with neither would look
 *  like a hang. */
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
  /** Parts 2..n of a segmented post -- the author's own continuation replies,
   *  in the order posted. Empty for an ordinary post.
   *
   *  Here for the reason the whole block exists: a renderer that shows
   *  `caption` alone shows part 1 of 3 and looks complete doing it. Structure
   *  (order, gaps, ids) is `ManifestSource.segments`, which the GUI does not
   *  mirror; these are the words. */
  continuation: string[];
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
 *  reads them, and nothing can read a video. This product does not turn
 *  speech into text; what the video says has to be handled elsewhere. */
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
  /** Fetch the post's video(s) too, in case they matter to the explanation.
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
