/**
 * 需要的程式: the panel that turns 「缺 yt-dlp」 from a sentence into a button.
 *
 * What it replaces is worth stating, because the shape is a reaction to it.
 * Before this, a machine without yt-dlp offered: a row in 相依工具檢查 reading
 * 「yt-dlp 缺少」, a sentence telling the reader to 「安裝它，或在設定檔裡指定
 * 完整路徑」, and no control anywhere that could do either. That instruction
 * assumes the reader knows what a PATH is, where a config file lives, and
 * which of the eleven files on a GitHub release page is the one they want.
 * The person this app is for knows none of those things, and should not have
 * to.
 *
 * Three rules shape what is on screen.
 *
 * **A row says what the program is FOR before it says its name.** 「影片下載
 * 引擎」 first, `yt-dlp` after it in small type. The name is not an
 * explanation; it is a search term for later.
 *
 * **Every row that is not fine carries the action that fixes it.** One
 * button, primary, with the size of the transfer in it -- 106 MB is a fact
 * somebody on a phone tether needs BEFORE pressing, not after. The manual
 * route stays beside it as a link, because a corporate proxy or an
 * over-eager antivirus will beat the automatic one and a dead end is worse
 * than a longer road.
 *
 * **Which copy is running is always visible.** Not decoration:
 * 「我更新了 PATH 上那個，為什麼還是舊版」 is unanswerable without it, and
 * this app deliberately prefers its own copy over PATH.
 */

import { useEffect, useState } from "react";
import { Button } from "@/shared/ui/Button";
import { ConfirmDialog } from "@/shared/ui/ConfirmDialog";
import { ProgressBar } from "@/shared/ui/ProgressBar";
import {
  TOOL_COPY,
  describeProgress,
  describeSource,
  useTools,
} from "@/features/setup-tools/model/store";
import type { ToolStatus } from "@/shared/api/types";

/** MB, rounded, for a sentence a person reads before spending their data. */
function megabytes(bytes: number | null): string {
  if (!bytes) return "";
  return `約 ${Math.round(bytes / 1048576)} MB`;
}

function ToolRow({
  tool,
  installing,
  manage,
  onInstall,
  onRemove,
}: {
  tool: ToolStatus;
  installing: string | null;
  /** Whether this panel may change or delete what is already installed.
   *  False on the welcome screen (F11, ruling R4): that screen offers what
   *  is MISSING, and nothing that alters a machine that is already fine. */
  manage: boolean;
  onInstall: (name: string) => void;
  onRemove: (name: string) => void;
}) {
  const copy = TOOL_COPY[tool.name] ?? { label: tool.name, purpose: "", missing: "" };
  const busy = installing === tool.name;
  const anyBusy = installing !== null;
  const progress = useTools((state) => (busy ? state.progress : null));
  const ratio =
    progress && progress.total ? Math.min(1, (progress.bytes ?? 0) / progress.total) : null;

  return (
    <li
      className="mfp-tools__row"
      data-tool={tool.name}
      data-state={tool.installed ? "ready" : "missing"}
    >
      <div className="mfp-tools__head">
        <span className="mfp-tools__state" data-tone={tool.installed ? "ready" : "off"}>
          {tool.installed ? "已就緒" : "尚未安裝"}
        </span>
        <span className="mfp-tools__name">
          {copy.label}
          <small className="mfp-tools__id">{tool.name}</small>
        </span>
      </div>

      <p className="mfp-tools__purpose">{tool.installed ? copy.purpose : copy.missing}</p>

      {tool.installed && (
        <p className="mfp-tools__where">
          {describeSource(tool)}
          {tool.version ? ` · 版本 ${tool.version}` : ""}
          {tool.installedAt ? ` · ${tool.installedAt.slice(0, 10)} 安裝` : ""}
        </p>
      )}

      <div className="mfp-tools__actions">
        {tool.manageable && !tool.installed && (
          <Button variant="primary" disabled={anyBusy} onClick={() => onInstall(tool.name)}>
            {busy ? "安裝中…" : `自動安裝（${megabytes(tool.approxBytes)}）`}
          </Button>
        )}
        {manage && tool.manageable && tool.installed && (
          <Button disabled={anyBusy} onClick={() => onInstall(tool.name)}>
            {busy ? "更新中…" : "更新到最新版"}
          </Button>
        )}
        {/* Only for the copy WE put there. Removing it is how somebody goes
            back to the one they installed themselves, and it is the only
            file in this panel we are entitled to delete. */}
        {manage && tool.source === "managed" && (
          <Button variant="ghost" disabled={anyBusy} onClick={() => onRemove(tool.name)}>
            移除本程式下載的這份
          </Button>
        )}
        {/* Hidden for a program that is present and that we cannot install:
            offering Chrome's download page to somebody who is running Chrome
            is a control that does nothing, which is the defect the whole
            settings panel was rebuilt to remove. */}
        {tool.homepage && (tool.manageable || !tool.installed) && (
          // A plain link: the Electron shell sends http(s) to the system
          // browser already, so this needs no IPC channel of its own.
          <a
            className="mfp-tools__link"
            href={tool.homepage}
            target="_blank"
            rel="noreferrer noopener"
          >
            {tool.installed ? "官方頁面" : tool.manageable ? "改用手動下載" : "前往官方下載頁"} ↗
          </a>
        )}
      </div>

      {busy && (
        <div className="mfp-tools__progress" role="status">
          <ProgressBar ratio={ratio} label={`${copy.label}下載進度`} />
          <span>{describeProgress(progress) ?? "準備中…"}</span>
        </div>
      )}
    </li>
  );
}

