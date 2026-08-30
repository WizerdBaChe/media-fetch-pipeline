/**
 * 語音辨識 setup: the panel that answers 「可以聽寫了嗎」 and, when the answer
 * is no, the only place that says what to do about it.
 *
 * What it replaces is worth stating, because the shape of this file is a
 * reaction to it. Before this, a user whose machine could not transcribe saw:
 * a red line reading 「沒有語音辨識引擎」 followed by a sentence naming
 * `asr.python`, `config.json` and `faster-whisper`; a row in 相依工具檢查
 * labelled `asr` with a file path in it; and no control anywhere that could
 * change either. Every one of those is a developer looking at their own
 * machine. None of them is a person trying to transcribe a recording.
 *
 * Four rules shape what is on screen.
 *
 * **One verdict per capability, at the top, in a sentence.** 可以聽寫 or
 * 還不能聽寫, then why, then the buttons for what is missing. A user who
 * reads nothing else has read enough.
 *
 * **Two things to have, named as things rather than as fields.** 「引擎環境」
 * and 「語音模型」, each one row, each with its own button. `asr.python` is a
 * config key; 「引擎環境」 is the thing it points at.
 *
 * **Machine state is available, not displayed.** Format versions, mel bins
 * and spec names all still exist -- under 技術細節, collapsed. Removing them
 * would trade one unusable panel for another the moment something goes
 * wrong; the fix was never to hide them but to stop leading with them. The
 * engine's PATH is the exception and now sits with the engine: a row that
 * says 「尚未設定」 while the path lives in another band is C1.
 *
 * **One band per kind of question** (the 2026-08-28 repair). There are three
 * kinds of thing here and only three: a COMPONENT is present or not (引擎,
 * 模型資料夾), a CAPABILITY can be used or not (語音辨識, 翻譯), and the
 * INVENTORY is what is sitting in the folder. A capability is a function of
 * the other two and is never one of them. Before the bands, those three
 * alternated five times down the page and shared one green/orange vocabulary,
 * so 「引擎已就緒」 and 「還不能用」 read as a contradiction rather than as two
 * statements about different objects. The audit is
 * `docs/asr-readiness-view-model.md`; C-numbers below refer to it.
 */

import { Fragment, useEffect, useRef, useState } from "react";
import { Button } from "@/shared/ui/Button";
import { isDesktop, pickFolder, pickPythonFile, revealModelFolder } from "@/shared/lib/desktop";
import {
  capabilityTone,
  describeEngine,
  describeFetch,
  describeInstall,
  humanBytes,
  toneWord,
  useAsrSetup,
} from "@/features/setup-asr/model/store";
import type {
  AsrCapability,
  AsrInstallMode,
  AsrModel,
  AsrSetupStep,
} from "@/shared/api/types";
import { AsrGuide } from "./AsrGuide";

/** Where a configured interpreter came from, in words. The `environment`
 *  case is the one that matters: a shell variable silently overrides the
 *  setting, and a user who has forgotten setting one has no other way to
 *  find out why the path they just chose is being ignored. */
const ENGINE_SOURCE: Record<string, string> = {
  environment: "由環境變數 MFP_ASR_PYTHON 指定",
  settings: "在這裡設定的",
  "beside-the-app": "自動找到的（放在程式旁邊）",
};

/** One label per action, in one place.
 *
 *  `pick-model` browses for a folder that is not here yet; `choose-installed`
 *  picks among the ones that already are. They used to share a name and a
 *  handler, so a step reading 「選一個已經在資料夾裡的辨識模型」 rendered a
 *  button that opened a browse-for-a-new-folder dialog (C7). */
const ACTION_LABEL: Record<string, string> = {
  "pick-engine": "設定引擎…",
  "pick-model": "加入模型…",
  "choose-installed": "從下面的清單挑一個",
};

/** The button for one step, named for the KIND it is about where there is
 *  one. Both capabilities emit `pick-model`, so a static 「加入模型…」 put two
 *  identically-labelled buttons on the screen doing mechanically the same
 *  thing -- 「加入辨識模型…」 and 「加入翻譯模型…」 are one action each.
 *  `needsLabel` comes from the server's `KIND_LABEL`, which is where this
 *  taxonomy's words live so the CLI and the GUI cannot grow two of them. */
