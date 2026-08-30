/**
 * `blocked[].reason` → what the dialog says about it.
 *
 * Deliberately does NOT restate the required version number. The server
 * already computes it from `deps.lock.json` and puts both numbers in the
 * `doctor` detail; copying it here would create a second constant that goes
 * stale silently the moment the floor moves. So the label states the
 * condition and points at the one place that knows the numbers.
 */

export interface BlockedPresentation {
  /** What is wrong, in one clause. */
  label: string;
  /** What the user can do about it. Empty means "nothing you can do". */
  action: string;
}

export const BLOCKED_PRESENTATION: Record<string, BlockedPresentation> = {
  ytdlp_below_minimum: {
    label: "你安裝的 yt-dlp 版本過舊，這個平台會被拒絕下載",
    action: "更新 yt-dlp 後在「設定 → 相依工具」重新檢查即可解除",
  },
  ytdlp_unusable: {
    label: "找不到可用的 yt-dlp，這個平台需要它",
    action: "安裝 yt-dlp 後在「設定 → 相依工具」重新檢查即可解除",
  },
};

/**
 * Never returns undefined: a reason code this build has not seen must still
 * explain itself, or the user gets a link removed from their paste with no
 * account of why -- the exact failure the report exists to prevent (INV-7).
 */
export function blockedPresentation(reason: string): BlockedPresentation {
  return (
    BLOCKED_PRESENTATION[reason] ?? {
      label: `這個平台目前無法下載（${reason}）`,
      action: "請到「設定 → 相依工具」查看詳細狀態",
    }
  );
}
