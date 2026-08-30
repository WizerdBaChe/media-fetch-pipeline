/**
 * The desktop capability, feature-detected (G6 §6.2).
 *
 * `window.mfpDesktop` is injected by the Electron preload and is **absent**
 * in a browser. Absence is the signal: nothing here sniffs a user agent,
 * because a user agent describes what is rendering the page, not what the
 * page is allowed to do — and the same build has to run in both places.
 *
 * The shape mirrors `electron/contracts.ts`. It is duplicated rather than
 * imported because the renderer is a browser bundle and must not resolve
 * anything under `electron/` (that separation is the reason `electron/` has
 * its own package.json at all, PSM G6 §3.1). `getVersions` is deliberately
 * left out of this mirror until something in the UI needs it.
 */

export interface DesktopResult {
  ok: boolean;
  error?: string;
}

export type LogKind = "actions" | "errors";

export interface DesktopApi {
  readonly isDesktop: true;
  openPath(absolutePath: string): Promise<DesktopResult>;
  showInFolder(absolutePath: string): Promise<DesktopResult>;
  pickVideo(accept?: PickAccept): Promise<{ path: string | null }>;
  revealLogs(kind: LogKind, bundle?: string): Promise<DesktopResult>;
  pickFolder(purpose?: PickFolderPurpose): Promise<{ path: string | null }>;
  pickPython(): Promise<{ path: string | null }>;
  revealModels(): Promise<DesktopResult>;
}

/** Mirrors `electron/contracts.ts`. It only changes the dialog's title, but
 *  「選模型」 and 「選模型要放哪」 are opposite questions. */
export type PickFolderPurpose = "model-source" | "model-home";

/** Mirrors `electron/contracts.ts`. `document` is a different SET, not a
 *  widening: 文件翻譯 takes `.txt`/`.md`/`.markdown` and refuses everything
 *  the other two tools open. */
export type PickAccept = "video" | "media" | "document";

declare global {
  interface Window {
    mfpDesktop?: DesktopApi;
  }
}

/** The desktop bridge, or null in a browser. Read through a function rather
 *  than exported as a constant so a test can swap it between cases. */
export function desktop(): DesktopApi | null {
  if (typeof window === "undefined") return null;
  const api = window.mfpDesktop;
  // Duck-typed rather than trusted: a partially-initialised bridge that
  // answers `isDesktop` but is missing a method would render a button
  // that throws on click, which is worse than the browser fallback. Every
  // method the UI calls is checked -- a gate that covers some of them
  // reports "desktop" for a bridge that cannot do the thing the button
  // is about to ask for.
  return api &&
    typeof api.openPath === "function" &&
    typeof api.showInFolder === "function"
    ? api
    : null;
}

export function isDesktop(): boolean {
  return desktop() !== null;
}

/**
 * Ask the OS for a file. `null` in a browser, and `null` when the person
 * closed the dialog -- the caller cannot tell the two apart and does not
 * need to: both mean "no new source".
 *
 * `accept` defaults to `"video"`, which is what 引用長圖 wants and what this
 * function meant before 逐字稿 learned to listen to audio. Passing
 * `"media"` widens the dialog to audio containers as well.
 */
export async function pickVideoFile(
  accept: PickAccept = "video",
): Promise<string | null> {
  const api = desktop();
  if (!api || typeof api.pickVideo !== "function") return null;
  const picked = await api.pickVideo(accept);
  return picked?.path ?? null;
}

/**
 * Ask the OS for a DOCUMENT -- `.txt`, `.md`, `.markdown`.
 *
 * The same channel as `pickVideoFile`, with its own filter set, because the
 * difference between the two dialogs is a list of extensions and nothing
 * else. Named separately anyway: a caller asking for a document should not
 * have to know it is calling something called `pickVideo`, and an older
 * shell that has never heard of `document` opens the video dialog rather
 * than throwing -- the user can still type a path.
 */
export function pickDocumentFile(): Promise<string | null> {
  return pickVideoFile("document");
}

/**
 * Reveal something in the file manager, and say so when it cannot.
 *
 * `kind: "file"` goes through the contained `showInFolder` -- the shell must
 * not be handed a path from this side without the main process proving it
 * sits inside the output root. `kind: "errors"` names a HALF of the run log
 * plus, at most, a bundle name; the directory itself is the main process's
 * to work out.
 */
export async function revealPath(
  target: string,
  kind: "file" | "errors",
): Promise<string | null> {
  const api = desktop();
  if (!api) return "這個功能只在桌面版可用";
  const result =
    kind === "file"
      ? await api.showInFolder(target)
      : await api.revealLogs("errors", target);
  return result.ok ? null : (result.error ?? "無法開啟");
}

/**
 * Ask the OS for a DIRECTORY. `null` in a browser and `null` on cancel --
 * the caller cannot tell the two apart and does not need to.
 *
 * Method-checked rather than assumed present, for the reason `desktop()`
 * gives: a bridge from an older shell answers `isDesktop` and has never
 * heard of this channel, and a button that throws on click is worse than
 * one that quietly does nothing available.
 */
export async function pickFolder(
  purpose: PickFolderPurpose,
): Promise<string | null> {
  const api = desktop();
  if (!api || typeof api.pickFolder !== "function") return null;
  const picked = await api.pickFolder(purpose);
  return picked?.path ?? null;
}

/** Ask the OS for the interpreter that has the recognition engine in it. */
export async function pickPythonFile(): Promise<string | null> {
  const api = desktop();
  if (!api || typeof api.pickPython !== "function") return null;
  const picked = await api.pickPython();
  return picked?.path ?? null;
}

/**
 * Open the model folder, creating it if this is the first time.
 *
 * Takes no path: the location is the sidecar's to know. Returns a refusal
 * sentence or `null` for success, matching `revealPath` -- a button that
 * silently does nothing is the failure this shape exists to avoid.
 */
export async function revealModelFolder(): Promise<string | null> {
  const api = desktop();
  if (!api || typeof api.revealModels !== "function") {
    return "這個功能只在桌面版可用";
  }
  const result = await api.revealModels();
  return result.ok ? null : (result.error ?? "無法開啟");
}

export async function revealLogFolder(kind: LogKind): Promise<string | null> {
  const api = desktop();
  if (!api || typeof api.revealLogs !== "function") return "這個功能只在桌面版可用";
  const result = await api.revealLogs(kind);
  return result.ok ? null : (result.error ?? "無法開啟");
}
