import type { ReactNode } from "react";
import { Button } from "./Button";
import { useDialog } from "./useDialog";

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  /**
   * Must name the concrete consequence, not "are you sure". O-6 requires the
   * confirm box to say how many in-flight tasks will be cancelled.
   */
  body: ReactNode;
  confirmLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/** The open/closed switch, kept out of the body so the body may hold hooks.
 *  A component that returns before `useDialog` would be calling it
 *  conditionally, which React forbids -- and mounting IS the event the
 *  focus and the Escape listener are about. */
export function ConfirmDialog({ open, ...rest }: ConfirmDialogProps) {
  if (!open) return null;
  return <ConfirmDialogBody {...rest} />;
}

function ConfirmDialogBody({
  title,
  body,
  confirmLabel = "確定",
  danger = false,
  onConfirm,
  onCancel,
}: Omit<ConfirmDialogProps, "open">) {
  // Focus lands on 取消, in a box whose other button can be destructive
  // (F10). Escape cancels; closing puts focus back where it came from.
  const { surface, initial } = useDialog<HTMLDivElement, HTMLButtonElement>(onCancel);

  return (
    <div className="mfp-modal__backdrop" onClick={onCancel} role="presentation">
      <div
        className="mfp-modal"
        role="alertdialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        ref={surface}
        onClick={(event) => event.stopPropagation()}
      >
        <h2 className="mfp-modal__title">{title}</h2>
        <div className="mfp-modal__body">{body}</div>
        <div className="mfp-modal__actions">
          <Button ref={initial} onClick={onCancel}>
            取消
          </Button>
          <Button variant={danger ? "danger" : "primary"} onClick={onConfirm}>
            {confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}
