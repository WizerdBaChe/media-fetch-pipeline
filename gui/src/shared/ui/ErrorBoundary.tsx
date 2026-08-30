/**
 * The thing that stops one bad field blanking the window.
 *
 * Measured 2026-08-30: a `/v1/asr/readiness` response missing `home` makes
 * `AsrSetupPanel` throw `TypeError: Cannot read properties of undefined
 * (reading 'path')` inside render, and with no boundary anywhere React unmounts
 * the ENTIRE tree -- header, queue, status bar, all of it -- leaving a white
 * window with no text on it. Nothing else in the app can report that, because
 * everything that could report it has just been unmounted too.
 *
 * The same class was already written down and never guarded: `e2e/fixtures.ts`
 * carries a note that `CapabilityNotice` reads `capabilities` unguarded and
 * "takes the whole React tree down". Two known instances of one shape is what
 * makes this a missing LAYER rather than two missing null checks -- and a null
 * check only ever protects the line someone thought of.
 *
 * Three rules, all of them from this project's own standing invariant that a
 * silent blank screen is a defect:
 *
 *   1. **Say what broke, in the reader's language, and what still works.**
 *      A boundary that renders an empty div is the blank screen with extra
 *      steps.
 *   2. **Keep the technical line.** The message and the component stack are
 *      what a bug report needs; they are shown quietly rather than hidden,
 *      the same way 相依工具檢查 keeps the server's own sentence under the
 *      Chinese one.
 *   3. **Offer the way out.** 重試 remounts the subtree, which is a real
 *      recovery when the cause was one bad response and the next one is fine.
 *
 * NOT a substitute for handling the error where it happens. A boundary is the
 * floor, not the fix: it turns "the app vanished" into "this panel is broken
 * and the rest works", which is the difference between a bug the user can
 * report and one they can only describe as 「打不開」.
 */

import { Component, Fragment, type ErrorInfo, type ReactNode } from "react";

interface ErrorBoundaryProps {
  /** What broke, named as the user would name it: 「設定」, 「佇列」, 「逐字稿」. */
  label: string;
  /** One sentence about what still works, so the notice is not a dead end. */
  intact?: string;
  /** Bump this to clear a caught error — e.g. the settings category id, so
   *  moving to another category retries rather than staying broken. */
  resetKey?: string | number;
  children: ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
  stack: string | null;
  /** Forces a remount of the subtree on 重試: without a new key React reuses
   *  the same element tree and the same render throws again immediately. */
  attempt: number;
  seenResetKey: string | number | undefined;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  override state: ErrorBoundaryState = {
    error: null,
    stack: null,
    attempt: 0,
    seenResetKey: undefined,
  };

  static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    return { error };
  }

  /** Clear on a new `resetKey` WITHOUT waiting for a click: navigating to
   *  another settings category is the user saying "not this one", and leaving
   *  the previous panel's error on the new one would be reporting the wrong
   *  thing about it. */
  static getDerivedStateFromProps(
    props: ErrorBoundaryProps,
    state: ErrorBoundaryState,
  ): Partial<ErrorBoundaryState> | null {
    if (props.resetKey === state.seenResetKey) return null;
    return { error: null, stack: null, seenResetKey: props.resetKey };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    this.setState({ stack: info.componentStack ?? null });
    // Structured and on ONE line, because this is what someone pastes into a
    // report. `console.error` rather than a notice: the notice system lives in
    // a store this render may have just failed to reach.
    console.error(
      `[mfp] render failed in ${this.props.label}: ${error.message}`,
      { label: this.props.label, error, componentStack: info.componentStack },
    );
  }

  private retry = () => {
    this.setState((state) => ({
      error: null,
      stack: null,
      attempt: state.attempt + 1,
    }));
  };

  override render(): ReactNode {
    const { error, stack, attempt } = this.state;
    if (error === null) {
      // A keyed FRAGMENT, which renders no element at all. The key is what
      // makes 重試 build a fresh subtree instead of re-showing the one that
      // just threw -- but a wrapping `<div>` is not free: it broke the app
      // shell the first time this shipped. `.mfp-app__surface` is a flex
      // column and `.mfp-main` is its `flex: 1` child; one plain div between
      // them and the queue stopped scrolling inside `.mfp-main` and started
      // growing the whole surface instead. Caught by the geometry gate, which
      // is why that gate reads the scrollbar it needs rather than assuming it.
      //
      // A boundary is dropped into three different layout contexts here, so
      // being layout-transparent is a property it has to keep.
      return <Fragment key={attempt}>{this.props.children}</Fragment>;
    }

    return (
      <div className="mfp-broken" role="alert" data-testid="error-boundary" data-label={this.props.label}>
        <strong className="mfp-broken__what">「{this.props.label}」這一塊沒能顯示出來</strong>
        <p className="mfp-broken__intact">
          {this.props.intact ?? "其他部分不受影響，可以繼續使用。"}
        </p>
        <p className="mfp-broken__why">
          通常是這台機器回來的資料少了一個欄位。按「重試」會重新畫一次；如果一直失敗，把下面這行附在回報裡。
        </p>
        <code className="mfp-broken__detail mfp-mono">{error.message}</code>
        {stack && (
          <details className="mfp-broken__stack">
            <summary>技術細節</summary>
            <pre className="mfp-mono">{stack.trim()}</pre>
          </details>
        )}
        <button type="button" className="mfp-button mfp-button--primary" onClick={this.retry}>
          重試
        </button>
      </div>
    );
  }
}
