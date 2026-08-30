/**
 * Display formatters. Pure, so they are cheap to test exhaustively.
 *
 * All of these render into monospace columns (PSM §6), so the priority is
 * stable width over maximum precision -- a number that changes width every
 * tick makes a table jitter, which reads as instability.
 */

const UNITS = ["B", "KB", "MB", "GB", "TB"] as const;

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return "—";
  if (bytes < 0) return "—";
  if (bytes === 0) return "0 B";

  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const digits = unit === 0 ? 0 : value >= 100 ? 0 : 1;
  return `${value.toFixed(digits)} ${UNITS[unit]}`;
}

export function formatSpeed(bytesPerSec: number | null | undefined): string {
  if (!bytesPerSec || bytesPerSec <= 0) return "—";
  return `${formatBytes(bytesPerSec)}/s`;
}

/** `mm:ss`, or `h:mm:ss` past an hour. Anything absurd renders as `—`. */
export function formatEta(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds > 24 * 3600) return "—";

  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");

  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

export function formatPercent(done: number, total: number | null | undefined): string {
  if (!total || total <= 0) return "—";
  return `${Math.min(100, Math.floor((done / total) * 100))}%`;
}

/** Countdown for the `⏳ 等待配額` row (§6). Never shows a negative. */
export function formatCountdown(ms: number): string {
  return formatEta(Math.max(0, ms) / 1000);
}
