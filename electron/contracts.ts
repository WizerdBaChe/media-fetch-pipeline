/**
 * The IPC surface (G6 §6), shared by main, preload and -- as a type-only
 * mirror in `gui/src/shared/lib/desktop.ts` -- the renderer.
 *
 * The renderer already talks to `/v1` directly over HTTP and that stays.
 * IPC carries ONLY what a web page cannot do, which today is exactly three
 * things: open a folder, reveal a file, and report which Electron built it.
 * Every addition here should have to answer "why can a fetch not do this?".
 */

/** The ready handshake the sidecar prints on stdout, once, ever (§4.1). */
export interface SidecarReadyEvent {
  event: "ready";
  port: number;
  apiVersion: number;
  pid: number;
}

/** What `openPath`/`showInFolder` answer. Never throws across the bridge:
 *  an IPC rejection surfaces in the renderer as an opaque Error string, and
 *  a refusal the user cannot read is indistinguishable from a bug. */
export interface DesktopResult {
  ok: boolean;
  /** Present iff `ok` is false. Already user-facing Traditional Chinese. */
  error?: string;
}

export interface DesktopVersions {
  app: string;
  electron: string;
  chrome: string;
}

/**
 * `window.mfpDesktop`, absent in browser mode.
 *
 * Absence IS the capability signal: the GUI feature-detects
 * `window.mfpDesktop?.openPath` and never sniffs the user agent. A shell
 * that exposed a stubbed-out object would defeat that.
 */
/** Which half of the run log to reveal. A name, never a path: the
 *  renderer does not get to say which directory the shell opens. */
export type LogKind = "actions" | "errors";

export interface MfpDesktopApi {
  readonly isDesktop: true;
  openPath(absolutePath: string): Promise<DesktopResult>;
  showInFolder(absolutePath: string): Promise<DesktopResult>;
  getVersions(): Promise<DesktopVersions>;
  /** The OS file picker, filtered to the video containers 引用長圖 stacks
   *  frames from. A path the user chose in a system dialog is the one kind
   *  of path the renderer may introduce: choosing it IS consent. */
  pickVideo(): Promise<{ path: string | null }>;
  /**
   * Reveal the log directory, or one error bundle inside it.
   *
   * The directory is asked of the sidecar rather than recomputed here:
   * Python owns where logs live, and a second implementation of that path
   * would eventually open a folder that is not the one being written to.
   * `bundle` is a single directory name, checked to be one.
   */
  revealLogs(kind: LogKind, bundle?: string): Promise<DesktopResult>;
}

export const IPC = {
  openPath: "mfp:open-path",
  showInFolder: "mfp:show-in-folder",
  getVersions: "mfp:get-versions",
  pickVideo: "mfp:pick-video",
  revealLogs: "mfp:reveal-logs",
} as const;
