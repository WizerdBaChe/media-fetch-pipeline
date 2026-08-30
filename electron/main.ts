/**
 * Main process (G6 §5).
 *
 * Startup order and why it is this order:
 *
 *   single-instance lock  -> a second launch focuses the first window
 *   whenReady             ->
 *   sidecar.start()       -> failure shows the §8.1 dialog, and the app
 *                            still opens a window with a diagnostic page
 *   createWindow()        -> loadURL(sidecar.baseUrl)
 *
 * The window is pointed at `http://127.0.0.1:<port>/`, not at a file. That
 * is the one architectural decision of this milestone (§2): renderer and
 * API share an origin, so the O-3 loopback guard needs no exception, and
 * the "run the server and open a browser" fallback is the same server
 * rather than a second code path that can rot unnoticed.
 */

import { app, BrowserWindow, dialog, ipcMain, screen, shell } from "electron";
import { appendFile, mkdir } from "node:fs/promises";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { containedInRoot } from "./containment.js";
import { planReveal } from "./reveal.js";
import { bringToFront } from "./focus.js";
import type { DesktopResult } from "./contracts.js";
import { Sidecar, SidecarStartupError } from "./sidecar.js";
import { WINDOW } from "./window-geometry.js";

const here = path.dirname(fileURLToPath(import.meta.url));

/**
 * Put Electron's own state in the SAME directory as the queue and the
 * config, rather than the `%APPDATA%/媒體擷取` that `productName` would
 * otherwise produce. One directory to find, one directory to delete.
 *
 * Must run before `whenReady`, and matters more than it looks: §9.2 tells
 * the user that removing the portable exe leaves `%APPDATA%/
 * media-fetch-pipeline` behind. If Electron scattered a second, differently
 * named folder next to it, that note would be quietly wrong.
 */
app.setPath("userData", path.join(app.getPath("appData"), "media-fetch-pipeline"));

const sidecar = new Sidecar();
let mainWindow: BrowserWindow | null = null;

/** `--surface` from `gui/src/app/theme.css`. The first paint happens before
 *  the renderer has any CSS, so a default white window flashes against a
 *  warm-paper UI. */
const SURFACE = "#f7f5f2";

function logPath(): string {
  return path.join(app.getPath("userData"), "logs", "main.log");
}

async function logToDisk(message: string): Promise<void> {
  try {
    await mkdir(path.dirname(logPath()), { recursive: true });
    await appendFile(logPath(), `${new Date().toISOString()} ${message}\n`, "utf-8");
  } catch {
    // Logging must never be the thing that crashes the app.
  }
}

/**
 * §8.1: every startup failure announces itself, and every dialog offers a
 * next step. A dialog with only 確定 on a startup failure leaves the user
 * with a dead app and nothing to do.
 *
 * Returns true when the user chose 重試.
 */
async function showStartupFailure(error: unknown): Promise<boolean> {
  const startup = error instanceof SidecarStartupError ? error : null;
  const message = startup?.message ?? (error instanceof Error ? error.message : String(error));
  const diagnostics = startup?.diagnostics ?? "";

  await logToDisk(`startup failure: ${message}\n${diagnostics}`);

  const { response } = await dialog.showMessageBox({
    type: "error",
    title: "媒體擷取無法啟動",
    message,
    detail: [
      diagnostics ? `詳細訊息：\n${diagnostics}` : "",
      `紀錄檔：${logPath()}`,
    ]
      .filter(Boolean)
      .join("\n\n"),
    buttons: ["重試", "結束"],
    defaultId: 0,
    cancelId: 1,
    noLink: true,
  });
  return response === 0;
}

/** §8.2: a renderer that failed to load must say so. A blank window is a
 *  defect, not a failure mode. */
function diagnosticPage(title: string, detail: string): string {
  const escape = (text: string) =>
    text.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c] ?? c);
  return `data:text/html;charset=utf-8,${encodeURIComponent(`
