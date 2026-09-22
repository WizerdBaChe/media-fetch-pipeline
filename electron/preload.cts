/**
 * The context bridge (G6 §6). CommonJS by file extension, not by taste:
 * Electron loads preload scripts as CJS, and a `.ts` compiled to ESM here
 * would fail at runtime with a message that names neither the cause nor
 * this file.
 *
 * Everything crossing the bridge is a plain, structured-cloneable value.
 * No `ipcRenderer` is exposed, so the renderer can call exactly the three
 * channels below and nothing else -- which is the point of
 * `contextIsolation`.
 */

import { contextBridge, ipcRenderer } from "electron";
import type { DesktopVersions, MfpDesktopApi } from "./contracts.js";

const api: MfpDesktopApi = {
  isDesktop: true,
  openPath: (absolutePath: string) => ipcRenderer.invoke("mfp:open-path", absolutePath),
  showInFolder: (absolutePath: string) =>
    ipcRenderer.invoke("mfp:show-in-folder", absolutePath),
  getVersions: (): Promise<DesktopVersions> => ipcRenderer.invoke("mfp:get-versions"),
  pickVideo: () => ipcRenderer.invoke("mfp:pick-video"),
  revealLogs: (kind, bundle) => ipcRenderer.invoke("mfp:reveal-logs", kind, bundle),
};

contextBridge.exposeInMainWorld("mfpDesktop", api);
