/**
 * SSE subscription to `GET /v1/events` (PSM Batch 2 §4.4).
 *
 * `EventSource` reconnects on its own, which is the reason SSE was chosen over
 * WebSocket. What it does NOT do is reliably tell us the connection dropped --
 * and that is not a theoretical gap. Measured 2026-08-16 (R5-8): killing the
 * API left the dev proxy logging `ECONNRESET` while this page's indicator sat
 * on green for more than 30 seconds. `onerror` never arrived.
 *
 * So liveness is not inferred from socket events. The server sends a `ping`
 * every few seconds and this module runs a watchdog: no frame of any kind
 * within `STALE_AFTER_MS` means the stream is dead, whatever the socket says.
 * A silently dead event stream looks exactly like an idle queue, and an idle
 * queue is this app's normal state.
 *
 * The same watchdog also does the reconnecting, for the same reason. The
 * documented auto-reconnect is not unconditional: when the API dies, the dev
 * proxy answers the pending stream with an ordinary error response, which is
 * a FATAL error for `EventSource` -- it closes for good and never retries.
 * Measured 2026-08-16: with the API restarted and the page untouched, the
 * server reported zero subscribers indefinitely. So reconnection is ours to
 * do, and "restart the server and refresh the page" stops being the recovery.
 */

import type {
  AsrInstallProgress,
  AsrProgress,
  MtProgress,
  BudgetWait,
  Notice,
  QueueStats,
  StackJob,
  Task,
  TasksRemoved,
  ToolInstallProgress,
} from "./types";

/**
 * "stale" is not a synonym for "connecting": it means we had a stream, no
 * frame has arrived for too long, and nothing on screen can be trusted to be
 * current. The status bar must not render it as a healthy state.
 */
export type StreamStatus = "connecting" | "open" | "stale" | "closed";

/**
 * Must stay above the server's `HEARTBEAT_INTERVAL_S` (5s) by enough margin
 * that one late frame is not reported as an outage. Three intervals: a single
 * dropped heartbeat is tolerated, two in a row are not.
 */
export const STALE_AFTER_MS = 16_000;

/** How often a stale stream is torn down and dialled again. */
export const RECONNECT_EVERY_MS = 5_000;

export interface EventHandlers {
  onTask?: (task: Task) => void;
  onRemoved?: (removed: TasksRemoved) => void;
  onQueueStats?: (stats: QueueStats) => void;
  onBudgetWait?: (wait: BudgetWait) => void;
  onNotice?: (notice: Notice) => void;
  /** A stack job moved. Same stream as the queue's events: one
   *  connection per tab is the budget, and the workspace is not worth
   *  a second one. */
  onStackJob?: (job: StackJob) => void;
  /** How far a transcription has got. Same stream as everything else: the
   *  budget is one connection per tab, and progress for a request that is
   *  already in flight does not earn a second one. */
  onAsrProgress?: (progress: AsrProgress) => void;
  /** Bytes copied while a model is being brought into the model folder.
   *  A 3 GB copy is minutes, and it earns the same treatment recognition
   *  got: a moving number rather than a frozen dialog. */
  onAsrInstall?: (progress: AsrInstallProgress) => void;
  /** Bytes moving while yt-dlp or ffmpeg is being fetched. Its own event
   *  rather than sharing `asrInstall`: a person setting the app up for the
   *  first time is not setting up recognition, and one reader for both
   *  would have to guess which panel the frame belonged to. */
  onToolInstall?: (progress: ToolInstallProgress) => void;
  /** How many lines a translation has got through. Its own event rather
   *  than sharing `asr`: the two can never be in flight together, but they
   *  count different things and one reader would have to guess which unit
   *  had arrived. */
  onTranslate?: (progress: MtProgress) => void;
  onStatus?: (status: StreamStatus) => void;
}

/** Minimal EventSource surface, so tests can inject a fake. */
export interface EventSourceLike {
  addEventListener(type: string, listener: (event: MessageEvent) => void): void;
  close(): void;
  onopen: ((event: Event) => void) | null;
  onerror: ((event: Event) => void) | null;
}