function labelFor(action: string, needsLabel: string): string {
  if (action === "pick-model" && needsLabel) return `加入${needsLabel}…`;
  return ACTION_LABEL[action] ?? "處理…";
}

/**
 * What to do to the sound before recognising it.
 *
 * Written for somebody describing their RECORDING, not their signal chain:
 * a person knows「一邊很大聲一邊很小聲」and does not know「dynaudnorm」.
 *
 * Each hint says what was actually measured, including the unflattering
 * part. 「壓背景雜訊」 is offered and told plainly that it does not help the
 * case people will reach for it in — hiding it would be tidier and less
 * honest, and a user who tries it and gets a worse transcript deserves to
 * have been warned rather than to find out.
 */
const AUDIO_MODES = [
  {
    id: "none",
    label: "一般（建議）",
    hint: "照錄音原本的樣子聽。多數檔案這樣最準；如果聽不出東西，程式會自己再試一次。",
  },
  {
    id: "level",
    label: "一邊大聲一邊小聲",
    hint:
      "電話、會議、隔著桌子的錄音——有人聲音特別小的那種。" +
      "實測能讓小聲那一方被聽見，代價是處理慢一點，結果偶爾會不太一樣。",
  },
  {
    id: "denoise",
    label: "背景很吵",
    hint:
      "只適合「講話的人夠大聲、但背景一直有噪音」。" +
      "如果有人講話很小聲，請不要選這個——實測它會把小聲的人聲跟雜訊一起消掉，幫不上忙。",
  },
] as const;

/** The heading of one band. Bands are the whole point of the layout, so they
 *  say what QUESTION they answer rather than naming a noun. */
function Band({
  id,
  title,
  hint,
  children,
}: {
  id: string;
  title: string;
  hint: string;
  children: React.ReactNode;
}) {
  return (
    <section className="mfp-asr__band" data-band={id} data-testid={`asr-band-${id}`}>
      <h4 className="mfp-asr__band-head">
        {title}
        <small className="mfp-asr__muted">{hint}</small>
      </h4>
      {children}
    </section>
  );
}

/** Colour-coded by KIND, so the two model types are distinguishable at a
 *  glance and not only by reading a label. */
function KindBadge({ model }: { model: AsrModel }) {
  return (
    <span className="mfp-asr__badge" data-kind={model.kind}>
      {model.kindLabel}
    </span>
  );
}

function ModelLine({ model }: { model: AsrModel }) {
  return (
    <>
      <span className="mfp-asr__model-name">{model.name}</span>
      <KindBadge model={model} />
      {model.isLink && <span className="mfp-asr__badge">捷徑</span>}
      <span className="mfp-asr__muted">{model.summary}</span>
      {/* The one thing that turns "download a model" into "download a model
          AND install a package". Said on the row rather than buried in the
          notes, because it changes what the user has to go and do. */}
      {model.tokenizerPackage && (
        <span className="mfp-asr__badge" data-kind="warn">
          需要 {model.tokenizerPackage}
        </span>
      )}
    </>
  );
}

/**
 * One capability's verdict, its reason, and its buttons.
 *
 * Rendered per capability rather than for recognition alone, which is what
 * finally put 「還沒有翻譯功能」 on a screen -- the server had been computing
 * that headline since the capability existed and nothing ever displayed it.
 * The tone is `optional`'s only consumer: a translation model nobody asked
 * for is grey and prefixed 「＋」, a recognition engine that will not import
 * is orange and prefixed 「！」 (C6).
 */
