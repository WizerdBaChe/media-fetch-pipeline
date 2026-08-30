/**
 * Aim the subtitle band by dragging it onto a real frame.
 *
 * This exists because of a measured failure. `--roi 1300:1545` was carried
 * from one video's acceptance run to another's, where it landed across a
 * speaker's chest -- on a 2160-row frame it is 60%-72% down -- and every
 * visual check failed for a reason that had nothing to do with what it was
 * checking. Numbers alone cannot be judged; a band drawn on the picture can.
 *
 * Two rules hold the coordinate maths together:
 *
 * 1. **The frame's own height is the unit.** `--roi` is in video pixels, so
 *    every drag converts through the RENDERED height of the image element,
 *    measured at the moment of the drag. Using the server's `imageHeight`
 *    would be wrong the instant CSS lays the image out at any other size.
 * 2. **The handles are sliders.** `role="slider"` with arrow-key support is
 *    not decoration: a band is a number, some people cannot drag, and a
 *    keyboard-reachable handle is also the only kind a test can move without
 *    simulating pointer physics.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "@/shared/api/client";
import type { FrameShot } from "@/shared/api/types";
import { Button } from "@/shared/ui/Button";

export interface RoiPickerProps {
  video: string;
  /** `TOP:BOTTOM` in frame pixels, or null when nothing is chosen yet. */
  value: string | null;
  onChange: (roi: string) => void;
  /** Where in the clip to show. The band usually only makes sense on a
   *  frame that HAS a subtitle, so this is part of the picker, not beside it. */
  at: string;
  onAtChange: (at: string) => void;
}

interface Band {
  top: number;
  bottom: number;
}

export function parseRoi(value: string | null): Band | null {
  if (!value) return null;
  const parts = value.split(":").map((part) => Number.parseInt(part, 10));
  const [top, bottom] = [parts[0] ?? NaN, parts[1] ?? NaN];
  if (!Number.isFinite(top) || !Number.isFinite(bottom) || bottom <= top) return null;
  return { top, bottom };
}

export function formatRoi(band: Band): string {
  return `${Math.round(band.top)}:${Math.round(band.bottom)}`;
}

/** A band for a frame nobody has aimed at yet: the lower quarter, where
 *  burned-in subtitles usually are. A starting point to drag, never a guess
 *  presented as an answer -- `stack` itself refuses to detect this. */
export function defaultBand(frameHeight: number): Band {
  return {
    top: Math.round(frameHeight * 0.72),
    bottom: Math.round(frameHeight * 0.84),
  };
}

