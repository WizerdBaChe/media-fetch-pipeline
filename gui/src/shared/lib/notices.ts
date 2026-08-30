/**
 * `Notice` → the sentence the user reads, and how loudly to say it.
 *
 * The sibling of `presentError`, and it exists for the same reason one layer
 * up. A notice used to carry its own prose, which meant the SERVER wrote the
 * sentence — and the server is English by contract. So a bulk removal
 * answered with an English banner in the middle of a Chinese UI. Translating
 * those two strings in place would have fixed those two strings; moving the
 * prose to the presentation layer is what stops the next one being added in
 * English by someone following the existing pattern.
 *
 * Tone is not the same as level, and this is where they separate:
 *
 *   info  → a confirmation of something the user just did. It is news, not a
 *           problem, and it should not occupy a full-width bar at the top of
 *           the window styled like the ones that are.
 *   warn  → something happened that the user did not ask for and may need to
 *           act on.
 *   error → something failed.
 */

import type { Notice } from "@/shared/api/types";
import { presentError } from "./errors";

export type NoticeTone = "toast" | "banner";

export interface NoticePresentation {
  text: string;
  tone: NoticeTone;
}

/** Codes the server sends with structured fields instead of prose. */
const SERVER_CODES: Record<string, (notice: Notice) => string> = {
  records_removed: (notice) => {
    const removed = notice.removed ?? 0;
    const cancelled = notice.cancelledInFlight ?? 0;
    const base = `已移除 ${removed} 筆紀錄`;
    // Only mentioned when it happened. "cancelled 0 in flight" is noise that
    // makes the reader check whether something went wrong.
    return cancelled > 0 ? `${base}，並取消 ${cancelled} 筆進行中的傳輸` : base;
  },
  formats_excluded: (notice) => {
    // The source's format list is not a quality menu: YouTube ships
    // storyboard contact sheets and bare audio streams in the same array as
    // the renditions. They are filtered out of the queue (M10 4a) and this
    // is the half that says so.
    // Fixed order, not the JSON's: object key order is the server's
    // business, and a sentence that reshuffles between two runs of the same
    // fetch reads like two different things happened.
    const labels: [string, string][] = [
      ["storyboard", "分鏡縮圖"],
      ["audio_only", "純音訊"],
      ["segmented", "分段串流"],
    ];
    const excluded = notice.excluded ?? {};
    const known = labels.filter(([reason]) => (excluded[reason] ?? 0) > 0);
    const unknown = Object.keys(excluded)
      .filter((reason) => !labels.some(([known]) => known === reason))
      .filter((reason) => (excluded[reason] ?? 0) > 0)
      .map((reason): [string, string] => [reason, reason]);
    const parts = [...known, ...unknown].map(
      ([reason, label]) => `${label} ${excluded[reason]} 項`,
    );
    if (parts.length === 0) return "來源提供的格式已全部列為畫質選項";
    return `已排除 ${parts.join("、")}：這些不是可下載的影片畫質`;
  },
};

/**
 * A confirmation is transient; anything the user has to act on is not.
 *
 * Keyed on level rather than on code so a notice added later gets a sensible
 * tone without being listed here — the failure mode to avoid is a new error
 * quietly appearing as a toast that fades before it is read.
 */
function toneFor(notice: Notice): NoticeTone {
  return notice.level === "info" ? "toast" : "banner";
}

export function presentNotice(notice: Notice): NoticePresentation {
  const tone = toneFor(notice);
  const build = SERVER_CODES[notice.code];
  if (build) {
    const text = build(notice);
    return { text: notice.detail ? `${text}（${notice.detail}）` : text, tone };
  }

  // A notice that carries its own prose is client-generated and already in
  // the right language.
  if (notice.message) return { text: notice.message, tone };

  // Otherwise it is a task failure travelling by its error code, which the
  // row-level table already knows how to name. Reusing it keeps the banner
  // and the row from describing the same failure two different ways.
  const presented = presentError(notice.code);
  const text = notice.detail ? `${presented.label}：${notice.detail}` : presented.label;
  return { text, tone };
}
