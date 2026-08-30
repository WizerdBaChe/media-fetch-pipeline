import { useConfigStore } from "@/entities/config/model/store";
import { useSessionStore } from "@/entities/session/model/store";
import { useTaskStore } from "@/entities/task/model/store";
import { aggregateBytesPerSec } from "@/entities/task/model/selectors";
import { formatSpeed } from "@/shared/lib/format";
import { presentError } from "@/shared/lib/errors";
import { BackgroundJobs } from "./BackgroundJobs";

const STREAM_LABEL = {
  connecting: "連線中…",
  open: "即時更新中",
  // Deliberately blunt. "已中斷" alone reads as a network hiccup; the thing
  // the user has to know is that what they are looking at may be out of date.
  stale: "連線中斷，畫面可能不是最新",
  closed: "已中斷",
} as const;

const STREAM_HINT = {
  connecting: "事件串流狀態：正在連線",
  open: "事件串流狀態：中斷時畫面不會即時更新",
  stale: "超過 16 秒沒有收到伺服器訊息。請確認 `mfp serve` 仍在執行，然後重新整理。",
  closed: "事件串流已關閉",
} as const;

export function StatusBar() {
  const tasks = useTaskStore((state) => state.tasks);
  const lastError = useTaskStore((state) => state.lastError);
  const config = useConfigStore((state) => state.config);
  const streamStatus = useSessionStore((state) => state.streamStatus);
  const queueStats = useSessionStore((state) => state.queueStats);

  const speed = aggregateBytesPerSec(tasks);
  const budget = queueStats?.budgetRemainingThisHour;

  return (
    <footer className="mfp-status">
      <span className="mfp-mono">合計 {formatSpeed(speed)}</span>
      <span className="mfp-status__sep">·</span>
      <span className="mfp-mono">
        本小時配額剩 {budget === null || budget === undefined ? "—" : budget}
      </span>
      <span className="mfp-status__sep">·</span>
      <span className="mfp-status__path" title={config?.outputRoot}>
        輸出 {config?.outputRoot ?? "—"}
      </span>

      <div className="mfp-status__spacer" />

      {/* Before the error and the stream light, because those two are about
          the app's health and this is about work the user asked for. */}
      <BackgroundJobs />

      {lastError && (
        // A failed action must not vanish silently -- this is the catch-all
        // for anything that did not land on a specific row.
        <span className="mfp-status__error" role="alert">
          ⚠ {presentError(lastError.errorCode).label}：{lastError.message}
        </span>
      )}

      <span
        className={`mfp-status__stream mfp-status__stream--${streamStatus}`}
        data-state={streamStatus}
        title={STREAM_HINT[streamStatus]}
        role={streamStatus === "stale" ? "alert" : undefined}
      >
        ● {STREAM_LABEL[streamStatus]}
      </span>
    </footer>
  );
}
