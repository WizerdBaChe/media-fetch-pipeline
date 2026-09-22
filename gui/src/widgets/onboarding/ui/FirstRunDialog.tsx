/**
 * The first thing a new user sees, and the only screen that assumes nothing.
 *
 * The defect it closes: on a machine with neither yt-dlp nor ffmpeg -- which
 * is every machine that has just installed this -- the app opened onto an
 * empty queue that looked completely ready. Pasting a link and pressing 開始
 * then failed with a sentence about PATH. Nothing before that moment had said
 * anything had to be installed, and nothing anywhere could install it. The
 * first barrier was invisible until it was hit, and then it was a dead end.
 *
 * So this dialog does exactly two things, in this order: say what the program
 * is for in two sentences, and offer the missing pieces with a button each.
 * It is NOT a wizard -- there are no pages, nothing is mandatory, and
 * 先跳過 closes it for good. D-80's objection to wizards was that they make
 * you walk a path to reach the last step; this has one step and an exit.
 *
 * It is shown once. 設定 → 診斷 holds the same panel permanently, and the
 * banner above the queue reappears on its own while anything required is
 * still missing -- so skipping here costs nothing and hides nothing.
 */

import { useEffect } from "react";
import { Button } from "@/shared/ui/Button";
import { useConfigStore } from "@/entities/config/model/store";
import { FIRST_RUN } from "@/entities/onboarding/model/tours";
import { useOnboarding } from "@/entities/onboarding/model/store";
import { ToolsPanel } from "@/features/setup-tools/ui/ToolsPanel";
import { missingTools, useTools } from "@/features/setup-tools/model/store";

export function FirstRunDialog() {
  const shouldShow = useOnboarding((state) => state.shouldShow(FIRST_RUN));
  const dismiss = useOnboarding((state) => state.dismiss);
  const config = useConfigStore((state) => state.config);
  const installing = useTools((state) => state.installing);
  const tools = useTools((state) => state.tools);
  const missing = missingTools(tools);

  useEffect(() => {
    if (!shouldShow) return undefined;
    const onKey = (event: KeyboardEvent) => {
      // Escape is deliberately NOT bound to a dismissal here, unlike the
      // per-tool guides: this dialog can have a 106 MB transfer running
      // inside it, and a stray keypress closing the only place its progress
      // is explained is not a trade worth making. The button is the exit.
      if (event.key === "Escape") event.stopPropagation();
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [shouldShow]);

  if (!shouldShow) return null;

  const outputRoot = typeof config?.outputRoot === "string" ? config.outputRoot : null;
  const ready = missing.length === 0;

  return (
    <div className="mfp-modal__backdrop" role="presentation">
      <div
        className="mfp-modal mfp-firstrun"
        role="dialog"
        aria-modal="true"
        aria-label="第一次使用"
        data-testid="first-run"
      >
        <h2 className="mfp-modal__title">歡迎使用媒體擷取</h2>

        <p className="mfp-firstrun__lede">
          貼上一個公開貼文的網址，這個程式會把裡面的影片或圖片存到你的電腦上。
          全部在本機處理，不會登入任何帳號，也不會把你的東西送到別的地方。
        </p>

        {outputRoot !== null && (
          <p className="mfp-firstrun__where">
            檔案會存到 <code>{outputRoot}</code>，這可以在「設定 → 一般」裡改。
          </p>
        )}

        {/* The live panel, not a copy of it. One implementation of "which
            programs are here and what installs them" -- a first-run version
            that drifted from the settings version would be two answers to
            one question, and the user would meet both. */}
        {/* `manage={false}`: this screen offers what is MISSING and nothing
            that changes or deletes what is already there (F11, ruling R4).
            Still the live panel, not a copy -- one implementation of 「which
            programs are here」, with one of its powers withheld. */}
        <ToolsPanel heading={false} manage={false} />

        <div className="mfp-modal__actions">
          <Button
            variant="primary"
            disabled={installing !== null}
            onClick={() => dismiss(FIRST_RUN)}
          >
            {ready ? "開始使用" : "先跳過，之後再裝"}
          </Button>
        </div>

        {!ready && (
          <p className="mfp-firstrun__note">
            跳過也沒關係：缺什麼會一直顯示在佇列上方，隨時可以回到「設定 → 診斷」安裝。
          </p>
        )}
      </div>
    </div>
  );
}