function VerdictCard({
  capability,
  busy,
  onAct,
}: {
  capability: AsrCapability;
  busy: boolean;
  onAct: (action: string) => void;
}) {
  const tone = capabilityTone(capability);
  return (
    <div
      className="mfp-asr__verdict"
      data-tone={tone}
      data-ready={capability.ready ? "true" : "false"}
      data-capability={capability.id}
      data-state={capability.state}
      data-testid={`asr-verdict-${capability.id}`}
      role="status"
    >
      <strong>
        {tone === "ready" ? "✓ " : tone === "blocked" ? "！ " : "＋ "}
        {capability.headline}
      </strong>
      <p className="mfp-asr__why">{capability.detail}</p>
      {/* No 「還需要一個{kind}」 line here, and the reason is worth keeping.
          The old capability table carried one because the table had no prose
          of its own; this card does, and EVERY `no_model` detail the server
          writes already names the kind -- 「還差一個辨識模型」, 「翻譯需要另外
          一種模型——翻譯模型」, 「資料夾裡有 2 個可以用的翻譯模型」. Adding the
          line back put the same fact in the card twice, which is C4's shape
          arriving through the repair for C2 (caught in the rendered picture,
          not by any of the assertions written for it). The instruction is
          kept; it is kept ONCE, in the sentence the server composed. */}
      {capability.steps.length > 0 && (
        <ul className="mfp-asr__steps">
          {capability.steps.map((step: AsrSetupStep) => {
            // The engine is SHARED, so its button has one owner. Both
            // capabilities emit `pick-engine` when it is missing, and
            // rendering both put two identically-named buttons on one screen
            // -- the thing this file removed from the capability table for
            // the same reason. Translation's own detail already says 「要先把
            // 上面那個引擎設定好」, and the engine's row carries the control.
            const owned = step.action !== "pick-engine" || capability.id === "recognition";
            return (
              <li key={step.text}>
                <span>{step.text}</span>
                {step.action && step.action !== "guide" && owned && (
                  <Button
                    variant={tone === "blocked" ? "primary" : undefined}
                    disabled={busy}
                    onClick={() => onAct(step.action ?? "")}
                  >
                    {labelFor(step.action, capability.needsLabel)}
                  </Button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/**
 * Two capabilities, side by side: what each does, whether it works, and the
 * one button that changes it.
 *
 * It is a table rather than two cards because the COMPARISON is the point --
 * a reader who does not know what a model is can still see that one row is
 * green and the other is not.
 *
 * It carries no prose and no buttons.
 *
 * The prose went because it printed `detail`, the same sentence the card
 * above already shows, so one fact appeared twice within one screen under
 * two different labels (C4). The 「用哪個模型」 column went because it read
 * `active` while the status column beside it read `ready` -- 「可以用」 next
 * to 「還缺一個辨識模型」, and 「有 2 個可以用的翻譯模型」 next to 「還缺一個翻
 * 譯模型」 (C3, C2); which model is in use is an INVENTORY question and the
 * model list answers it with the capability's name on the badge. The buttons
 * went because each capability's card carries the same one, and this file's
 * own rule about 「換一個…」 says it: two identically-named buttons in one
 * small panel is a choice the reader has to make between two things that do
 * the same thing.
 */
function CapabilityTable({ capabilities }: { capabilities: AsrCapability[] }) {
  return (
    <table className="mfp-asr__capabilities" data-testid="asr-capabilities">
      <thead>
        <tr>
          <th scope="col">功能</th>
          <th scope="col">狀態</th>
        </tr>
      </thead>
      <tbody>
        {capabilities.map((entry) => {
          const tone = capabilityTone(entry);
          return (
            <tr
              key={entry.id}
              data-capability={entry.id}
              data-ready={entry.ready ? "true" : "false"}
              data-tone={tone}
            >
              <th scope="row">
                <span className="mfp-asr__cap-label">{entry.label}</span>
                <small className="mfp-asr__muted">{entry.what}</small>
              </th>
              <td>
                <span className="mfp-asr__cap-state" data-tone={tone}>
                  {toneWord(tone)}
                </span>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export function AsrSetupPanel() {
  const readiness = useAsrSetup((state) => state.readiness);
  const error = useAsrSetup((state) => state.error);
  const busy = useAsrSetup((state) => state.busy);
  const scan = useAsrSetup((state) => state.scan);
  const chosen = useAsrSetup((state) => state.chosen);
  const mode = useAsrSetup((state) => state.mode);
  const install = useAsrSetup((state) => state.install);
  const catalogue = useAsrSetup((state) => state.catalogue);
  const downloading = useAsrSetup((state) => state.downloading);
  const load = useAsrSetup((state) => state.load);

  /** Browser mode has no file dialogs, so the same actions are reachable by
   *  typing a path. Not a fallback nobody uses: `mfp serve` plus a browser is
   *  a supported way to run this. */
  const [typedPath, setTypedPath] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  /** Set by `choose-installed`, so the step that says 「從清單挑一個」 can
   *  point at the list instead of leaving the reader to find it. */
  const [pointingAtList, setPointingAtList] = useState(false);
  const listRef = useRef<HTMLUListElement | null>(null);
  const desktop = isDesktop();

  useEffect(() => {
    void load();
  }, [load]);

  const store = useAsrSetup.getState;

  // Escape closes the folder-scan dialog -- the only dismissal a keyboard
  // user has, since the backdrop click is the mouse one. Not while a copy is
  // in flight: closing then would hide a 3 GB transfer that is still running.
  useEffect(() => {
    if (scan === null) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) useAsrSetup.getState().clearScan();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [scan, busy]);

  const installLine = describeInstall(install);
  const fetchLine = describeFetch(install);

  const chooseEngine = async () => {
    const picked = desktop ? await pickPythonFile() : typedPath.trim();
    if (!picked) return;
    await store().setEngine(picked);
    setTypedPath("");
  };

  const chooseModelFolder = async () => {
    const picked = desktop ? await pickFolder("model-source") : typedPath.trim();
    if (!picked) return;
    await store().scanFolder(picked);
    setTypedPath("");
  };

  const chooseHome = async () => {
    const picked = desktop ? await pickFolder("model-home") : typedPath.trim();
    if (!picked) return;
    await store().setHome(picked);
    setTypedPath("");
  };

  const openHome = async () => {
    const refusal = await revealModelFolder();
    setNotice(refusal);
  };

  /** The step's action, done. `choose-installed` is the odd one: the models
   *  are already here and the control that switches between them lives on
   *  each row of the list, so this points at the list rather than opening a
   *  dialog that would go looking for a folder the user already has. */
  const act = (action: string) => {
    if (action === "pick-engine") {
      void chooseEngine();
      return;
    }
    if (action === "choose-installed") {
      setPointingAtList(true);
      // jsdom has no layout engine and therefore no `scrollIntoView`; the
      // pointer above is what the test asserts, and the scroll is the part
      // only a real browser can do.
      listRef.current?.scrollIntoView?.({ behavior: "smooth", block: "center" });
      return;
    }
    void chooseModelFolder();
  };

  // Kept identical to the loaded heading. It used to read 「語音辨識（逐字稿
  // 要用的）」 while loading and 「語音辨識與翻譯（逐字稿要用的）」 afterwards,
  // so the section retitled itself under the reader.
  const heading = "語音辨識與翻譯（逐字稿要用的）";

  if (readiness === null) {
    return (
      <section className="mfp-settings__section" data-testid="asr-setup">
        <div className="mfp-settings__section-head">
          <h3>{heading}</h3>
        </div>
        <p className="mfp-asr__muted">{error ?? "檢查中…"}</p>
      </section>
    );
  }

  const home = readiness.home;
  const engine = describeEngine(readiness.engine);
  const usableScanned = scan?.models.filter((model) => model.usable) ?? [];
  const activeMode = scan?.modes.find((entry) => entry.mode === mode);

  return (
    <section className="mfp-settings__section mfp-asr" data-testid="asr-setup">
      <div className="mfp-settings__section-head">
        <h3>{heading}</h3>
        <Button onClick={() => void load()} disabled={busy}>
          {busy ? "檢查中…" : "重新檢查"}
        </Button>
      </div>

      {error !== null && (
        <p className="mfp-settings__error" role="alert" data-testid="asr-error">
          {error}
        </p>
      )}
      {notice !== null && (
        <p className="mfp-settings__error" role="alert">
          {notice}
        </p>
      )}

      {/* ═══ 帶 A ═══ 能力：能不能做 ═══════════════════════════════════ */}
      <Band id="can" title="你現在能做什麼" hint="這兩件事各自需要什麼，下面兩帶會說">
        {/* Every capability, including the ready ones. 「✓ 可以聽寫」 is the
            positive confirmation this panel exists to produce, and a surface
            that only speaks up when something is wrong never tells anybody
            that it is now right. */}
        {readiness.capabilities.map((entry) => (
          <VerdictCard key={entry.id} capability={entry} busy={busy} onAct={act} />
        ))}
        <CapabilityTable capabilities={readiness.capabilities} />
      </Band>

      {/* ═══ 帶 B ═══ 元件與清單：有什麼 ══════════════════════════════ */}
      <Band id="have" title="這台機器上有什麼" hint="上面兩件事就是靠這些東西完成的">
        {/* The shared prerequisite, as a thing. Not a capability -- it is what
            BOTH capabilities run on, which is why it sits in this band and
            why the translation card points back at it when it is missing. */}
        <div className="mfp-asr__part" data-testid="asr-part-engine" data-tone={engine.tone}>
          <span className="mfp-asr__part-name">引擎環境</span>
          <span className="mfp-asr__part-state" data-tone={engine.tone}>
            {engine.text}
          </span>
          {/* Deliberately NOT 「選擇 Python…」: the card above already has a
              button when the engine is missing, and two identically-named
              buttons in one small panel is a choice the reader has to make
              between two things that do the same thing. */}
          <Button disabled={busy} onClick={() => void chooseEngine()}>
            {readiness.engine.path ? "換一個…" : "設定…"}
          </Button>
          {/* The path lives HERE, next to the verdict about it. It used to be
              in 技術細節 three bands down, which is how 「尚未設定」 and a real
              interpreter path ended up on the same screen (C1). */}
          {readiness.engine.path && (
            <span className="mfp-asr__part-path mfp-mono" title={readiness.engine.path}>
              {readiness.engine.path}
            </span>
          )}
          {engine.problem && <span className="mfp-asr__part-problem">{engine.problem}</span>}
          <small className="mfp-asr__muted">
            {readiness.engine.source ? `${ENGINE_SOURCE[readiness.engine.source]}。` : ""}
            語音辨識和翻譯共用這一個，設定一次就好。
          </small>
        </div>

        {/* --- where the models live ------------------------------------- */}
        <div className="mfp-asr__part" data-testid="asr-home">
          <span className="mfp-asr__part-name">模型資料夾</span>
          <span className="mfp-asr__home-path mfp-mono" title={home.path}>
            {home.path}
          </span>
          <Button disabled={busy} onClick={() => void openHome()}>
            開啟資料夾
          </Button>
          <Button disabled={busy} onClick={() => void chooseHome()}>
            換位置…
          </Button>
          <small className="mfp-asr__muted">
            {home.exists ? "" : "還不存在，加入第一個模型時會自動建立。"}
            {home.freeBytes !== null ? `這個磁碟還剩 ${humanBytes(home.freeBytes)}。` : ""}
            {home.writable ? "" : " 這個位置沒有寫入權限，請換一個。"}
          </small>
        </div>

        {!desktop && (
          <label className="mfp-asr__typed">
            <span>路徑（瀏覽器版沒有檔案選擇視窗，請直接貼上）</span>
            <input
              type="text"
              value={typedPath}
              placeholder="例如 D:\asr-venv\Scripts\python.exe 或模型資料夾"
              onChange={(event) => setTypedPath(event.target.value)}
            />
          </label>
        )}

        {/* Everything in the folder, with its type and WHO IS USING IT.
            Always shown when there is anything at all: with two kinds in
            play, "which of these is which" is the question the list exists
            to answer, and "which one is my transcription actually using" is
            the question the capability table used to answer badly. */}
        {readiness.models.length > 0 ? (
          <ul
            className="mfp-asr__models"
            data-testid="asr-model-list"
            data-pointed={pointingAtList ? "true" : "false"}
            ref={listRef}
          >
            {readiness.models.map((model) => {
              const usedBy = readiness.capabilities.find(
                (entry) => entry.active?.path === model.path,
              );
              return (
                <li key={model.path} data-usable={model.usable ? "true" : "false"}>
                  <ModelLine model={model} />
                  {usedBy ? (
                    <span className="mfp-asr__badge mfp-asr__badge--ok">
                      {usedBy.label}使用中
                    </span>
                  ) : model.usable ? (
                    <Button disabled={busy} onClick={() => void store().select(model.name)}>
                      改用這個
                    </Button>
                  ) : null}
                  {/* Removal is offered ONLY for shortcuts, because that is
                      the only thing the server will remove. A real folder is
                      several GB the user put there, and Explorer has a
                      recycle bin where this panel would not. */}
                  {model.isLink && (
                    <Button
                      variant="ghost"
                      disabled={busy}
                      onClick={() => void store().remove(model.name)}
                    >
                      移除捷徑
                    </Button>
                  )}
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="mfp-asr__muted" data-testid="asr-model-list-empty">
            這個資料夾裡還沒有任何模型。
          </p>
        )}

      </Band>

      {/* --- the add-a-model flow, after a folder has been picked ---------
          A DIALOG, not a paragraph further down the band.

          The defect it repairs (user report 2026-08-28): picking a folder
          answered in a block below the fold, so a reader who did not scroll
          concluded the picker had done nothing and picked again. Choosing a
          folder is the moment the question changes from 「哪個資料夾」 to
          「這個模型要用複製還是捷徑」, and a question the reader cannot see is
          a question they cannot answer. The wrong-folder case is the same
          event and gets the same weight -- it used to be the quietest thing
          on the page. */}
      {scan !== null && (
        <div
          className="mfp-modal__backdrop"
          role="presentation"
          onClick={() => !busy && store().clearScan()}
        >
          <div
            className="mfp-modal mfp-modal--wide mfp-asr__scan"
            role="dialog"
            aria-modal="true"
            aria-label={
              usableScanned.length === 0 ? "這個資料夾沒有可以用的模型" : "選好資料夾了"
            }
            data-testid="asr-scan"
            onClick={(event) => event.stopPropagation()}
          >
            <h2 className="mfp-modal__title">
              {usableScanned.length === 0
                ? "這個資料夾裡沒有可以用的模型"
                : "選好資料夾了，接下來決定怎麼放進去"}
            </h2>
            <p className="mfp-asr__scan-picked">
              <span className="mfp-asr__part-name">你選的資料夾</span>
              <code title={scan.diagnosis.path}>{scan.diagnosis.path}</code>
            </p>
            {usableScanned.length === 0 ? (
              <>
                <p className="mfp-asr__scan-verdict">{scan.diagnosis.summary}</p>
                {scan.diagnosis.notes.map((note) => (
                  <p key={note} className="mfp-asr__muted">
                    {note}
                  </p>
                ))}
                {scan.models.length > 0 && (
                  <ul className="mfp-asr__models">
                    {scan.models.map((model) => (
                      <li key={model.path} data-usable="false">
                        <ModelLine model={model} />
                      </li>
                    ))}
                  </ul>
                )}
                <div className="mfp-modal__actions">
                  {/* The way OUT of a wrong folder, offered where the wrong
                      folder is being reported. Sending the reader back to
                      find the 瀏覽… button behind a dialog is how a dead end
                      gets built. */}
                  <Button disabled={busy} onClick={() => void chooseModelFolder()}>
                    換一個資料夾…
                  </Button>
                  <Button onClick={() => store().clearScan()}>關閉</Button>
                </div>
              </>
            ) : (
              <>
                <p className="mfp-asr__scan-verdict">
                  找到 {usableScanned.length} 個可以用的模型。
                </p>
                <ul className="mfp-asr__models">
                  {usableScanned.map((model) => (
                    <li key={model.path} data-usable="true">
                      <label>
                        <input
                          type="radio"
                          name="asr-scan-model"
                          checked={chosen?.path === model.path}
                          onChange={() => store().choose(model)}
                        />
                        <ModelLine model={model} />
                      </label>
                    </li>
                  ))}
                </ul>

                <fieldset className="mfp-asr__modes">
                  <legend>要怎麼把它放進模型資料夾？</legend>
                  {scan.modes.map((option) => (
                    <label
                      key={option.mode}
                      data-available={option.available ? "true" : "false"}
                    >
                      <input
                        type="radio"
                        name="asr-install-mode"
                        value={option.mode}
                        checked={mode === option.mode}
                        disabled={!option.available || busy}
                        onChange={() => store().setMode(option.mode as AsrInstallMode)}
                      />
                      <span className="mfp-asr__mode-label">{option.label}</span>
                      <small className="mfp-asr__muted">
                        {option.detail}
                        {option.reason ? `（${option.reason}）` : ""}
                      </small>
                    </label>
                  ))}
                </fieldset>

                {/* The consequences of the chosen mode, BEFORE the button. A
                    warning that arrives with the result is a report, not a
                    warning. */}
                {activeMode && activeMode.warnings.length > 0 && (
                  <ul className="mfp-asr__warnings" data-testid="asr-install-warnings">
                    {activeMode.warnings.map((warning) => (
                      <li key={warning}>{warning}</li>
                    ))}
                  </ul>
                )}

                <div className="mfp-asr__scan-actions">
                  <Button
                    variant="primary"
                    disabled={busy || chosen === null}
                    onClick={() => void store().installChosen()}
                    data-testid="asr-install"
                  >
                    {busy ? "處理中…" : "確認加入"}
                  </Button>
                  <Button disabled={busy} onClick={() => store().clearScan()}>
                    取消
                  </Button>
                  {installLine && (
                    <span
                      className="mfp-asr__progress"
                      role="status"
                      data-testid="asr-install-progress"
                    >
                      {installLine}
                    </span>
                  )}
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {/* ═══ 帶 C ═══ 設定與動作：可以動什麼 ══════════════════════════ */}
      <Band id="do" title="你可以動什麼" hint="這些都可以隨時改，改完立刻生效">
        {/* --- fetching one, rather than being pointed at one ------------- */}
        <div className="mfp-asr__catalogue" data-testid="asr-catalogue">
          <div className="mfp-asr__catalogue-head">
            <span className="mfp-asr__part-name">下載延伸資源</span>
            {catalogue === null ? (
              <Button disabled={busy} onClick={() => void store().loadCatalogue()}>
                看有哪些可以下載
              </Button>
            ) : (
              <small className="mfp-asr__muted">
                直接下載到上面那個模型資料夾，不必自己找檔案。
                {/* Names the row by the label it actually carries. It used to
                    say 「上面的辨識引擎」, pointing at a heading that is not
                    on the screen. */}
                {readiness.engine.present ? "" : " 需要先設定好上面的「引擎環境」。"}
              </small>
            )}
          </div>

          {catalogue?.map((entry) => (
            <div className="mfp-asr__catalogue-row" key={entry.id} data-kind={entry.kind}>
              <span className="mfp-asr__catalogue-name">
                {entry.label}
                {entry.recommended && <em className="mfp-asr__pick">建議</em>}
              </span>
              <span className="mfp-asr__muted">{entry.detail}</span>
              {/* The size BEFORE the click, not discovered during it. This is
                  the same reason `allowDownload` is off by default: a 3 GB
                  transfer nobody was warned about looks like a hang. */}
              <span className="mfp-mono">{humanBytes(entry.bytes)}</span>
              {entry.installed ? (
                <span className="mfp-asr__muted">已經有了</span>
              ) : (
                <Button
                  disabled={busy || !readiness.engine.present}
                  onClick={() => void store().download(entry.id)}
                >
                  {downloading === entry.id ? "下載中…" : "下載"}
                </Button>
              )}
              {downloading === entry.id && fetchLine && (
                <span className="mfp-asr__progress" role="status">
                  {fetchLine}
                </span>
              )}
            </div>
          ))}
        </div>

        {/* Offered because both were measured, and described by what a person
            would notice rather than by the filter that does it. `denoise`
            carries its own warning: it was measured to take away as much of a
            quiet voice as it recovers, and hiding an option that helps one
            case and hurts another would be less honest than saying which is
            which (user ruling 2026-08-28). */}
        <div className="mfp-asr__audio" data-testid="asr-audio">
          <span className="mfp-asr__part-name">錄音的聲音狀況</span>
          <select
            className="mfp-select"
            value={readiness.audio ?? "none"}
            disabled={busy}
            aria-label="錄音的聲音狀況"
            onChange={(event) => void store().setAudio(event.target.value)}
          >
            {AUDIO_MODES.map((option) => (
              <option key={option.id} value={option.id}>
                {option.label}
              </option>
            ))}
          </select>
          <small className="mfp-asr__muted">
            {AUDIO_MODES.find((m) => m.id === (readiness.audio ?? "none"))?.hint}
          </small>
        </div>

        <label className="mfp-settings__row">
          <span>允許自動下載模型</span>
          <input
            type="checkbox"
            checked={readiness.allowDownload}
            disabled={busy}
            onChange={(event) => void store().setDownload(event.target.checked)}
          />
          <small>
            打開之後，第一次聽寫時若資料夾裡沒有模型，會自己去下載約 3 GB。
            預設關閉：沒有預告的大量下載跟當掉看起來一模一樣。
          </small>
        </label>
      </Band>

      {/* ═══ 帶 D ═══ 說明：看不懂的時候 ═════════════════════════════ */}
      <Band id="help" title="看不懂的時候" hint="平常不用打開">
        {/* Where an already-downloaded model tends to be, for somebody who has
            one and cannot find it. Deliberately a HINT and not a search: a
            whole-disk keyword index is what would actually solve this, and
            building one into a media downloader is the wrong place for it
            (user ruling 2026-08-28) — so this names the folders and points at
            the tool that already does that job properly. */}
        <details className="mfp-asr__disclosure" data-testid="asr-where">
          <summary>已經下載過模型，但找不到放在哪？</summary>
          <p className="mfp-asr__muted">
            模型資料夾裡一定有一個 <code>model.bin</code>，而且很大（0.5～3 GB）。
            用檔案總管搜尋 <code>model.bin</code> 很慢，建議用{" "}
            <strong>Everything</strong> 這類檔名索引工具搜 <code>model.bin</code>，
            再把它<strong>所在的資料夾</strong>用「加入模型…」指過來。
          </p>
          <p className="mfp-asr__muted">
            最常見的位置：
            <code>C:\Users\你\.cache\huggingface\hub</code>（其他程式下載的）、
            以及你自己指定的下載位置。
            如果都找不到，直接用「下載延伸資源」重抓一份會比較快。
          </p>
        </details>

        <details className="mfp-asr__disclosure">
          <summary>怎麼取得引擎與模型？（含硬體與版本說明）</summary>
          <AsrGuide />
        </details>

        <details className="mfp-asr__disclosure" data-testid="asr-technical">
          <summary>技術細節</summary>
          <dl className="mfp-asr__tech">
            {/* 引擎路徑 is NOT here any more -- it belongs beside the engine's
                own verdict, and its absence from that row was C1. */}
            <dt>模型資料夾</dt>
            <dd className="mfp-mono">{home.path}</dd>
            {/* Per capability, because there are two settings now and a single
                「設定裡的模型名稱」 could only ever have shown one of them. */}
            {readiness.capabilities.map((entry) => (
              <Fragment key={entry.id}>
                <dt>{entry.label}：設定的名稱</dt>
                <dd className="mfp-mono">{entry.configured || "（未設定）"}</dd>
                {entry.active && (
                  <>
                    <dt>{entry.label}：使用中</dt>
                    <dd className="mfp-mono">{entry.active.path}</dd>
                    <dt>{entry.label}：格式</dt>
                    <dd className="mfp-mono">
                      {entry.active.spec ?? "unknown"} v{entry.active.binaryVersion ?? "?"}
                      {entry.active.melBins ? `, ${entry.active.melBins} mel` : ""}
                      {entry.active.languages ? `, ${entry.active.languages} 語言` : ""}
                      {entry.active.tokenizer ? `, ${entry.active.tokenizer}` : ""}
                    </dd>
                  </>
                )}
              </Fragment>
            ))}
            <dt>可以建立捷徑</dt>
            <dd>{readiness.canLink ? "是" : `否 — ${readiness.linkDetail}`}</dd>
          </dl>
        </details>
      </Band>
    </section>
  );
}
