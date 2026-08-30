/**
 * Sidecar supervisor (G6 §4).
 *
 * Readiness is a **handshake, not a poll**. The sidecar prints exactly one
 * line to stdout and nothing else, ever:
 *
 *     {"event":"ready","port":51877,"apiVersion":1,"pid":24188}
 *
 * Polling `/health` would have to guess a port it does not know and a
 * timeout it cannot justify; the handshake delivers the port and the
 * readiness signal in one event.
 *
 * Shutdown is **parent-death-driven**. The child reads stdin; when this
 * process dies -- cleanly, by crash, or by Task Manager -- the pipe closes,
 * the child sees EOF and exits. `stop()` is the fast path for the case
 * where we are still alive and simply want it gone.
 *
 * No graceful drain, deliberately (§4.1): every mutating endpoint persists
 * before responding (G3) and INV-4 demotes PROBING/DOWNLOADING to PAUSED on
 * load, so an abrupt kill is already safe. A drain would add a failure mode
 * to protect state that is already protected.
 */

import { app } from "electron";
import { spawn, spawnSync, type ChildProcessByStdio } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import readline from "node:readline";
import type { Writable, Readable } from "node:stream";
import { fileURLToPath } from "node:url";
import type { SidecarReadyEvent } from "./contracts.js";

/** §4.4. The packaged figure is larger because a onefile PyInstaller binary
 *  unpacks to %TEMP% on first run, and on a cold disk behind real-time AV
 *  scanning that is slow. Do NOT tune this down without measuring on a cold
 *  machine: a timeout that fires on a slow first launch turns a working
 *  build into a broken one. */
const READY_TIMEOUT_PACKAGED_MS = 60_000;
const READY_TIMEOUT_DEV_MS = 20_000;

/** How long `stop()` waits for the polite stdin-EOF exit before taking the
 *  process tree down by force. */
const STOP_GRACE_MS = 1_500;

const DIAGNOSTICS_LIMIT = 4_000;

export type SidecarFailureKind =
  | "executable_missing"
  | "venv_missing"
  | "exited_early"
  | "timeout"
  | "queue_locked"
  | "bind_failed"
  | "spawn_failed";

export class SidecarStartupError extends Error {
  constructor(
    readonly kind: SidecarFailureKind,
    message: string,
    /** Last stderr, already trimmed to something a dialog can hold. */
    readonly diagnostics: string = "",
    /** Set only for `queue_locked`, and only when the PID was parseable. */
    readonly holderPid: number | null = null,
  ) {
    super(message);
    this.name = "SidecarStartupError";
  }
}

/**
 * Dev only. Derived from this file's own location --
 * `<repo>/electron/dist-electron/sidecar.js`, so up two -- rather than from
 * `app.getAppPath()`, which answers `<repo>/electron` when launched as
 * `electron .` and something else when launched any other way. Pinning it
 * to the compiled file makes the answer independent of the invocation.
 */
function repoRoot(): string {
  return path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
}

export class Sidecar {
  private child: ChildProcessByStdio<Writable, Readable, Readable> | null = null;
  private readyEvent: SidecarReadyEvent | null = null;
  private startupError: Error | null = null;
  private stderrTail = "";
  private starting: Promise<void> | null = null;
  private stopping = false;
  private onUnexpectedExit: ((detail: string) => void) | null = null;

  get baseUrl(): string {
    if (!this.readyEvent) {
      throw this.startupError ?? new Error("核心程式尚未就緒。");
    }
    return `http://127.0.0.1:${this.readyEvent.port}`;
  }

  get ready(): boolean {
    return this.readyEvent !== null;
  }

  get lastError(): string | null {
    return this.startupError ? this.startupError.message : null;
  }

  /** Last 4000 characters of stderr. Exists because a sidecar that dies at
   *  startup prints its reason and then is gone; without this the user gets
   *  「啟動失敗」 and nothing else, which is unactionable. */
  get diagnostics(): string {
    return this.stderrTail.trim();
  }

