/**
 * Says out loud what this build cannot do.
 *
 * The queue happily holds a task in PROBING or DOWNLOADING, but nothing
 * advances it while M2/M3 acquisition is frozen. Without this banner the app
 * would look like it hangs on the first click — a silent stall is exactly the
 * failure mode that gets misdiagnosed as a bug. The banner disappears on its
 * own once `/v1/health` reports the capability.
 */

import { noticeKey, useSessionStore } from "@/entities/session/model/store";
import { presentNotice } from "@/shared/lib/notices";
import { Button } from "@/shared/ui/Button";

export function CapabilityNotice() {
  const capabilities = useSessionStore((state) => state.capabilities);
  const reachable = useSessionStore((state) => state.reachable);
  const notices = useSessionStore((state) => state.notices);
  const dismissNotice = useSessionStore((state) => state.dismissNotice);

  const acquisitionMissing = reachable === true && (!capabilities.probe || !capabilities.download);

  return (
    <>
      {reachable === false && (
        <div className="mfp-banner mfp-banner--error" role="alert">
          <strong>無法連線到本機服務。</strong> 請在專案根目錄執行 <code>mfp serve</code>
          （預設 127.0.0.1:47821），然後重新整理。
        </div>
      )}

      {acquisitionMissing && (
        <div className="mfp-banner mfp-banner--warn" role="status">
          <strong>取得層尚未實作（M2/M3）。</strong>
          目前可以貼上網址、整理佇列、設定畫質與管理紀錄；
          「分析」與「下載」按鈕會停用，因為背後還沒有引擎會推進它們。
          這是已知狀態，不是故障。
        </div>
      )}

      {/* Banners only. An `info` notice is a receipt for something the
          user just did and belongs in a toast, not in a full-width bar
          shaped like "the local service is unreachable". */}
      {notices
        .map((notice) => ({ notice, presented: presentNotice(notice) }))
        .filter((entry) => entry.presented.tone === "banner")
        .map(({ notice, presented }) => {
        // Keyed by identity rather than by position, so the key survives a
        // dismissal in another tab reordering the list under it.
        const key = noticeKey(notice);
        return (
          <div key={key} className={`mfp-banner mfp-banner--${notice.level}`}>
            <span>{presented.text}</span>
            <Button variant="ghost" onClick={() => dismissNotice(key)} aria-label="關閉提示">
              ✕
            </Button>
          </div>
        );
      })}
    </>
  );
}
