/**
 * Settings + Doctor (PSM Batch 2 GUI §9, milestone G5).
 *
 * Two rules decide what appears here, and both come from the same lesson:
 * a control that does nothing is worse than no control.
 *
 *   1. A row is EDITABLE only if something reads the field. 下載完成後 and
 *      重複貼上時 are in §9's table and are not here, because nothing
 *      consumes them yet.
 *   2. A row that exists but must not be raised is shown READ-ONLY with the
 *      reason beside it, rather than hidden. §9 already does this for
 *      速率上限; 同時下載數 joins it, because §9's "1-8" predates D-36
 *      (transfers are serial) and a spinner that lies is not a feature.
 *
 * ONE CATEGORY AT A TIME (the 2026-08-30 rework). This was seven sibling
 * blocks on one scroll, separated by a 1px rule and nothing else: 2266px of
 * content in a 760px window, of which 語音辨識 alone was 1150 -- 51% of the
 * page -- and the first block, holding 輸出資料夾 and 預設畫質, had no heading
 * at all. Everything was the same white on the same white; measured, every
 * section's background was `rgba(0,0,0,0)`. There was no level to read, so
 * finding a setting meant scrolling past all the others and recognising it.
 *
 * The categories are the answer to 「這是關於什麼的」, not to 「這住在哪個
 * component」 -- which is why 不可調整 sits with 相依工具檢查 under 診斷 rather
 * than with the settings it looks like: both answer 「這台機器現在是什麼狀況」
 * and neither can be changed here.
 *
 * The Doctor sub-panel renders `GET /v1/doctor` live. It is the one place
 * the user can see that a dependency is missing before a task fails, and
 * since O-11 it is also where "no JavaScript engine" is stated in words
 * instead of arriving later as a bare 403. It now loads when 診斷 is opened
 * rather than every time 設定 is, which is also why it is a category and not
 * a footer.
 */

import { useEffect, useState } from "react";
import { api } from "@/shared/api/client";
import type { DoctorCheck, DoctorReport } from "@/shared/api/types";
import { POLICY_OPTIONS, useConfigStore } from "@/entities/config/model/store";
import { useOnboarding } from "@/entities/onboarding/model/store";
import { LogControls } from "@/features/manage-logs/ui/LogControls";
import { ToolsPanel } from "@/features/setup-tools/ui/ToolsPanel";
import { Button } from "@/shared/ui/Button";
import { ErrorBoundary } from "@/shared/ui/ErrorBoundary";

const AUTO_CLEAR_OPTIONS = [
  { value: 0, label: "不自動清除" },
  { value: 1, label: "1 天" },
  { value: 3, label: "3 天" },
  { value: 7, label: "7 天" },
  { value: 30, label: "30 天" },
] as const;

/**
 * The three things someone comes here to do.
 *
 * Each hint says what the category is FOR, in the words of the person looking
 * for it, because a list of nouns is a list you have to read all of.
 */
const CATEGORIES = [
  { id: "general", label: "一般", hint: "存到哪裡、下載什麼畫質" },
  { id: "logs", label: "紀錄", hint: "動作紀錄與錯誤資料" },
  { id: "diagnostics", label: "診斷", hint: "這台機器現在的狀況" },
] as const;

/** Exported so a caller can open 設定 AT a category. */
export type SettingsCategory = (typeof CATEGORIES)[number]["id"];

/** What a check's name means to a person. Unknown names fall through to
 *  themselves rather than disappearing — a dependency added later must still
 *  be visible before this table learns about it. */
const CHECK_LABELS: Record<string, string> = {
  "yt-dlp": "yt-dlp",
  // gallery-dl is gone from this table because the server stopped checking
  // it: nothing in the product invokes it, so 「未安裝（選用）」 was telling
  // every reader they lacked something that would do nothing for them.
  // `doctor.py`'s REQUIRED_BINARIES holds the conditions for bringing it back.
  ffmpeg: "ffmpeg",
  "javascript-runtime": "JavaScript 執行環境",
  chrome: "Chrome",
};

function statusOf(check: DoctorCheck): { text: string; tone: string } {
  if (check.ok) return { text: "正常", tone: "ok" };
  // Optional and missing is not a fault. Rendering it red taught the reader
  // to ignore the whole panel, which is R6-5's finding one layer up.
  if (!check.required) return { text: "未安裝（選用）", tone: "warn" };
  return { text: "缺少", tone: "error" };
}

/**
 * Why a check is not OK, in the reader's language.
 *
 * The server's `detail` is a developer's sentence -- 「gallery-dl not found on
 * PATH and no override configured」 -- and it was the only explanation this
 * Chinese panel offered. It is not translated at the source on purpose: it
 * also goes to `mfp doctor --json` and to that command's stderr, which are
 * English, and moving a machine-facing string into another language to fix a
 * GUI is the wrong layer.
 *
 * Every case here is derived from fields the panel ALREADY receives, so no
 * schema had to grow a field either: `found` separates absent from present,
 * and `version` separates "will not run" from "ran but said nothing usable".
 * The server's own sentence is still shown underneath, quietly -- it is the
 * line worth pasting into a bug report, and O-11's javascript-runtime detail
 * ("Note that Java is a different thing") says something no derived sentence
 * would.
 */
