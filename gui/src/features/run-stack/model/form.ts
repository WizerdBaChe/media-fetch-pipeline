/**
 * The workspace's form, and the request it means.
 *
 * Pure on purpose. Everything a person can get wrong before pressing 產生
 * is decided here, where it can be tested without a browser -- and the
 * mapping from "what the form says" to "what the CLI would be told" lives in
 * ONE function, so the equivalent command shown below the form is the
 * command that runs.
 */

import type { StackRequest } from "@/shared/api/types";

/** Where the words come from. This is the fork that decides the rest of the
 *  form, and the M2 failure was a video put on the wrong side of it. */
export type CaptionSource = "burned" | "file" | "url";

export interface StackFormState {
  video: string;
  captionSource: CaptionSource;
  subsFile: string;
  subsUrl: string;
  /** `TOP:BOTTOM`, produced by the picker rather than typed. */
  roi: string | null;
  start: string;
  end: string;
  /** Which frame the picker is showing. Not part of the request. */
  previewAt: string;
  maxStrips: string;
  fontSize: string;
  linesPerStrip: string;
  transcriptChars: string;
  headFull: boolean;
  uniformStrips: boolean;
  out: string;
}

export function emptyForm(video = ""): StackFormState {
  return {
    video,
    captionSource: "burned",
    subsFile: "",
    subsUrl: "",
    roi: null,
    start: "",
    end: "",
    previewAt: "0:05",
    maxStrips: "",
    fontSize: "",
    linesPerStrip: "",
    transcriptChars: "",
    headFull: false,
    uniformStrips: false,
    out: "",
  };
}

/**
 * A form that arrives with captions already beside the video starts on the
 * caption file, not on the band picker: the commonest reason to be here
 * with a downloaded video is that its captions are a separate track.
 */
export function formForVideo(video: string, sidecar?: string | null): StackFormState {
  const form = emptyForm(video);
  if (!sidecar) return form;
  return { ...form, captionSource: "file", subsFile: sidecar };
}

const NUMERIC_SETTINGS: ReadonlyArray<[keyof StackFormState, string]> = [
  ["fontSize", "font_size"],
  ["linesPerStrip", "lines_per_strip"],
  ["transcriptChars", "transcript_chars"],
];

export function toRequest(form: StackFormState): StackRequest {
  const settings: Record<string, string> = {};
  for (const [field, key] of NUMERIC_SETTINGS) {
    const raw = String(form[field] ?? "").trim();
    if (raw) settings[key] = raw;
  }
  if (form.headFull) settings.head_mode = "full";
  if (form.uniformStrips) settings.tight_strips = "false";

  const request: StackRequest = {
    video: form.video.trim(),
    preview: true,
    settings,
  };
  if (form.captionSource === "burned") {
    request.roi = form.roi;
  } else {
    request.subs =
      form.captionSource === "file" ? form.subsFile.trim() : form.subsUrl.trim();
  }
  if (form.start.trim()) request.start = form.start.trim();
  if (form.end.trim()) request.end = form.end.trim();
  if (form.maxStrips.trim()) request.maxStrips = Number(form.maxStrips.trim());
  if (form.out.trim()) request.out = form.out.trim();
  return request;
}

/**
 * What still has to be decided, in the words a person would use.
 *
 * Returned as a list rather than a boolean so the button can say WHY it is
 * disabled. A disabled control with no explanation is the defect this
 * project keeps finding in itself.
 */
export function formIssues(form: StackFormState): string[] {
  const issues: string[] = [];
  if (!form.video.trim()) issues.push("還沒有選影片");
  if (form.captionSource === "burned" && !form.roi) {
    issues.push("還沒有框出字幕的位置");
  }
  if (form.captionSource === "file" && !form.subsFile.trim()) {
    issues.push("還沒有選字幕檔");
  }
  if (form.captionSource === "url" && !form.subsUrl.trim()) {
    issues.push("還沒有填貼文網址");
  }
  if (form.maxStrips.trim() && !/^\d+$/.test(form.maxStrips.trim())) {
    issues.push("條數上限要是整數");
  }
  for (const [field, key] of NUMERIC_SETTINGS) {
    const raw = String(form[field] ?? "").trim();
    if (raw && !/^\d+$/.test(raw)) issues.push(`${key} 要是整數`);
  }
  return issues;
}
