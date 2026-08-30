/**
 * How far the speech-recognition engine has got, for the one request in
 * flight — and what it thought of its own output when it finished.
 *
 * A store rather than component state, because the two ends are in different
 * places: the SSE stream is wired once at the app root and the readout is in
 * the workspace, and threading a callback between them would put the app
 * layer in the business of knowing what a transcript workspace looks like.
 *
 * Singular on purpose -- there is no id here and no map of them. Reading a
 * transcript is a request, not a job: one is in flight or none is, the
 * workspace that started it is the one waiting for it, and a second
 * concurrent transcription is not a thing this surface can produce. If it
 * ever becomes one, this becomes a map keyed by whatever identifies them,
 * and the absence of a key today is the honest description of today.
 *
 * The VERDICT outlives the readout. `progress` is about a wait and is gone
 * the moment the wait ends; `health` is about the file the reader is now
 * looking at, so it survives until the next run begins.
 */

import { create } from "zustand";
import type { AsrHealth, AsrProgress, TranscriptFinding } from "@/shared/api/types";

export interface AsrProgressState {
  /** `null` when nothing is being transcribed, which is the normal state. */
  progress: AsrProgress | null;
  /** The engine's verdict on the transcript now on screen, or `null`. */
  health: AsrHealth | null;
  /** What the product could DETERMINE about the same transcript. Separate
   *  from `health` because one is a black box's self-report and the other is
   *  a property of the file. */
  findings: TranscriptFinding[];
  /** When the first segment arrived, for the estimate. Not when the request
   *  started: decode and model load run at a different speed and would make
   *  the first estimate wildly pessimistic. */
  startedAt: number | null;
  apply: (progress: AsrProgress) => void;
  /** A new run: forget everything about the last one, verdict included. */
  begin: () => void;
  /** The run is over: drop the readout, KEEP the verdict. */
  clear: () => void;
}

export const useAsrProgress = create<AsrProgressState>((set) => ({
  progress: null,
  health: null,
  findings: [],
  startedAt: null,
  apply: (progress) =>
    set((state) => {
      if (progress.phase === "health") {
        return { health: progress as AsrHealth };
      }
      if (progress.phase === "findings") {
        const record = progress as unknown as { findings?: TranscriptFinding[] };
        return { findings: record.findings ?? [] };
      }
      return {
        progress,
        startedAt:
          progress.phase === "segment" && state.startedAt === null
            ? Date.now()
            : state.startedAt,
      };
    }),
  begin: () => set({ progress: null, health: null, findings: [], startedAt: null }),
  clear: () => set({ progress: null, startedAt: null }),
}));