export function ToolsPanel({
  heading = true,
  manage = true,
}: {
  heading?: boolean;
  /**
   * Whether this instance may change or delete what is already installed.
   *
   * `false` on the welcome screen (F11, ruling R4). A machine where
   * everything is ready was being shown 更新到最新版 and 移除本程式下載的這份
   * on the FIRST screen it ever draws -- one mis-click there took yt-dlp
   * away, and the next thing the reader saw was 還不能下載 above an empty
   * queue. What is missing still has its 自動安裝 button, because that is
   * what the screen is for.
   */
  manage?: boolean;
}) {
  const tools = useTools((state) => state.tools);
  const installing = useTools((state) => state.installing);
  const error = useTools((state) => state.error);
  const load = useTools((state) => state.load);
  const install = useTools((state) => state.install);
  const remove = useTools((state) => state.remove);
  /** Which tool a 移除 is being asked about. Removing used to call the API
   *  on the click (F11): recoverable, but only by downloading it again. */
  const [pending, setPending] = useState<ToolStatus | null>(null);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section className="mfp-settings__section mfp-tools" data-testid="tools-panel">
      {heading && (
        <div className="mfp-settings__section-head">
          <h3>需要的程式</h3>
          <Button onClick={() => void load()} disabled={installing !== null}>
            重新檢查
          </Button>
        </div>
      )}

      {error !== null && (
        <p className="mfp-settings__error" role="alert">
          {error}
        </p>
      )}

      {tools === null ? (
        <p className="mfp-tools__loading">正在檢查這台電腦上有哪些程式…</p>
      ) : (
        <ul className="mfp-tools__list">
          {tools.map((tool) => (
            <ToolRow
              key={tool.name}
              tool={tool}
              installing={installing}
              manage={manage}
              onInstall={(name) => void install(name)}
              onRemove={() => setPending(tool)}
            />
          ))}
        </ul>
      )}

      {/* 「說出代價」 (tours.ts rule 3) for a destructive action: what goes,
          what stays, and what getting it back costs in megabytes. Removing
          is recoverable -- by downloading it again, which is the whole point
          of saying the number before the button is pressed. */}
      <ConfirmDialog
        open={pending !== null}
        danger
        title={pending ? `移除本程式下載的${TOOL_COPY[pending.name]?.label ?? pending.name}？` : ""}
        confirmLabel="移除"
        onCancel={() => setPending(null)}
        onConfirm={() => {
          const name = pending?.name;
          setPending(null);
          if (name) void remove(name);
        }}
        body={
          pending && (
            <>
              <p>
                只會刪掉本程式自己下載的那一份，不會動到你自己安裝的版本，
                也不會刪掉任何已經下載好的影片。
              </p>
              <p>
                之後要再用到它，得重新下載{" "}
                <strong>{megabytes(pending.approxBytes) || "一次"}</strong>。
              </p>
            </>
          )
        }
      />

      {/* Said once, at the bottom, rather than on every row. Where the files
          go is the question people ask AFTER pressing the button, and a
          panel that answers it three times in the middle of the actions is
          harder to act on. */}
      <p className="mfp-tools__note">
        自動安裝會把程式放進本程式自己的資料夾，不會動到系統設定，也不需要系統管理員權限。
        下載的檔案都會跟發行方公布的檔案指紋比對過才安裝。
      </p>
    </section>
  );
}
