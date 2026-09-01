/**
 * The one-time explanation a tool shows the first time somebody opens it.
 *
 * Why per-tool and not one big tour at launch: a tour explains four screens
 * to a person who has seen none of them, and is forgotten before they reach
 * the second. This appears when the screen it describes is already on the
 * other side of it, which is the only moment the words mean anything.
 *
 * Why a modal, when D-80 rejected a wizard for 延伸工具: a wizard makes you
 * walk a path to change its last step, every time. This is shown ONCE, is
 * dismissed with one click, and never stands between the user and the
 * controls again -- 使用說明 brings it back on purpose. The objection to
 * wizards was about a recurring loop; this is not in one.
 *
 * Escape closes it and counts as read. Anything else would leave a person
 * who pressed Escape being shown the same dialog on every launch.
 */

import { useEffect, useRef } from "react";
import { Button } from "@/shared/ui/Button";
import { TOURS } from "@/entities/onboarding/model/tours";
import { useOnboarding } from "@/entities/onboarding/model/store";

export function FeatureGuide({ id }: { id: string }) {
  const shouldShow = useOnboarding((state) => state.shouldShow(id));
  const dismiss = useOnboarding((state) => state.dismiss);
  const tour = TOURS[id];
  const surface = useRef<HTMLDivElement | null>(null);
  const open = shouldShow && tour !== undefined;

  useEffect(() => {
    if (!open) return undefined;
    surface.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") dismiss(id);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, id, dismiss]);

  if (!open) return null;

  return (
    <div className="mfp-modal__backdrop" role="presentation" onClick={() => dismiss(id)}>
      <div
        className="mfp-modal mfp-guide"
        role="dialog"
        aria-modal="true"
        aria-label={tour.title}
        data-testid={`guide-${id}`}
        tabIndex={-1}
        ref={surface}
        onClick={(event) => event.stopPropagation()}
      >
        <h2 className="mfp-modal__title">{tour.title}</h2>
        <p className="mfp-guide__lede">{tour.lede}</p>
        <ol className="mfp-guide__steps">
          {tour.steps.map((step) => (
            <li key={step.title}>
              <strong>{step.title}</strong>
              <span>{step.body}</span>
            </li>
          ))}
        </ol>
        {tour.caveat && (
          // Its own block, and never inside the numbered list: it is the
          // paragraph that decides whether somebody should spend the next
          // twenty minutes, and a step 5 is a step people skim.
          <p className="mfp-guide__caveat">{tour.caveat}</p>
        )}
        <div className="mfp-modal__actions">
          <Button variant="primary" onClick={() => dismiss(id)}>
            知道了，開始使用
          </Button>
        </div>
        <p className="mfp-guide__reopen">之後想再看一次，按這個畫面上的「使用說明」。</p>
      </div>
    </div>
  );
}

/** Bring one back on purpose. Rendered in the header of the screen it
 *  explains, so the answer to 「剛剛那個說明呢」 is on the same screen the
 *  question is asked on. */
export function GuideButton({ id, className }: { id: string; className?: string }) {
  const open = useOnboarding((state) => state.open);
  if (TOURS[id] === undefined) return null;
  return (
    <Button
      variant="ghost"
      className={className}
      data-testid={`guide-button-${id}`}
      onClick={() => open(id)}
    >
      使用說明
    </Button>
  );
}
