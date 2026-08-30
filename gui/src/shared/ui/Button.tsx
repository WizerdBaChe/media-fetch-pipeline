import type { ComponentPropsWithRef, ReactNode } from "react";

export type ButtonVariant = "default" | "primary" | "danger" | "ghost";

/** `ComponentPropsWithRef`, so a caller can hold on to the element. React 19
 *  passes `ref` as an ordinary prop, so it needs no `forwardRef` -- and a
 *  caller that has to put focus back on a control after a mode closes has no
 *  other way to name it. */
interface ButtonProps extends ComponentPropsWithRef<"button"> {
  variant?: ButtonVariant;
  children: ReactNode;
}

export function Button({ variant = "default", className, children, ...rest }: ButtonProps) {
  return (
    <button
      type="button"
      className={["mfp-button", `mfp-button--${variant}`, className].filter(Boolean).join(" ")}
      {...rest}
    >
      {children}
    </button>
  );
}