export type EventSourceFactory = (url: string) => EventSourceLike;

/** Injected in tests so the watchdog can be driven without waiting 16s. */
export interface Timers {
  setInterval: (fn: () => void, ms: number) => unknown;
  clearInterval: (handle: unknown) => void;
  now: () => number;
}

const defaultFactory: EventSourceFactory = (url) =>
  new EventSource(url) as unknown as EventSourceLike;

const defaultTimers: Timers = {
  setInterval: (fn, ms) => setInterval(fn, ms),
  clearInterval: (handle) => clearInterval(handle as ReturnType<typeof setInterval>),
  now: () => Date.now(),
};

/**
 * Subscribe to the event stream. Returns an unsubscribe function.
 *
 * A malformed payload on one event must not kill the stream: it is logged and
 * skipped, because losing every subsequent progress update over one bad frame
 * is a far worse failure than dropping the frame.
 */
export function subscribeEvents(
  handlers: EventHandlers,
  factory: EventSourceFactory = defaultFactory,
  timers: Timers = defaultTimers,
): () => void {
  let lastFrameAt = timers.now();
  let lastAttemptAt = timers.now();
  let status: StreamStatus = "connecting";
  let source: EventSourceLike;

  const report = (next: StreamStatus) => {
    if (next === status) return; // no re-render per heartbeat
    status = next;
    handlers.onStatus?.(next);
  };

  /** Any frame at all — including a ping — proves the stream is alive. */
  const sawFrame = () => {
    lastFrameAt = timers.now();
    report("open");
  };

  const bind = <T>(
    target: EventSourceLike,
    name: string,
    handler: ((payload: T) => void) | undefined,
  ) => {
    target.addEventListener(name, (event: MessageEvent) => {
      sawFrame();
      if (!handler) return;
      try {
        handler(JSON.parse(event.data) as T);
      } catch (error) {
        console.error(`[events] dropped a malformed "${name}" frame`, error);
      }
    });
  };

  const connect = () => {
    lastAttemptAt = timers.now();
    const next = factory("/v1/events");

    bind<Task>(next, "task", handlers.onTask);
    bind<TasksRemoved>(next, "removed", handlers.onRemoved);
    bind<QueueStats>(next, "queueStats", handlers.onQueueStats);
    bind<BudgetWait>(next, "budgetWait", handlers.onBudgetWait);
    bind<Notice>(next, "notice", handlers.onNotice);
    bind<StackJob>(next, "stack", handlers.onStackJob);
    bind<AsrProgress>(next, "asr", handlers.onAsrProgress);
    bind<AsrInstallProgress>(next, "asrInstall", handlers.onAsrInstall);
    bind<ToolInstallProgress>(next, "toolInstall", handlers.onToolInstall);
    bind<MtProgress>(next, "mt", handlers.onTranslate);
    // No handler: a ping carries no information beyond "still here", which
    // `sawFrame` has already recorded by the time we get here.
    bind<unknown>(next, "ping", undefined);

    next.onopen = () => sawFrame();
    // Not "open": a browser fires this on a transport error and again on each
    // failed reconnect. Whether it arrives at all is exactly what cannot be
    // relied on, so it is treated as corroboration, never as the detector.
    next.onerror = () => report(status === "connecting" ? "connecting" : "stale");

    source = next;
  };

  handlers.onStatus?.("connecting");
  connect();

  const watchdog = timers.setInterval(() => {
    const now = timers.now();
    if (now - lastFrameAt <= STALE_AFTER_MS) return;
    report("stale");
    if (now - lastAttemptAt < RECONNECT_EVERY_MS) return;
    // Close before redialling: a half-dead EventSource still occupies one of
    // the six connections a browser allows per origin, and leaking one per
    // attempt would eventually wedge every request on the origin, not just
    // this stream (that failure was reproduced deliberately, 2026-08-16).
    source.close();
    connect();
  }, 2000);

  return () => {
    timers.clearInterval(watchdog);
    source.close();
    status = "closed";
    handlers.onStatus?.("closed");
  };
}
