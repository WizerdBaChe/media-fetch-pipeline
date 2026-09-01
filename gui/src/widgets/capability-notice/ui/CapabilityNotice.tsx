/**
 * Says out loud what this build cannot do, and what this MACHINE cannot do.
 *
 * Two banners, one reason. The queue happily holds a task in PROBING or
 * DOWNLOADING, and a silent stall is the failure mode that gets misdiagnosed
 * as a bug — so anything that will stop the first click from working is said
 * before the click, in the only place a person sees before they make it.
 *
 * The first banner is about the BUILD: nothing advances a task while M2/M3
 * acquisition is frozen. It disappears once `/v1/health` reports the
 * capability.
 *
 * The second is about the machine, and is the one this file gained on
 * 2026-09-02: yt-dlp or ffmpeg is not here, so 開始 would fail with a
 * sentence about PATH. Unlike a notice it cannot be dismissed — the
 * condition is not an opinion, it un-renders itself the moment the program
 * is installed, and a banner that can be closed while the app still cannot
 * download anything is a banner that gets closed.
 */

import { useEffect } from "react";
import { noticeKey, useSessionStore } from "@/entities/session/model/store";
import { presentNotice } from "@/shared/lib/notices";
import {
  TOOL_COPY,
  describeProgress,
  missingTools,
  useTools,
} from "@/features/setup-tools/model/store";
import { Button } from "@/shared/ui/Button";

export function CapabilityNotice() {
  const capabilities = useSessionStore((state) => state.capabilities);
  const reachable = useSessionStore((state) => state.reachable);
  const notices = useSessionStore((state) => state.notices);
  const dismissNotice = useSessionStore((state) => state.dismissNotice);

  const acquisitionMissing = reachable === true && (!capabilities.probe || !capabilities.download);

  /**
   * The missing-program banner.
   *
   * Here rather than in 設定, because this is the only surface a person sees
   * before they press 開始 -- and 開始 failing with a sentence about PATH is
   * the failure this whole round exists to remove. It is not dismissible:
   * the condition is not an opinion, it un-renders itself the moment the
   * program is installed, and a banner that can be closed while the app
   * still cannot download anything is a banner that gets closed.
   */
  const tools = useTools((state) => state.tools);
  const installing = useTools((state) => state.installing);
  const progress = useTools((state) => state.progress);
  const loadTools = useTools((state) => state.load);
  const install = useTools((state) => state.install);
  const toolError = useTools((state) => state.error);
  const missing = missingTools(tools);

  useEffect(() => {
    // Only once the server is answering. Asking a server we already know is
    // unreachable produces a second failure banner about the same fact.
    if (reachable === true && tools === null) void loadTools();
  }, [reachable, tools, loadTools]);

  return (
    <>
      {reachable === false && (
        <div className="mfp-banner mfp-banner--error" role="alert">
          <strong>無法連線到本機服務。</strong> 請在專案根目錄執行 <code>mfp serve</code>
          （預設 127.0.0.1:47821），然後重新整理。
        </div>
      )}

      {missing.length > 0 && (
        <div className="mfp-banner mfp-banner--error" role="alert" data-testid="tools-missing">
          <div className="mfp-banner__body">
            <strong>還缺 {missing.length} 個程式，現在還不能下載。</strong>
            <ul className="mfp-banner__list">
              {missing.map((tool) => (
                <li key={tool.name}>
                  {TOOL_COPY[tool.name]?.label ?? tool.name}（{tool.name}）：
                  {TOOL_COPY[tool.name]?.missing ?? ""}
                </li>
              ))}
            </ul>
            {installing !== null && (
              <span className="mfp-banner__progress">
                {describeProgress(progress) ?? "準備中…"}
              </span>
            )}
            {toolError !== null && <span className="mfp-banner__error">{toolError}</span>}
          </div>
          {/* One button that installs every missing one in turn. A row of
              buttons would make the user press two things to reach a state
              in which one thing works; the panel in 設定 is where an
              individual choice belongs. */}
          <Button
            variant="primary"
            disabled={installing !== null}
            onClick={() => {
              void (async () => {
                for (const tool of missing) {
                  if (!(await install(tool.name))) break;
                }
              })();
            }}
          >
            {installing !== null ? "安裝中…" : "幫我自動安裝"}
          </Button>
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