function explain(check: DoctorCheck): string | null {
  if (check.ok) return null;
  const name = CHECK_LABELS[check.name] ?? check.name;
  if (!check.found) {
    // The old sentence here read 「請安裝它，或在設定檔裡指定完整路徑」, which
    // assumed the reader knew what a PATH was, where the config file lived,
    // and which file on a release page to take. Since 需要的程式 sits at the
    // top of this same category with a button, the honest instruction is to
    // point at it.
    return check.required
      ? `找不到 ${name}。上面的「需要的程式」可以直接幫你安裝，裝好後按「重新檢查」。`
      : `這台機器上沒有 ${name}。只有部分來源需要它，其他功能不受影響。`;
  }
  if (!check.version) {
    return `找到了 ${name}，但它沒有回報一個看得懂的版本，所以無法確認能不能用。`;
  }
  return `找到了 ${name}，但它無法執行。通常是檔案損毀或權限問題。`;
}

export function DoctorPanel() {
  const [report, setReport] = useState<DoctorReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = async () => {
    setBusy(true);
    try {
      setReport(await api.getDoctor());
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    void refresh();
  }, []);

  return (
    <section className="mfp-settings__section" data-testid="doctor-panel">
      <div className="mfp-settings__section-head">
        <h3>相依工具檢查</h3>
        <Button onClick={() => void refresh()} disabled={busy}>
          {busy ? "檢查中…" : "重新檢查"}
        </Button>
      </div>

      {error !== null && (
        <p className="mfp-settings__error" role="alert">
          無法讀取檢查結果：{error}
        </p>
      )}

      {report !== null && (
        <table className="mfp-settings__doctor">
          <tbody>
            {report.checks.map((check) => {
              const status = statusOf(check);
              return (
                <tr key={check.name} data-check={check.name} data-tone={status.tone}>
                  <th scope="row">{CHECK_LABELS[check.name] ?? check.name}</th>
                  <td>{status.text}</td>
                  <td className="mfp-settings__doctor-version">{check.version ?? "—"}</td>
                  {/* Where a dependency explains itself, for every row that is
                      not simply fine, so the reader never has to go looking.
                      Two lines: what it means, then what the server said. */}
                  <td className="mfp-settings__doctor-detail">
                    {explain(check) && <span>{explain(check)}</span>}
                    {(check.ok && check.name !== "javascript-runtime"
                      ? ""
                      : (check.detail ?? "")) && (
                      <span className="mfp-settings__doctor-raw">
                        {check.ok && check.name !== "javascript-runtime"
                          ? ""
                          : (check.detail ?? "")}
                      </span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

/** 一般: where files land and what gets fetched. */
function GeneralCategory() {
  const config = useConfigStore((state) => state.config);
  const patch = useConfigStore((state) => state.patch);
  if (config === null) return null;

  return (
    <section className="mfp-settings__section" data-testid="settings-general">
      <div className="mfp-settings__section-head">
        <h3>一般</h3>
      </div>

      {/* A named group, because these four had no heading of any kind: the
          block holding 輸出資料夾 and 預設畫質 opened the page with nothing
          saying what it was. */}
      <div className="mfp-settings__group">
        <label className="mfp-settings__row">
          <span>輸出資料夾</span>
          <input
            type="text"
            value={config.outputRoot}
            onChange={(event) => void patch({ outputRoot: event.target.value })}
          />
        </label>

        <label className="mfp-settings__row">
          <span>預設畫質</span>
          <select
            value={config.policy}
            onChange={(event) => void patch({ policy: event.target.value })}
          >
            {POLICY_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>

        {/* On/off only, and no language beside it: asking for a named
            language asks the platform to TRANSLATE, and a stored default
            nobody remembers setting is the worst place for that decision
            (D-156/P-49). The wording says which file appears and where,
            because that is what the user will see change. */}
        <label className="mfp-settings__row">
          <span>一併存字幕</span>
          <input
            type="checkbox"
            checked={config.writeSubs === true}
            onChange={(event) => void patch({ writeSubs: event.target.checked })}
          />
          <small>
            有字幕軌的影片，會在影片旁邊多存一個字幕檔，語言是影片裡實際說的那一種。
            沒有字幕軌的影片照常下載，不會失敗。
          </small>
        </label>

        <label className="mfp-settings__row">
          <span>自動清除已完成</span>
          <select
            value={config.autoClearDays}
            onChange={(event) => void patch({ autoClearDays: Number(event.target.value) })}
          >
            {AUTO_CLEAR_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <small>只清除紀錄，不會刪除已下載的檔案</small>
        </label>

        <label className="mfp-settings__row">
          <span>顯示 Chrome 視窗</span>
          <input
            type="checkbox"
            checked={config.chrome.visible}
            onChange={(event) =>
              void patch({ chrome: { ...config.chrome, visible: event.target.checked } })
            }
          />
          <small>分析時是否讓瀏覽器視窗出現在畫面上</small>
        </label>
      </div>
    </section>
  );
}

/** 診斷: what this machine is like right now.
 *
 *  「不可調整」 no longer opens it. 需要的程式 does, because it is the one
 *  block here somebody can ACT on -- and because a person sent to 診斷 by
 *  the missing-program banner has come to press exactly one button. The
 *  read-only rows and the raw dependency table follow it; they answer
 *  「這台機器是什麼狀況」, which is the question you ask second. */
function DiagnosticsCategory() {
  const resetGuides = useOnboarding((state) => state.resetAll);
  const [guidesReset, setGuidesReset] = useState(false);

  return (
    <>
      <ToolsPanel />

      <section className="mfp-settings__section mfp-settings__section--readonly">
        <div className="mfp-settings__section-head">
          <h3>不可調整</h3>
        </div>
        <div className="mfp-settings__group">
          <div className="mfp-settings__row" data-testid="setting-concurrency">
            <span>同時下載數</span>
            <strong>1</strong>
            <small>傳輸刻意序列進行，速度顯示才是一個人讀得懂的數字</small>
          </div>
          <div className="mfp-settings__row" data-testid="setting-rate-limit">
            <span>速率上限</span>
            <strong>依平台預設</strong>
            <small>這是機器人防護的節流，放寬會讓來源端把我們當成爬蟲</small>
          </div>
        </div>
      </section>

      <DoctorPanel />

      {/* The undo for a click that was too fast. Without it the honest
          answer to 「剛剛那個說明可以叫回來嗎」 involves editing a JSON file
          in %APPDATA%, which is not an answer. */}
      <section className="mfp-settings__section" data-testid="guides-reset">
        <div className="mfp-settings__section-head">
          <h3>使用說明</h3>
          <Button
            onClick={() => {
              void resetGuides().then(() => setGuidesReset(true));
            }}
          >
            全部重新顯示
          </Button>
        </div>
        <p className="mfp-settings__hint">
          {guidesReset
            ? "好了。下次打開每個工具時，第一次使用的說明會再出現一次。"
            : "每個工具第一次打開時會有一段簡短說明，看過就不再出現。按這裡可以讓它們全部再出現一次。"}
        </p>
      </section>
    </>
  );
}

export function SettingsPanel({
  onClose,
  initial = "general",
}: {
  onClose: () => void;
  /** Which category to land on. */
  initial?: SettingsCategory;
}) {
  const config = useConfigStore((state) => state.config);
  const lastError = useConfigStore((state) => state.lastError);
  const [category, setCategory] = useState<SettingsCategory>(initial);

  if (config === null) {
    return (
      <div className="mfp-settings" data-testid="settings-panel">
        <p>讀取設定中…</p>
      </div>
    );
  }

  return (
    <div className="mfp-settings" data-testid="settings-panel">
      <div className="mfp-settings__head">
        <h2>設定</h2>
        {/* The single exit, and it says what it does. 設定 is a mode you
            RESUME from -- closing it puts you back on whatever you were
            looking at, queue or workspace -- so there is no second 「回到佇列」
            to offer: that would be a destination, and this is not. */}
        <Button onClick={onClose}>關閉設定</Button>
      </div>

      {lastError !== null && (
        <p className="mfp-settings__error" role="alert">
          設定未儲存：{lastError}
        </p>
      )}

      <div className="mfp-settings__layout">
        <nav className="mfp-settings__nav" aria-label="設定分類" data-testid="settings-nav">
          <ul>
            {CATEGORIES.map((entry) => (
              <li key={entry.id}>
                <button
                  type="button"
                  className="mfp-settings__nav-item"
                  data-category={entry.id}
                  data-active={entry.id === category ? "true" : "false"}
                  aria-current={entry.id === category ? "page" : undefined}
                  onClick={() => setCategory(entry.id)}
                >
                  <span className="mfp-settings__nav-label">{entry.label}</span>
                  <span className="mfp-settings__nav-hint">{entry.hint}</span>
                </button>
              </li>
            ))}
          </ul>
        </nav>

        {/* Contained here so a throw in one category leaves the category
            list reachable, so the user can still reach 一般 or 紀錄 --
            and switching category clears the error by `resetKey` rather
            than needing the button. */}
        <ErrorBoundary
          label={CATEGORIES.find((entry) => entry.id === category)?.label ?? category}
          intact="左邊的分類還在，其他設定都可以照常使用。"
          resetKey={category}
        >
        <div className="mfp-settings__page" data-category={category}>
          {category === "general" && <GeneralCategory />}
          {category === "logs" && <LogControls />}
          {category === "diagnostics" && <DiagnosticsCategory />}
        </div>
        </ErrorBoundary>
      </div>
    </div>
  );
}
