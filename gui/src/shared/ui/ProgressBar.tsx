interface ProgressBarProps {
  /** 0–1. `null` means "running but length unknown" — rendered indeterminate. */
  ratio: number | null;
  state?: "active" | "paused" | "done" | "failed";
  label?: string;
}

export function ProgressBar({ ratio, state = "active", label }: ProgressBarProps) {
  const clamped = ratio === null ? null : Math.max(0, Math.min(1, ratio));
  const percent = clamped === null ? null : Math.round(clamped * 100);

  return (
    <div
      className={`mfp-progress mfp-progress--${state}${clamped === null ? " mfp-progress--indeterminate" : ""}`}
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      {...(percent === null ? {} : { "aria-valuenow": percent })}
      aria-label={label ?? "下載進度"}
    >
      <div
        className="mfp-progress__fill"
        style={clamped === null ? undefined : { width: `${percent}%` }}
      />
    </div>
  );
}
