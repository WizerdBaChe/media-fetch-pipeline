/**
 * Confirmation dialog for a multi-result paste.
 *
 * It exists so the input field can stay a fixed height: the results are shown
 * here rather than by growing the textarea and pushing the whole page down.
 * The list scrolls inside the dialog, so 2 results and 200 results produce the
 * same outer layout.
 */

import type { ParsedItem, ParseResult } from "@/shared/api/types";
import { Button } from "@/shared/ui/Button";
import { useDialog } from "@/shared/ui/useDialog";
import { blockedPresentation } from "@/shared/lib/blocked";
import { platformLabel } from "@/shared/lib/platform";
import { foundNothing, summarizeReport } from "../model/preview";

/** Provisional ids (`url:…`, `share:…`) are noise; show the link instead. */
function displayId(item: ParsedItem): string {
  return item.postId.includes(":") ? item.canonicalUrl : item.postId;
}

interface ParsePreviewDialogProps {
  result: ParseResult | null;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/** The open/closed switch, kept out of the body so the body may hold hooks:
 *  a component that returned before `useDialog` would be calling it
 *  conditionally, and mounting IS the event it is about. */
export function ParsePreviewDialog({ result, ...rest }: ParsePreviewDialogProps) {
  if (!result) return null;
  return <ParsePreviewBody result={result} {...rest} />;
}

function ParsePreviewBody({
  result,
  busy,
  onConfirm,
  onCancel,
}: ParsePreviewDialogProps & { result: ParseResult }) {
  // Escape cancels, focus starts on 取消 and goes back to the control that
  // opened this when it closes (F10). The reader arrives here having just
  // pressed Enter in the textarea behind; without this, pressing it again
  // re-submitted that textarea.
  const { surface, initial } = useDialog<HTMLDivElement, HTMLButtonElement>(onCancel);

  const changes = summarizeReport(result.report);
  const nothing = foundNothing(result);
  const blocked = result.report.blocked ?? [];

  // "Nothing usable" and "everything usable was refused" are different
  // things and must not share a sentence. Saying 沒有找到可用的網址 over a
  // link we recognized perfectly well would send the user checking their
  // clipboard for a fault that is on this machine.
  const title = !nothing
    ? `找到 ${result.items.length} 個項目`
    : blocked.length > 0
      ? "沒有可以加入的項目"
      : "沒有找到可用的網址";

  return (
    <div className="mfp-modal__backdrop" onClick={onCancel} role="presentation">
      <div
        className="mfp-modal mfp-modal--preview"
        role="dialog"
        aria-modal="true"
        aria-label="確認要加入的項目"
        data-testid="parse-preview"
        tabIndex={-1}
        ref={surface}
        onClick={(event) => event.stopPropagation()}
      >
        <h2 className="mfp-modal__title">{title}</h2>

        {changes.length > 0 && (
          <ul className="mfp-preview__changes">
            {changes.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        )}

        {result.items.length > 0 && (
          <ol className="mfp-preview__list">
            {result.items.map((item) => (
              <li key={`${item.platform}:${item.postId}`} className="mfp-preview__item">
                <span className="mfp-preview__platform">{platformLabel(item.platform)}</span>
                <span className="mfp-mono mfp-preview__id" title={item.canonicalUrl}>
                  {displayId(item)}
                </span>
              </li>
            ))}
          </ol>
        )}

        {blocked.length > 0 && (
          <div className="mfp-preview__blocked" data-testid="preview-blocked">
            <p className="mfp-preview__blocked-title">
              有 {blocked.length} 個連結無法下載，不會加入：
            </p>
            <ul>
              {blocked.map((entry) => {
                const { label, action } = blockedPresentation(entry.reason);
                return (
                  <li key={entry.url}>
                    <span className="mfp-preview__platform">{platformLabel(entry.platform)}</span>
                    <span className="mfp-mono mfp-preview__id" title={entry.url}>
                      {entry.url}
                    </span>
                    <span className="mfp-preview__blocked-why">{label}</span>
                    {action && <span className="mfp-preview__blocked-action">{action}</span>}
                  </li>
                );
              })}
            </ul>
          </div>
        )}

        {result.report.unrecognized.length > 0 && (
          <div className="mfp-preview__unrecognized">
            <p className="mfp-preview__unrecognized-title">
              {nothing
                ? "以下內容沒有可用的網址："
                : `有 ${result.report.unrecognized.length} 段無法辨識，不會加入：`}
            </p>
            <ul>
              {result.report.unrecognized.map((text, index) => (
                <li key={index} className="mfp-mono">
                  {text}
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="mfp-modal__actions">
          <Button ref={initial} onClick={onCancel}>
            {nothing ? "關閉" : "取消"}
          </Button>
          {!nothing && (
            <Button variant="primary" disabled={busy} onClick={onConfirm}>
              {busy ? "加入中…" : `加入佇列（${result.items.length}）`}
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