/** `72:15` / `1:02:15`. Hours only when there are hours. */
function clock(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

/**
 * The readout, as one line of Traditional Chinese, or `null` when there is
 * nothing worth saying yet.
 *
 * Pure and exported so it can be tested without a browser, which is the same
 * reason `selection.ts` next door is pure. The phases before the first
 * segment are named rather than folded into a percentage: loading a 3 GB
 * model takes seconds during which no fraction exists, and a bar sitting at
 * 0% is a worse answer than a sentence saying what is happening.
 *
 * `startedAt` and `now` are passed in rather than read, so the estimate is
 * testable. A percentage is enough for a three-minute clip; for a two-hour
 * talk show it does not answer the only question the reader has, which is
 * whether to wait or come back later.
 */
export function describeProgress(
  progress: AsrProgress | null,
  startedAt?: number | null,
  now: number = Date.now(),
): string | null {
  if (!progress) return null;
  switch (progress.phase) {
    case "decoded": {
      const length = progress.duration ? `${Math.round(progress.duration)} 秒` : "";
      return `已讀取音訊${length ? `（${length}）` : ""}，正在載入語音模型…`;
    }
    case "loaded":
      return "模型已載入，正在判斷語言…";
    case "language":
      return `語言：${progress.language ?? "偵測中"}，開始聽寫…`;
    case "segment": {
      const at = progress.at ?? 0;
      const duration = progress.duration ?? 0;
      // No percentage without a duration to divide by. An invented one is
      // the kind of confident wrong number this project keeps finding.
      const share = duration > 0 ? `${Math.round((at / duration) * 100)}%` : "";
      const lines = progress.lines ? `，${progress.lines} 行` : "";
      return `聽寫中${share ? ` ${share}` : ""}${lines}${eta(at, duration, startedAt, now)}`;
    }
    // The engine judged its own first attempt unusable and started a second
    // one. Said out loud because otherwise the readout jumps back to 0% with
    // no explanation, and a wait that restarts silently is indistinguishable
    // from a hang — which is exactly the failure this whole round began with.
    case "retry":
      return progress.reason ?? "第一次的結果看起來漏掉了內容，正在重跑…";
    default:
      return null;
  }
}

/**
 * `，約還需 12:30` once there is enough to say it with.
 *
 * Withheld below 20 seconds of processed audio, because the first few
 * segments swing the number by minutes and a figure that jumps around is
 * worse than none. Withheld again under half a minute remaining, where the
 * honest answer is that it is nearly done.
 */
function eta(
  at: number,
  duration: number,
  startedAt: number | null | undefined,
  now: number,
): string {
  if (!startedAt || duration <= 0 || at < 20) return "";
  const elapsed = (now - startedAt) / 1000;
  if (elapsed <= 0) return "";
  const remaining = (duration - at) * (elapsed / at);
  if (remaining < 30) return "";
  return `，約還需 ${clock(remaining)}`;
}

/**
 * The verdicts a reader should see, worst first: what the product could
 * DETERMINE about the transcript, then what the engine said about itself.
 *
 * Findings come first because they are evidence rather than testimony —
 * `simplified-script` is a fact about characters in the file, while
 * `contextDrops` is the engine's account of its own difficulty. The engine's
 * account is still worth having; it just does not lead.
 *
 * Returns `[]` on a clean run, and the caller renders nothing. A panel that
 * reports after every transcription trains people to stop reading it.
 */
export function describeVerdicts(
  findings: TranscriptFinding[],
  health: AsrHealth | null,
): { severity: string; text: string }[] {
  const rows = findings.map((f) => ({
    severity: f.severity === "warn" ? "warn" : "note",
    text: f.detail,
  }));

  if (rows.length === 0) {
    // No findings at all: either the transcript is clean, or nothing ran the
    // inspection stage. Those two are indistinguishable from here, and the
    // engine's own account is the only thing left — so if it reports
    // degeneration while the inspector reported nothing, say so. The two
    // instruments are asserted to agree in `tests/conformance`; disagreement
    // in the field means one is broken, and the safe direction is to speak.
    const fallback = describeHealth(health);
    if (fallback) rows.push({ severity: "warn", text: fallback });
    return rows;
  }

  // The engine's report, and ONLY the part the findings cannot see.
  // Repetition is checked on both sides, so carrying it twice would say the
  // same thing in two voices. `most-windows-fell-back` is the same story
  // told better — a share with both numbers in it — so when that finding is
  // present the raw drop count stays quiet.
  // A verdict that names no action is 「技術告知」 and the reader's answer to
  // it is 「所以我到底要不要重跑」 (UAT E-24). `vad-dropped-most` is the one
  // finding that now HAS an answer, so it carries it.
  if (findings.some((f) => f.code === "vad-dropped-most")) {
    rows.push({
      severity: "note",
      text:
        "如果這是一邊大聲一邊小聲的通話或會議錄音，" +
        "到「設定 → 語音辨識」把「錄音的聲音狀況」改成「一邊大聲一邊小聲」，" +
        "再按一次「重新抓取」會有幫助。",
    });
  }

  const alreadySaid = findings.some((f) => f.code === "most-windows-fell-back");
  const drops = health?.contextDrops ?? 0;
  if (drops >= 3 && !alreadySaid) {
    rows.push({
      severity: "note",
      text:
        `有 ${drops} 個段落特別難辨識，引擎在那裡重新開始，` +
        `前後文的連貫性可能會斷開。`,
    });
  }
  return rows;
}

/**
 * The engine's verdict on a finished transcript, or `null` when there is
 * nothing a reader needs to act on.
 *
 * Kept for the case where no findings arrived at all — an older sidecar, or
 * a transcript that never went through the inspection stage.
 */
export function describeHealth(health: AsrHealth | null): string | null {
  if (!health) return null;
  const notes: string[] = [];
  if (health.degenerated) {
    const run = health.longestRepeatRun ?? 0;
    const text = health.repeatedText ? `「${health.repeatedText.slice(0, 20)}」` : "";
    notes.push(
      `有一段連續出現 ${run} 行一模一樣的內容${text}，` +
        `這通常是引擎卡住而不是講者真的重複，那一段請自行確認。`,
    );
  }
  // Only worth mentioning when there are enough to have shaped the result.
  // One or two are ordinary on any real recording.
  if ((health.contextDrops ?? 0) >= 3) {
    notes.push(
      `有 ${health.contextDrops} 個段落特別難辨識，引擎在那裡重新開始，` +
        `前後文的連貫性可能會斷開。`,
    );
  }
  return notes.length > 0 ? notes.join("") : null;
}
