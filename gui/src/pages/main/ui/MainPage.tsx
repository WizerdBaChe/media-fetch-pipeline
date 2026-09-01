import { useCallback, useRef, useState } from "react";
import { Button } from "@/shared/ui/Button";
import { ErrorBoundary } from "@/shared/ui/ErrorBoundary";
import { Tabs, type TabDef } from "@/shared/ui/Tabs";
import * as history from "@/shared/lib/history";
import { countsByTab, type TabId } from "@/entities/task/model/selectors";
import { useTaskStore } from "@/entities/task/model/store";
import { AddUrlsForm } from "@/features/add-urls/ui/AddUrlsForm";
import { ExtensionMenu } from "@/features/pick-extension/ui/ExtensionMenu";
import { CapabilityNotice } from "@/widgets/capability-notice/ui/CapabilityNotice";
import { DocumentWorkspace } from "@/widgets/document-workspace/ui/DocumentWorkspace";
import { PostWorkspace } from "@/widgets/post-workspace/ui/PostWorkspace";
import { QueueTable } from "@/widgets/queue-table/ui/QueueTable";
import {
  StackWorkspace,
  type StackHandoff,
  type StackSnapshot,
} from "@/widgets/stack-workspace/ui/StackWorkspace";
import {
  TranscriptWorkspace,
  type TranscriptSnapshot,
} from "@/widgets/transcript-workspace/ui/TranscriptWorkspace";
import { StatusBar } from "@/widgets/status-bar/ui/StatusBar";
import { SettingsOverlay } from "@/widgets/settings-panel/ui/SettingsOverlay";
import type { SettingsCategory } from "@/widgets/settings-panel/ui/SettingsPanel";
import { Toolbar } from "@/widgets/toolbar/ui/Toolbar";

/**
 * One window, one page. A延伸工具 opens IN the content area rather than in
 * a modal or a second window: its job takes tens of seconds, the queue
 * keeps working behind it, and the status bar has to stay readable the
 * whole time. Going back is one click and loses nothing -- the job lives on
 * the server, and its events keep arriving.
 *
 * Each view carries a `restore` because a move between them used to be a
 * one-way door: 逐字稿 → 引用長圖 destroyed the transcript, and the only way
 * out was 回到佇列 and choosing the audio file again (user report
 * 2026-08-28). One slot of history, held here because this is the only place
 * that knows both which view is showing and what it was holding.
 */
type View =
  | { kind: "queue" }
  | {
      kind: "quotestack";
      path?: string;
      handoff?: StackHandoff;
      restore?: StackSnapshot;
    }
  | { kind: "transcript"; path?: string; restore?: TranscriptSnapshot }
  // No `restore`: everything this one holds -- the document, the two
  // languages, the result -- lives in its own store and is still there when
  // the view comes back. The other two carry a transcript or a frame that
  // cost minutes to produce, which is what a snapshot is for.
  | { kind: "translatedoc" }
  // Same reasoning: the link, the package and the draft all live in
  // `explain-post`'s own store, so coming back finds them there. The
  // draft especially -- it is text a person pasted, and losing it to a
  // navigation would be the one unrecoverable thing this panel holds.
  | { kind: "brief" };

/** What 上一步 calls the screen it would take you back to. */
function labelOf(view: View): string {
  if (view.kind === "transcript") return "逐字稿";
  if (view.kind === "translatedoc") return "文件翻譯";
  if (view.kind === "brief") return "貼文解說";
  return view.kind === "quotestack" ? "引用長圖" : "佇列";
}

