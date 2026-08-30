/**
 * Deciding what a paste needs before it enters the queue.
 *
 * Ruled 2026-08-12: a multi-result paste opens a small confirmation dialog
 * instead of growing the input and pushing the page around. The input field
 * is a fixed height and never moves.
 *
 * The fast path is preserved deliberately. §7 ruled "預覽後一鍵", and popping
 * a dialog for every single pasted link would turn one click into two for the
 * most common action in the app. So a single clean result goes straight in,
 * and cosmetic-only cleanup is reported in a reserved-height line under the
 * input — reserved, so showing it shifts nothing.
 *
 * INV-7 still holds either way: every automatic modification is reported,
 * in the dialog or in that line. Nothing is ever changed silently.
 */

import type { ParseReport, ParseResult } from "@/shared/api/types";

/**
 * Things the user should look at before committing: more than one result, or
 * anything that was dropped, merged, or not understood. Cosmetic tidying of a
 * single link is not in this list — it is reported, not confirmed.
 */
export function needsConfirmation(result: ParseResult): boolean {
  const { items, report } = result;
  return (
    items.length > 1 ||
    report.unrecognized.length > 0 ||
    // A dropped link always opens the dialog, even when it is the only
    // thing in the paste. The fast path exists to save a click on a clean
    // single link; letting it swallow a removal silently would be the one
    // case where saving that click costs the user their input.
    (report.blocked?.length ?? 0) > 0 ||
    report.duplicatesInBatch > 0 ||
    report.alreadyInQueue > 0
  );
}

/** Every itemized change, in the order they happen in the pipeline. */
export function summarizeReport(report: ParseReport): string[] {
  const lines: string[] = [];
  if (report.glued > 0) lines.push(`切開 ${report.glued} 個黏在一起的網址`);
  if (report.extracted > 0) lines.push(`從文字中抽出 ${report.extracted} 個網址`);
  if (report.trackingStripped.length > 0) {
    // Naming the parameters is the point. "移除追蹤參數" without saying which
    // is a reassurance, not a report.
    lines.push(`移除追蹤參數：${report.trackingStripped.join("、")}`);
  }
  if (report.duplicatesInBatch > 0) lines.push(`同批重複 ${report.duplicatesInBatch} 筆已合併`);
  if (report.alreadyInQueue > 0) lines.push(`${report.alreadyInQueue} 筆已在佇列中`);
  return lines;
}

/** The one-line note for the fast path. Empty string means "show nothing". */
export function inlineNote(result: ParseResult): string {
  if (needsConfirmation(result)) return "";
  return summarizeReport(result.report).join(" · ");
}

/** True when the paste produced nothing usable at all. */
export function foundNothing(result: ParseResult): boolean {
  return result.items.length === 0;
}
