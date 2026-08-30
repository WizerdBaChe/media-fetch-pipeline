/**
 * The one place long-running work is always visible.
 *
 * It lives in the status bar because the status bar is the only thing on
 * screen that no view owns and 設定 does not cover -- which is exactly the
 * property a 3 GB download needs. Opening a tool, going back to the queue,
 * opening and closing 設定: none of them can take this off the screen.
 *
 * Inside `widgets/status-bar` rather than as a widget of its own, because a
 * widget may not import a sibling widget (PSM §3.1) and this has to be drawn
 * IN that bar. It reaches down to `entities/background-job`, which is the
 * direction the layering allows.
 */

import { useEffect, useRef, useState } from "react";
import {
  runningJobs,
  useBackgroundJobs,
  type BackgroundJob,
} from "@/entities/background-job/model/store";
import { Button } from "@/shared/ui/Button";
import { ProgressBar } from "@/shared/ui/ProgressBar";

/** What the button says when it is not counting anything in flight. */
function summarise(jobs: BackgroundJob[]): { text: string; tone: string } {
  const running = runningJobs(jobs);
  if (running.length > 0) {
    const first = running[0]!;
    const percent = first.ratio === null ? null : `${Math.round(first.ratio * 100)}%`;
    return {
      text:
        running.length === 1
          ? `⟳ ${first.label}${percent ? ` ${percent}` : ""}`
          : `⟳ ${running.length} 件背景工作進行中`,
      tone: "running",
    };
  }
  const failed = jobs.filter((job) => job.phase === "failed");
  if (failed.length > 0) return { text: `⚠ ${failed.length} 件背景工作失敗`, tone: "failed" };
  return { text: `✓ ${jobs.length} 件背景工作已完成`, tone: "done" };
}

export function BackgroundJobs() {
  const jobs = useBackgroundJobs((state) => state.jobs);
  const dismiss = useBackgroundJobs((state) => state.dismiss);
  const clearFinished = useBackgroundJobs((state) => state.clearFinished);
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement | null>(null);

  // Close on a click anywhere else and on Escape. A popover in the status bar
  // that can only be closed by finding its own button again is a popover that
  // covers the thing underneath it for the rest of the session.
  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Nothing has run, so there is nothing to say. A control that is always
  // there and empty most of the time teaches people to stop looking at it.
  if (jobs.length === 0) return null;

  const summary = summarise(jobs);
  const finished = jobs.filter((job) => job.phase !== "running");

  return (
    <div className="mfp-jobs" ref={root} data-testid="background-jobs">
      <Button
        variant="ghost"
        className="mfp-jobs__toggle"
        data-tone={summary.tone}
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen((was) => !was)}
      >
        {summary.text}
      </Button>

      {open && (
        <div className="mfp-jobs__panel" role="dialog" aria-label="背景工作">
          <ul className="mfp-jobs__list">
            {jobs.map((job) => (
              <li key={job.id} data-phase={job.phase} data-testid={`background-job-${job.id}`}>
                <span className="mfp-jobs__label">{job.label}</span>
                {job.phase === "running" && (
                  <ProgressBar ratio={job.ratio} state="active" label={`${job.label} 進度`} />
                )}
                {job.detail && <span className="mfp-jobs__detail">{job.detail}</span>}
                {/* Only a finished job can be removed. Removing a running one
                    would either lie about the transfer or cancel it, and the
                    server owns that decision, not this list. */}
                {job.phase !== "running" && (
                  <Button variant="ghost" onClick={() => dismiss(job.id)} aria-label="移除這筆紀錄">
                    ✕
                  </Button>
                )}
              </li>
            ))}
          </ul>

          {finished.length > 0 && (
            <div className="mfp-jobs__actions">
              <Button onClick={() => clearFinished()}>清除已完成（{finished.length}）</Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
