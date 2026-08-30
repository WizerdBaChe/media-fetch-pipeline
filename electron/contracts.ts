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

/** Which files the picker offers. `video` is the default so an existing
 *  caller that passes nothing keeps exactly the dialog it had.
 *
 *  `document` is not a widening of the other two but a different SET --
 *  `.txt`, `.md`, `.markdown`, which 文件翻譯 takes and which neither of the
 *  other tools can open. It is still one channel, because the difference is
 *  a list of extensions and nothing else. */
export type PickAccept = "video" | "media" | "document";

/** Which folder is being asked for. It only ever changes the dialog's
 *  TITLE -- a folder is a folder -- but a dialog that says "選擇模型資料夾"
 *  and one that says "選擇模型要放在哪裡" are asking opposite questions,
 *  and getting them the wrong way round is the mistake this names away. */
export type PickFolderPurpose = "model-source" | "model-home";

export interface MfpDesktopApi {
  readonly isDesktop: true;
  openPath(absolutePath: string): Promise<DesktopResult>;
  showInFolder(absolutePath: string): Promise<DesktopResult>;
  getVersions(): Promise<DesktopVersions>;
  /** The OS file picker. A path the user chose in a system dialog is the
   *  one kind of path the renderer may introduce: choosing it IS consent.
   *
   *  `accept` is a WIDENING, not a preference. 引用長圖 stacks video frames
   *  and an mp3 has none, so its picker must not offer one; 逐字稿 listens
   *  to audio and must. One dialog with two filter sets rather than two
   *  channels, because the difference between them is a list of extensions
   *  and nothing else. */
  pickVideo(accept?: PickAccept): Promise<{ path: string | null }>;
  /** A DIRECTORY the user chose. Same consent rule as `pickVideo`: the
   *  renderer may not name a path, but a path picked in a system dialog is
   *  one the user named themselves.
   *
   *  Separate from `pickVideo` rather than a third `accept` value because
   *  `properties: ["openDirectory"]` is a different dialog, not a different
   *  filter, and folding them together would make the accept string decide
   *  which dialog opens -- a thing the renderer supplies. */
  pickFolder(purpose?: PickFolderPurpose): Promise<{ path: string | null }>;
  /** The Python that has the recognition engine in it. A file dialog rather
   *  than a folder one: what is being named is an interpreter, and asking
   *  for the venv instead would mean guessing `Scripts/python.exe` on
   *  Windows and `bin/python` elsewhere -- a guess this side should not be
   *  making about someone else's environment. */
  pickPython(): Promise<{ path: string | null }>;
  /** Open the model folder in Explorer. Takes NO path: the location is the
   *  sidecar's to know, exactly as `revealLogs` works, so the renderer
   *  cannot ask the shell to open somewhere of its choosing. */
  revealModels(): Promise<DesktopResult>;
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
  pickFolder: "mfp:pick-folder",
  pickPython: "mfp:pick-python",
  revealModels: "mfp:reveal-models",
} as const;
