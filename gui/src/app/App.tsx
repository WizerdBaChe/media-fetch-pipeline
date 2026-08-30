import { MainPage } from "@/pages/main/ui/MainPage";
import { ErrorBoundary } from "@/shared/ui/ErrorBoundary";
import { Toasts } from "@/widgets/toasts/ui/Toasts";
import { useLiveUpdates } from "./providers/useLiveUpdates";

export function App() {
  useLiveUpdates();
  return (
    <>
      {/* The last line, not the only one. Three boundaries nest, each holding
          a smaller blast radius than the one above it: a settings CATEGORY
          (SettingsPanel), the view AREA (MainPage), and this. If a throw gets
          this far the window is mostly gone anyway -- but "mostly gone with a
          sentence and a button" is a different thing from a white rectangle,
          which is what this app did until 2026-08-30. */}
      <ErrorBoundary
        label="主畫面"
        intact="這通常是暫時的。按「重試」會重新畫一次；如果沒有用，關掉程式再開一次。"
      >
        <MainPage />
      </ErrorBoundary>
      {/* Outside the page so it is not laid out by it: a toast
          overlays the corner and must not push the table.

          Outside the boundary too, and deliberately: a toast is how the app
          says something went wrong, and it must not be taken down by the thing
          it is reporting. */}
      <Toasts />
    </>
  );
}