  /** Called when the sidecar dies *after* a successful start. Not a
   *  respawn hook: §8.2 forbids silently restarting, because a crash loop
   *  that reconnects invisibly is how a reproducible bug stops being
   *  reproducible. */
  onCrash(handler: (detail: string) => void): void {
    this.onUnexpectedExit = handler;
  }

  /** Idempotent: concurrent callers share one in-flight start. */
  start(): Promise<void> {
    if (this.readyEvent) return Promise.resolve();
    if (this.starting) return this.starting;
    this.starting = this.spawnAndWait().finally(() => {
      this.starting = null;
    });
    return this.starting;
  }

  private async spawnAndWait(): Promise<void> {
    this.startupError = null;
    this.stderrTail = "";
    this.stopping = false;

    const { executable, args } = this.resolveInvocation();

    let child: ChildProcessByStdio<Writable, Readable, Readable>;
    try {
      child = spawn(executable, args, {
        cwd: app.isPackaged ? process.resourcesPath : repoRoot(),
        windowsHide: true,
        stdio: ["pipe", "pipe", "pipe"],
        env: { ...process.env, PYTHONUNBUFFERED: "1", PYTHONIOENCODING: "utf-8" },
      }) as ChildProcessByStdio<Writable, Readable, Readable>;
    } catch (error) {
      throw new SidecarStartupError(
        "spawn_failed",
        `無法啟動核心程式：${executable}`,
        error instanceof Error ? error.message : String(error),
      );
    }

    this.child = child;
    child.stderr.setEncoding("utf-8");
    child.stderr.on("data", (chunk: string) => {
      this.stderrTail = `${this.stderrTail}${chunk}`.slice(-DIAGNOSTICS_LIMIT);
    });

    const lines = readline.createInterface({ input: child.stdout });

    await new Promise<void>((resolve, reject) => {
      let settled = false;

      const timer = setTimeout(() => {
        if (settled) return;
        settled = true;
        this.fail(
          reject,
          new SidecarStartupError(
            "timeout",
            "核心程式啟動逾時。",
            this.diagnostics,
          ),
        );
      }, app.isPackaged ? READY_TIMEOUT_PACKAGED_MS : READY_TIMEOUT_DEV_MS);

      child.once("error", (error: Error) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        this.fail(
          reject,
          new SidecarStartupError("spawn_failed", `無法啟動核心程式：${executable}`, error.message),
        );
      });

      child.once("exit", (code) => {
        clearTimeout(timer);
        if (!settled) {
          settled = true;
          this.fail(reject, this.classifyEarlyExit(code));
          return;
        }
        // Died after a successful start.
        this.child = null;
        this.readyEvent = null;
        if (!this.stopping) {
          const detail = `核心程式結束（代碼 ${code ?? "unknown"}）。`;
          this.startupError = new Error(detail);
          this.onUnexpectedExit?.(`${detail}${this.diagnostics ? `\n${this.diagnostics}` : ""}`);
        }
      });

      lines.on("line", (line: string) => {
        if (settled) return;
        let event: Partial<SidecarReadyEvent>;
        try {
          event = JSON.parse(line) as Partial<SidecarReadyEvent>;
        } catch {
          // Not the handshake. stdout is supposed to carry nothing else, so
          // this is noise from a library that ignored that -- drop it rather
          // than treating it as a failure.
          return;
        }
        if (event.event !== "ready" || typeof event.port !== "number") return;
        settled = true;
        clearTimeout(timer);
        this.readyEvent = event as SidecarReadyEvent;
        resolve();
      });
    });
  }

  /**
   * Turn "it exited before saying ready" into something §8.1 can render.
   * The tags come from `mfp.sidecar`, which prints `serve: <tag>: <prose>`
   * on stderr precisely so this does not have to guess.
   */
  private classifyEarlyExit(code: number | null): SidecarStartupError {
    const detail = this.diagnostics;
    if (detail.includes("queue_locked")) {
      const match = /PID (\d+)/.exec(detail);
      const pid = match?.[1] ? Number(match[1]) : null;
      return new SidecarStartupError(
        "queue_locked",
        pid === null
          ? "另一個 media-fetch-pipeline 正在執行。"
          : `另一個 media-fetch-pipeline 正在執行（PID ${pid}）。`,
        detail,
        pid,
      );
    }
    if (detail.includes("bind_failed")) {
      return new SidecarStartupError("bind_failed", "無法在本機開啟連接埠。", detail);
    }
    return new SidecarStartupError(
      "exited_early",
      `核心程式提前結束（代碼 ${code ?? "unknown"}）。`,
      detail,
    );
  }

  private fail(reject: (error: Error) => void, error: SidecarStartupError): void {
    this.startupError = error;
    this.killTree();
    this.child = null;
    reject(error);
  }

  stop(): void {
    const child = this.child;
    this.stopping = true;
    this.child = null;
    this.readyEvent = null;
    if (!child) return;

    // Polite first: closing stdin is the same EOF the child would see if we
    // had crashed, so this exercises the backstop rather than bypassing it.
    try {
      child.stdin.end();
    } catch {
      // Already gone.
    }

    const pid = child.pid;
    if (pid === undefined) return;
    setTimeout(() => {
      if (child.exitCode === null && child.signalCode === null) {
        this.killTree(pid);
      }
    }, STOP_GRACE_MS).unref?.();
  }

  /**
   * Kill the whole process tree, not just the child we hold.
   *
   * This is not belt-and-braces. Both spawn targets put a launcher in
   * front of the real interpreter -- the venv `python.exe` redirector in
   * dev, the PyInstaller onefile bootloader when packaged -- so
   * `child.kill()` reaches the launcher and can leave the server behind.
   * That orphan is §11.2 item 4, the defect you cannot see by eye.
   */
  private killTree(explicitPid?: number): void {
    const pid = explicitPid ?? this.child?.pid;
    if (pid === undefined) return;
    if (process.platform === "win32") {
      spawnSync("taskkill", ["/pid", String(pid), "/t", "/f"], { windowsHide: true });
    } else {
      try {
        process.kill(pid);
      } catch {
        // Already gone.
      }
    }
  }

  /** §4.3. Dev and production differ in exactly one deliberate place, and
   *  this is it -- see the delivery note for the dev-port deviation. */
  private resolveInvocation(): { executable: string; args: string[] } {
    if (app.isPackaged) {
      const executable = path.join(process.resourcesPath, "sidecar", "mfp-sidecar.exe");
      if (!existsSync(executable)) {
        throw new SidecarStartupError(
          "executable_missing",
          `找不到核心程式：\n${executable}`,
          "",
        );
      }
      return {
        executable,
        args: [
          "serve",
          "--port",
          "0",
          "--ready-json",
          "--exit-on-stdin-eof",
          "--gui-dist",
          path.join(process.resourcesPath, "gui"),
        ],
      };
    }

    const root = repoRoot();
    const python = path.join(root, ".venv", "Scripts", "python.exe");
    if (!existsSync(python)) {
      throw new SidecarStartupError(
        "venv_missing",
        `找不到專案 Python 環境：\n${python}\n\n請先執行： py -3.12 -m venv .venv`,
        "",
      );
    }

    const args = [
      "-m",
      "mfp.sidecar",
      "serve",
      "--port",
      // Fixed rather than ephemeral in dev: the Vite dev server proxies /v1
      // to a port written in `gui/vite.config.ts`, and it is started before
      // this process knows what an ephemeral port would have been. The
      // packaged path -- the one §4.3 cares about -- still uses `--port 0`.
      process.env.MFP_DEV_API_PORT ?? "47821",
      "--ready-json",
      "--exit-on-stdin-eof",
    ];
    // Without Vite there is no renderer unless the sidecar serves the built
    // SPA. Passing the directory that may not exist is deliberate: the
    // sidecar refuses to start and names it, which is a §8.1 dialog rather
    // than a blank window.
    if (!process.env.VITE_DEV_SERVER_URL) {
      args.push("--gui-dist", path.join(root, "gui", "dist"));
    }
    return { executable: python, args };
  }
}
