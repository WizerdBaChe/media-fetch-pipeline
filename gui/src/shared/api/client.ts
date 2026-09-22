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

  /**
   * Parse + enqueue in one call. The response carries the INV-7 change report.
   *
   * `writeSubs` is tri-state on purpose: omitted (or `null`) leaves the new
   * rows following the global setting, so this call means the same thing it
   * meant before the field existed.
   */
  addTasks: (text: string, writeSubs: boolean | null = null) =>
    request<AddResponse>("/queue", {
      method: "POST",
      body: JSON.stringify({ text, writeSubs }),
    }),

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