export function RoiPicker({ video, value, onChange, at, onAtChange }: RoiPickerProps) {
  const [shot, setShot] = useState<FrameShot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const imageRef = useRef<HTMLImageElement | null>(null);
  const dragging = useRef<"top" | "bottom" | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .stackFrame(video, at)
      .then((next) => {
        if (cancelled) return;
        setShot(next);
        setError(null);
        if (!parseRoi(value)) onChange(formatRoi(defaultBand(next.frameHeight)));
      })
      .catch((cause) => {
        if (!cancelled) {
          setError(cause instanceof ApiError ? cause.message : String(cause));
        }
      })
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
    // `value`/`onChange` deliberately absent: a frame is fetched for a video
    // and a timestamp. Refetching because the band moved would put a network
    // round trip inside a drag.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [video, at]);

  const band = parseRoi(value) ?? (shot ? defaultBand(shot.frameHeight) : null);

  /**
   * The last band this component emitted.
   *
   * A held arrow key, or a pointer that moves twice before React commits,
   * produces several moves against the SAME rendered `value` -- four
   * presses then land on the same row, once. Measured in the live app:
   * four shift-steps moved the edge by one step. Every move is therefore
   * based on what was last emitted, and this is put back in step with the
   * prop on each render, so a value the parent rejects or rewrites wins.
   */
  const emitted = useRef<Band | null>(null);
  useEffect(() => {
    emitted.current = parseRoi(value);
  }, [value]);

  const toFrameY = useCallback(
    (clientY: number): number | null => {
      const element = imageRef.current;
      if (!element || !shot) return null;
      const box = element.getBoundingClientRect();
      if (box.height <= 0) return null;
      const ratio = (clientY - box.top) / box.height;
      return Math.round(Math.min(1, Math.max(0, ratio)) * shot.frameHeight);
    },
    [shot],
  );

  const moveEdge = useCallback(
    (edge: "top" | "bottom", frameY: number) => {
      const base = emitted.current ?? band;
      if (!base || !shot) return;
      // A band with no height is not a band; keep at least a strip's worth
      // between the edges so the other handle stays grabbable.
      const gap = Math.max(8, Math.round(shot.frameHeight * 0.01));
      const next =
        edge === "top"
          ? { top: Math.min(frameY, base.bottom - gap), bottom: base.bottom }
          : { top: base.top, bottom: Math.max(frameY, base.top + gap) };
      const clamped = {
        top: Math.max(0, next.top),
        bottom: Math.min(shot.frameHeight, next.bottom),
      };
      emitted.current = clamped;
      onChange(formatRoi(clamped));
    },
    [band, shot, onChange],
  );

  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!dragging.current) return;
    const frameY = toFrameY(event.clientY);
    if (frameY !== null) moveEdge(dragging.current, frameY);
  };

  const onKeyDown = (edge: "top" | "bottom") => (event: React.KeyboardEvent) => {
    const base = emitted.current ?? band;
    if (!base || !shot) return;
    const step = event.shiftKey ? 25 : 5;
    const current = edge === "top" ? base.top : base.bottom;
    if (event.key === "ArrowUp") {
      moveEdge(edge, current - step);
    } else if (event.key === "ArrowDown") {
      moveEdge(edge, current + step);
    } else {
      return;
    }
    event.preventDefault();
  };

  if (error) {
    return (
      <div className="mfp-roi mfp-roi--error" role="alert">
        <p>無法取得畫面：{error}</p>
        <Button onClick={() => onAtChange(at)}>重試</Button>
      </div>
    );
  }

  const topPercent = band && shot ? (band.top / shot.frameHeight) * 100 : 0;
  const bottomPercent = band && shot ? (band.bottom / shot.frameHeight) * 100 : 0;

  return (
    <div className="mfp-roi" data-testid="roi-picker">
      <div
        className="mfp-roi__stage"
        onPointerMove={onPointerMove}
        onPointerUp={() => (dragging.current = null)}
        onPointerLeave={() => (dragging.current = null)}
      >
        {shot && (
          <img
            ref={imageRef}
            className="mfp-roi__frame"
            src={shot.image}
            alt={`影片第 ${shot.at.toFixed(1)} 秒的畫面`}
            draggable={false}
          />
        )}
        {shot && band && (
          <>
            <div
              className="mfp-roi__band"
              style={{ top: `${topPercent}%`, height: `${bottomPercent - topPercent}%` }}
              data-testid="roi-band"
            />
            {(["top", "bottom"] as const).map((edge) => (
              <div
                key={edge}
                className={`mfp-roi__handle mfp-roi__handle--${edge}`}
                style={{ top: `${edge === "top" ? topPercent : bottomPercent}%` }}
                role="slider"
                tabIndex={0}
                aria-label={edge === "top" ? "字幕帶上緣" : "字幕帶下緣"}
                aria-valuemin={0}
                aria-valuemax={shot.frameHeight}
                aria-valuenow={edge === "top" ? band.top : band.bottom}
                data-testid={`roi-handle-${edge}`}
                onPointerDown={(event) => {
                  dragging.current = edge;
                  event.currentTarget.setPointerCapture(event.pointerId);
                }}
                onKeyDown={onKeyDown(edge)}
              />
            ))}
          </>
        )}
        {loading && <p className="mfp-roi__loading">取得畫面中…</p>}
      </div>

      <div className="mfp-roi__controls">
        <label>
          預覽時間
          <input
            type="text"
            value={at}
            onChange={(event) => onAtChange(event.target.value)}
            aria-label="預覽畫面的時間點"
            placeholder="0:05"
          />
        </label>
        <span className="mfp-roi__readout" data-testid="roi-readout">
          {band ? `${band.top}:${band.bottom}` : "尚未框選"}
          {shot ? `（畫面高 ${shot.frameHeight}px）` : ""}
        </span>
      </div>
      <p className="mfp-roi__hint">
        把上下兩條線拖到字幕上，讓紅框完整含住文字並留一點空隙。這一段只對
        <strong>畫面上燒死的字幕</strong>有意義。
      </p>
    </div>
  );
}