<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><title>媒體擷取</title>
<style>
 body{margin:0;padding:48px;background:${SURFACE};color:#2b2622;
      font-family:"Segoe UI","Microsoft JhengHei",system-ui,sans-serif;line-height:1.7}
 h1{font-size:20px;margin:0 0 16px}
 pre{white-space:pre-wrap;word-break:break-all;background:#fff;border:1px solid #e2ddd6;
     border-radius:8px;padding:16px;font-size:12px;color:#5a5048}
</style></head><body>
<h1>${escape(title)}</h1>
<pre>${escape(detail)}</pre>
<p>紀錄檔：<code>${escape(logPath())}</code></p>
</body></html>`)}`;
}

function createWindow(): BrowserWindow {
  // The preferred size is chosen for the TABLE (window-geometry.ts, which the
  // queue geometry test reads too, so the table must fit the window we ship).
  // The display gets the last word: asking for 1360 on a 1280-wide screen
  // opens a window with part of itself off the edge, which is the shape this
  // whole round is about -- something the user cannot see and did nothing to
  // cause. `screen` is safe here because createWindow only runs after
  // `whenReady`. The floor still wins, since a window below `minWidth` cannot
  // draw even the narrow layout.
  const work = screen.getPrimaryDisplay().workAreaSize;
  const window = new BrowserWindow({
    width: Math.max(WINDOW.minWidth, Math.min(WINDOW.width, work.width)),
    height: Math.max(WINDOW.minHeight, Math.min(WINDOW.height, work.height)),
    minWidth: WINDOW.minWidth,
    minHeight: WINDOW.minHeight,
    title: "媒體擷取",
    backgroundColor: SURFACE,
    autoHideMenuBar: true,
    show: false,
    webPreferences: {
      preload: path.join(here, "preload.cjs"),
      // Non-negotiable (§5). The renderer is loaded over HTTP; anything
      // less would hand a compromised page Node privileges.
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  window.once("ready-to-show", () => window.show());
  installNavigationLockdown(window);

  // Logged because "the window opened" and "the renderer loaded" are
  // different claims, and only the second one rules out a blank window.
  window.webContents.on("did-finish-load", () => {
    void logToDisk(`renderer loaded ${window.webContents.getURL()}`);
  });

  window.webContents.on("did-fail-load", (_event, code, description, url) => {
    // -3 is ERR_ABORTED, which fires on ordinary in-page navigations.
    if (code === -3) return;
    void logToDisk(`did-fail-load ${code} ${description} ${url}`);
    void window.loadURL(
      diagnosticPage(
        "畫面載入失敗",
        `無法載入 ${url}\n\n${description}（${code}）\n\n` +
          "本機服務可能已經結束。請關閉本視窗再開啟一次；若持續發生，請附上下面的紀錄檔路徑回報。",
      ),
    );
  });

  mainWindow = window;
  window.on("closed", () => {
    if (mainWindow === window) mainWindow = null;
  });
  return window;
}

/**
 * §5 navigation lockdown. Without this, one bad link in a caption turns the
 * app window into an uncontrolled browser holding Electron privileges.
 * Anything not on the sidecar's origin is either opened in the real browser
 * (http/https) or dropped.
 */
function installNavigationLockdown(window: BrowserWindow): void {
  const allowedOrigin = (): string | null => {
    try {
      return new URL(sidecar.baseUrl).origin;
    } catch {
      return null;
    }
  };
  const isInternal = (target: string): boolean => {
    const origin = allowedOrigin();
    if (origin === null) return false;
    try {
      return new URL(target).origin === origin;
    } catch {
      return false;
    }
  };
  const openExternally = (target: string): void => {
    if (/^https?:$/.test(new URL(target).protocol)) void shell.openExternal(target);
  };

  window.webContents.on("will-navigate", (event, target) => {
    if (isInternal(target) || target.startsWith("data:text/html")) return;
    event.preventDefault();
    try {
      openExternally(target);
    } catch {
      void logToDisk(`blocked navigation to an unparseable URL: ${target}`);
    }
  });

  window.webContents.setWindowOpenHandler(({ url }) => {
    try {
      openExternally(url);
    } catch {
      void logToDisk(`blocked window.open to an unparseable URL: ${url}`);
    }
    return { action: "deny" };
  });
}

/**
 * The configured output root, read from the sidecar rather than from
 * `config.json`.
 *
 * The server is the single owner of that value -- the Settings panel writes
 * it through `PUT /v1/config` and it can change while the app is open -- so
 * re-reading it per call keeps the containment check honest instead of
 * enforcing a root the user has since moved away from. It is one loopback
 * request against a server we already require to be up.
 */
async function outputRoot(): Promise<string> {
  const response = await fetch(`${sidecar.baseUrl}/v1/config`);
  if (!response.ok) throw new Error(`GET /v1/config -> ${response.status}`);
  const config = (await response.json()) as { outputRoot?: unknown };
  if (typeof config.outputRoot !== "string" || config.outputRoot.length === 0) {
    throw new Error("設定檔沒有 outputRoot");
  }
  return config.outputRoot;
}

/**
 * §6.1. `shell.openPath` runs an executable it is handed, so the path is
 * checked HERE, against the configured root, before it goes anywhere.
 * `reveal` picks `showItemInFolder` (select the item in Explorer) over
 * `openPath` (open the item itself).
 */
async function openContained(
  target: unknown,
  reveal: boolean,
): Promise<DesktopResult> {
  if (typeof target !== "string") {
    return { ok: false, error: "路徑格式不正確。" };
  }

  let root: string;
  try {
    root = await outputRoot();
  } catch (error) {
    void logToDisk(`openPath: could not read outputRoot: ${String(error)}`);
    return { ok: false, error: "無法讀取設定，暫時不能開啟資料夾。" };
  }

  const verdict = containedInRoot(target, root);
  if (!verdict.ok) {
    // Logged at every refusal: a containment check that never says what it
    // stopped is a check nobody can audit after the fact.
    void logToDisk(`openPath REFUSED (${verdict.reason}) target=${target} root=${root}`);
    if (verdict.reason === "missing") {
      return { ok: false, error: "找不到這個位置，檔案可能已經被移動或刪除。" };
    }
    return {
      ok: false,
      error: `這個位置不在輸出資料夾（${root}）底下，基於安全考量不予開啟。`,
    };
  }

  if (reveal) {
    const plan = planReveal(verdict.resolved);
    if (plan.action === "select") {
      // Re-checked rather than trusted for being a child of a path that
      // already passed: a directory entry can be a symlink, and the whole
      // point of this module is that nothing reaches the shell unproven.
      const child = containedInRoot(plan.target, root);
      if (child.ok) {
        shell.showItemInFolder(child.resolved);
        return { ok: true };
      }
    }
    // An empty folder, or a re-check that refused the file inside it.
    // Opening the folder is the only remaining way to land the reader in
    // the right place.
    const failure = await shell.openPath(verdict.resolved);
    return failure ? { ok: false, error: failure } : { ok: true };
  }
  const failure = await shell.openPath(verdict.resolved);
  return failure ? { ok: false, error: failure } : { ok: true };
}

/**
 * Reveal the run log, or one error bundle in it.
 *
 * The renderer names a HALF ("actions" or "errors") and, at most, one
 * bundle NAME -- never a path. The directory comes from the sidecar, which
 * is the process that writes it; recomputing `%APPDATA%/...` here would be
 * a second definition of the same location, and the first time the two
 * disagreed this button would open an empty folder and say nothing.
 */
async function revealLogs(kind: unknown, bundle: unknown): Promise<DesktopResult> {
  if (kind !== "actions" && kind !== "errors") {
    return { ok: false, error: "不認得的紀錄類型" };
  }
  if (bundle !== undefined && typeof bundle !== "string") {
    return { ok: false, error: "不認得的錯誤資料名稱" };
  }
  // One path segment, or nothing. `..` and separators are how a name
  // becomes a traversal, and this string arrives from the renderer.
  if (bundle && (bundle.includes("/") || bundle.includes("\\") || bundle.includes(".."))) {
    return { ok: false, error: "不合法的錯誤資料名稱" };
  }

  let directories: { logDir: string; errorDir: string };
  try {
    const response = await fetch(`${sidecar.baseUrl}/v1/logs?limit=1`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    directories = (await response.json()) as { logDir: string; errorDir: string };
  } catch (error) {
    await logToDisk(`reveal-logs failed: ${String(error)}`);
    return { ok: false, error: "本機服務沒有回應，無法確認紀錄位置" };
  }

  const base = kind === "errors" ? directories.errorDir : directories.logDir;
  const target = bundle ? path.join(base, bundle) : base;
  if (!existsSync(target)) {
    return { ok: false, error: "紀錄資料夾還不存在（還沒有產生過紀錄）" };
  }
  // `showItemInFolder` on a directory opens its PARENT with it selected,
  // which is one level further out than the user asked for.
  const failure = await shell.openPath(target);
  return failure ? { ok: false, error: failure } : { ok: true };
}

function registerIpc(): void {
  ipcMain.handle("mfp:get-versions", () => ({
    app: app.getVersion(),
    electron: process.versions.electron,
    chrome: process.versions.chrome,
  }));
  ipcMain.handle("mfp:open-path", (_event, target: unknown) => openContained(target, false));
  ipcMain.handle("mfp:show-in-folder", (_event, target: unknown) =>
    openContained(target, true),
  );
  ipcMain.handle("mfp:pick-video", async (_event, accept: unknown) => {
    const window = mainWindow ?? BrowserWindow.getAllWindows()[0];
    // Anything other than the one recognised widening is treated as the
    // default. The renderer supplies this string, and a dialog is not the
    // place to start trusting one.
    const wide = accept === "media";
    // Its own set rather than a widening: 文件翻譯 takes prose and nothing
    // else, and offering it an mp4 would promise a translation of something
    // the verb refuses.
    const documents = accept === "document";
    const video = ["mp4", "mkv", "webm", "mov", "m4v", "avi"];
    // Everything ffmpeg decodes with an audio track in it. `caf` and `m4a`
    // are here because that is what an iPhone hands over.
    const audio = [
      "mp3", "m4a", "aac", "wav", "flac", "ogg", "oga", "opus",
      "wma", "aiff", "aif", "caf", "amr", "m4b",
    ];
    // Mirrors DOCUMENT_SUFFIXES in src/mfp/translate_doc.py. A dialog that
    // offered more than the verb accepts would turn a picked file into a
    // refusal the user could not have predicted.
    const documentExtensions = ["txt", "md", "markdown"];
    const title = documents
      ? "選擇要翻譯的文件"
      : wide
        ? "選擇音檔或影片"
        : "選擇影片檔";
    const filters = documents
      ? [
          { name: "文件", extensions: documentExtensions },
          { name: "所有檔案", extensions: ["*"] },
        ]
      : wide
        ? [
            { name: "音訊與影片", extensions: [...audio, ...video] },
            { name: "音訊", extensions: audio },
            { name: "影片", extensions: video },
            { name: "字幕", extensions: ["srt", "vtt", "txt"] },
            { name: "所有檔案", extensions: ["*"] },
          ]
        : [
            { name: "影片", extensions: video },
            { name: "所有檔案", extensions: ["*"] },
          ];
    const result = await dialog.showOpenDialog(window!, {
      title,
      properties: ["openFile"],
      filters,
    });
    return { path: result.canceled ? null : (result.filePaths[0] ?? null) };
  });
  ipcMain.handle("mfp:reveal-logs", (_event, kind: unknown, bundle: unknown) =>
    revealLogs(kind, bundle),
  );
  ipcMain.handle("mfp:pick-folder", async (_event, purpose: unknown) => {
    const window = mainWindow ?? BrowserWindow.getAllWindows()[0];
    // The purpose only picks a title. Anything unrecognised gets the
    // neutral one rather than being trusted into a branch, for the same
    // reason `pick-video` treats an unknown `accept` as the default.
    const title =
      purpose === "model-home"
        ? "選擇模型要放在哪個資料夾"
        : purpose === "model-source"
          ? "選擇你下載好的模型資料夾"
          : "選擇資料夾";
    const result = await dialog.showOpenDialog(window!, {
      title,
      // `createDirectory` so a user pointing at a NEW model folder does not
      // have to leave the app to make one first.
      properties: ["openDirectory", "createDirectory"],
    });
    return { path: result.canceled ? null : (result.filePaths[0] ?? null) };
  });
  ipcMain.handle("mfp:pick-python", async () => {
    const window = mainWindow ?? BrowserWindow.getAllWindows()[0];
    const result = await dialog.showOpenDialog(window!, {
      title: "選擇裝了語音辨識引擎的 Python",
      properties: ["openFile"],
      filters: [
        { name: "Python", extensions: ["exe"] },
        { name: "所有檔案", extensions: ["*"] },
      ],
    });
    return { path: result.canceled ? null : (result.filePaths[0] ?? null) };
  });
  ipcMain.handle("mfp:reveal-models", () => revealModels());
}

/**
 * Open the model folder, at a location the SIDECAR names.
 *
 * Same shape as `revealLogs` and for the same reason: the folder is derived
 * from how the product was installed and from a setting the user can
 * change, so a second implementation here would eventually open a different
 * directory from the one models are being written to -- and would do it
 * silently, because an empty Explorer window looks like an empty folder.
 *
 * It deliberately does NOT go through `openContained`: that check proves a
 * path is inside the OUTPUT root, and the model folder usually is not.
 * Containment is not the guarantee being relied on here; not accepting a
 * path from the renderer at all is.
 */
async function revealModels(): Promise<DesktopResult> {
  let home: string;
  try {
    const response = await fetch(`${sidecar.baseUrl}/v1/asr/readiness`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const verdict = (await response.json()) as { home?: { path?: unknown } };
    if (typeof verdict.home?.path !== "string" || verdict.home.path.length === 0) {
      throw new Error("readiness did not name a model folder");
    }
    home = verdict.home.path;
  } catch (error) {
    await logToDisk(`reveal-models failed: ${String(error)}`);
    return { ok: false, error: "本機服務沒有回應，無法確認模型資料夾位置" };
  }

  if (!existsSync(home)) {
    // Created rather than refused: this button's whole job is "show me where
    // to put the model", and a folder that does not exist yet is the normal
    // state before the first one is added.
    try {
      await mkdir(home, { recursive: true });
    } catch (error) {
      return {
        ok: false,
        error: `無法建立模型資料夾（${home}）：${String(error)}`,
      };
    }
  }
  const failure = await shell.openPath(home);
  return failure ? { ok: false, error: failure } : { ok: true };
}

/** Start the sidecar, offering 重試 until it works or the user gives up. */
async function startSidecarWithRetry(): Promise<boolean> {
  for (;;) {
    try {
      await sidecar.start();
      return true;
    } catch (error) {
      const retry = await showStartupFailure(error);
      if (!retry) return false;
    }
  }
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    const window = mainWindow ?? BrowserWindow.getAllWindows()[0];
    if (!window) return;
    bringToFront(window);
  });

  process.on("uncaughtException", (error) => {
    void logToDisk(`uncaughtException: ${error.stack ?? error.message}`);
    dialog.showErrorBox(
      "媒體擷取發生未預期的錯誤",
      `${error.message}\n\n紀錄檔：${logPath()}`,
    );
  });

  void app.whenReady().then(async () => {
    registerIpc();

    sidecar.onCrash((detail) => {
      void logToDisk(`sidecar crashed: ${detail}`);
      // Non-modal, and NOT a silent respawn (§8.2): a crash loop that
      // reconnects invisibly is how a reproducible bug stops being one.
      // The SPA's own SSE indicator already shows 已中斷 beside this.
      if (mainWindow && !mainWindow.isDestroyed()) {
        void dialog.showMessageBox(mainWindow, {
          type: "warning",
          title: "本機服務已中斷",
          message: "核心程式結束了，佇列暫時無法操作。",
          detail: `${detail}\n\n請關閉本程式再開啟一次。`,
          buttons: ["知道了"],
          noLink: true,
        });
      }
    });

    const started = await startSidecarWithRetry();
    if (!started) {
      app.quit();
      return;
    }

    const window = createWindow();
    const target = process.env.VITE_DEV_SERVER_URL ?? sidecar.baseUrl;
    void logToDisk(`loading ${target}`);
    void window.loadURL(target);

    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });
}

app.on("before-quit", () => sidecar.stop());
app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