export function MainPage() {
  const tasks = useTaskStore((state) => state.tasks);
  const [tab, setTab] = useState<TabId>("all");
  /**
   * Which category 設定 is showing, or `null` for closed.
   *
   * Not a boolean any more, because 設定 shows one category at a time: both
   * workspaces send people here to set up recognition, and landing them on
   * 一般 would make them go looking for the thing they were just told they
   * needed.
   */
  const [settingsAt, setSettingsAt] = useState<SettingsCategory | null>(null);
  const settingsOpen = settingsAt !== null;
  const settingsButton = useRef<HTMLButtonElement>(null);

  /**
   * Leave 設定 and put focus back where it came from.
   *
   * Here rather than inside the overlay, because the overlay cannot do it: the
   * surface becomes `inert` in the same commit that mounts it, so the browser
   * has already blurred the 設定 button before any effect there could read
   * `document.activeElement`. The frame's delay is not decoration either --
   * `inert` is removed by this same state change, and a control inside an
   * inert subtree cannot take focus.
   */
  const closeSettings = useCallback(() => {
    setSettingsAt(null);
    requestAnimationFrame(() => settingsButton.current?.focus());
  }, []);
  const [past, setPast] = useState<history.History<View>>(() =>
    history.start<View>({ kind: "queue" }),
  );
  const view = past.view;

  // What each workspace is holding RIGHT NOW, reported as it changes. Refs
  // rather than state: this is read only at the moment of leaving, and
  // re-rendering the page on every keystroke inside a workspace would be a
  // cost paid continuously for a value used once.
  const transcriptState = useRef<TranscriptSnapshot | null>(null);
  const stackState = useRef<StackSnapshot | null>(null);

  /** The view being left, with its workspace's state folded in. */
  const withState = useCallback((current: View): View => {
    if (current.kind === "transcript") {
      return { ...current, restore: transcriptState.current ?? undefined };
    }
    if (current.kind === "quotestack") {
      return { ...current, restore: stackState.current ?? undefined };
    }
    return current;
  }, []);

  /** Go somewhere, remembering this screen whole. */
  const go = useCallback(
    (next: View) =>
      setPast((state) =>
        history.go(state, next, {
          view: withState(state.view),
          label: labelOf(state.view),
        }),
      ),
    [withState],
  );

  const goBack = useCallback(() => setPast(history.back), []);

  const counts = countsByTab(tasks);
  const tabs: readonly TabDef<TabId>[] = [
    { id: "all", label: "全部", count: counts.all },
    { id: "downloading", label: "進行中", count: counts.downloading },
    { id: "pending", label: "待處理", count: counts.pending },
    { id: "completed", label: "已完成", count: counts.completed },
    { id: "failed", label: "失敗", count: counts.failed },
  ];

  const openTool = (id: string, path?: string) => {
    if (id === "quotestack") go({ kind: "quotestack", path });
    if (id === "transcript") go({ kind: "transcript", path });
    // No `path`: a queue row's folder is a downloaded video, which is the
    // one thing this tool cannot take. The row's menu says so and offers it
    // disabled; from the header there is nothing to hand over yet.
    if (id === "translatedoc") go({ kind: "translatedoc" });
    // No `path`, for the same reason: a queue row hands over the folder
    // it downloaded into, and this verb takes a post URL. The row offers
    // it disabled with the reason (D-137's rule).
    if (id === "brief") go({ kind: "brief" });
  };

  return (
    <div className="mfp-app">
      {/* The working surface, kept MOUNTED while 設定 is open and made inert.
          Unmounting it would take a transcript that cost minutes with it; inert
          is what makes the tabs, the toolbar and 一鍵刪除紀錄 genuinely
          unreachable rather than merely covered up. */}
      <div className="mfp-app__surface" inert={settingsOpen}>
      <header className="mfp-header">
        <h1 className="mfp-header__title">媒體擷取</h1>
        <AddUrlsForm />
        <ExtensionMenu onPick={(id) => openTool(id)} />
        {/* A real button, not a bare `<button>` with a positioning class.
            This rendered as Chromium's own control -- 23px tall, its own font,
            its own grey -- beside a 35px 延伸工具, and no amount of alignment
            could have fixed it because nothing was drawing it (report
            2026-08-30). */}
        <Button
          ref={settingsButton}
          className="mfp-header__settings"
          onClick={() => (settingsOpen ? closeSettings() : setSettingsAt("general"))}
          aria-expanded={settingsOpen}
        >
          設定
        </Button>
      </header>

      <CapabilityNotice />

      {/* One slot, one button, in one place for all three views. Rendered
          only when there IS somewhere to go: a disabled control that never
          enables teaches people it is broken. The hint is not decoration --
          it is the promise this button can actually keep, and a reader who
          expects a full undo stack would find out by losing something. */}
      {past.previous && (
        <div className="mfp-back" data-testid="go-back">
          <button type="button" className="mfp-button" onClick={goBack}>
            ← 回到上一步（{past.previous.label}）
          </button>
          <span className="mfp-back__hint">
            只保留最近一次的畫面，內容都還在，不必重跑
          </span>
        </div>
      )}

      {view.kind === "queue" && (
        <>
          <Tabs tabs={tabs} active={tab} onChange={setTab} />
          <Toolbar />
        </>
      )}

      {/* The view area only. The header, the tabs, the toolbar and the status
          bar stay OUTSIDE, so a workspace that throws leaves the user
          somewhere to go rather than nowhere -- 回到佇列 is a control, and a
          control inside the boundary would go down with the thing it rescues.
          `resetKey` on the view kind: moving to another view is the user
          saying "not this one", and carrying the last one's error onto it
          would report the wrong thing about the new screen. */}
      <ErrorBoundary
        label={labelOf(view)}
        intact="上面的工具列和下面的狀態列都還在，可以切換到別的畫面。"
        resetKey={view.kind}
      >
      <main className="mfp-main">
        {view.kind === "queue" && <QueueTable tab={tab} onUseExtension={openTool} />}

        {view.kind === "quotestack" && (
          <StackWorkspace
            initialPath={view.path}
            handoff={view.handoff}
            restore={view.restore}
            onSnapshot={(snapshot) => (stackState.current = snapshot)}
            onClose={() => go({ kind: "queue" })}
          />
        )}

        {view.kind === "transcript" && (
          <TranscriptWorkspace
            initialPath={view.path}
            restore={view.restore}
            onSnapshot={(snapshot) => (transcriptState.current = snapshot)}
            onClose={() => go({ kind: "queue" })}
            // The two tools are one workflow: read, pick, quote. Going
            // through the queue and re-answering "which video, which
            // captions, which window" would make them two.
            onQuote={(handoff) => go({ kind: "quotestack", handoff })}
            // 設定 is where speech recognition is set up, and the workspace
            // is where a person finds out they need it. Passed down rather
            // than reached for: which panels exist is this page's business.
            onOpenSettings={() => setSettingsAt("asr")}
          />
        )}

        {view.kind === "brief" && (
          <PostWorkspace onClose={() => go({ kind: "queue" })} />
        )}

        {view.kind === "translatedoc" && (
          <DocumentWorkspace
            onClose={() => go({ kind: "queue" })}
            // Same reason 逐字稿 takes it: 設定 is where the translation
            // model is added, and this is where a person finds out they
            // need one.
            onOpenSettings={() => setSettingsAt("asr")}
          />
        )}
      </main>
      </ErrorBoundary>
      </div>

      {/* Over the surface, under nothing. The status bar below stays live and
          reachable: it is where a background transfer reports itself, and
          hiding that behind 設定 would rebuild the defect one layer up. */}
      {settingsAt !== null && <SettingsOverlay onClose={closeSettings} initial={settingsAt} />}

      <StatusBar />
    </div>
  );
}
